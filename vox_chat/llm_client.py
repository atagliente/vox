"""The single place where an OpenAI-compatible client is built and used.

Streaming deltas are accumulated into exactly one assistant message; tool
calls are merged by index, because providers send them fragment by fragment.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .models import Message, ToolCall
from .reasoning import ThinkSplitter

if TYPE_CHECKING:  # the SDK is loaded lazily; see _openai()
    from openai import OpenAI
from .usage import TokenUsage

ErrorKind = Literal["connection", "timeout", "http", "cancelled", "protocol", "context"]


class LLMError(Exception):
    """A provider failure already turned into something worth showing."""

    def __init__(self, kind: ErrorKind, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.kind: ErrorKind = kind
        self.message = message
        self.detail = detail

    def __str__(self) -> str:
        return self.message


EventType = Literal[
    "text", "reasoning", "token", "tool_calls", "usage", "done", "cancelled"
]


@dataclass
class StreamEvent:
    """One step of a streaming response."""

    type: EventType
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: TokenUsage | None = None
    token: TokenSample | None = None
    phase: str = ""


def _delta_of(chunk: Any) -> Any:
    choices = getattr(chunk, "choices", None)
    if not choices:
        return None
    return getattr(choices[0], "delta", None)


def _finish_reason_of(chunk: Any) -> str | None:
    choices = getattr(chunk, "choices", None)
    if not choices:
        return None
    return getattr(choices[0], "finish_reason", None)


def _reasoning_of(delta: Any) -> str | None:
    """Reasoning from the fields providers use for it, when present."""
    for field_name in ("reasoning_content", "reasoning", "thinking"):
        value = getattr(delta, field_name, None)
        if value:
            return str(value)
    extra = getattr(delta, "model_extra", None)
    if isinstance(extra, dict):
        for field_name in ("reasoning_content", "reasoning", "thinking"):
            value = extra.get(field_name)
            if value:
                return str(value)
    return None


@dataclass(frozen=True)
class TokenSample:
    """One emitted token and the distribution it came from, as reported."""

    text: str
    logprob: float
    alternatives: tuple[tuple[str, float], ...] = ()


def _tokens_of(chunk: Any) -> list[TokenSample]:
    """Read the logprobs block a chunk carries, if the provider sent one."""
    choices = getattr(chunk, "choices", None)
    if not choices:
        return []
    logprobs = getattr(choices[0], "logprobs", None)
    entries = getattr(logprobs, "content", None) if logprobs is not None else None
    if not entries:
        return []
    samples: list[TokenSample] = []
    for entry in entries:
        text = getattr(entry, "token", None)
        logprob = getattr(entry, "logprob", None)
        if text is None or logprob is None:
            continue
        alternatives = tuple(
            (getattr(alt, "token", ""), float(getattr(alt, "logprob", 0.0)))
            for alt in (getattr(entry, "top_logprobs", None) or [])
        )
        samples.append(TokenSample(str(text), float(logprob), alternatives))
    return samples


def _usage_of(chunk: Any) -> TokenUsage | None:
    """Read the usage block providers append to the final chunk, if any."""
    counted = getattr(chunk, "usage", None)
    if counted is None:
        return None
    prompt = getattr(counted, "prompt_tokens", None)
    completion = getattr(counted, "completion_tokens", None)
    if prompt is None and completion is None:
        return None
    # Prompt caching: the part of the prompt the provider did not have to
    # process again. Reported under prompt_tokens_details by OpenAI and the
    # gateways that follow it, and simply absent everywhere else — which is
    # why it is read defensively rather than assumed.
    details = getattr(counted, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None) if details is not None else None
    if cached is None and isinstance(details, dict):
        cached = details.get("cached_tokens")
    return TokenUsage(int(prompt or 0), int(completion or 0), int(cached or 0))


def consume_stream(
    chunks: Iterable[Any], cancel: threading.Event | None = None
) -> Iterator[StreamEvent]:
    """Turn raw streaming chunks into ordered :class:`StreamEvent` values.

    Kept free of any OpenAI import so it can be exercised with fake chunks.
    Text deltas are emitted as they arrive; tool call fragments are buffered
    by index and emitted once, at the end.
    """
    pending: dict[int, dict[str, str]] = {}
    finish_reason: str | None = None
    reported: TokenUsage | None = None
    splitter = ThinkSplitter()
    # Nothing has told us which phase we are in until a delta does.
    phase = "unattributed"

    for chunk in chunks:
        if cancel is not None and cancel.is_set():
            yield StreamEvent("cancelled")
            return
        counted = _usage_of(chunk)
        if counted is not None:
            reported = counted
        reason = _finish_reason_of(chunk)
        if reason:
            finish_reason = reason
        delta = _delta_of(chunk)
        if delta is None:
            continue
        thought = _reasoning_of(delta)
        if thought:
            yield StreamEvent("reasoning", text=thought)
        text = getattr(delta, "content", None)
        if text:
            # Some models inline their thinking in the content instead.
            for kind, piece in splitter.feed(text):
                yield StreamEvent(
                    "reasoning" if kind == "reasoning" else "text", text=piece
                )
        # Logprobs ride along with the delta that produced them, which is the
        # only honest way to tell a thinking token from an answer token. A
        # chunk that carries a logprob but no text — measured to be about a
        # quarter of them on a reasoning model — does not change the phase,
        # so the one already open continues.
        if thought or splitter.thinking:
            phase = "thinking"
        elif text:
            phase = "answer"
        for token in _tokens_of(chunk):
            yield StreamEvent("token", token=token, phase=phase)
        for fragment in getattr(delta, "tool_calls", None) or []:
            index = getattr(fragment, "index", 0) or 0
            slot = pending.setdefault(index, {"id": "", "name": "", "arguments": ""})
            identifier = getattr(fragment, "id", None)
            if identifier:
                slot["id"] = identifier
            function = getattr(fragment, "function", None)
            if function is not None:
                name = getattr(function, "name", None)
                if name:
                    slot["name"] = name
                arguments = getattr(function, "arguments", None)
                if arguments:
                    slot["arguments"] += arguments

    for kind, piece in splitter.flush():
        yield StreamEvent("reasoning" if kind == "reasoning" else "text", text=piece)

    if pending:
        calls = [
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=slot["arguments"],
            )
            for index, slot in sorted(pending.items())
        ]
        yield StreamEvent("tool_calls", tool_calls=calls)

    if reported is not None:
        yield StreamEvent("usage", usage=reported)

    yield StreamEvent("done", finish_reason=finish_reason)


def _openai():
    """The SDK, imported the first time something actually needs it.

    Measured: importing ``openai`` is 1.67s of VOX's 2.6s startup, which is
    most of the wait between typing `vox` and seeing a screen. Nothing needs
    it until a provider is contacted — the parser, the transcript, the
    configuration and every command are indifferent to it — so it is loaded
    on the first construction of a client instead of on the way in.

    Cached by ``sys.modules``, so this is a dictionary lookup after the first
    call. Nothing here is on a hot path anyway: it runs once per client and
    once per exception.
    """
    import openai

    return openai


def to_api_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Render conversation messages for the chat completions endpoint."""
    payload: list[dict[str, Any]] = []
    for message in messages:
        rendered = message.to_api()
        if rendered is not None:
            payload.append(rendered)
    return payload


class LLMClient:
    """Thin wrapper over :class:`openai.OpenAI` with readable failures."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 600.0,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        self.base_url = base_url
        self.timeout = float(timeout)
        self.extra_body: dict[str, Any] = dict(extra_body or {})
        self.logprobs_refused = False
        """Set when the provider rejected a request carrying logprobs."""
        self._api_key = api_key or "not-needed"
        self._client = _openai().OpenAI(
            base_url=base_url, api_key=self._api_key, timeout=self.timeout
        )
        self._stream_lock = threading.Lock()
        self._active_request_client: OpenAI | None = None
        self._active_stream: Any | None = None

    def _new_request_client(self) -> OpenAI:
        """Return a disposable client so an in-flight request can be aborted."""
        return _openai().OpenAI(
            base_url=self.base_url,
            api_key=self._api_key,
            timeout=self.timeout,
        )

    def cancel_active_stream(self) -> bool:
        """Close the active response and its transport from another thread.

        Merely setting a cancellation event is not enough while the HTTP
        client is waiting for response headers or the next stream chunk.
        Closing both objects wakes that blocking read so the worker can stop.
        """
        with self._stream_lock:
            stream = self._active_stream
            request_client = self._active_request_client

        closed = False
        for target in (stream, request_client):
            closer = getattr(target, "close", None)
            if callable(closer):
                # Cancellation is best-effort; the worker converts the
                # resulting transport error into a cancelled event.
                with contextlib.suppress(Exception):
                    closer()
                closed = True
        return closed

    @classmethod
    def from_provider(cls, provider: Mapping[str, Any]) -> LLMClient:
        return cls(
            base_url=str(provider.get("base_url", "")),
            api_key=str(provider.get("api_key", "")),
            timeout=float(provider.get("timeout_seconds", 600)),
            extra_body=provider.get("extra_body") or {},
        )

    def warm(self, model: str, timeout: float | None = None) -> float:
        """Ask for a single token so the server loads the model now.

        Returns how long it took. Cold local models can take minutes; doing
        this in the background means the operator waits while typing rather
        than after pressing enter.
        """
        started = time.monotonic()
        client = self._client.with_options(max_retries=0)
        if timeout is not None:
            client = client.with_options(timeout=timeout)
        try:
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=1,
                temperature=0.0,
                extra_body=self.extra_body or None,
            )
        except _openai().APITimeoutError as exc:
            waited = time.monotonic() - started
            raise LLMError(
                "timeout",
                f"the model did not load within {waited:.0f}s",
                str(exc),
            ) from exc
        except _openai().APIConnectionError as exc:
            raise LLMError(
                "connection", f"cannot reach provider: {self.base_url}", str(exc)
            ) from exc
        except _openai().APIStatusError as exc:
            raise _status_error(exc) from exc
        except _openai().OpenAIError as exc:
            raise LLMError("protocol", f"preload failed: {exc}", str(exc)) from exc
        return time.monotonic() - started

    def close(self) -> None:
        """Release the connection pool. Safe to call more than once."""
        # Shutting down is best effort: a pool that is already gone, or a
        # socket the peer closed first, is not a problem worth reporting.
        with contextlib.suppress(OSError, RuntimeError):
            self._client.close()

    def list_models(self, timeout: float | None = None) -> list[str]:
        """Non-destructive reachability check against ``/v1/models``."""
        try:
            # No retries: a reachability probe must fail fast and stay honest.
            client = self._client.with_options(max_retries=0)
            if timeout is not None:
                client = client.with_options(timeout=timeout)
            response = client.models.list()
        except _openai().APITimeoutError as exc:
            raise LLMError(
                "timeout", f"provider timed out: {self.base_url}", str(exc)
            ) from exc
        except _openai().APIConnectionError as exc:
            raise LLMError(
                "connection", f"cannot reach provider: {self.base_url}", str(exc)
            ) from exc
        except _openai().APIStatusError as exc:
            raise LLMError(
                "http", f"provider returned HTTP {exc.status_code}", str(exc)
            ) from exc
        except _openai().OpenAIError as exc:
            raise LLMError("protocol", f"provider error: {exc}", str(exc)) from exc
        return sorted(model.id for model in response.data)

    def ping(self, timeout: float = 5.0) -> tuple[bool, str]:
        """Return ``(reachable, detail)`` without raising."""
        try:
            models = self.list_models(timeout=timeout)
        except LLMError as exc:
            return False, exc.message
        return True, f"{len(models)} models"

    def stream_chat(
        self,
        messages: Sequence[Message],
        model: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        cancel: threading.Event | None = None,
        include_usage: bool = True,
        top_logprobs: int | None = None,
        sampling: dict[str, Any] | None = None,
        native: dict[str, Any] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> Iterator[StreamEvent]:
        """Stream a completion, yielding text deltas then any tool calls.

        With ``include_usage`` the provider is asked to append exact token
        counts to the final chunk; with ``top_logprobs`` it is asked for the
        distribution behind each token. Providers that reject either option
        are retried once without it, so an ordinary chat never fails because
        of a measurement.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": to_api_messages(messages),
            "temperature": temperature,
            "stream": True,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        # Only what was actually set. top_k and repeat_penalty are not OpenAI
        # parameters and a strict gateway rejects them, so they go in
        # extra_body where llama.cpp and Ollama read them; sending a default
        # for every knob would mean every validating provider refuses every
        # request VOX makes.
        if sampling:
            payload.update(sampling)
        if response_format:
            payload["response_format"] = response_format
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if include_usage:
            payload["stream_options"] = {"include_usage": True}
        if top_logprobs and tools:
            # Ollama answers "logprobs is not supported with tools + stream"
            # with a 400, which costs the whole turn. A tool-calling turn
            # cannot be measured here, so do not ask.
            self.logprobs_refused = True
            top_logprobs = None
        if top_logprobs:
            payload["logprobs"] = True
            payload["top_logprobs"] = int(top_logprobs)
        extra = dict(self.extra_body) if self.extra_body else {}
        if native:
            extra.update(native)
        if extra:
            # Provider-specific extras, e.g. Ollama keep_alive and think.
            payload["extra_body"] = extra

        stream: Any | None = None
        request_client = self._new_request_client()
        with self._stream_lock:
            self._active_request_client = request_client
            self._active_stream = None

        try:
            try:
                try:
                    stream = request_client.chat.completions.create(**payload)
                except _openai().APIStatusError as exc:
                    retried = False
                    if top_logprobs and _rejects_logprobs(exc):
                        payload.pop("logprobs", None)
                        payload.pop("top_logprobs", None)
                        self.logprobs_refused = True
                        retried = True
                    elif include_usage and _rejects_stream_options(exc):
                        payload.pop("stream_options", None)
                        retried = True
                    if not retried:
                        raise
                    stream = request_client.chat.completions.create(**payload)
            except _openai().APITimeoutError as exc:
                if cancel is not None and cancel.is_set():
                    yield StreamEvent("cancelled")
                    return
                raise LLMError("timeout", "request timed out", str(exc)) from exc
            except _openai().APIConnectionError as exc:
                if cancel is not None and cancel.is_set():
                    yield StreamEvent("cancelled")
                    return
                raise LLMError(
                    "connection", f"cannot reach provider: {self.base_url}", str(exc)
                ) from exc
            except _openai().APIStatusError as exc:
                if cancel is not None and cancel.is_set():
                    yield StreamEvent("cancelled")
                    return
                raise _status_error(exc) from exc
            except _openai().OpenAIError as exc:
                if cancel is not None and cancel.is_set():
                    yield StreamEvent("cancelled")
                    return
                raise LLMError("protocol", f"provider error: {exc}", str(exc)) from exc
            except Exception:
                # Wide on purpose: tearing the client down mid-request makes
                # the SDK raise whatever its transport happened to be holding,
                # and a cancellation the operator asked for is not an error to
                # report. Anything else is re-raised untouched.
                if cancel is not None and cancel.is_set():
                    yield StreamEvent("cancelled")
                    return
                raise

            with self._stream_lock:
                if self._active_request_client is request_client:
                    self._active_stream = stream

            if cancel is not None and cancel.is_set():
                yield StreamEvent("cancelled")
                return

            try:
                yield from consume_stream(stream, cancel)
            except _openai().APITimeoutError as exc:
                if cancel is not None and cancel.is_set():
                    yield StreamEvent("cancelled")
                    return
                raise LLMError("timeout", "stream timed out", str(exc)) from exc
            except _openai().APIConnectionError as exc:
                if cancel is not None and cancel.is_set():
                    yield StreamEvent("cancelled")
                    return
                raise LLMError("connection", "stream interrupted", str(exc)) from exc
            except _openai().APIStatusError as exc:
                if cancel is not None and cancel.is_set():
                    yield StreamEvent("cancelled")
                    return
                raise _status_error(exc) from exc
            except _openai().OpenAIError as exc:
                if cancel is not None and cancel.is_set():
                    yield StreamEvent("cancelled")
                    return
                raise LLMError("protocol", f"stream error: {exc}", str(exc)) from exc
            except Exception:
                # Wide on purpose: tearing the client down mid-request makes
                # the SDK raise whatever its transport happened to be holding,
                # and a cancellation the operator asked for is not an error to
                # report. Anything else is re-raised untouched.
                if cancel is not None and cancel.is_set():
                    yield StreamEvent("cancelled")
                    return
                raise
        finally:
            with self._stream_lock:
                if self._active_request_client is request_client:
                    self._active_request_client = None
                    self._active_stream = None
            for target in (stream, request_client):
                closer = getattr(target, "close", None)
                if callable(closer):
                    with contextlib.suppress(Exception):
                        closer()


def _unwrap_message(text: str) -> str:
    """Ollama nests a whole JSON error document inside ``error.message``."""
    for _ in range(3):
        stripped = text.strip()
        if not stripped.startswith("{"):
            return text
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return text
        inner = data.get("error") if isinstance(data, dict) else None
        if isinstance(inner, dict) and inner.get("message"):
            text = str(inner["message"])
        elif isinstance(inner, str):
            text = inner
        else:
            return text
    return text


def _status_detail(exc: _openai().APIStatusError) -> str:
    """Best-effort human text out of an HTTP error body."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return _unwrap_message(str(error["message"]))
        return json.dumps(body)[:500]
    return str(exc)


_OVERFLOW = re.compile(
    r"request \((\d+) tokens?\) exceeds the available context size "
    r"\((\d+) tokens?\)",
    re.IGNORECASE,
)


def context_overflow(text: str) -> tuple[int, int] | None:
    """``(prompt tokens, window)`` when the refusal was lack of room.

    The prompt no longer fits, which is a different problem from a malformed
    request and has a different remedy, so it must not be reported as a bare
    HTTP 400.
    """
    match = _OVERFLOW.search(text)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def _status_error(exc: _openai().APIStatusError) -> LLMError:
    """The failure, named as precisely as the body allows."""
    detail = _status_detail(exc)
    room = context_overflow(detail) or context_overflow(str(exc))
    if room is not None:
        used, window = room
        return LLMError(
            "context",
            f"prompt is {used} tokens, the model has room for {window} - "
            f"shorten the turn, or load the model with a larger window",
            detail,
        )
    return LLMError("http", f"provider returned HTTP {exc.status_code}", detail)


def _rejects_logprobs(exc: _openai().APIStatusError) -> bool:
    """True when the provider refused the measurement, not the request."""
    if exc.status_code not in (400, 404, 422, 501):
        return False
    return "logprob" in f"{exc} {_status_detail(exc)}".lower()


def _rejects_stream_options(exc: _openai().APIStatusError) -> bool:
    """True when the provider refused the usage option rather than the call."""
    if exc.status_code not in (400, 404, 422):
        return False
    return "stream_options" in f"{exc} {_status_detail(exc)}".lower()


def supports_tools_error(exc: LLMError) -> bool:
    """True when the failure looks like the model refusing tool calling."""
    if exc.kind not in ("http", "protocol"):
        return False
    haystack = f"{exc.message} {exc.detail}".lower()
    return any(
        marker in haystack
        for marker in ("does not support tools", "tools", "tool_choice", "function")
    )
