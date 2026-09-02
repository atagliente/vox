"""The sampling parameters a request carries, and where they come from.

VOX passed two of them — ``temperature`` and ``max_tokens`` — and every other
knob a modern provider offers went unused. This module holds the rest, and one
rule about all of them: **a parameter is only sent when it has been set**.

That rule is not fussiness. ``top_k`` and ``repeat_penalty`` are not OpenAI
parameters at all; they reach Ollama and llama.cpp through ``extra_body`` and
a strict gateway will reject them. ``seed`` is accepted almost everywhere and
ignored in places. Sending a default for each would mean every provider that
validates its input rejects every request VOX makes, so the default is to say
nothing and let the server's own default stand.

Three places can set one, most specific winning:

1. the active model's preset — different models want different settings, and
   storing that beside the model is the only arrangement that survives
   switching between them;
2. the active role, for the two it already carried;
3. the ``generation`` block, as the answer for everything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Names the OpenAI chat-completions API takes at the top level of a request.
STANDARD = ("top_p", "seed", "stop", "presence_penalty", "frequency_penalty")

# Names that are not OpenAI parameters and travel in extra_body: llama.cpp and
# Ollama read them, a strict gateway rejects them, and neither should have to
# know about the other.
NATIVE = ("top_k", "repeat_penalty", "min_p", "typical_p")

# What the operator may set, with the shape each one has to have.
SETTABLE: dict[str, type | tuple[type, ...]] = {
    "temperature": float,
    "max_tokens": int,
    "top_p": float,
    "top_k": int,
    "seed": int,
    "repeat_penalty": float,
    "min_p": float,
    "typical_p": float,
    "presence_penalty": float,
    "frequency_penalty": float,
    "stop": list,
    "reasoning_effort": str,
    "think": bool,
}

REASONING_EFFORTS = ("minimal", "low", "medium", "high")


@dataclass(frozen=True)
class Sampling:
    """One resolved set of parameters, ready to become a request.

    Every field is optional. ``None`` means "not set", which means "not sent",
    which means the server decides — and that is a different thing from any
    value this could pick.
    """

    values: dict[str, Any]

    def get(self, name: str) -> Any:
        return self.values.get(name)

    def request_fields(self) -> dict[str, Any]:
        """The parameters that go at the top level of the request."""
        out = {name: self.values[name] for name in STANDARD if name in self.values}
        effort = self.values.get("reasoning_effort")
        if effort:
            out["reasoning_effort"] = effort
        return out

    def native_fields(self) -> dict[str, Any]:
        """The parameters that travel in ``extra_body``.

        ``think`` is Ollama's switch for asking a reasoning model to reason.
        VOX has always known how to *read* thinking; this is how it asks for
        it, which is a different thing and was the gap.
        """
        out = {name: self.values[name] for name in NATIVE if name in self.values}
        if "think" in self.values:
            out["think"] = bool(self.values["think"])
        return out

    def describe(self) -> str:
        """What /settings and /stats show: only what is actually being sent."""
        if not self.values:
            return "provider defaults throughout"
        return "  ·  ".join(
            f"{name}={_render(value)}" for name, value in sorted(self.values.items())
        )


def _render(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def coerce(name: str, raw: Any) -> Any:
    """The value for ``name``, or raise ValueError saying what it should be.

    Written once so the configuration check, the ``/set`` command and the
    preset loader cannot disagree about what a valid ``top_p`` is.
    """
    if name not in SETTABLE:
        raise ValueError(f"unknown parameter {name}; try one of: {known()}")
    wanted = SETTABLE[name]
    if wanted is bool:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("on", "true", "yes", "1"):
            return True
        if text in ("off", "false", "no", "0"):
            return False
        raise ValueError(f"{name} is on or off")
    if wanted is list:
        if isinstance(raw, list):
            items = [str(item) for item in raw]
        else:
            items = [part for part in str(raw).split(",") if part]
        if len(items) > 4:
            raise ValueError(f"{name} takes at most 4 sequences")
        return items
    if name == "reasoning_effort":
        text = str(raw).strip().lower()
        if text not in REASONING_EFFORTS:
            raise ValueError(
                f"reasoning_effort is one of: {', '.join(REASONING_EFFORTS)}"
            )
        return text
    if wanted is int:
        try:
            value = int(str(raw).strip())
        except ValueError:
            raise ValueError(f"{name} is a whole number") from None
        if name in ("max_tokens", "top_k") and value <= 0:
            raise ValueError(f"{name} is a positive whole number")
        return value
    try:
        value = float(str(raw).strip())
    except ValueError:
        raise ValueError(f"{name} is a number") from None
    if name in ("temperature", "top_p", "min_p", "typical_p") and not 0 <= value <= 2:
        raise ValueError(f"{name} is between 0 and 2")
    return value


def known() -> str:
    return ", ".join(sorted(SETTABLE))


def presets(config: dict[str, Any]) -> dict[str, Any]:
    """The ``model_presets`` block: model name -> parameters for that model."""
    block = config.get("model_presets")
    return block if isinstance(block, dict) else {}


def resolve(config: dict[str, Any], role: Any = None) -> Sampling:
    """What this turn should send, from the model preset, the role and the block.

    Most specific wins. Anything not set anywhere is not sent at all.
    """
    values: dict[str, Any] = {}
    generation = config.get("generation")
    generation = generation if isinstance(generation, dict) else {}

    for name in SETTABLE:
        if name in generation and generation[name] is not None:
            values[name] = generation[name]

    if role is not None and getattr(role, "temperature", None) is not None:
        values["temperature"] = float(role.temperature)

    model = str(config.get("active_model", ""))
    preset = presets(config).get(model)
    if isinstance(preset, dict):
        for name, value in preset.items():
            if name in SETTABLE and value is not None:
                values[name] = value

    cleaned: dict[str, Any] = {}
    for name, value in values.items():
        try:
            cleaned[name] = coerce(name, value)
        except ValueError:
            # A bad value in the configuration is reported by validate_config,
            # and dropping it here is better than failing the turn over it.
            continue
    return Sampling(cleaned)


def errors_in(block: Any, where: str) -> list[str]:
    """What is wrong with a block of parameters, for validate_config."""
    if not isinstance(block, dict):
        return [f"{where} must be an object"]
    found: list[str] = []
    for name, value in block.items():
        if name not in SETTABLE:
            continue  # generation carries other keys of its own
        if value is None:
            continue
        try:
            coerce(name, value)
        except ValueError as exc:
            found.append(f"{where}.{name}: {exc}")
    return found
