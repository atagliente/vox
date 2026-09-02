"""What each slash command does.

Plain functions over the application rather than methods on it: the command
table in ``dispatch`` is what joins a name to one of these, and ``VoxApp`` is
back to drawing the screen. The first argument is the application because
several of these still need it whole — a command that opens a modal or starts
a worker is asking the Textual event loop for something, and pretending
otherwise would be a fiction. What it does buy is that the forty of them are
no longer forty methods on the class that also owns the widget tree, and that
adding one is adding a function and a table entry.

`vox_chat.commands.ui.CommandUI` is the narrow view; handlers that need
nothing more are annotated with it, and those are the ones a test can drive
without mounting an application.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .. import consensus as cns
from .. import ollama, searchd
from .. import report as reporting
from . import spec as commands

if TYPE_CHECKING:
    from ..app import VoxApp


def cmd_warm(app: VoxApp, argument: str) -> None:
    app.preload_model()


def cmd_inspect(app: VoxApp, argument: str) -> None:
    value = argument.strip().lower()
    if not value:
        app.action_open_inspect()
        return
    if value not in ("on", "off"):
        app.write_error("USAGE: /inspect [on|off]")
        return
    app.config.setdefault("inspect", {})["enabled"] = value == "on"
    app.persist_config()
    if value == "on":
        top_k = app.inspect_config().get("top_k", 5)
        app.write_system(
            f"INSPECTION ON - the next answer is measured with top-k {top_k}. "
            "Ctrl+T shows the table."
        )
    else:
        app.write_system("INSPECTION OFF - requests carry no logprobs again")
    app.refresh_status()


def cmd_help(app: VoxApp, argument: str) -> None:
    app.write_system(commands.help_text())


def cmd_keys(app: VoxApp, argument: str) -> None:
    app.action_open_keys()


def cmd_new(app: VoxApp, argument: str) -> None:
    app.action_new_session()


def cmd_clear(app: VoxApp, argument: str) -> None:
    app.transcript.clear_messages()
    app.session.messages.clear()
    app.dirty = False
    app.write_system("TRANSCRIPT CLEARED")


def cmd_settings(app: VoxApp, argument: str) -> None:
    app.action_open_settings()


def cmd_config(app: VoxApp, argument: str) -> None:
    app.edit_config_file()


def cmd_provider(app: VoxApp, argument: str) -> Any:
    providers = list(app.config.get("providers", {}))
    if not argument:
        return app._pick_provider(providers)
    if argument not in providers:
        app.write_error(f"UNKNOWN PROVIDER: {argument} - HAVE: {', '.join(providers)}")
        return None
    app._set_provider(argument)
    return None


def cmd_model(app: VoxApp, argument: str) -> Any:
    parts = argument.split()
    if parts and parts[0].lower() == "ctx":
        app.cmd_model_ctx(" ".join(parts[1:]))
        return None
    if parts and parts[0].lower() == "gpu":
        app.cmd_model_gpu(" ".join(parts[1:]))
        return None
    if argument:
        app.config["active_model"] = argument
        app.persist_config()
        app.write_system(f"MODEL SET - {argument}")
        app.refresh_status()
        if app.connected and app.config.get("generation", {}).get("preload", True):
            app.preload_model()
        return None
    return app._pick_model()


def cmd_model_ctx(app: VoxApp, argument: str) -> None:
    """Show, raise or drop the context window of the active model."""
    provider = app.provider_block() or {}
    base_url = str(provider.get("base_url", ""))
    model = str(app.config.get("active_model", ""))
    if not ollama.looks_like_ollama(base_url):
        app.write_error(
            "MODEL CTX - only Ollama: every other provider fixes the window"
        )
        return
    if not model:
        app.write_error("MODEL CTX - no active model")
        return
    wanted = argument.strip().lower()
    if wanted in ("off", "reset", "none"):
        if not ollama.is_derived(model):
            app.write_system(f"MODEL CTX - {model} is already the original")
            return
        app.revert_model_ctx(base_url, model)
        return
    if not wanted:
        app.report_model_ctx(base_url, model)
        return
    if not wanted.isdigit():
        app.write_error("MODEL CTX - usage: /model ctx [N|off]")
        return
    app.write_system(f"MODEL CTX - building a {wanted}-token build of {model}...")
    app.build_model_ctx(base_url, model, int(wanted))


def cmd_model_gpu(app: VoxApp, argument: str) -> None:
    """Show or set how much of the active model lives on the GPU."""
    provider = app.provider_block() or {}
    base_url = str(provider.get("base_url", ""))
    model = str(app.config.get("active_model", ""))
    if not ollama.looks_like_ollama(base_url):
        app.write_error(
            "MODEL GPU - only Ollama: every other provider runs on its own hardware"
        )
        return
    if not model:
        app.write_error("MODEL GPU - no active model")
        return
    wanted = argument.strip().lower()
    if wanted in ("off", "auto", "none"):
        app.config.setdefault("model_build", {})["num_gpu"] = None
        app.persist_config()
        app.write_system("MODEL GPU OFF - later builds leave the split to Ollama")
        return
    if not wanted:
        app.report_model_gpu(base_url, model)
        return
    if wanted == "max":
        app.build_model_gpu(base_url, model, None)
        return
    if not wanted.isdigit():
        app.write_error("MODEL GPU - usage: /model gpu [max|N|off]")
        return
    app.build_model_gpu(base_url, model, int(wanted))


def cmd_role(app: VoxApp, argument: str) -> Any:
    if argument:
        app.set_role(argument)
        return None
    app.action_open_roles()
    return None


def cmd_roles(app: VoxApp, argument: str) -> None:
    app.action_open_roles()


def cmd_prompts(app: VoxApp, argument: str) -> None:
    app.action_open_prompts()


def cmd_prompt(app: VoxApp, argument: str) -> None:
    if not argument:
        app.write_error("USAGE: /prompt <name>")
        return
    app.load_prompt(argument)


def cmd_prompt_save(app: VoxApp, argument: str) -> Any:
    content = app.input_area.text.strip()
    if not content:
        app.write_error("NOTHING TO SAVE - THE INPUT IS EMPTY")
        return None
    if argument:
        app.prompt_store.save_prompt(argument, content)
        app.write_system(f"PROMPT SAVED - {argument}")
        return None
    return app._ask_prompt_name(content)


def cmd_prompt_delete(app: VoxApp, argument: str) -> Any:
    if not argument:
        app.write_error("USAGE: /prompt-delete <name>")
        return None
    return app._delete_prompt(argument)


def cmd_sessions(app: VoxApp, argument: str) -> None:
    app.action_open_sessions()


def cmd_session_save(app: VoxApp, argument: str) -> Any:
    if argument:
        app.save_session(argument)
        return None
    app.action_save_session()
    return None


def cmd_session_load(app: VoxApp, argument: str) -> Any:
    if not argument:
        app.action_open_sessions()
        return None
    app.load_session(argument)
    return None


def cmd_session_delete(app: VoxApp, argument: str) -> Any:
    if not argument:
        app.write_error("USAGE: /session-delete <name>")
        return None
    return app._delete_session(argument)


def cmd_agent(app: VoxApp, argument: str) -> None:
    value = argument.strip().lower()
    if value not in ("on", "off"):
        state = "ON" if app.config.get("agent", {}).get("enabled") else "OFF"
        app.write_system(f"AGENT MODE IS {state} - USAGE: /agent on|off")
        return
    app.config.setdefault("agent", {})["enabled"] = value == "on"
    app.persist_config()
    app.write_system(
        f"AGENT MODE {value.upper()} - WORKSPACE {app.workspace_path}"
        if value == "on"
        else "AGENT MODE OFF"
    )
    app.refresh_status()


def cmd_workspace(app: VoxApp, argument: str) -> None:
    if not argument:
        app.write_system(f"WORKSPACE: {app.workspace_path}")
        return
    app.set_workspace(argument)


def cmd_export(app: VoxApp, argument: str) -> None:
    wanted = argument.strip().lower().split()
    if not wanted:
        app.export_report()
        return
    unknown = [fmt for fmt in wanted if fmt not in reporting.FORMATS]
    if unknown:
        app.write_error(
            f"UNKNOWN FORMAT: {', '.join(unknown)} - use {', '.join(reporting.FORMATS)}"
        )
        return
    app.export_report(tuple(wanted))


def cmd_web(app: VoxApp, argument: str) -> None:
    value = argument.strip().lower()
    settings = app.web_settings()
    if value == "start":
        app.ensure_search_server(settings)
        return
    if value in ("stop", "kill"):
        app.search_server.stop()
        app.write_system("SEARCH SERVER - stopped")
        return
    if value == "status":
        where = f"{searchd.HOST}:{app.search_server.port}"
        state = "listening" if app.search_server.running else "not running"
        app.write_system(f"SEARCH SERVER - {state} ({where})")
        return
    if value in ("on", "off"):
        app.config.setdefault("web", {})["enabled"] = value == "on"
        app.persist_config()
        settings = app.web_settings()
        reason = settings.unusable() if value == "on" else None
        app.write_system(f"WEB SEARCH {value.upper()} - {settings.describe()}")
        if reason:
            app.write_error(f"NOT USABLE YET - {reason}")
        return
    if value:
        app.write_error("USAGE: /web [on|off|start|stop|status]")
        return
    state = "ON" if settings.enabled else "OFF"
    lines = [f"WEB SEARCH IS {state} - {settings.describe()}"]
    reason = settings.unusable()
    if reason:
        lines.append(reason)
    lines.append(
        f"pages are {'read on request' if settings.allow_fetch else 'never read'}"
        f" · {settings.max_results} results at a time"
    )
    app.write_system("\n".join(lines))


def cmd_search(app: VoxApp, argument: str) -> None:
    query = argument.strip()
    if not query:
        app.write_error("USAGE: /search <what you are looking for>")
        return
    settings = app.web_settings()
    reason = settings.unusable()
    if reason:
        app.write_error(f"CANNOT SEARCH - {reason}")
        return
    if app.wants_local_server(settings) and not app.ensure_search_server(settings):
        return
    app.write_system(f"SEARCHING {settings.describe()} FOR: {query}")
    app.run_search(query, settings)


def cmd_fetch(app: VoxApp, argument: str) -> None:
    url = argument.strip()
    if not url:
        app.write_error("USAGE: /fetch <url>")
        return
    settings = app.web_settings()
    if not settings.enabled:
        app.write_error("CANNOT FETCH - web access is off - /web on")
        return
    app.write_system(f"READING {url}")
    app.run_fetch(url, settings)


def cmd_consensus(app: VoxApp, argument: str) -> None:
    value = argument.strip().lower()
    settings = app.consensus_settings()
    if value in ("on", "off"):
        app.config.setdefault("consensus", {})["enabled"] = value == "on"
        app.persist_config()
        app.install_answer_hook()
        app.write_system(f"CONSENSUS {value.upper()}")
        return
    if value:
        app.write_error("USAGE: /consensus [on|off]")
        return

    state = "ON" if settings.get("enabled", True) else "OFF"
    refusal = app.consensus_refusal()
    peers = app.mesh.processors(
        str(settings.get("verb", "infer")), int(settings.get("max_peers", 5))
    )
    lines = [
        f"CONSENSUS IS {state} - mark text with {cns.OPEN} … {cns.CLOSE}",
        f"answers other agents: "
        f"{'yes' if settings.get('answer_requests', True) else 'no'}",
    ]
    if refusal:
        lines.append(refusal)
    else:
        named = ", ".join(peer.name or peer.agent_id for peer in peers) or "nobody"
        lines.append(f"would ask: {named}")
    app.write_system("\n".join(lines))


def cmd_mesh(app: VoxApp, argument: str) -> None:
    value = argument.strip().lower()
    if not value:
        state = "ONLINE" if app.mesh.online else "OFFLINE"
        app.write_system(
            f"MESH IS {state} - {app.mesh.agent_id} · {app.mesh.category} "
            f"- /mesh on|off"
        )
        return
    if value in ("new-ca", "newca"):
        app.replace_mesh_ca(demo=False)
        return
    if value in ("sample-ca", "demo-ca"):
        app.replace_mesh_ca(demo=True)
        return
    if value not in ("on", "off"):
        app.write_error("USAGE: /mesh [on|off|new-ca|sample-ca]")
        return
    if (value == "on") == app.mesh.online:
        app.write_system(f"MESH IS ALREADY {value.upper()}")
        return
    app.action_toggle_mesh()


def cmd_universe(app: VoxApp, argument: str) -> None:
    app.action_open_universe()


def cmd_round(app: VoxApp, argument: str) -> None:
    app.action_open_round()


def cmd_stats(app: VoxApp, argument: str) -> None:
    app.write_system(app.usage.report(app.context_window()))


def cmd_code(app: VoxApp, argument: str) -> None:
    """Show the code panel, or copy one block by number."""
    app._panel_mode = "code"
    app.query_one("#side-panel").set_class(True, "visible")
    app.refresh_panel()
    if not argument:
        if app.code_blocks:
            summary = ", ".join(
                block.label(index)
                for index, block in enumerate(app.code_blocks, start=1)
            )
            app.write_system(f"CODE BLOCKS: {summary}  ·  /code <n> to copy")
        else:
            app.write_system("NO CODE BLOCK IN THE LAST ANSWER")
        return
    try:
        number = int(argument.split()[0])
    except ValueError:
        app.write_error("USAGE: /code [number]")
        return
    app.copy_code_block(number)


def cmd_panel(app: VoxApp, argument: str) -> None:
    mode = argument.strip().lower() or (
        "index" if app._panel_mode == "code" else "code"
    )
    if mode not in ("code", "index", "consensus"):
        app.write_error("USAGE: /panel code|index|consensus")
        return
    app._panel_mode = mode
    app.query_one("#side-panel").set_class(True, "visible")
    app.refresh_panel()


def cmd_connect(app: VoxApp, argument: str) -> None:
    app.write_system("PROBING /v1/models…")
    app.check_connection()


def cmd_stop(app: VoxApp, argument: str) -> None:
    app.action_stop()


def cmd_exit(app: VoxApp, argument: str) -> None:
    app.action_request_quit()
