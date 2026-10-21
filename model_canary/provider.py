"""OpenAI-compatible chat-completions client (stdlib only).

Measures time-to-first-token via streaming, records token usage when the
provider reports it, and captures error bodies so degradation/throttle
signals (e.g. capacity errors, sharing-violation 403s) survive into the ledger.
"""
from __future__ import annotations

import http.client
import json
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

    Returns dict with keys: text, ttft_s, latency_s, output_chars,
    prompt_tokens, completion_tokens, total_tokens, http_status.
    Raises ProbeError on transport/HTTP failure (body preserved).
    """
    scheme, host, port, base = _split_endpoint(endpoint)
    path = base + "/chat/completions"
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "temperature": 0,
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
    try:
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
        # Stream SSE chunks; TTFT = time until first content-bearing chunk.
        ttft = None
        texts: list = []
        usage: dict = {}
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            if ttft is None:
                # peek: any data at all counts as first byte for TTFT purposes
                ttft = time.monotonic() - start
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    continue
                try:
                    obj = json.loads(data)
                except (json.JSONDecodeError, ValueError):
                    continue
                choices = obj.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        texts.append(content)
                if "usage" in obj and isinstance(obj["usage"], dict):
                    usage = obj["usage"]
        latency = time.monotonic() - start
        text = "".join(texts)
        return {
            "text": text,
            "ttft_s": round(ttft if ttft is not None else latency, 3),
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
