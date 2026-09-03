"""Command line entry point: ``vox`` and ``python -m vox_chat``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config, save_global_config

SHUTDOWN_GRACE_SECONDS = 1.5


def _stuck_threads() -> list[threading.Thread]:
    """Non-daemon threads that would hold the interpreter open."""
    return [
        thread
        for thread in threading.enumerate()
        if thread is not threading.main_thread()
        and not thread.daemon
        and thread.is_alive()
    ]


def leave(code: int = 0, grace_seconds: float = SHUTDOWN_GRACE_SECONDS) -> int:
    """Return once the screen is gone, forcing the point if a worker is stuck.

    Textual runs thread workers on a pool whose threads are joined when the
    interpreter exits, so one blocked on a provider that never answers keeps
    the process alive long after the TUI has torn down — the terminal comes
    back but the shell does not. Everything of ours is already on disk by
    then (configuration, sessions and history are written as they change), so
    after a short grace period it is better to leave than to hang.
    """
    deadline = time.monotonic() + grace_seconds
    while _stuck_threads() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not _stuck_threads():
        return code
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vox",
        description="VOX - retro TUI coding chat for OpenAI-compatible providers.",
    )
    parser.add_argument("--version", action="version", version=f"vox {__version__}")
    parser.add_argument(
        "-w",
        "--workspace",
        metavar="PATH",
        help="Workspace directory for the coding-agent tools (default: cwd).",
    )
    parser.add_argument(
        "-p",
        "--provider",
        metavar="NAME",
        help="Use this provider for this run.",
    )
    parser.add_argument(
        "-m",
        "--model",
        metavar="NAME",
        help="Use this model for this run.",
    )
    parser.add_argument(
        "-a",
        "--ask",
        metavar="QUESTION",
        help=(
            "Answer one question on stdout and exit. No screen, no agent "
            "tools, no mesh: for pipes and scripts."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reopen the session that was last saved in this workspace.",
    )
    parser.add_argument(
        "--no-splash", action="store_true", help="Skip the boot banner."
    )
    parser.add_argument(
        "--screen-reader",
        action="store_true",
        help=(
            "Quiet mode: no spinner, no splash, and a status line that "
            "changes once a second instead of ten times."
        ),
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Persist --provider / --model to the global config.",
    )

    subparsers = parser.add_subparsers(dest="command")
    doctor = subparsers.add_parser("doctor", help="Run the VOX system check and exit.")
    doctor.add_argument("--plain", action="store_true", help="Disable ANSI colours.")
    doctor.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Seconds to wait for the provider probe (default: 5).",
    )
    doctor.add_argument("-w", "--workspace", metavar="PATH", default=None)

    subparsers.add_parser("config-path", help="Print the config file path and exit.")

    ui = subparsers.add_parser(
        "ui",
        help="Serve VOX as a page on 127.0.0.1 and open it.",
        description=(
            "The same VOX in a browser: one conversation, streamed, with the "
            "slash commands and the agent's confirmations. Bound to "
            "127.0.0.1 and nothing else - from another device, use a tunnel."
        ),
    )
    ui.add_argument("-w", "--workspace", metavar="PATH", default=None)
    ui.add_argument(
        "--port", type=int, default=8899, help="Port to listen on (default: 8899)."
    )
    ui.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the address instead of opening it.",
    )

    serve = subparsers.add_parser(
        "mcp-serve",
        help="Speak MCP on stdin/stdout, offering VOX's workspace tools.",
        description=(
            "Offer this workspace's tools to an MCP client over stdio. "
            "Read-only unless you say otherwise: there is no operator on "
            "this side to confirm a write, so the confirmation is this "
            "command line."
        ),
    )
    serve.add_argument("-w", "--workspace", metavar="PATH", default=None)
    serve.add_argument(
        "--allow-write",
        action="store_true",
        help="Also offer write_file, edit_file and apply_patch.",
    )
    serve.add_argument(
        "--allow-run",
        action="store_true",
        help=(
            "Also offer run_command. The agent.commands deny list from the "
            "configuration still applies."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).expanduser() if args.workspace else Path.cwd()

    if args.command == "doctor":
        from .doctor import main as doctor_main

        return doctor_main(workspace=workspace, plain=args.plain, timeout=args.timeout)

    if args.command == "ui":
        from .webui import WebHost
        from .webui import serve as serve_ui

        try:
            loaded_ui = load_config(workspace)
        except ConfigError as exc:
            print(f"vox: {exc}; run vox doctor", file=sys.stderr)
            return 2
        host = WebHost(loaded_ui, workspace)
        try:
            return serve_ui(host, args.port, open_browser=not args.no_browser)
        except OSError as exc:
            print(f"vox: {exc}", file=sys.stderr)
            host.close()
            return 2

    if args.command == "mcp-serve":
        from .config import load_config as _load
        from .mcp.server import Server, build_offer

        # Nothing may be printed on stdout but JSON-RPC: that stream is the
        # protocol. Logging already goes to the VOX log file, and argparse
        # errors have gone to stderr.
        offer = build_offer(
            workspace,
            allow_write=args.allow_write,
            allow_run=args.allow_run,
            agent_config=_load(workspace).data.get("agent", {}),
        )
        return Server(offer).serve()

    if args.command == "config-path":
        from .storage import global_config_path

        print(global_config_path())
        return 0

    loaded = load_config(workspace)
    if args.screen_reader:
        # A flag is a request for this run, so it goes in the loaded
        # configuration rather than being saved anywhere.
        loaded.data.setdefault("ui", {})["screen_reader"] = True
    if args.provider:
        if args.provider not in loaded.data.get("providers", {}):
            available = ", ".join(loaded.data.get("providers", {}))
            print(
                f"unknown provider: {args.provider} (available: {available})",
                file=sys.stderr,
            )
            return 2
        loaded.data["active_provider"] = args.provider
    if args.model:
        loaded.data["active_model"] = args.model
    if args.ask:
        from .oneshot import ask

        return ask(args.ask, loaded, workspace)

    if args.save and (args.provider or args.model):
        try:
            save_global_config(loaded.data)
        except ConfigError as exc:
            print(f"cannot save configuration: {exc}", file=sys.stderr)
            return 2

    from .app import VoxApp

    app = VoxApp(loaded=loaded, workspace=workspace, show_splash=not args.no_splash)
    if args.resume:
        # Resolved before the screen exists, so a missing session is a line on
        # stderr and a normal start rather than an error inside the TUI.
        sessions = app.session_store.list()
        if sessions:
            app.resume_on_start = sessions[0].name
        else:
            print("vox: no saved session in this workspace", file=sys.stderr)
    app.run()
    return leave(0)


if __name__ == "__main__":
    raise SystemExit(main())
