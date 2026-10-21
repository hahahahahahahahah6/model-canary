"""OpenAI-compatible chat-completions client (stdlib only).

Measures time-to-first-token via streaming, records token usage when the
provider reports it, and surfaces in-stream errors instead of silently
treating truncated/error payloads as successful responses.
"""
from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.parse


class ProbeError(Exception):
    """The probe request itself failed (network, timeout, HTTP error)."""

    def __init__(self, message: str, http_status: int | None = None, body: str = ""):
        super().__init__(message)
        self.http_status = http_status
        self.body = body


def _split_endpoint(endpoint: str) -> tuple:
    """Split 'https://host:port/base' -> (scheme, host, port, base_path)."""
    parts = urllib.parse.urlsplit(endpoint)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"endpoint must be http(s) URL, got: {endpoint!r}")
    host = parts.hostname or ""
    if parts.scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        # Sending a Bearer key over plaintext HTTP off-localhost.
        print(f"warning: sending API key over plaintext http to {host}",
              file=sys.stderr)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    base = parts.path.rstrip("/")
    return parts.scheme, host, port, base


def chat_once(
    endpoint: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: float = 60.0,
    extra_headers: dict | None = None,
) -> dict:
    """Send one chat-completions request with streaming; return measurements.

    Returns dict with keys: text, ttft_s (first non-empty content delta),
    connect_s, latency_s, output_chars, prompt_tokens, completion_tokens,
    total_tokens, http_status.
    Raises ProbeError on transport/HTTP failure, in-stream errors, truncated
    streams, or empty responses (body preserved on the exception).
    """
    scheme, host, port, base = _split_endpoint(endpoint)
    path = base + "/chat/completions"
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "temperature": 0,
            # Without this, OpenAI-compatible streams omit usage entirely.
            "stream_options": {"include_usage": True},
        }
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "Accept": "text/event-stream",
    }
    if extra_headers:
        headers.update(extra_headers)

    conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(host, port, timeout=timeout)
    start = time.monotonic()
    deadline = start + timeout  # total deadline, not just per-socket-op
    try:
        conn.connect()
        connect_s = time.monotonic() - start
        conn.request("POST", path, body=payload, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        if status != 200:
            body = resp.read().decode("utf-8", "replace")[:4000]
            raise ProbeError(
                f"HTTP {status} from {endpoint}",
                http_status=status,
                body=body,
            )
        # Stream SSE chunks. TTFT = first non-empty content delta (excludes
        # TLS handshake, SSE comments, and role-only deltas).
        ttft = None
        texts: list = []
        usage: dict = {}
        saw_done = False
        finish_reason = None
        buf = b""
        while True:
            if time.monotonic() > deadline:
                raise ProbeError(
                    f"probe exceeded total timeout of {timeout}s (slow-drip server?)")
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    saw_done = True
                    continue
                try:
                    obj = json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                # In-stream errors: many relays return HTTP 200 and then
                # emit data: {"error": {...}}. Must not be read as success.
                if obj.get("error"):
                    raise ProbeError(
                        "provider returned an in-stream error",
                        http_status=status,
                        body=json.dumps(obj)[:2000],
                    )
                choices = obj.get("choices") or []
                if choices:
                    choice = choices[0] or {}
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        if ttft is None:
                            ttft = time.monotonic() - start
                        texts.append(content)
                if "usage" in obj and isinstance(obj["usage"], dict):
                    usage = obj["usage"]
        latency = time.monotonic() - start
        if not saw_done:
            raise ProbeError("incomplete stream: connection closed without [DONE]")
        if finish_reason is not None and finish_reason != "stop":
            raise ProbeError(f"incomplete stream: finish_reason={finish_reason!r}")
        text = "".join(texts)
        if not text:
            raise ProbeError("empty response text")
        return {
            "text": text,
            "ttft_s": round(ttft if ttft is not None else latency, 3),
            "connect_s": round(connect_s, 3),
            "latency_s": round(latency, 3),
            "output_chars": len(text),
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "http_status": status,
        }
    except ProbeError:
        raise
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise ProbeError(f"transport failure: {exc}") from exc
    finally:
        conn.close()
