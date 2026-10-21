"""Canary Cloud backend: a keyless public API.

INVARIANT (see docs/DESIGN.md §2): this server has NO endpoint that accepts,
stores, or forwards API keys. Request bodies are schema-validated, and any
key-like field (api_key, apikey, authorization, bearer, secret, token) in a
JSON body is rejected with 400 -- loudly, not silently.

Endpoints:
  GET  /api/health
  GET  /api/question-templates
  GET  /api/style-profiles
  POST /api/verdict            {"baseline": {...}, "runs": [...]} -> verdict
  GET  /                       serves the frontend (dev convenience)

Stdlib only. Run:  python3 app.py  (serves on 127.0.0.1:8077 by default)
"""
from __future__ import annotations

import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import question_bank
import style_profiles
import verdict

VERSION = "0.1.0-scaffold"

# Any of these top-level keys in a JSON body => 400. We would rather loudly
# refuse a misbehaving client than silently swallow a secret.
KEYLIKE_FIELDS = frozenset({
    "api_key", "apikey", "api-key", "authorization", "bearer",
    "secret", "secret_key", "token", "access_token", "x-api-key",
})

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "frontend")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
}


def _rejects_keylike(obj: object) -> str | None:
    """Return the offending field name, or None if clean."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in KEYLIKE_FIELDS:
                return str(k)
            hit = _rejects_keylike(v)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = _rejects_keylike(v)
            if hit:
                return hit
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "CanaryCloud/" + VERSION

    def _send(self, code: int, obj: object) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> object:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            raise ValueError("bad content length")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # -- GET ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 (http.server convention)
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/health":
            self._send(200, {"ok": True, "version": VERSION,
                             "keyless": True})
        elif path == "/api/question-templates":
            self._send(200, {"templates": question_bank.get_templates()})
        elif path == "/api/style-profiles":
            self._send(200, {"features": list(style_profiles.FEATURE_NAMES),
                             "profiles": style_profiles.get_profiles()})
        else:
            self._serve_frontend(path)

    def _serve_frontend(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        # Path traversal guard: stay inside FRONTEND_DIR.
        full = os.path.normpath(os.path.join(FRONTEND_DIR, rel))
        if not full.startswith(os.path.normpath(FRONTEND_DIR) + os.sep):
            self._send(403, {"error": "forbidden"})
            return
        if not os.path.isfile(full):
            self._send(404, {"error": "not found"})
            return
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        # No third-party scripts, ever (BYO invariant support).
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self'; "
                         "connect-src 'self' https:; img-src 'self' data:")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- POST -----------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 (http.server convention)
        path = urllib.parse.urlparse(self.path).path
        if path != "/api/verdict":
            self._send(404, {"error": "not found"})
            return
        try:
            body = self._read_json()
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "invalid JSON body"})
            return
        hit = _rejects_keylike(body)
        if hit:
            # Fail closed and LOUD: this server must never see a secret.
            self._send(400, {
                "error": "key-like field rejected",
                "field": hit,
                "hint": "Canary Cloud is keyless: keys stay in your browser. "
                        "See docs/DESIGN.md §2.",
            })
            return
        if not isinstance(body, dict) or not isinstance(
                body.get("baseline"), dict) or not isinstance(
                body.get("runs"), list):
            self._send(400, {
                "error": "body must be {baseline: {...}, runs: [...]}",
            })
            return
        try:
            result = verdict.verdict_for(body["baseline"], body["runs"])
        except Exception as exc:  # never leak tracebacks; keep it a 400
            self._send(400, {"error": f"verdict failed: {exc}"})
            return
        self._send(200, result)

    def log_message(self, *args: object) -> None:  # quieter logs
        pass


def run(host: str = "127.0.0.1", port: int = 8077) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"canary-cloud backend {VERSION} on http://{host}:{port} (keyless)")
    server.serve_forever()


if __name__ == "__main__":
    run()
