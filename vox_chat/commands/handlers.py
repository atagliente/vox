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

import json
from typing import TYPE_CHECKING, Any

from .. import consensus as cns
from .. import images, mcp, ollama, sampling, searchd
from .. import report as reporting
from .. import web as web_module
from ..tools import ToolError
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
    if value == "cache":
        app.write_system(f"WEB CACHE - {web_module.CACHE.describe()}")
        return
    if value in ("cache-clear", "uncache"):
        removed = web_module.CACHE.clear()
        web_module.ROBOTS.forget()
        app.write_system(f"WEB CACHE - {removed} entr(y/ies) dropped")
        return
    if value:
        app.write_error("USAGE: /web [on|off|start|stop|status|cache|cache-clear]")
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


def cmd_revoke(app: VoxApp, argument: str) -> None:
    """Refuse a peer from now on, without waiting for its certificate.

    Certificates last a day, which is short enough to be the practical form
    of revocation and far too long to be the only one.
    """
    agent_id = argument.strip()
    if not agent_id:
        refused = sorted(app.mesh.revoked)
        app.write_system(
            "REVOKED - "
            + (", ".join(refused) if refused else "nobody")
            + "\n  /revoke <agent-id>   ·   /revoke allow <agent-id>"
        )
        return
    if agent_id.startswith("allow "):
        app.write_system("MESH - " + app.mesh.unrevoke(agent_id[6:].strip()))
        return
    app.write_system("MESH - " + app.mesh.revoke(agent_id))


def cmd_peers(app: VoxApp, argument: str) -> None:
    """What each peer has done: answered, how fast, how often alone."""
    records = sorted(
        app.reputation.peers.values(), key=lambda r: (-r.asked, r.name or r.agent_id)
    )
    if not records:
        app.write_system("PEERS - no round has been run yet")
        return
    width = max(len(r.name or r.agent_id) for r in records)
    lines = ["PEERS - what each has done, across every round on this machine", ""]
    lines += [
        f"  {(r.name or r.agent_id).ljust(width)}   {r.describe()}" for r in records
    ]
    lines += [
        "",
        "A record, not a judgement: VOX cannot tell which of two disagreeing "
        "peers is right, and does not claim to.",
    ]
    app.write_system("\n".join(lines))


def cmd_universe(app: VoxApp, argument: str) -> None:
    app.action_open_universe()


def cmd_rounds(app: VoxApp, argument: str) -> None:
    """Every consensus round this session, and what each peer said."""
    app.write_system(app.rounds_report())


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


def cmd_mcp(app: VoxApp, argument: str) -> None:
    """Turn MCP on or off, list what is connected, or reconnect.

    Connecting spawns other people's programs and talks to other people's
    servers, so it happens when asked for and not on startup: `mcp.enabled`
    in the configuration, or `/mcp on` here.
    """
    action = argument.strip().lower() or "list"
    settings = mcp.settings_from_config(app.config)

    if action in ("on", "off"):
        app.config.setdefault("mcp", {})["enabled"] = action == "on"
        app.persist_config()
        if action == "off":
            app.mcp.close()
            app.write_system("MCP OFF - the servers are shut down")
            return
        action = "reload"

    if action == "reload":
        if not settings["servers"]:
            app.write_error(
                "MCP - no servers configured. Add them under mcp.servers in "
                "/config: a name, and either a command to run or a url."
            )
            return
        app.connect_mcp()
        return

    if action != "list":
        app.write_error("MCP - usage: /mcp [on|off|list|reload]")
        return

    if not app.mcp.clients and not app.mcp.status:
        state = "on" if settings["enabled"] else "off"
        app.write_system(
            f"MCP {state.upper()} - nothing connected. "
            f"{len(settings['servers'])} server(s) configured; /mcp reload"
        )
        return
    app.write_system(_mcp_report(app))


def _mcp_report(app: VoxApp) -> str:
    """One line per server, then one per tool: what the model can actually see."""
    lines = ["MCP SERVERS", ""]
    for status in app.mcp.status:
        mark = "OK  " if status.connected else "FAIL"
        detail = f"{status.tools} tool(s)" if status.connected else status.error
        lines.append(f"  [{mark}] {status.name}  ·  {status.where}")
        lines.append(f"         {detail}")
    tools = app.mcp.tools()
    if tools:
        lines += ["", "TOOLS THE MODEL CAN CALL", ""]
        width = max(len(tool.qualified) for tool in tools)
        for tool in sorted(tools, key=lambda t: t.qualified):
            note = "read-only" if tool.read_only else "confirmed"
            if tool.destructive:
                note = "destructive, always confirmed"
            lines.append(f"  {tool.qualified.ljust(width)}   {note}")
    lines += [
        "",
        "What a server says about itself is data, not instructions: the tool "
        "descriptions below reach the model as text written by somebody else.",
    ]
    return "\n".join(lines)


def cmd_set(app: VoxApp, argument: str) -> None:
    """Show, set or clear one sampling parameter.

    `/set` alone lists what is actually being sent, which is not the same as
    what could be: a parameter VOX has not been told about is not sent at all,
    so the provider's own default stands.
    """
    role = app.role_store.get(str(app.config.get("active_role", "")))
    parts = argument.split(None, 1)
    if not parts:
        current = sampling.resolve(app.config, role)
        app.write_system(
            "SAMPLING\n"
            f"  in force   {current.describe()}\n"
            f"  settable   {sampling.known()}\n"
            "  /set <name> <value>   ·   /set <name> off   ·   /set preset"
        )
        return

    name = parts[0].strip().lower()
    if name == "preset":
        _write_preset(app, role)
        return

    block = app.config.setdefault("generation", {})
    if len(parts) == 1 or parts[1].strip().lower() in ("off", "default", "clear"):
        if block.pop(name, None) is None and name in sampling.SETTABLE:
            app.write_system(f"SET - {name} was not set; the provider decides")
        else:
            app.persist_config()
            app.write_system(f"SET - {name} cleared; the provider decides again")
        return

    try:
        value = sampling.coerce(name, parts[1].strip())
    except ValueError as exc:
        app.write_error(f"SET - {exc}")
        return
    block[name] = value
    app.persist_config()
    app.write_system(f"SET - {name} = {value}")


def _write_preset(app: VoxApp, role) -> None:
    """Store what is in force now as this model's own preset."""
    model = str(app.config.get("active_model", ""))
    if not model:
        app.write_error("SET PRESET - no active model")
        return
    current = sampling.resolve(app.config, role)
    if not current.values:
        app.write_error("SET PRESET - nothing is set, so there is nothing to store")
        return
    app.config.setdefault("model_presets", {})[model] = dict(current.values)
    app.persist_config()
    app.write_system(
        f"SET PRESET - {model} now carries its own settings:\n  {current.describe()}"
    )


def cmd_format(app: VoxApp, argument: str) -> None:
    """Make the answer match a JSON Schema, or stop making it.

    Held for the session and not saved: a schema belongs to the question being
    asked, not to the installation.
    """
    text = argument.strip()
    if not text or text.lower() in ("off", "none", "clear"):
        app.response_format = None
        app.write_system("FORMAT - off; answers are prose again")
        return
    if text.lower() == "json":
        app.response_format = {"type": "json_object"}
        app.write_system("FORMAT - the answer will be a JSON object")
        return
    try:
        schema = json.loads(text)
    except ValueError as exc:
        app.write_error(
            f"FORMAT - that is not JSON ({exc}). Usage: /format json, "
            "/format {a JSON Schema}, or /format off"
        )
        return
    if not isinstance(schema, dict):
        app.write_error("FORMAT - a schema is a JSON object")
        return
    app.response_format = {
        "type": "json_schema",
        "json_schema": {"name": "vox_answer", "strict": True, "schema": schema},
    }
    app.write_system(
        "FORMAT - the answer will match that schema.\n"
        "  A model that does not support structured output will say so."
    )


def cmd_image(app: VoxApp, argument: str) -> None:
    """Attach an image to the next message, for a model that can read one.

    Attached rather than sent: the picture is half the question and the words
    are the other half, so it waits in the prompt box until there is something
    to ask about it.
    """
    path = argument.strip().strip('"').strip("'")
    if not path or path.lower() in ("off", "clear", "none"):
        count = len(app.pending_images)
        app.pending_images.clear()
        app.write_system(
            f"IMAGE - {count} attachment(s) dropped"
            if count
            else "IMAGE - usage: /image <path>, or /image off"
        )
        return
    try:
        attachment = images.load(path)
    except images.ImageError as exc:
        app.write_error(f"IMAGE - {exc}")
        return
    app.pending_images.append(attachment)
    app.write_system(f"IMAGE ATTACHED - {attachment.describe()}")
    app.show_image(attachment)
    app.check_vision_support()


def cmd_index(app: VoxApp, argument: str) -> None:
    """Build, refresh, drop or query the workspace index.

    Building reads every file in the workspace and asks the embedding model
    for a vector per chunk. That is a real cost on somebody's laptop, so it
    happens when asked for and never on its own.
    """
    action = argument.strip().lower() or "status"
    if action in ("build", "refresh", "update"):
        app.build_index()
        return
    if action in ("off", "drop", "clear"):
        app.drop_index()
        return
    if action in ("on", "use"):
        app.config.setdefault("index", {})["enabled"] = True
        app.persist_config()
        app.write_system("INDEX ON - relevant files go in front of each question")
        return
    if action == "status":
        app.write_system(app.index_report())
        return
    app.search_index(argument.strip())


def cmd_undo(app: VoxApp, argument: str) -> None:
    """Take back the last write the agent was authorised to make.

    One step. Every write was confirmed before it happened, and this is for
    the case where the confirmation was a judgement made from a diff that
    looked right and was not.
    """
    if not app.undo.available:
        app.write_system(app.undo.describe())
        return
    try:
        app.write_system(app.undo.undo())
    except ToolError as exc:
        app.write_error(f"UNDO - {exc}")


def cmd_plan(app: VoxApp, argument: str) -> None:
    """Show the plan the model wrote for itself.

    It is also written into the transcript as the model updates it; this is
    for looking at it again without scrolling back.
    """
    app.write_system("PLAN\n\n" + app.todos.render())
