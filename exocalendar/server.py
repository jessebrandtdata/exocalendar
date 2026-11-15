"""HTTP server shell: routing, Basic auth gate, TLS.

Routes: `/dav/*` and `/.well-known/caldav` → CalDAV; `/api/*` → the web
UI's JSON API; `/feed/*` → token-authed read-only ICS feeds (exempt from
Basic auth); everything else → the static web UI.

A server started with no config serves only the first-run form (`setup.py`)
until the account exists, then switches to the routes above in place.
"""

from __future__ import annotations

import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address

from . import __version__
from . import setup as websetup
from .auth import check_basic
from .config import Config
from .dav import DavHandlerLogic
from .store import Store

_MAX_BODY = 20 * 1024 * 1024


def _is_loopback(bind: str) -> bool:
    if bind in ("localhost",):
        return True
    try:
        return ip_address(bind).is_loopback
    except ValueError:
        return False


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "exocalendar/" + __version__

    # one generic dispatcher for every method
    def _handle(self):
        try:
            status, headers, body = self._route()
        except Exception:  # noqa: BLE001 - the server must not die on a request
            import traceback

            traceback.print_exc()
            status, headers, body = 500, {"Content-Type": "text/plain"}, b"internal error"
        self.send_response(status)
        headers.setdefault("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _route(self) -> tuple[int, dict[str, str], bytes]:
        srv: "ExoCalendarServer" = self.server  # type: ignore[assignment]
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            return 413, {"Content-Type": "text/plain"}, b"body too large"
        body = self.rfile.read(length) if length else b""
        path = self.path

        if srv.cfg is None:
            return srv.first_run(self.command, path, body, self.client_address[0], dict(self.headers))

        if path.startswith("/feed/"):
            return srv.webapi.handle_feed(path)

        if not srv.no_auth and not check_basic(
            self.headers.get("Authorization"), srv.cfg
        ):
            return (
                401,
                {
                    "WWW-Authenticate": 'Basic realm="exocalendar"',
                    "Content-Type": "text/plain",
                },
                b"authentication required",
            )

        if path == "/.well-known/caldav" or path == "/dav" or path.startswith("/dav/"):
            return srv.dav.handle(self.command, path, dict(self.headers), body)
        if path == "/api" or path.startswith("/api/"):
            return srv.webapi.handle_api(self.command, path, body)
        return srv.webapi.handle_static(self.command, path)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass  # quiet by default; systemd/journald captures stderr otherwise

    do_GET = do_HEAD = do_PUT = do_DELETE = do_OPTIONS = _handle
    do_PROPFIND = do_PROPPATCH = do_REPORT = do_MKCALENDAR = do_MKCOL = _handle
    do_POST = do_PATCH = _handle


class ExoCalendarServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        cfg: Config | None,
        no_auth: bool = False,
        setup: websetup.SetupTarget | None = None,
    ):
        """`cfg=None` with a `setup` target starts in first-run mode."""
        if cfg is None and setup is None:
            raise ValueError("a server needs a config or a setup target")
        bind = cfg.bind if cfg else setup.bind
        if no_auth and not _is_loopback(bind):
            raise SystemExit(
                f"--no-auth is only allowed on loopback binds, not {bind!r}"
            )
        self.cfg = None
        self.no_auth = no_auth
        self.setup = setup
        self._setup_lock = threading.Lock()
        if cfg:
            self._activate(cfg)
        super().__init__((bind, cfg.port if cfg else setup.port), _Handler)
        if cfg and cfg.tls_cert and cfg.tls_key:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cfg.tls_cert, cfg.tls_key)
            self.socket = ctx.wrap_socket(self.socket, server_side=True)
            self.scheme = "https"
        else:
            self.scheme = "http"


    def _activate(self, cfg: Config) -> None:
        from .webapi import WebApi

        self.store = Store(cfg.data_dir)
        self.dav = DavHandlerLogic(self.store)
        self.webapi = WebApi(self.store)
        self.cfg = cfg  # last: a request sees cfg only once the rest exists

    def first_run(self, method, path, body, client_ip, headers):
        """A request while there is no account yet."""
        if not websetup.client_allowed(client_ip, headers):
            return websetup.REFUSED
        with self._setup_lock:
            if self.cfg is not None:  # another request finished setup first
                return 303, {"Location": "/", "Content-Type": "text/plain"}, b""
            status, out_headers, out_body, cfg = websetup.handle(method, path, body, self.setup)
            if cfg is not None:
                self._activate(cfg)
        return status, dict(out_headers), out_body


def serve(
    cfg: Config | None, no_auth: bool = False, setup: websetup.SetupTarget | None = None
) -> None:
    server = ExoCalendarServer(cfg, no_auth=no_auth, setup=setup)
    host, port = server.server_address[:2]
    print(f"exocalendar serving on {server.scheme}://{host}:{port}/", flush=True)
    if cfg is None:
        print(
            f"No config at {setup.config_path} yet: open the address above in a "
            "browser to create the account, or run `exocalendar setup` here.",
            flush=True,
        )
    bind = cfg.bind if cfg else setup.bind
    if server.scheme == "http" and not _is_loopback(bind):
        print(
            "WARNING: plain HTTP on a non-loopback bind — Basic auth credentials "
            "travel unencrypted. Configure tls_cert/tls_key or a reverse proxy."
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
