"""The context window: fitting a request to it, and raising it.

Nothing here talks to Ollama. The native API is exercised through a stubbed
``urlopen``, so the shapes VOX depends on are pinned by a test rather than by
one lucky machine.
"""

from __future__ import annotations

import io
import json
import urllib.request
from email.message import EmailMessage
from pathlib import Path

import pytest
from textual.widgets import ListView

from vox_chat import fitting, ollama
from vox_chat.app import VoxApp, describe_model, describe_residency
from vox_chat.commands import handlers
from vox_chat.config import default_config, load_config
from vox_chat.llm_client import LLMError
from vox_chat.models import Message
from vox_chat.storage import global_config_path
from vox_chat.ui.modals import PickerItem, PickerModal
from vox_chat.ui.widgets import KeyBar, MessageBox


@pytest.fixture
def app(workspace: Path) -> VoxApp:
    config = default_config()
    config["providers"]["local-ollama"]["base_url"] = "http://127.0.0.1:1/v1"
    config["providers"]["local-ollama"]["timeout_seconds"] = 2
    config["generation"]["preload"] = False
    global_config_path().write_text(json.dumps(config), encoding="utf-8")
    return VoxApp(loaded=load_config(workspace), workspace=workspace)


def transcript(app: VoxApp) -> str:
    return "\n".join(
        box.message.content
        for box in app.transcript.children
        if isinstance(box, MessageBox)
    )


# ------------------------------------------------------------------- fitting


def a_request(sources: int = 4000) -> list[Message]:
    return [
        Message(role="system", content="You are terse."),
        Message(role="system", content="SOURCES\n" + "x" * sources),
        Message(role="user", content="what is the postcode of Latiano?"),
    ]


def test_the_research_block_is_what_gets_cut() -> None:
    """The largest system message is the sources, and the tail of it is the
    least valuable text in the request."""
    messages = a_request()
    smaller = fitting.shrink(messages, used=4227, window=4096)
    assert smaller is not None
    assert smaller[0].content == "You are terse.", "the role survives"
    assert smaller[2].content == messages[2].content, "and so does the question"
    assert len(smaller[1].content) < len(messages[1].content)
    assert smaller[1].content.endswith(fitting.TRIM_NOTE)


def test_the_cut_is_large_enough_to_be_worth_making() -> None:
    """131 tokens over, plus headroom, is about 1.6 kB of characters."""
    messages = a_request()
    smaller = fitting.shrink(messages, used=4227, window=4096)
    assert smaller is not None
    removed = len(messages[1].content) - len(smaller[1].content)
    assert removed > (4227 - 4096 + fitting.HEADROOM_TOKENS) * 4


def test_a_block_with_nothing_left_to_give_is_dropped_whole() -> None:
    messages = a_request(sources=500)
    smaller = fitting.shrink(messages, used=9000, window=4096)
    assert smaller is not None
    assert len(smaller) == 2
    assert all(m.role != "system" or "SOURCES" not in m.content for m in smaller)


def test_a_request_that_already_fits_is_left_alone() -> None:
    assert fitting.shrink(a_request(), used=100, window=4096) is None


def test_there_is_nothing_to_cut_without_a_system_message() -> None:
    messages = [Message(role="user", content="hello")]
    assert fitting.shrink(messages, used=9000, window=4096) is None


def test_the_operator_is_told_what_was_given_up() -> None:
    messages = a_request()
    smaller = fitting.shrink(messages, used=4227, window=4096)
    assert smaller is not None
    line = fitting.describe(messages, smaller)
    assert line.startswith("PROMPT TRIMMED")
    assert any(character.isdigit() for character in line)


# --------------------------------------------------------- the retry, in the app


class OneRefusal:
    """A provider that refuses the first request for lack of room."""

    logprobs_refused = False

    def __init__(self) -> None:
        self.sizes: list[int] = []

    def close(self) -> None:
        """The app closes its client on the way out."""

    def stream_chat(self, messages, **kwargs):
        self.sizes.append(sum(len(m.content) for m in messages))
        if len(self.sizes) == 1:
            raise LLMError(
                "context",
                "prompt is 4227 tokens, the model has room for 4096",
                "request (4227 tokens) exceeds the available context size "
                "(4096 tokens), try increasing it",
            )
        return iter(())


async def test_a_turn_refused_for_room_is_retried_smaller(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        client = OneRefusal()
        app.client = client  # type: ignore[assignment]
        app.connected = True
        app._web_prompt = "SOURCES\n" + "x" * 4000
        app.session.messages.append(Message(role="user", content="a question"))
        app.start_generation()
        await app.workers.wait_for_complete()
        await pilot.pause()
        written = transcript(app)

    assert len(client.sizes) == 2, "one refusal, one retry"
    assert client.sizes[1] < client.sizes[0], "and the retry is smaller"
    assert "PROMPT TRIMMED" in written


class AlwaysRefuses(OneRefusal):
    def stream_chat(self, messages, **kwargs):
        self.sizes.append(sum(len(m.content) for m in messages))
        raise LLMError(
            "context",
            "prompt is 90000 tokens, the model has room for 4096",
            "request (90000 tokens) exceeds the available context size (4096 tokens)",
        )


async def test_a_window_that_can_never_fit_is_reported_not_retried_for_ever(
    app: VoxApp,
) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        client = AlwaysRefuses()
        app.client = client  # type: ignore[assignment]
        app.connected = True
        app._web_prompt = "SOURCES\n" + "x" * 4000
        app.session.messages.append(Message(role="user", content="a question"))
        app.start_generation()
        await app.workers.wait_for_complete()
        await pilot.pause()
        written = transcript(app)

    assert len(client.sizes) <= 3, "the retries are bounded"
    # Once there is nothing left to cut, the provider's own words are the
    # honest answer: the prompt is too large for this model, full stop.
    assert "room for 4096" in written


class OtherFailure(OneRefusal):
    def stream_chat(self, messages, **kwargs):
        self.sizes.append(sum(len(m.content) for m in messages))
        raise LLMError("http", "provider returned HTTP 400", "unknown field")


async def test_any_other_failure_is_not_retried(app: VoxApp) -> None:
    async with app.run_test() as pilot:
        await pilot.pause()
        client = OtherFailure()
        app.client = client  # type: ignore[assignment]
        app.connected = True
        app.session.messages.append(Message(role="user", content="a question"))
        app.start_generation()
        await app.workers.wait_for_complete()
        await pilot.pause()
        written = transcript(app)

    assert len(client.sizes) == 1
    assert "HTTP 400" in written


# ------------------------------------------------------------- the native API


def test_the_native_base_is_the_openai_one_without_the_version() -> None:
    assert ollama.native_base("http://localhost:11434/v1") == "http://localhost:11434"
    assert ollama.native_base("http://localhost:11434/v1/") == "http://localhost:11434"
    assert ollama.native_base("http://localhost:11434") == "http://localhost:11434"


def test_only_ollama_endpoints_are_asked_with_ollamas_own_api() -> None:
    assert ollama.looks_like_ollama("http://localhost:11434/v1")
    assert not ollama.looks_like_ollama("https://api.openai.com/v1")


def test_the_derived_name_is_stable_and_says_what_it_is() -> None:
    assert ollama.derived_name("granite4.2:3b", 16384) == "vox-granite4.2-3b:ctx16384"
    assert ollama.derived_name("openbmb/minicpm5:latest", 8192) == (
        "vox-openbmb-minicpm5-latest:ctx8192"
    )
    # Asking twice gives the same manifest rather than a second one.
    assert ollama.derived_name("granite4.2:3b", 16384) == ollama.derived_name(
        "granite4.2:3b", 16384
    )


def test_a_derived_model_is_derived_again_from_the_original(monkeypatch) -> None:
    """Otherwise every change of window keeps the previous one alive on disk."""
    http = FakeHTTP(
        {
            "/api/show": {"details": {"parent_model": "granite4.2:3b"}},
            "/api/create": {"status": "success"},
        }
    )
    monkeypatch.setattr(urllib.request, "urlopen", http)
    name = ollama.create_with_context(
        "http://localhost:11434/v1", "vox-granite4.2-3b:ctx8192", 16384
    )
    assert name == "vox-granite4.2-3b:ctx16384"
    assert http.calls[-1][1]["from"] == "granite4.2:3b", "not the derived one"


def test_only_names_vox_wrote_count_as_derived() -> None:
    assert ollama.is_derived("vox-granite4.2-3b:ctx16384")
    assert not ollama.is_derived("granite4.2:3b")
    assert not ollama.is_derived("vox-something:latest")


class FakeHTTP:
    """Stands in for urlopen: records the calls, answers from a script."""

    def __init__(self, answers: dict[str, dict]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request, timeout=None):
        url = request if isinstance(request, str) else request.full_url
        payload = {}
        if not isinstance(request, str) and request.data:
            payload = json.loads(request.data.decode("utf-8"))
        path = url.split("11434", 1)[-1]
        self.calls.append((path, payload))
        body = json.dumps(self.answers.get(path, {})).encode("utf-8")
        return _closing(body)


class _closing(io.BytesIO):
    """What urlopen hands back, near enough: a body, headers and a URL."""

    def __init__(self, body: bytes, url: str = "http://127.0.0.1:11434/") -> None:
        super().__init__(body)
        self._url = url
        headers = EmailMessage()
        headers["Content-Type"] = "application/json"
        self.headers = headers
        self.status = 200

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_model_list_carries_what_tells_two_local_builds_apart(monkeypatch) -> None:
    http = FakeHTTP(
        {
            "/api/tags": {
                "models": [
                    {
                        "name": "granite4.2:3b",
                        "size": 2_244_023_965,
                        "details": {
                            "parameter_size": "3.7B",
                            "quantization_level": "Q4_K_M",
                        },
                    },
                    {"name": "qwen2.5:3b", "size": 1_900_000_000, "details": {}},
                    {"no name": True},
                ]
            }
        }
    )
    monkeypatch.setattr(urllib.request, "urlopen", http)
    rows = ollama.list_models("http://localhost:11434/v1")
    assert [row["name"] for row in rows] == ["granite4.2:3b", "qwen2.5:3b"]
    assert rows[0]["parameters"] == "3.7B"
    assert describe_model(rows[0]) == "2.2 GB  3.7B  Q4_K_M"


def test_creating_a_window_sends_from_and_num_ctx(monkeypatch) -> None:
    http = FakeHTTP({"/api/create": {"status": "success"}})
    monkeypatch.setattr(urllib.request, "urlopen", http)
    name = ollama.create_with_context(
        "http://localhost:11434/v1", "granite4.2:3b", 16384
    )
    assert name == "vox-granite4.2-3b:ctx16384"
    path, payload = http.calls[-1]
    assert path == "/api/create"
    assert payload["from"] == "granite4.2:3b", "the weights are not copied"
    assert payload["parameters"] == {"num_ctx": 16384}


def test_an_unusable_window_is_refused_before_the_server_is_asked(monkeypatch) -> None:
    http = FakeHTTP({})
    monkeypatch.setattr(urllib.request, "urlopen", http)
    with pytest.raises(ollama.OllamaError):
        ollama.create_with_context("http://localhost:11434/v1", "m", 128)
    assert http.calls == []


def test_the_loaded_window_is_read_from_what_is_resident(monkeypatch) -> None:
    http = FakeHTTP(
        {
            "/api/ps": {
                "models": [
                    {"name": "granite4.2:3b", "context_length": 16384},
                ]
            }
        }
    )
    monkeypatch.setattr(urllib.request, "urlopen", http)
    base = "http://localhost:11434/v1"
    assert ollama.loaded_context(base, "granite4.2:3b") == 16384
    assert ollama.loaded_context(base, "not resident") is None


def test_the_trained_window_comes_from_the_weights(monkeypatch) -> None:
    http = FakeHTTP(
        {
            "/api/show": {
                "model_info": {
                    "granite.context_length": 131072,
                    "granite.block_count": 40,
                },
                "parameters": "num_ctx                        8192\ntemperature 1",
            }
        }
    )
    monkeypatch.setattr(urllib.request, "urlopen", http)
    base = "http://localhost:11434/v1"
    assert ollama.trained_context(base, "granite4.2:3b") == 131072
    assert ollama.configured_context(base, "granite4.2:3b") == 8192


# --------------------------------------------------------------- /model ctx


async def test_model_ctx_refuses_on_a_provider_that_fixes_the_window(
    app: VoxApp,
) -> None:
    app.config["providers"]["local-ollama"]["base_url"] = "https://api.openai.com/v1"
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers.cmd_model_ctx(app, "16384")
        await pilot.pause()
        assert "only Ollama" in transcript(app)


async def test_model_ctx_wants_a_number(app: VoxApp) -> None:
    app.config["providers"]["local-ollama"]["base_url"] = "http://localhost:11434/v1"
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers.cmd_model_ctx(app, "big")
        await pilot.pause()
        assert "/model ctx [N|off]" in transcript(app)


async def test_model_ctx_off_goes_back_to_the_original(
    app: VoxApp, monkeypatch
) -> None:
    """The name cannot be reversed - granite4.2:3b flattens to granite4.2-3b -
    so the parent is read from Ollama rather than guessed at."""
    app.config["providers"]["local-ollama"]["base_url"] = "http://localhost:11434/v1"
    app.config["active_model"] = "vox-granite4.2-3b:ctx16384"
    monkeypatch.setattr(ollama, "parent_model", lambda *a, **k: "granite4.2:3b")
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers.cmd_model_ctx(app, "off")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "MODEL CTX OFF" in transcript(app)
    assert app.config["active_model"] == "granite4.2:3b"


async def test_model_ctx_off_on_a_model_nobody_derived(app: VoxApp) -> None:
    app.config["providers"]["local-ollama"]["base_url"] = "http://localhost:11434/v1"
    app.config["active_model"] = "granite4.2:3b"
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers.cmd_model_ctx(app, "off")
        await pilot.pause()
        assert "already the original" in transcript(app)


async def test_a_window_larger_than_the_weights_is_refused(
    app: VoxApp, monkeypatch
) -> None:
    app.config["providers"]["local-ollama"]["base_url"] = "http://localhost:11434/v1"
    app.config["active_model"] = "granite4.2:3b"
    monkeypatch.setattr(ollama, "trained_context", lambda *a, **k: 4096)
    created: list = []
    monkeypatch.setattr(
        ollama, "create_with_context", lambda *a, **k: created.append(a) or "never"
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers.cmd_model_ctx(app, "131072")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "trained for 4096" in transcript(app)
    assert created == [], "nothing is written for a window the weights cannot use"


@pytest.mark.slow
async def test_a_built_window_becomes_the_active_model(
    app: VoxApp, monkeypatch
) -> None:
    app.config["providers"]["local-ollama"]["base_url"] = "http://localhost:11434/v1"
    app.config["active_model"] = "granite4.2:3b"
    monkeypatch.setattr(ollama, "trained_context", lambda *a, **k: 131072)
    monkeypatch.setattr(
        ollama, "create_with_context", lambda *a, **k: "vox-granite4.2-3b:ctx16384"
    )
    # Without this the build measures the GPU, which means a real request to
    # localhost:11434. On a machine with Ollama running the test passed; on
    # CI it failed on all nine runners with a connection error in place of the
    # message being asserted. A test about what VOX writes should not depend
    # on what is listening.
    monkeypatch.setattr(ollama, "fit_layers", lambda *a, **k: (0, None))
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers.cmd_model_ctx(app, "16384")
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "MODEL CTX - 16384 tokens" in transcript(app)
    assert app.config["active_model"] == "vox-granite4.2-3b:ctx16384"


# ------------------------------------------------------------- keys and picker


def test_the_key_bar_leads_with_where_everything_else_is() -> None:
    """It carried five entries on the principle that a legend nobody can read
    at a glance is decoration. That was costing more than it saved: the one
    thing that has to be on screen is where the rest is written down, so ^L
    is first — and the row is dropped from the right, so it is also the entry
    a narrow terminal keeps."""
    labels = [label for _key, label in KeyBar.KEYS]
    assert labels == ["all keys", "send", "copy/paste", "quit", "stop", "mode"]
    assert KeyBar.KEYS[0][0] == "^L"
    assert dict(KeyBar.KEYS)["F2"] == "mode"


def test_mode_is_on_f2_and_the_models_are_on_f12() -> None:
    actions: dict[str, list[str]] = {}
    for binding in VoxApp.BINDINGS:
        actions.setdefault(binding.action, []).append(binding.key)
    assert "f2" in actions["toggle_agent"]
    assert actions["pick_model"] == ["f12"]
    # Inspect had f2; it keeps the key it can always be reached by.
    assert "f2" not in actions["open_inspect"]
    assert "ctrl+t" in actions["open_inspect"]


async def test_the_picker_is_driven_by_the_arrows_alone(app: VoxApp) -> None:
    """Down, down, enter - without typing a filter, the way a launcher works."""
    chosen: list[str | None] = []
    items = [PickerItem(name, name) for name in ("alpha", "beta", "gamma")]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(PickerModal("MODELS", items), chosen.append)
        await pilot.pause()
        listing = app.screen.query_one("#items", ListView)
        assert listing.index == 0, "the first row is live as soon as it opens"

        await pilot.press("down")
        await pilot.pause()
        assert listing.index == 1
        await pilot.press("up")
        await pilot.pause()
        assert listing.index == 0

        await pilot.press("down", "down")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert chosen == ["gamma"]


async def test_filtering_still_works_and_leaves_a_row_live(app: VoxApp) -> None:
    chosen: list[str | None] = []
    items = [PickerItem(name, name) for name in ("alpha", "beta", "gamma")]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.push_screen(PickerModal("MODELS", items), chosen.append)
        await pilot.pause()
        await pilot.press("g", "a")
        await pilot.pause()
        assert app.screen.query_one("#items", ListView).index == 0
        await pilot.press("enter")
        await pilot.pause()
    assert chosen == ["gamma"]


async def test_the_active_model_is_the_row_the_picker_opens_on(
    app: VoxApp, monkeypatch
) -> None:
    app.config["providers"]["local-ollama"]["base_url"] = "http://localhost:11434/v1"
    app.config["active_model"] = "qwen2.5:3b"
    monkeypatch.setattr(
        ollama,
        "list_models",
        lambda *a, **k: [
            {
                "name": "granite4.2:3b",
                "size": 2_244_023_965,
                "parameters": "3.7B",
                "quantization": "Q4_K_M",
            },
            {
                "name": "qwen2.5:3b",
                "size": 1_900_000_000,
                "parameters": "3B",
                "quantization": "",
            },
        ],
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        items = app.model_items()
    assert items[0].value == "qwen2.5:3b"
    assert items[0].title.startswith(">"), "and it is marked"
    assert "3.7B" in items[1].subtitle


# ------------------------------------------------------------------- the GPU


def resident(**kwargs) -> ollama.Residency:
    defaults = dict(
        size=3_085_539_736,
        size_vram=2_845_499_719,
        context=16384,
        gpu_used_mb=2755,
        gpu_total_mb=4096,
    )
    defaults.update(kwargs)
    return ollama.Residency(**defaults)


def test_a_model_that_fits_is_not_reported_as_spilling() -> None:
    assert resident().spilling(384) is False
    assert resident().on_cpu == 3_085_539_736 - 2_845_499_719


def test_a_full_card_means_the_rest_is_in_shared_memory() -> None:
    """Ollama says the whole model is in VRAM; the card says it is full. The
    card is the one telling the truth."""
    full = resident(size=5_470_000_000, size_vram=5_120_000_000, gpu_used_mb=4003)
    assert full.spilling(384) is True
    assert "shared memory" in describe_residency(full)


def test_nothing_is_claimed_when_there_is_no_card_to_ask() -> None:
    blind = resident(gpu_used_mb=None, gpu_total_mb=None)
    assert blind.spilling(384) is False
    assert "card" not in describe_residency(blind)


def test_the_fit_search_stops_at_the_first_layout_that_stays_on_the_card(
    monkeypatch,
) -> None:
    tried: list[int] = []

    def fake_probe(base_url, model, options, timeout=900.0):
        layers = options["num_gpu"]
        tried.append(layers)
        # Everything above 30 layers overflows this imaginary card.
        return resident(gpu_used_mb=4003 if layers > 30 else 2600)

    monkeypatch.setattr(ollama, "layer_count", lambda *a, **k: 40)
    monkeypatch.setattr(ollama, "probe", fake_probe)
    best, where = ollama.fit_layers("http://localhost:11434/v1", "m", 16384, 384)
    assert best == 30, tried
    assert tried == [40, 35, 30], "it steps down, it does not binary search blindly"
    assert where is not None and not where.spilling(384)


def test_a_card_that_can_hold_nothing_gives_up_rather_than_looping(monkeypatch) -> None:
    monkeypatch.setattr(ollama, "layer_count", lambda *a, **k: 40)
    monkeypatch.setattr(ollama, "probe", lambda *a, **k: resident(gpu_used_mb=4090))
    best, where = ollama.fit_layers("http://localhost:11434/v1", "m", 16384, 384)
    assert best == 0 and where is None


def test_the_build_parameters_are_written_beside_the_window(monkeypatch) -> None:
    http = FakeHTTP({"/api/create": {"status": "success"}})
    monkeypatch.setattr(urllib.request, "urlopen", http)
    ollama.create_with_context(
        "http://localhost:11434/v1",
        "granite4.2:3b",
        8192,
        {"num_gpu": 40, "num_batch": 256},
    )
    _path, payload = http.calls[-1]
    assert payload["parameters"] == {"num_ctx": 8192, "num_gpu": 40, "num_batch": 256}


def test_the_layer_count_is_the_ceiling_for_num_gpu(monkeypatch) -> None:
    http = FakeHTTP({"/api/show": {"model_info": {"granite.block_count": 40}}})
    monkeypatch.setattr(urllib.request, "urlopen", http)
    assert ollama.layer_count("http://localhost:11434/v1", "granite4.2:3b") == 40


def test_the_card_is_read_from_nvidia_smi(monkeypatch) -> None:
    class Completed:
        stdout = "2755, 4096\n"

    monkeypatch.setattr(ollama.shutil, "which", lambda name: "nvidia-smi")
    monkeypatch.setattr(ollama.subprocess, "run", lambda *a, **k: Completed())
    assert ollama.vram_mb() == (2755, 4096)


def test_no_card_is_not_an_error(monkeypatch) -> None:
    monkeypatch.setattr(ollama.shutil, "which", lambda name: None)
    assert ollama.vram_mb() is None


# ------------------------------------------------------------- /model gpu


async def test_the_default_build_fills_the_gpu(app: VoxApp, monkeypatch) -> None:
    """num_gpu "max" is the shipped default, and it is measured before use."""
    assert default_config()["model_build"]["num_gpu"] == "max"
    app.config["providers"]["local-ollama"]["base_url"] = "http://localhost:11434/v1"
    app.config["active_model"] = "granite4.2:3b"
    monkeypatch.setattr(ollama, "trained_context", lambda *a, **k: 131072)
    monkeypatch.setattr(ollama, "fit_layers", lambda *a, **k: (40, resident()))
    written: list = []

    def fake_create(base_url, model, num_ctx, parameters=None, timeout=120.0):
        written.append((num_ctx, parameters))
        return "vox-granite4.2-3b:ctx16384"

    monkeypatch.setattr(ollama, "create_with_context", fake_create)
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers.cmd_model_ctx(app, "16384")
        await app.workers.wait_for_complete()
        await pilot.pause()
        written_text = transcript(app)
    assert written == [(16384, {"num_gpu": 40})]
    assert "40 layers on the GPU" in written_text


async def test_a_build_can_leave_the_split_to_ollama(app: VoxApp, monkeypatch) -> None:
    app.config["providers"]["local-ollama"]["base_url"] = "http://localhost:11434/v1"
    app.config["active_model"] = "granite4.2:3b"
    app.config["model_build"]["num_gpu"] = None
    monkeypatch.setattr(ollama, "trained_context", lambda *a, **k: 131072)
    monkeypatch.setattr(
        ollama,
        "fit_layers",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("nothing should be measured when the split is not ours")
        ),
    )
    written: list = []
    monkeypatch.setattr(
        ollama,
        "create_with_context",
        lambda b, m, n, parameters=None, timeout=120.0: (
            written.append(parameters) or "vox-x:ctx16384"
        ),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers.cmd_model_ctx(app, "16384")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert written == [{}]


async def test_model_gpu_max_measures_then_writes(app: VoxApp, monkeypatch) -> None:
    app.config["providers"]["local-ollama"]["base_url"] = "http://localhost:11434/v1"
    app.config["active_model"] = "granite4.2:3b"
    monkeypatch.setattr(ollama, "configured_context", lambda *a, **k: 16384)
    monkeypatch.setattr(ollama, "fit_layers", lambda *a, **k: (33, resident()))
    written: list = []
    monkeypatch.setattr(
        ollama,
        "create_with_context",
        lambda b, m, n, parameters=None, timeout=120.0: (
            written.append((n, parameters)) or "vox-granite4.2-3b:ctx16384"
        ),
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers.cmd_model_gpu(app, "max")
        await app.workers.wait_for_complete()
        await pilot.pause()
        said = transcript(app)
    assert written == [(16384, {"num_gpu": 33})]
    assert "33 layers on the card" in said
    assert app.config["active_model"] == "vox-granite4.2-3b:ctx16384"


async def test_model_gpu_off_hands_the_split_back(app: VoxApp) -> None:
    app.config["providers"]["local-ollama"]["base_url"] = "http://localhost:11434/v1"
    app.config["active_model"] = "granite4.2:3b"
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers.cmd_model_gpu(app, "off")
        await pilot.pause()
        assert "MODEL GPU OFF" in transcript(app)
    assert app.config["model_build"]["num_gpu"] is None


async def test_model_gpu_reports_where_the_model_lives(
    app: VoxApp, monkeypatch
) -> None:
    app.config["providers"]["local-ollama"]["base_url"] = "http://localhost:11434/v1"
    app.config["active_model"] = "granite4.2:3b"
    monkeypatch.setattr(ollama, "residency", lambda *a, **k: resident())
    monkeypatch.setattr(ollama, "layer_count", lambda *a, **k: 40)
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers.cmd_model_gpu(app, "")
        await app.workers.wait_for_complete()
        await pilot.pause()
        said = transcript(app)
    assert "2.85 GB on the GPU" in said
    assert "card 2755/4096 MB" in said


async def test_model_gpu_is_ollama_only(app: VoxApp) -> None:
    app.config["providers"]["local-ollama"]["base_url"] = "https://api.openai.com/v1"
    async with app.run_test() as pilot:
        await pilot.pause()
        handlers.cmd_model_gpu(app, "max")
        await pilot.pause()
        assert "only Ollama" in transcript(app)


def test_a_derived_model_is_found_under_its_parents_name(monkeypatch) -> None:
    """Regression: a derived model shares its blobs, and /api/ps lists it under
    the model it was built from, so an exact match never finds it."""
    http = FakeHTTP(
        {
            "/api/ps": {
                "models": [
                    {
                        "name": "granite4.2:3b",
                        "size": 3_085_539_736,
                        "size_vram": 2_845_499_719,
                        "context_length": 16384,
                    },
                ]
            },
            "/api/show": {"details": {"parent_model": "granite4.2:3b"}},
        }
    )
    monkeypatch.setattr(urllib.request, "urlopen", http)
    monkeypatch.setattr(ollama, "vram_mb", lambda: (2755, 4096))
    base = "http://localhost:11434/v1"
    where = ollama.residency(base, "vox-granite4.2-3b:ctx16384")
    assert where is not None and where.context == 16384
    assert ollama.loaded_context(base, "vox-granite4.2-3b:ctx16384") == 16384
    # A model that is genuinely not loaded is still reported as not loaded.
    assert ollama.residency(base, "qwen2.5:3b") is None
