"""Backend tests for the Canary Cloud scaffold. Stdlib unittest only."""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, "..")

import question_bank
import style_profiles
import verdict
from app import Handler, ThreadingHTTPServer


def _base(pid="identity-svg", sample="A" * 3000):
    return {
        "probe_id": pid,
        "sample_text": sample,
        "sim_threshold": 0.55,
        "ttft_median": 0.5,
        "len_median": 3000,
        "tok_ratio_median": 0.25,
    }


def _run(pid="identity-svg", text="A" * 3000, kind="identity", ok=True,
          **kw):
    r = {"probe_id": pid, "kind": kind, "text": text, "ok": ok,
         "ttft_s": 0.5, "completion_tokens": 750}
    r.update(kw)
    return r


class VerdictTest(unittest.TestCase):
    def test_clean_run_is_ok(self):
        res = verdict.verdict_for(
            {"probes": [_base()]}, [_run()])
        self.assertEqual(res["verdict"], "ok")
        self.assertEqual(res["findings"], [])

    def test_swapped_identity_is_degraded(self):
        # Same length, totally different content -> fingerprint drift HIGH.
        res = verdict.verdict_for(
            {"probes": [_base()]}, [_run(text="B" * 3000)])
        self.assertEqual(res["verdict"], "degraded")
        kinds = {f["kind"] for f in res["findings"]}
        self.assertIn("fingerprint_drift", kinds)

    def test_identical_truncation_both_sides(self):
        # Regression guard for the C1-class bug: both sides must be
        # identically normalized AND identically truncated.
        long_text = "<svg>" + "x" * 5000 + "</svg>"
        sim = verdict.identity_similarity(long_text, long_text)
        self.assertEqual(sim, 1.0)

    def test_throttled_is_not_degraded(self):
        res = verdict.verdict_for(
            {"probes": [_base()]},
            [_run(ok=False, http_status=429,
                  error_body='{"error":{"code":"rate_limit_exceeded"}}')])
        self.assertEqual(res["verdict"], "throttled")

    def test_plain_500_is_probe_failed_not_degraded(self):
        res = verdict.verdict_for(
            {"probes": [_base()]},
            [_run(ok=False, http_status=500, error_body="boom")])
        self.assertEqual(res["verdict"], "probe_failed")

    def test_broad_capacity_word_is_not_throttle(self):
        # "capacity" alone must NOT count (narrow matching only).
        res = verdict.verdict_for(
            {"probes": [_base()]},
            [_run(ok=False, http_status=500,
                  error_body="model is at capacity, try later")])
        self.assertEqual(res["verdict"], "probe_failed")

    def test_account_flag_marker(self):
        res = verdict.verdict_for(
            {"probes": [_base()]},
            [_run(ok=False, http_status=403,
                  error_body="subscription_sharing_user_not_eligible")])
        kinds = {f["kind"] for f in res["findings"]}
        self.assertIn("account_flag", kinds)

    def test_low_only_warns(self):
        base = _base()
        base["len_median"] = 3000
        run = _run(text="A" * 3000 + "B" * 2500)  # big length drift, low sev
        res = verdict.verdict_for({"probes": [base]}, [run])
        self.assertEqual(res["verdict"], "warn")


class QuestionBankTest(unittest.TestCase):
    def test_templates_have_required_fields(self):
        for t in question_bank.get_templates():
            for field in ("id", "version", "kind", "template",
                          "output_schema", "description"):
                self.assertIn(field, t, f"template {t.get('id')}")
            self.assertIn("{seed}", t["template"])

    def test_validate_probe_accepts_good(self):
        ok, _ = question_bank.validate_probe({
            "prompt": "Draw X as SVG.", "kind": "identity",
            "check_regex": "^<svg",
        })
        self.assertTrue(ok)

    def test_validate_probe_rejects_bad(self):
        for bad in (None, "x", {}, {"prompt": "x"},
                    {"prompt": "x", "kind": "nope"},
                    {"prompt": "x", "kind": "identity",
                     "check_regex": "(unclosed"}):
            ok, _ = question_bank.validate_probe(bad)
            self.assertFalse(ok, f"should reject {bad!r}")

    def test_rotation_is_deterministic(self):
        bank = [{"id": f"p{i}"} for i in range(10)]
        a = question_bank.select_rotation("seed-1", bank, 4)
        b = question_bank.select_rotation("seed-1", bank, 4)
        c = question_bank.select_rotation("seed-2", bank, 4)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 4)
        self.assertNotEqual(a, c)


class StyleProfilesTest(unittest.TestCase):
    def test_profiles_have_features(self):
        profs = style_profiles.get_profiles()
        self.assertTrue(profs)
        n = len(style_profiles.FEATURE_NAMES)
        for p in profs:
            self.assertEqual(len(p["mean_vector"]), n)
            self.assertIn("placeholder", p)

    def test_nearest_family(self):
        v = style_profiles.get_profiles()[0]["mean_vector"]
        near = style_profiles.nearest_family(v)
        self.assertEqual(
            near["family"], style_profiles.get_profiles()[0]["family"])
        self.assertAlmostEqual(near["distance"], 0.0, places=6)


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.thread.join()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get(self, path):
        try:
            with urllib.request.urlopen(self._url(path)) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _post(self, path, obj):
        req = urllib.request.Request(
            self._url(path), data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health(self):
        code, body = self._get("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["keyless"])

    def test_templates(self):
        code, body = self._get("/api/question-templates")
        self.assertEqual(code, 200)
        self.assertTrue(len(body["templates"]) >= 3)

    def test_style_profiles(self):
        code, body = self._get("/api/style-profiles")
        self.assertEqual(code, 200)
        self.assertIn("features", body)
        self.assertTrue(body["profiles"])

    def test_verdict_roundtrip(self):
        code, body = self._post("/api/verdict", {
            "baseline": {"probes": [_base()]},
            "runs": [_run()],
        })
        self.assertEqual(code, 200)
        self.assertEqual(body["verdict"], "ok")

    def test_verdict_detects_swap(self):
        code, body = self._post("/api/verdict", {
            "baseline": {"probes": [_base()]},
            "runs": [_run(text="Z" * 3000)],
        })
        self.assertEqual(code, 200)
        self.assertEqual(body["verdict"], "degraded")

    def test_verdict_rejects_api_key_loudly(self):
        # THE invariant: a key sent to the server must be refused with 400.
        for sneaky in ({"baseline": {}, "runs": [], "api_key": "sk-xxx"},
                       {"baseline": {}, "runs": [],
                        "auth": {"authorization": "Bearer x"}},
                       {"baseline": {}, "runs": [], "token": "abc"}):
            code, body = self._post("/api/verdict", sneaky)
            self.assertEqual(code, 400, f"should reject {sneaky}")
            self.assertIn("key-like", body["error"])

    def test_verdict_rejects_bad_schema(self):
        code, _ = self._post("/api/verdict", {"hello": 1})
        self.assertEqual(code, 400)

    def test_no_such_endpoint(self):
        code, _ = self._get("/api/nope")
        self.assertEqual(code, 404)


if __name__ == "__main__":
    unittest.main()
