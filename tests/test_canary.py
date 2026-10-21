"""Tests for model-canary using a fake OpenAI-compatible SSE server (stdlib only)."""
from __future__ import annotations

import http.server
import json
import os
import socket
import tempfile
import threading
import unittest

from model_canary import cli, detect, ledger, provider

SVG_A = "<svg><circle cx='10'/><text>pelican</text></svg>"
SVG_B = ("<svg viewBox='0 0 200 200'><g transform='rotate(15)'>"
         "<path d='M0,0 C50,80 120,20 190,90' stroke='black' fill='none'/>"
         "<ellipse cx='100' cy='150' rx='40' ry='12'/></g></svg>")
# ~2.9k chars: under the old code this ALWAYS false-positived (C1) because
# the baseline sample was truncated to 500 chars while runs kept 4000.
LONG_SVG = ("<svg viewBox='0 0 400 400'>"
            + "".join(f"<path d='M{i},{i} L{i + 10},{i + 10}'/>" for i in range(120))
            + "</svg>")
assert len(LONG_SVG) > 2000

CANDY_RE = r"^\s*2\s*\.?$"


def sse_stream(text: str, finish_reason: str | None = None) -> bytes:
    chunks = [text[i:i + 12] for i in range(0, len(text), 12)] or [""]
    out = b""
    for c in chunks:
        payload = json.dumps({"choices": [{"delta": {"content": c}}]}).encode()
        out += b"data: " + payload + b"\n\n"
    if finish_reason:
        out += json.dumps(
            {"choices": [{"delta": {}, "finish_reason": finish_reason}]}).encode()
        out = b"data: " + out + b"\n\n"
    out += b'data: {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}\n\n'
    out += b"data: [DONE]\n\n"
    return out


class FakeProvider(http.server.BaseHTTPRequestHandler):
    mode = "ok"  # ok | swapped | degraded | capacity | dead | instream |
                 # truncated | empty | longsvg

    def log_message(self, *a):
        pass

    def _send(self, status: int, body: bytes, ctype: str = "text/event-stream"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            prompt = json.loads(body)["messages"][0]["content"]
        except Exception:
            prompt = ""
        mode = FakeProvider.mode
        if mode == "dead":
            self._send(500, b"upstream exploded", "text/plain")
            return
        if mode == "capacity":
            self._send(429, b'{"error": {"code": "rate_limit_exceeded", '
                            b'"message": "Selected model is at capacity. '
                            b'Please try a different model."}}',
                       "application/json")
            return
        if mode == "instream":
            data = (b'data: {"error": {"code": "internal", '
                    b'"message": "backend exploded mid-stream"}}\n\n'
                    b"data: [DONE]\n\n")
            self._send(200, data)
            return
        if mode == "truncated":
            # Chunks, but the connection just ends: no [DONE].
            data = b"".join(
                b"data: " + json.dumps(
                    {"choices": [{"delta": {"content": c}}]}).encode() + b"\n\n"
                for c in ["hel", "lo"])
            self._send(200, data)
            return
        if mode == "empty":
            data = (b'data: {"choices": [{"delta": {"role": "assistant"}}]}\n\n'
                    b'data: {"usage": {"prompt_tokens": 10, "completion_tokens": 0, '
                    b'"total_tokens": 10}}\n\n'
                    b"data: [DONE]\n\n")
            self._send(200, data)
            return
        if "pelican" in prompt:
            if mode == "longsvg":
                text = LONG_SVG
            else:
                text = SVG_B if mode == "swapped" else SVG_A
        elif "candies" in prompt:
            text = "seven" if mode == "degraded" else "2"
        elif "blue" in prompt:
            text = "blue, blue, blue, blue" if mode == "degraded" \
                else "blue, blue, blue, blue, blue"
        elif "leap" in prompt:
            text = "365" if mode == "degraded" else "366"
        else:
            text = "Some Name"
        self._send(200, sse_stream(text))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _ok_run(pid="candy-arithmetic", text="2", **kw):
    rec = {"probe_id": pid, "kind": "reasoning", "status": "ok",
           "model": "m", "endpoint": "e", "prompt_sha": "s",
           "text": text, "ttft_s": 0.5, "output_chars": len(text),
           "expected_regex": CANDY_RE, "ts": "2026-10-21T00:00:00Z"}
    rec.update(kw)
    return rec


class CanaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.server = http.server.HTTPServer(("127.0.0.1", cls.port), FakeProvider)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.endpoint = f"http://127.0.0.1:{cls.port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.d = os.path.join(self.tmp, ".model-canary")
        os.environ["MODEL_CANARY_API_KEY"] = "test-key"
        FakeProvider.mode = "ok"

    def _init(self):
        rc = cli.main(["--dir", self.d, "init", "--endpoint", self.endpoint,
                       "--model", "test-model"])
        self.assertEqual(rc, 0)

    def _run(self, *extra):
        return cli.main(["--dir", self.d, "run", *extra])

    def _check(self, *extra):
        return cli.main(["--dir", self.d, "check", *extra])

    # --- original suite, updated for the new design -----------------------
    def test_01_init_and_baseline_run(self):
        self._init()
        self.assertEqual(self._run(), 0)
        runs = ledger.read_runs(os.path.join(self.d, "ledger.jsonl"))
        self.assertEqual(len(runs), 4 * 5)  # 4 probes x 5 samples
        self.assertTrue(all(r["status"] == "ok" for r in runs))
        base = ledger.load_baseline(os.path.join(self.d, "baseline.json"))
        self.assertIsNotNone(base)
        self.assertEqual(set(base), {"identity-svg", "candy-arithmetic",
                                    "instruction-repeat", "factual-anchor"})
        self.assertTrue(all(v["n"] == 5 for v in base.values()))

    def test_02_identical_run_is_clean(self):
        self._init()
        self._run()
        self._run()
        self.assertEqual(self._check(), 0)

    def test_03_swapped_model_triggers_fingerprint_drift(self):
        self._init()
        self._run()
        FakeProvider.mode = "swapped"
        self._run("--probe", "identity-svg")
        self.assertEqual(self._check(), 2)

    def test_04_degraded_reasoning_triggers_quality_drift(self):
        self._init()
        self._run()
        FakeProvider.mode = "degraded"
        self._run("--probe", "candy-arithmetic")
        self.assertEqual(self._check(), 2)

    def test_05_capacity_429_is_throttled_not_degraded(self):
        self._init()
        self._run()
        FakeProvider.mode = "capacity"
        self._run("--probe", "candy-arithmetic")
        # 429 -> throttled (exit 4), never misreported as degraded (exit 2)
        self.assertEqual(self._check(), 4)
        runs = ledger.read_runs(os.path.join(self.d, "ledger.jsonl"))
        failed = [r for r in runs if r["status"] == "probe_failed"]
        self.assertTrue(failed)
        self.assertEqual(failed[-1].get("http_status"), 429)

    def test_06_transport_failure_is_not_degradation(self):
        self._init()
        self._run()
        FakeProvider.mode = "dead"
        self._run("--probe", "candy-arithmetic")
        # probe_failed -> exit 3, never misreported as degraded (exit 2)
        self.assertEqual(self._check(), 3)

    def test_07_report_renders(self):
        self._init()
        self._run()
        out = os.path.join(self.tmp, "report.md")
        rc = cli.main(["--dir", self.d, "report", "--output", out, "--last", "20"])
        self.assertEqual(rc, 0)
        with open(out, encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("model-canary report", content)
        self.assertIn("identity-svg", content)

    def test_08_provider_unit(self):
        m = provider.chat_once(self.endpoint, "k", "m", "hello pelican")
        self.assertIn("<svg>", m["text"])
        self.assertEqual(m["total_tokens"], 30)
        self.assertGreaterEqual(m["ttft_s"], 0)
        self.assertGreaterEqual(m["connect_s"], 0)

    def test_09_detect_unit(self):
        base = {"identity-svg": {
            "model": "m", "endpoint": "e", "prompt_sha": "s",
            "fingerprint": "x", "sample_text": SVG_A, "sim_threshold": 0.55,
            "ttft_median": 0.5, "len_median": len(SVG_A), "n": 5,
            "tok_ratio_median": None}}
        run = {"probe_id": "identity-svg", "kind": "identity", "status": "ok",
               "model": "m", "endpoint": "e", "prompt_sha": "s",
               "text": SVG_A, "ttft_s": 0.5, "output_chars": len(SVG_A)}
        self.assertEqual(detect.detect(run, base), [])
        run2 = dict(run, text=SVG_B)
        findings = detect.detect(run2, base)
        self.assertTrue(any(f["kind"] == "fingerprint_drift" and f["severity"] == "high"
                            for f in findings))

    # --- new regression tests for the 2026-10-21 review -------------------
    def test_10_long_svg_no_false_drift(self):
        """C1: identical long outputs must not drift (old truncation bug)."""
        self._init()
        FakeProvider.mode = "longsvg"
        self._run("--probe", "identity-svg")
        self._run("--probe", "identity-svg")
        self.assertEqual(self._check(), 0)

    def test_11_instream_error_is_probe_failed(self):
        """C3: HTTP-200 in-stream errors are failures, not degradation."""
        self._init()
        self._run()
        FakeProvider.mode = "instream"
        self._run("--probe", "candy-arithmetic")
        self.assertEqual(self._check(), 3)

    def test_12_truncated_stream_is_probe_failed(self):
        """C3: streams closed without [DONE] are failures."""
        self._init()
        self._run()
        FakeProvider.mode = "truncated"
        self._run("--probe", "candy-arithmetic")
        self.assertEqual(self._check(), 3)

    def test_13_empty_response_is_probe_failed(self):
        """C3: empty successful responses are failures, not 'ok'."""
        self._init()
        self._run()
        FakeProvider.mode = "empty"
        self._run("--probe", "candy-arithmetic")
        self.assertEqual(self._check(), 3)

    def test_14_concurrent_appends_stay_valid(self):
        """H5: overlapping writers must not interleave partial lines."""
        path = os.path.join(self.tmp, "ledger.jsonl")
        def writer(n):
            for i in range(25):
                ledger.append_run(path, {"probe_id": "p", "i": n * 25 + i})
        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        runs = ledger.read_runs(path)
        self.assertEqual(len(runs), 200)
        self.assertEqual(len({r["i"] for r in runs}), 200)

    def test_15_baseline_two_pass_excludes_poisoned_runs(self):
        """H4: runs that already show drift must not poison the baseline."""
        ok = [_ok_run() for _ in range(6)]
        bad = [_ok_run(text="seven") for _ in range(2)]
        base = ledger.build_baseline(ok + bad)
        self.assertEqual(base["candy-arithmetic"]["n"], 6)

    def test_16_regex_rejects_lookalikes(self):
        """M5: '12' and six blues must fail their probes."""
        bad_candy = _ok_run(text="12")
        bad_blue = _ok_run(pid="instruction-repeat", kind="instruction",
                           text="blue, blue, blue, blue, blue, blue",
                           expected_regex=r"(?i)^\s*blue(\s*,\s*blue){4}\s*\.?\s*$")
        for run in (bad_candy, bad_blue):
            findings = detect.detect(run, None)
            self.assertTrue(any(f["kind"] == "quality_drift" for f in findings),
                            run["text"])

    def test_17_single_flaky_low_does_not_page(self):
        """H1: one latency spike in the window must not exit 2."""
        from model_canary import probes as _probes
        self._init()
        self._run()
        candy_sha = ledger._prompt_sha(_probes.get_probe("candy-arithmetic").prompt)
        ledger.append_run(
            os.path.join(self.d, "ledger.jsonl"),
            _ok_run(model="test-model", endpoint=self.endpoint,
                    prompt_sha=candy_sha, ttft_s=9.9))
        # Single LOW in a 6-record window: unconfirmed -> rc 0, not 2.
        self.assertEqual(self._check(), 0)

    def test_18_model_change_reports_stale_not_drift(self):
        """H4: a model switch reports baseline_stale, not drift."""
        self._init()
        self._run()
        rec = _ok_run(model="other-model", endpoint=self.endpoint,
                      prompt_sha=ledger._prompt_sha("x"), text=SVG_B,
                      pid="identity-svg", kind="identity")
        ledger.append_run(os.path.join(self.d, "ledger.jsonl"), rec)
        # Only one stale record in a 6-record window: unconfirmed, rc 0.
        self.assertEqual(self._check(), 0)
        findings = detect.detect(rec, ledger.load_baseline(
            os.path.join(self.d, "baseline.json")))
        self.assertTrue(any(f["kind"] == "baseline_stale" for f in findings))

    def test_19_corrupt_baseline_raises(self):
        """H5: a corrupt baseline must raise, not silently rebuild."""
        path = os.path.join(self.tmp, "baseline.json")
        with open(path, "w") as fh:
            fh.write("{not json")
        with self.assertRaises(ValueError):
            ledger.load_baseline(path)

    def test_20_baseline_needs_quorum(self):
        """H4: fewer than 5 samples -> still baselining, no baseline file."""
        runs = [_ok_run() for _ in range(3)]
        self.assertEqual(ledger.build_baseline(runs), {})

    def test_21_run_lock_serializes(self):
        """H5: a second concurrent run is refused."""
        with ledger.run_lock(self.d):
            with self.assertRaises(RuntimeError):
                with ledger.run_lock(self.d):
                    pass


if __name__ == "__main__":
    unittest.main()
