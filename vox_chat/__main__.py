"""Command line entry point: ``vox`` and ``python -m vox_chat``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config, save_global_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vox",
        description="VOX - retro TUI coding chat for OpenAI-compatible providers.",
    )
    parser.add_argument("--version", action="version", version=f"vox {__version__}")
    parser.add_argument(
        "-w", "--workspace", metavar="PATH",
        help="Workspace directory for the coding-agent tools (default: cwd).",
    )
    parser.add_argument(
        "-p", "--provider", metavar="NAME",
        help="Use this provider for this run.",
    )
    parser.add_argument(
        "-m", "--model", metavar="NAME",
        help="Use this model for this run.",
    )
    parser.add_argument(
        "--no-splash", action="store_true", help="Skip the boot banner."
    )
    parser.add_argument(
        "--save", action="store_true",
        help="Persist --provider / --model to the global config.",
    )

    subparsers = parser.add_subparsers(dest="command")
    doctor = subparsers.add_parser(
        "doctor", help="Run the VOX system check and exit."
    )
    doctor.add_argument("--plain", action="store_true", help="Disable ANSI colours.")
    doctor.add_argument(
        "--timeout", type=float, default=5.0,
        help="Seconds to wait for the provider probe (default: 5).",
    )
    doctor.add_argument("-w", "--workspace", metavar="PATH", default=None)

    subparsers.add_parser("config-path", help="Print the config file path and exit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).expanduser() if args.workspace else Path.cwd()

    if args.command == "doctor":
        from .doctor import main as doctor_main

        return doctor_main(
            workspace=workspace, plain=args.plain, timeout=args.timeout
        )

    if args.command == "config-path":
        from .storage import global_config_path

        print(global_config_path())
        return 0

    loaded = load_config(workspace)
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
    if args.save and (args.provider or args.model):
        try:
            save_global_config(loaded.data)
        except ConfigError as exc:
            print(f"cannot save configuration: {exc}", file=sys.stderr)
            return 2

    from .app import VoxApp

    app = VoxApp(
        loaded=loaded, workspace=workspace, show_splash=not args.no_splash
    )
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
