"""Leaving the app: promptly, and without waiting on a stuck request."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from vox_chat.__main__ import _stuck_threads, leave
from vox_chat.app import VoxApp
from vox_chat.config import default_config, load_config
from vox_chat.storage import global_config_path


@pytest.fixture
def app(workspace: Path) -> VoxApp:
    config = default_config()
    config["providers"]["local-ollama"]["base_url"] = "http://127.0.0.1:1/v1"
    config["providers"]["local-ollama"]["timeout_seconds"] = 2
    config["generation"]["preload"] = False
    global_config_path().write_text(json.dumps(config), encoding="utf-8")
    return VoxApp(loaded=load_config(workspace), workspace=workspace)


def test_nothing_stuck_means_leave_returns_at_once() -> None:
    started = time.monotonic()
    assert leave(0) == 0
    assert time.monotonic() - started < 0.5, "a clean exit pays no penalty"


def test_a_blocked_worker_is_seen_and_the_process_is_forced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-daemon thread stuck on the network would hold the interpreter."""
    release = threading.Event()
    blocked = threading.Thread(
        target=release.wait, daemon=False, name="pretend-request"
    )
    blocked.start()
    try:
        assert any(t.name == "pretend-request" for t in _stuck_threads())

        forced: list[int] = []
        monkeypatch.setattr("vox_chat.__main__.os._exit", forced.append)
        started = time.monotonic()
        leave(3, grace_seconds=0.2)
        elapsed = time.monotonic() - started

        assert forced == [3], "it leaves with the requested code"
        assert 0.2 <= elapsed < 2.0, "after a short grace period, not minutes"
    finally:
        release.set()
        blocked.join(timeout=5)


def test_the_grace_period_lets_a_finishing_worker_end_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    almost_done = threading.Thread(target=lambda: time.sleep(0.2), daemon=False)
    almost_done.start()
    forced: list[int] = []
    monkeypatch.setattr("vox_chat.__main__.os._exit", forced.append)

    assert leave(0, grace_seconds=5) == 0
    assert forced == [], "no need to force anything"
    almost_done.join(timeout=5)


async def test_shutdown_closes_the_client_and_stops_the_timer(app: VoxApp) -> None:
    closed: list[bool] = []

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.client is not None
        app.client.close = lambda: closed.append(True)  # type: ignore[method-assign]
        app._usage_timer = app.set_interval(60, lambda: None)

        app.shutdown()
        assert app._shutting_down is True
        assert app.cancel_event.is_set()
        assert app._usage_timer is None
        assert closed == [True]


async def test_ctrl_q_shuts_down_before_leaving(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app._exit is True
        assert app._shutting_down is True, "the client and timers are released"


async def test_a_request_killed_by_the_shutdown_raises_no_dialog(app: VoxApp) -> None:
    """Tearing the client down mid-request must not pop an error on the way out."""
    from vox_chat.llm_client import LLMError

    async with app.run_test() as pilot:
        await pilot.pause()
        app._shutting_down = True
        app.generating = True
        errors_before = [
            box
            for box in app.transcript.children
            if getattr(getattr(box, "message", None), "role", "") == "error"
        ]
        app.finish_generation(None)
        await pilot.pause()
        errors_after = [
            box
            for box in app.transcript.children
            if getattr(getattr(box, "message", None), "role", "") == "error"
        ]
        assert len(errors_after) == len(errors_before)
        assert isinstance(LLMError("timeout", "x"), Exception)
