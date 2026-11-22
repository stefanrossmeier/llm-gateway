"""Service entry point; version/argument checks happen before credential loading."""

import argparse
import json
import os
import re
import sys
from pathlib import Path


def require_service_python(version: tuple[int, ...] | None = None) -> None:
    if (version or sys.version_info)[:2] != (3, 12):
        raise RuntimeError("service_python_required: use Python 3.12 for the service")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="llm-gateway")
    parser.add_argument("--version", action="store_true")
    commands = parser.add_subparsers(dest="command")
    config = commands.add_parser("config")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    validate = config_commands.add_parser("validate")
    validate.add_argument("--project-config", type=Path, required=True)
    token = commands.add_parser("token")
    token_commands = token.add_subparsers(dest="token_command", required=True)
    create = token_commands.add_parser("create")
    create.add_argument("--file", type=Path, required=True)
    create.add_argument("--rotate", action="store_true")
    serve_command = commands.add_parser("serve")
    serve_command.add_argument("--project-config", type=Path, action="append", required=True)
    serve_command.add_argument("--state-dir", type=Path, required=True)
    serve_command.add_argument("--host", default="127.0.0.1")
    serve_command.add_argument("--port", type=int, default=8000)
    serve_command.add_argument("--unix-socket", type=Path)
    serve_command.add_argument("--ipc-project")
    serve_command.add_argument("--egress-proxy")
    serve_command.add_argument("--max-concurrency", type=int, default=8)
    serve_command.add_argument("--max-queue", type=int, default=256)
    serve_command.add_argument("--max-body-bytes", type=int, default=1024 * 1024)
    state = commands.add_parser("state")
    state_commands = state.add_subparsers(dest="state_command", required=True)
    backup = state_commands.add_parser("backup")
    backup.add_argument("--state-dir", type=Path, required=True)
    backup.add_argument("--output", type=Path, required=True)
    restore = state_commands.add_parser("restore")
    restore.add_argument("--state-dir", type=Path, required=True)
    restore.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.version:
        from llm_gateway import __version__

        print(__version__)
        return 0
    try:
        require_service_python()
    except RuntimeError as error:
        parser.exit(2, str(error) + "\n")
    if args.command in {"serve", "token", "state"} and os.name != "posix":
        print(json.dumps({"status": "error", "code": "service_requires_posix"}), file=sys.stderr)
        return 2
    if args.command == "serve":
        try:
            from llm_gateway.service.server import serve
        except ModuleNotFoundError:
            print(
                json.dumps({"status": "error", "code": "service_extra_required"}), file=sys.stderr
            )
            return 2

        try:
            serve(
                project_paths=args.project_config,
                state_root=args.state_dir,
                host=args.host,
                port=args.port,
                unix_socket=args.unix_socket,
                ipc_project=args.ipc_project,
                deployment_proxy=args.egress_proxy,
                max_concurrency=args.max_concurrency,
                max_queue=args.max_queue,
                max_body_bytes=args.max_body_bytes,
            )
        except (ValueError, OSError) as error:
            code = getattr(error, "code", None) or str(error)
            if not re.fullmatch(r"[a-z_]{1,64}", code):
                code = "service_start_failed"
            print(json.dumps({"status": "error", "code": code}), file=sys.stderr)
            return 2
        return 0
    if args.command == "state":
        import sqlite3

        from llm_gateway.storage.backup import backup, restore

        try:
            result = (
                backup(args.state_dir, args.output)
                if args.state_command == "backup"
                else restore(args.input, args.state_dir)
            )
        except (ValueError, OSError, sqlite3.Error):
            print(
                json.dumps({"status": "error", "code": "state_operation_failed"}), file=sys.stderr
            )
            return 2
        print(json.dumps(result))
        return 0
    if args.command == "token":
        from llm_gateway.credentials import provision_gateway_token

        try:
            provision_gateway_token(args.file, rotate=args.rotate)
        except (ValueError, OSError):
            print(
                json.dumps({"status": "error", "code": "token_provision_failed"}), file=sys.stderr
            )
            return 2
        print(json.dumps({"status": "ok", "rotated": args.rotate}))
        return 0
    if args.command == "config" and args.config_command == "validate":
        from llm_gateway.config import ConfigError, load_project_config

        try:
            snapshot = load_project_config(args.project_config)
        except ConfigError as error:
            print(
                json.dumps({"status": "error", "code": error.code, "locations": error.locations}),
                file=sys.stderr,
            )
            return 2
        print(
            json.dumps(
                {
                    "status": "ok",
                    "project_id": snapshot.project_id,
                    "config_digest": snapshot.digest,
                    "alias_count": len(snapshot.config.aliases),
                }
            )
        )
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
