"""The few things Ollama can only be asked over its own API.

VOX speaks the OpenAI-compatible endpoint everywhere else, on purpose: it is
the one dialect every provider understands. But the size of the context window
is not in that dialect. Ollama loads a model at its own default - 4096 unless
the server was started otherwise - no matter what the weights support, and the
``/v1`` endpoint ignores ``num_ctx`` however it is passed.

The only way to change it without restarting the server is a derived model: a
new manifest pointing at the same weights, with ``num_ctx`` set. That is what
``create_with_context`` writes, and the derived model is then used through the
ordinary endpoint like any other.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from . import http

TIMEOUT = 120.0


class OllamaError(RuntimeError):
    """Anything the native API refused or could not answer."""


def native_base(base_url: str) -> str:
    """``http://host:11434/v1`` -> ``http://host:11434``."""
    return re.sub(r"/v\d+/?$", "", base_url.rstrip("/")) or base_url


def looks_like_ollama(base_url: str) -> bool:
    """Whether this endpoint is worth asking with Ollama's own API."""
    lowered = base_url.lower()
    return "11434" in lowered or "ollama" in lowered


def _post(
    base_url: str, path: str, payload: dict[str, Any], timeout: float = TIMEOUT
) -> dict[str, Any]:
    url = f"{native_base(base_url)}{path}"
    try:
        body = http.request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        ).text()
    except http.HttpError as exc:
        if exc.kind == "http":
            raise OllamaError(
                f"{path} refused: HTTP {exc.status} {exc.body[:300]}"
            ) from exc
        raise OllamaError(f"cannot reach {url}: {exc.message}") from exc
    try:
        data = json.loads(body or "{}")
    except json.JSONDecodeError as exc:
        raise OllamaError(f"{path} answered with something that is not JSON") from exc
    if not isinstance(data, dict):
        raise OllamaError(f"{path} answered with {type(data).__name__}, not an object")
    return data


def _get(base_url: str, path: str, timeout: float = 10.0) -> dict[str, Any]:
    url = f"{native_base(base_url)}{path}"
    try:
        data = json.loads(http.request(url, timeout=timeout).text() or "{}")
    except http.HttpError as exc:
        if exc.kind == "http":
            raise OllamaError(f"{path} refused: HTTP {exc.status}") from exc
        raise OllamaError(f"cannot reach {url}: {exc.message}") from exc
    except json.JSONDecodeError as exc:
        raise OllamaError(f"{path} answered with something that is not JSON") from exc
    return data if isinstance(data, dict) else {}


def list_models(base_url: str, timeout: float = 10.0) -> list[dict[str, Any]]:
    """What ``ollama list`` shows: name, size on disk, parameters, quantisation."""
    data = _get(base_url, "/api/tags", timeout)
    models = data.get("models")
    if not isinstance(models, list):
        return []
    rows: list[dict[str, Any]] = []
    for model in models:
        if not isinstance(model, dict) or not model.get("name"):
            continue
        details = model.get("details") if isinstance(model.get("details"), dict) else {}
        rows.append(
            {
                "name": str(model["name"]),
                "size": int(model.get("size") or 0),
                "parameters": str(details.get("parameter_size") or ""),
                "quantization": str(details.get("quantization_level") or ""),
            }
        )
    rows.sort(key=lambda row: row["name"])
    return rows


def _resident_entry(base_url: str, model: str, timeout: float) -> dict[str, Any] | None:
    """The /api/ps row for ``model``, under whichever name it is listed.

    A derived model shares its blobs with the one it was built from, and
    Ollama sometimes lists it under that parent name, so an exact match is not
    enough to find it.
    """
    entries = [
        entry
        for entry in _get(base_url, "/api/ps", timeout).get("models") or []
        if isinstance(entry, dict)
    ]
    names = {model}
    if is_derived(model):
        try:
            parent = parent_model(base_url, model, timeout)
        except OllamaError:
            parent = None
        if parent:
            names.add(parent)
    for entry in entries:
        if entry.get("name") in names or entry.get("model") in names:
            return entry
    return None


def loaded_context(base_url: str, model: str, timeout: float = 10.0) -> int | None:
    """The window a resident model is actually loaded with, if it is loaded."""
    entry = _resident_entry(base_url, model, timeout)
    if entry is None:
        return None
    value = entry.get("context_length")
    return int(value) if value else None


def trained_context(base_url: str, model: str, timeout: float = 10.0) -> int | None:
    """The largest window the weights themselves were trained for."""
    data = _post(base_url, "/api/show", {"model": model}, timeout)
    info = data.get("model_info")
    if not isinstance(info, dict):
        return None
    for key, value in info.items():
        if key.endswith(".context_length") and value:
            return int(value)
    return None


# Families that read images, for servers old enough not to say so themselves.
# A list, not a guess: each of these is a vision model whose name says so.
VISION_FAMILIES = (
    "llava",
    "bakllava",
    "moondream",
    "minicpm-v",
    "llama3.2-vision",
    "qwen2-vl",
    "qwen2.5-vl",
    "qwen3-vl",
    "gemma3",
    "mistral-small3",
    "granite3.2-vision",
    "internvl",
    "pixtral",
)


def reads_images(base_url: str, model: str, timeout: float = 10.0) -> bool:
    """Can this model be shown a picture?

    Ollama says so directly in `capabilities` on newer servers. Where it does
    not, the family name is the answer — and getting this wrong the optimistic
    way costs a rejected request, so the fallback errs towards no.
    """
    data = _post(base_url, "/api/show", {"model": model}, timeout)
    capabilities = data.get("capabilities")
    if isinstance(capabilities, list):
        return "vision" in [str(item).lower() for item in capabilities]
    families = data.get("details", {}).get("families")
    names = (
        [str(item).lower() for item in families] if isinstance(families, list) else []
    )
    names.append(model.lower())
    return any(family in name for name in names for family in VISION_FAMILIES)


def configured_context(base_url: str, model: str, timeout: float = 10.0) -> int | None:
    """``num_ctx`` if the model carries one of its own, else None."""
    data = _post(base_url, "/api/show", {"model": model}, timeout)
    parameters = str(data.get("parameters") or "")
    match = re.search(r"^\s*num_ctx\s+(\d+)", parameters, re.MULTILINE)
    return int(match.group(1)) if match else None


def derived_name(model: str, num_ctx: int) -> str:
    """``granite4.2:3b`` at 16384 -> ``vox-granite4.2-3b:ctx16384``.

    Deterministic, so asking twice reuses the same manifest instead of
    littering the model list.
    """
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-").lower()
    stem = re.sub(r"-+", "-", stem)
    return f"vox-{stem}:ctx{num_ctx}"


def is_derived(model: str) -> bool:
    """Whether this name is one VOX wrote, rather than one that was pulled."""
    return re.fullmatch(r"vox-.+:ctx\d+", model) is not None


def parent_model(base_url: str, model: str, timeout: float = 10.0) -> str | None:
    """What a derived model was built from, according to Ollama.

    The name cannot be reversed - ``granite4.2:3b`` and ``granite4.2-3b`` both
    flatten to the same thing - so the parent is asked for rather than guessed.
    """
    data = _post(base_url, "/api/show", {"model": model}, timeout)
    details = data.get("details")
    if isinstance(details, dict) and details.get("parent_model"):
        return str(details["parent_model"])
    return None


def _resolve_source(base_url: str, model: str) -> str:
    """Derive from the original, never from a previous derivation.

    Chaining ``vox-...:ctx8192`` into ``vox-...:ctx16384`` would work, but every
    link would keep the one before it alive on disk for no reason.
    """
    if not is_derived(model):
        return model
    try:
        parent = parent_model(base_url, model)
    except OllamaError:
        parent = None
    return parent or model


def create_with_context(
    base_url: str,
    model: str,
    num_ctx: int,
    parameters: dict[str, Any] | None = None,
    timeout: float = TIMEOUT,
) -> str:
    """Write a derived model with ``num_ctx`` set, and return its name.

    The weights are not copied: this is a manifest pointing at the same blobs,
    so what it costs on disk is a few kilobytes. Anything in ``parameters`` -
    ``num_gpu`` above all - is written beside the window, so one build carries
    the whole configuration.
    """
    if num_ctx < 512:
        raise OllamaError("a context window under 512 tokens is not usable")
    source = _resolve_source(base_url, model)
    name = derived_name(source, num_ctx)
    body = {"num_ctx": int(num_ctx)}
    body.update(parameters or {})
    data = _post(
        base_url,
        "/api/create",
        {
            "model": name,
            "from": source,
            "parameters": body,
            "stream": False,
        },
        timeout,
    )
    status = str(data.get("status", ""))
    if status and status != "success":
        raise OllamaError(f"create answered {status!r}")
    return name


def delete_model(base_url: str, model: str, timeout: float = 30.0) -> None:
    """Remove a derived model. Only ever called on names VOX itself made."""
    url = f"{native_base(base_url)}/api/delete"
    try:
        http.request(
            url,
            data=json.dumps({"model": model}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="DELETE",
            timeout=timeout,
        )
    except http.HttpError as exc:
        if exc.kind == "http":
            raise OllamaError(f"delete refused: HTTP {exc.status}") from exc
        raise OllamaError(f"cannot reach {url}: {exc.message}") from exc


# --------------------------------------------------------------- the GPU


def layer_count(base_url: str, model: str, timeout: float = 10.0) -> int | None:
    """How many blocks the model has, which is the ceiling for ``num_gpu``."""
    data = _post(base_url, "/api/show", {"model": model}, timeout)
    info = data.get("model_info")
    if not isinstance(info, dict):
        return None
    for key, value in info.items():
        if key.endswith(".block_count") and value:
            return int(value)
    return None


def vram_mb() -> tuple[int, int] | None:
    """``(used, total)`` in MB from nvidia-smi, or None when it is not there.

    Ollama reports what it *believes* is in VRAM, which on Windows keeps
    counting past the end of the card: the driver silently moves the excess
    into shared memory. Only the card itself can say what really fits.
    """
    binary = shutil.which("nvidia-smi")
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [
                binary,
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = completed.stdout.strip().splitlines()
    if not first:
        return None
    try:
        used, total = (int(part.strip()) for part in first[0].split(",")[:2])
    except ValueError:
        return None
    return used, total


@dataclass
class Residency:
    """Where a loaded model actually lives."""

    size: int
    size_vram: int
    context: int | None = None
    gpu_used_mb: int | None = None
    gpu_total_mb: int | None = None

    @property
    def on_cpu(self) -> int:
        return max(0, self.size - self.size_vram)

    def spilling(self, reserve_mb: int = 0) -> bool:
        """True when the card is full and the rest is in shared memory.

        Ollama's own number cannot show this - it reports the whole model as
        resident - so it is the card's used figure against its total.
        """
        if self.gpu_used_mb is None or self.gpu_total_mb is None:
            return False
        # 192 MB is the floor because a card that has spilled sits right up
        # against its ceiling: 4003 of 4096 MB in the run this was written
        # from, against 2755 for the same model when it fitted.
        return self.gpu_used_mb >= self.gpu_total_mb - max(reserve_mb, 192)


def residency(base_url: str, model: str, timeout: float = 10.0) -> Residency | None:
    """What is resident for ``model`` right now, or None when it is not loaded."""
    entry = _resident_entry(base_url, model, timeout)
    if entry is None:
        return None
    card = vram_mb()
    return Residency(
        size=int(entry.get("size") or 0),
        size_vram=int(entry.get("size_vram") or 0),
        context=int(entry["context_length"]) if entry.get("context_length") else None,
        gpu_used_mb=card[0] if card else None,
        gpu_total_mb=card[1] if card else None,
    )


def probe(
    base_url: str, model: str, options: dict[str, Any], timeout: float = 900.0
) -> Residency | None:
    """Load ``model`` with these options and report where it ended up.

    One token is generated, which is the cheapest way to make Ollama commit to
    a layout. Nothing is written: the options are per request.
    """
    _post(
        base_url,
        "/api/generate",
        {
            "model": model,
            "prompt": "hi",
            "stream": False,
            "keep_alive": "60s",
            "options": dict(options, num_predict=1),
        },
        timeout,
    )
    return residency(base_url, model)


def fit_layers(
    base_url: str, model: str, num_ctx: int, reserve_mb: int = 384, on_step: Any = None
) -> tuple[int, Residency | None]:
    """The largest ``num_gpu`` that does not spill into shared memory.

    Measured rather than calculated: each candidate is loaded and the card is
    read. Spilling is the thing being avoided - it is slower than leaving the
    same layers on the CPU, 9.7 tok/s against 12.2 on the machine this was
    written on.
    """
    layers = layer_count(base_url, model) or 0
    if layers <= 0:
        raise OllamaError(f"{model} does not say how many layers it has")
    step = max(1, layers // 8)
    candidate = layers
    while candidate > 0:
        result = probe(base_url, model, {"num_ctx": num_ctx, "num_gpu": candidate})
        if on_step is not None:
            on_step(candidate, result)
        if result is None or not result.spilling(reserve_mb):
            return candidate, result
        candidate -= step
    return 0, None
