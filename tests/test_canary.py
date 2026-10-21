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


def sse_stream(text: str) -> bytes:
    chunks = [text[i:i + 12] for i in range(0, len(text), 12)] or [""]
    out = b""
    for c in chunks:
        payload = json.dumps({"choices": [{"delta": {"content": c}}]}).encode()
        out += b"data: " + payload + b"\n\n"
    out += b'data: {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}\n\n'
    out += b"data: [DONE]\n\n"
    return out


class FakeProvider(http.server.BaseHTTPRequestHandler):
    mode = "ok"  # ok | swapped | degraded | capacity | dead

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            prompt = json.loads(body)["messages"][0]["content"]
        except Exception:
            prompt = ""
        mode = FakeProvider.mode
        if mode == "dead":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"upstream exploded")
            return
        if mode == "capacity":
            self.send_response(429)
            self.end_headers()
            self.wfile.write(b'{"error": "Selected model is at capacity. Please try a different model."}')
            return
        if "pelican" in prompt:
            text = SVG_B if mode == "swapped" else SVG_A
        elif "candies" in prompt:
            text = "seven" if mode == "degraded" else "2"
        elif "blue" in prompt:
            text = "blue, blue, blue, blue" if mode == "degraded" else "blue, blue, blue, blue, blue"
        else:
            text = "Some Name"
        data = sse_stream(text)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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

    def test_01_init_and_baseline_run(self):
        self._init()
        self.assertEqual(self._run(), 0)
        runs = ledger.read_runs(os.path.join(self.d, "ledger.jsonl"))
        self.assertEqual(len(runs), 4)
        self.assertTrue(all(r["status"] == "ok" for r in runs))
        base = ledger.load_baseline(os.path.join(self.d, "baseline.json"))
        self.assertIsNotNone(base)
        self.assertEqual(set(base), {"identity-svg", "candy-arithmetic",
                                    "instruction-repeat", "factual-anchor"})

    def test_02_identical_run_is_clean(self):
        self._init()
        self._run()
        self._run()
        rc = cli.main(["--dir", self.d, "check"])
        self.assertEqual(rc, 0)

    def test_03_swapped_model_triggers_fingerprint_drift(self):
        self._init()
        self._run()
        FakeProvider.mode = "swapped"
        self._run("--probe", "identity-svg")
        rc = cli.main(["--dir", self.d, "check"])
        self.assertEqual(rc, 2)

    def test_04_degraded_reasoning_triggers_quality_drift(self):
        self._init()
        self._run()
        FakeProvider.mode = "degraded"
        self._run("--probe", "candy-arithmetic")
        rc = cli.main(["--dir", self.d, "check"])
        self.assertEqual(rc, 2)

    def test_05_capacity_error_is_error_signal(self):
        self._init()
        self._run()
        FakeProvider.mode = "capacity"
        self._run("--probe", "candy-arithmetic")
        rc = cli.main(["--dir", self.d, "check"])
        self.assertEqual(rc, 2)
        runs = ledger.read_runs(os.path.join(self.d, "ledger.jsonl"))
        failed = [r for r in runs if r["status"] == "probe_failed"]
        self.assertTrue(failed)
        self.assertIn("at capacity", failed[-1].get("error_body", "").lower())

    def test_06_transport_failure_is_not_degradation(self):
        self._init()
        self._run()
        FakeProvider.mode = "dead"
        self._run("--probe", "candy-arithmetic")
        rc = cli.main(["--dir", self.d, "check"])
        # probe_failed -> exit 3, never misreported as degraded (exit 2)
        self.assertEqual(rc, 3)

    def test_07_report_renders(self):
        self._init()
        self._run()
        out = os.path.join(self.tmp, "report.md")
        rc = cli.main(["--dir", self.d, "report", "--output", out, "--last", "4"])
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

    def test_09_detect_unit(self):
        run = {"probe_id": "identity-svg", "kind": "identity", "status": "ok",
               "text": SVG_A, "ttft_s": 0.5, "output_chars": len(SVG_A)}
        base = {"identity-svg": {"fingerprint": "x", "sample_text": SVG_A,
                                 "ttft_median": 0.5, "len_median": len(SVG_A), "n": 1}}
        self.assertEqual(detect.detect(run, base), [])
        run2 = dict(run, text=SVG_B)
        findings = detect.detect(run2, base)
        self.assertTrue(any(f["kind"] == "fingerprint_drift" and f["severity"] == "high"
                            for f in findings))


if __name__ == "__main__":
    unittest.main()
