"""Command-line interface: serve, setup, passwd, import, export."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import __version__
from .config import Config, default_config_path, interactive_setup, load, load_or_setup, save


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="exocalendar",
        description="Self-hostable CalDAV calendar server with a web UI.",
    )
    parser.add_argument("--version", action="version", version=f"exocalendar {__version__}")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"config file (default: {default_config_path()})",
    )
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="run the server")
    p_serve.add_argument("--bind", help="override bind address")
    p_serve.add_argument("--port", type=int, help="override port")
    p_serve.add_argument(
        "--no-auth",
        action="store_true",
        help="disable authentication (loopback binds only)",
    )

    sub.add_parser("setup", help="write a config file interactively")
    sub.add_parser("passwd", help="change the password")

    p_import = sub.add_parser("import", help="import an .ics file")
    p_import.add_argument("file", type=Path)
    p_import.add_argument("--calendar", required=True, help="target calendar id")

    p_export = sub.add_parser("export", help="write a calendar as .ics to stdout")
    p_export.add_argument("calendar", help="calendar id")

    args = parser.parse_args(argv)
    config_path = args.config or default_config_path()

    if args.command in ("passwd", "import", "export") and not config_path.is_file():
        print(
            f"No config at {config_path} — run `exocalendar setup` first.",
            file=sys.stderr,
        )
        return 1

    if args.command in (None, "serve"):
        from .server import serve

        cfg = load_or_setup(config_path)
        if getattr(args, "bind", None):
            cfg = Config(**{**cfg.__dict__, "bind": args.bind})
        if getattr(args, "port", None):
            cfg = Config(**{**cfg.__dict__, "port": args.port})
        serve(cfg, no_auth=getattr(args, "no_auth", False))
        return 0

    if args.command == "setup":
        interactive_setup(config_path)
        return 0

    if args.command == "passwd":
        from .auth import hash_password

        cfg = load(config_path)
        pw = getpass.getpass("New password: ")
        if not pw or getpass.getpass("Repeat password: ") != pw:
            print("Passwords empty or mismatched; nothing changed.", file=sys.stderr)
            return 1
        save(config_path, Config(**{**cfg.__dict__, "password_hash": hash_password(pw)}))
        print(f"Password updated in {config_path}.")
        return 0

    if args.command == "import":
        from .store import Store
        from .webapi import WebApi

        cfg = load(config_path)
        api = WebApi(Store(cfg.data_dir))
        status, _headers, body = api.handle_api(
            "POST", f"/api/import?calendar={args.calendar}", args.file.read_bytes()
        )
        print(body.decode())
        return 0 if status == 200 else 1

    if args.command == "export":
        from .store import Store
        from .webapi import WebApi

        cfg = load(config_path)
        api = WebApi(Store(cfg.data_dir))
        try:
            sys.stdout.write(api._export(args.calendar))
        except Exception as exc:  # noqa: BLE001
            print(f"export failed: {exc}", file=sys.stderr)
            return 1
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
