"""JSON API, ICS feeds, and static file serving for the web UI."""

from __future__ import annotations

from .store import Store


class WebApi:
    def __init__(self, store: Store):
        self.store = store

    def handle_api(self, method: str, path: str, body: bytes):
        return 404, {"Content-Type": "application/json"}, b'{"error": "not found"}'

    def handle_feed(self, path: str):
        return 404, {"Content-Type": "text/plain"}, b"not found"

    def handle_static(self, method: str, path: str):
        return 404, {"Content-Type": "text/plain"}, b"not found"
