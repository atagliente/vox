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
import urllib.error
import urllib.request
from typing import Any

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


def _post(base_url: str, path: str, payload: dict[str, Any],
          timeout: float = TIMEOUT) -> dict[str, Any]:
    url = f"{native_base(base_url)}{path}"
    request = urllib.request.Request(
        url, json.dumps(payload).encode("utf-8"),
        {"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise OllamaError(f"{path} refused: HTTP {exc.code} {detail}") from exc
    except OSError as exc:
        raise OllamaError(f"cannot reach {url}: {exc}") from exc
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
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        raise OllamaError(f"{path} refused: HTTP {exc.code}") from exc
    except OSError as exc:
        raise OllamaError(f"cannot reach {url}: {exc}") from exc
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
        rows.append({
            "name": str(model["name"]),
            "size": int(model.get("size") or 0),
            "parameters": str(details.get("parameter_size") or ""),
            "quantization": str(details.get("quantization_level") or ""),
        })
    rows.sort(key=lambda row: row["name"])
    return rows


def loaded_context(base_url: str, model: str, timeout: float = 10.0) -> int | None:
    """The window a resident model is actually loaded with, if it is loaded."""
    for entry in _get(base_url, "/api/ps", timeout).get("models") or []:
        if isinstance(entry, dict) and entry.get("name") == model:
            value = entry.get("context_length")
            return int(value) if value else None
    return None


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


def create_with_context(base_url: str, model: str, num_ctx: int,
                        timeout: float = TIMEOUT) -> str:
    """Write a derived model with ``num_ctx`` set, and return its name.

    The weights are not copied: this is a manifest pointing at the same blobs,
    so what it costs on disk is a few kilobytes.
    """
    if num_ctx < 512:
        raise OllamaError("a context window under 512 tokens is not usable")
    source = _resolve_source(base_url, model)
    name = derived_name(source, num_ctx)
    data = _post(base_url, "/api/create", {
        "model": name,
        "from": source,
        "parameters": {"num_ctx": int(num_ctx)},
        "stream": False,
    }, timeout)
    status = str(data.get("status", ""))
    if status and status != "success":
        raise OllamaError(f"create answered {status!r}")
    return name


def delete_model(base_url: str, model: str, timeout: float = 30.0) -> None:
    """Remove a derived model. Only ever called on names VOX itself made."""
    url = f"{native_base(base_url)}/api/delete"
    request = urllib.request.Request(
        url, json.dumps({"model": model}).encode("utf-8"),
        {"Content-Type": "application/json"}, method="DELETE",
    )
    try:
        urllib.request.urlopen(request, timeout=timeout).read()
    except urllib.error.HTTPError as exc:
        raise OllamaError(f"delete refused: HTTP {exc.code}") from exc
    except OSError as exc:
        raise OllamaError(f"cannot reach {url}: {exc}") from exc
