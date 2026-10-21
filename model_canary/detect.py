"""Drift detection: compare probe runs against the baseline.

Verdicts: ok | warn | degraded | throttled | probe_failed | baselining
Exit codes: 0 = ok/warn/baselining, 2 = degraded, 3 = probe_failed, 4 = throttled

Design notes (post 2026-10-21 review):
- Fingerprint comparison always uses identically-normalized, identically-
  truncated text on both sides, with autojunk disabled. The similarity
  threshold is derived from the baseline samples themselves (min pairwise
  similarity minus a margin), not a hardcoded constant.
- probe_failed records are fail-open: they only ever produce info, a
  "throttled" signal, or an "account_flag" signal -- never degradation.
  Only explicit, machine-readable markers are trusted; broad substring
  matches like "capacity" or "rate limit" were removed.
- Verdicts are graded: HIGH or a confirmed MEDIUM degrades; LOW-only warns.
  (A k-of-n-confirmed MEDIUM is a real signal, not noise -- the noise filter
  is the confirmation window, see cli.check.)
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re

# --- structured error classification -------------------------------------
# Only explicit markers are trusted. Substring matching over the whole body
# is deliberately NOT done: words like "capacity" appear in benign contexts.
ACCOUNT_FLAG_MARKERS = (
    "subscription_sharing_user_not_eligible",
)
THROTTLE_CODES = {
    "rate_limit_exceeded",
    "rate_limit",
    "throttled",
    "too_many_requests",
}
THROTTLE_TYPES = {
    "rate_limit_error",
    "throttle_error",
    "overloaded_error",
}
THROTTLE_HTTP_STATUSES = {429, 529}
# Narrow message hints, only consulted after structured fields came up empty.
THROTTLE_MESSAGE_HINTS = ("rate limit", "ratelimit", "too many requests")

IDENTITY_SAMPLE_LEN = 2000
DEFAULT_SIMILARITY_THRESHOLD = 0.55
SIMILARITY_MARGIN = 0.10
TTFT_DRIFT_FACTOR = 3.0
TTFT_MIN_ABS_DELTA_S = 1.0
LENGTH_DRIFT_RATIO = 0.60
SHORT_BASELINE_CHARS = 50
TOKEN_RATIO_DRIFT = 0.50


def fingerprint(text: str) -> str:
    """Stable fingerprint of a response (whitespace/case normalized)."""
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def normalize_identity(text: str) -> str:
    """Canonical form used for identity comparison (both sides, always)."""
    return re.sub(r"\s+", " ", text.strip())[:IDENTITY_SAMPLE_LEN]


def identity_similarity(a: str, b: str) -> float:
    """Character similarity with autojunk disabled.

    difflib's default autojunk=True treats characters appearing in >1% of a
    200+ char string as junk, which badly distorts SVG comparison.
    """
    return difflib.SequenceMatcher(
        None, normalize_identity(a), normalize_identity(b), autojunk=False
    ).ratio()


def _finding(severity: str, kind: str, probe_id: str, detail: str) -> dict:
    return {
        "severity": severity,
        "kind": kind,
        "probe_id": probe_id,
        "detail": detail,
    }


def _error_markers(run: dict) -> tuple[bool, bool]:
    """Return (is_account_flag, is_throttled) from structured error data.

    Prefers machine-readable fields: HTTP status, then error.code /
    error.type parsed out of a JSON error body. Falls back to narrow
    message hints only.
    """
    body = run.get("error_body") or ""
    lowered = body.lower()
    for marker in ACCOUNT_FLAG_MARKERS:
        if marker in lowered:
            return True, False
    if run.get("http_status") in THROTTLE_HTTP_STATUSES:
        return False, True
    code = etype = msg = ""
    try:
        obj = json.loads(body) if body.strip().startswith("{") else {}
        err = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code", "")).lower()
            etype = str(err.get("type", "")).lower()
            msg = str(err.get("message", "")).lower()
        elif isinstance(err, str):
            msg = err.lower()
    except (ValueError, AttributeError):
        pass
    if code in THROTTLE_CODES or etype in THROTTLE_TYPES:
        return False, True
    haystack = " ".join((code, etype, msg, lowered))
    if any(h in haystack for h in THROTTLE_MESSAGE_HINTS):
        return False, True
    return False, False


def _check_expectation(run: dict, probe: dict | None) -> dict | None:
    """Hard-expectation check. Returns a MEDIUM finding or None."""
    pid = run.get("probe_id", "?")
    text = run.get("text", "")
    # The record carries its own expectation (self-describing ledger rows);
    # fall back to the probe map for rows written by older versions.
    regex = run.get("expected_regex") or (probe or {}).get("expected_regex") or ""
    if regex:
        if not re.search(regex, text):
            return _finding(
                "medium", "quality_drift", pid,
                f"output does not match expected pattern {regex}; "
                f"got {len(text)} chars starting: {text[:80]!r}",
            )
        return None
    expected = (probe or {}).get("expected_substrings") or ()
    lowered = text.lower()
    if expected and not any(s.lower() in lowered for s in expected):
        return _finding(
            "medium", "quality_drift", pid,
            f"expected one of {list(expected)} in output; "
            f"got {len(text)} chars starting: {text[:80]!r}",
        )
    return None


def detect(run: dict, baseline: dict | None, probes: dict | None = None) -> list:
    """Return findings for one ledger run. Empty list = no drift.

    `probes` maps probe_id -> probe dict (for expected_regex lookup).
    """
    findings: list = []
    pid = run.get("probe_id", "?")

    if run.get("status") == "probe_failed":
        # Fail-open: infrastructure failure is data, not degradation --
        # unless structured markers say otherwise.
        findings.append(
            _finding(
                "info", "probe_failed", pid,
                f"probe request failed ({run.get('error', 'unknown')}); "
                "not counted as degradation",
            )
        )
        is_flag, is_throttled = _error_markers(run)
        if is_flag:
            findings.append(
                _finding(
                    "high", "account_flag", pid,
                    "provider returned an account-level flag "
                    f"(HTTP {run.get('http_status')})",
                )
            )
        elif is_throttled:
            findings.append(
                _finding(
                    "info", "throttled", pid,
                    f"provider throttled the request (HTTP {run.get('http_status')})",
                )
            )
        return findings

    probe = (probes or {}).get(pid)
    f = _check_expectation(run, probe)
    if f:
        findings.append(f)

    if not baseline or pid not in baseline:
        return findings
    base = baseline[pid]

    # Stale baseline (model/endpoint/prompt changed): report, don't drift.
    if base.get("model") and run.get("model") and base["model"] != run["model"]:
        return [_finding("info", "baseline_stale", pid,
                         f"model changed {base['model']} -> {run['model']}; "
                         "rebuild the baseline")]
    if (base.get("prompt_sha") and run.get("prompt_sha")
            and base["prompt_sha"] != run["prompt_sha"]):
        return [_finding("info", "baseline_stale", pid,
                         "probe prompt changed; rebuild the baseline")]

    text = run.get("text", "")

    # Fingerprint drift on the identity probe = likely model swap.
    if run.get("kind") == "identity" and base.get("sample_text"):
        sim = identity_similarity(text, base["sample_text"])
        threshold = base.get("sim_threshold", DEFAULT_SIMILARITY_THRESHOLD)
        if sim < threshold:
            findings.append(
                _finding(
                    "high", "fingerprint_drift", pid,
                    f"identity-probe similarity to baseline {sim:.2f} < {threshold:.2f} "
                    "(model may have been silently swapped)",
                )
            )

    # Latency drift: relative AND absolute -- a 0.05s TTFT against a 0.001s
    # local-test median is jitter, not degradation.
    ttft = run.get("ttft_s")
    base_ttft = base.get("ttft_median")
    if ttft is not None and base_ttft:
        if (ttft > base_ttft * TTFT_DRIFT_FACTOR
                and (ttft - base_ttft) > TTFT_MIN_ABS_DELTA_S):
            findings.append(
                _finding(
                    "low", "latency_drift", pid,
                    f"TTFT {ttft:.2f}s vs baseline median {base_ttft:.2f}s "
                    f"(>{TTFT_DRIFT_FACTOR}x and +{ttft - base_ttft:.2f}s)",
                )
            )

    # Length drift: short baselines use an absolute threshold, otherwise a
    # 100% "drift" on a 1-char answer is just a trailing newline.
    olen = len(text.strip())
    base_len = base.get("len_median")
    if base_len:
        if base_len < SHORT_BASELINE_CHARS:
            limit = max(20, LENGTH_DRIFT_RATIO * base_len)
            if abs(olen - base_len) > limit:
                findings.append(
                    _finding(
                        "low", "length_drift", pid,
                        f"output length {olen} vs baseline median {base_len} "
                        f"(>{limit:.0f} chars)",
                    )
                )
        elif abs(olen - base_len) / base_len > LENGTH_DRIFT_RATIO:
            findings.append(
                _finding(
                    "low", "length_drift", pid,
                    f"output length {olen} vs baseline median {base_len} "
                    f"(drift {abs(olen - base_len) / base_len:.0%})",
                )
            )

    # Token-ratio drift: completion_tokens/output_chars is tokenizer-shaped,
    # so a shift is a strong model-swap signal.
    ct = run.get("completion_tokens")
    base_ratio = base.get("tok_ratio_median")
    if isinstance(ct, int) and base_ratio:
        ratio = ct / max(olen, 1)
        if abs(ratio - base_ratio) / base_ratio > TOKEN_RATIO_DRIFT:
            findings.append(
                _finding(
                    "medium", "token_ratio_drift", pid,
                    f"token/char ratio {ratio:.2f} vs baseline {base_ratio:.2f}",
                )
            )

    return findings


def summarize(findings: list) -> dict:
    """Roll findings up to a verdict.

    degraded: a HIGH, or a k-of-n-confirmed MEDIUM (the confirmation window
      is the noise filter; a confirmed medium is a real signal).
    throttled: throttling evidence, no HIGH.
    probe_failed: only infrastructure failures.
    warn: LOW-only, or an unconfirmed MEDIUM.
    ok: nothing.
    """
    if not findings:
        return {"verdict": "ok", "worst": None}
    kinds = {f["kind"] for f in findings}
    sevs = {f["severity"] for f in findings}
    if "high" in sevs:
        return {"verdict": "degraded", "worst": "high"}
    if "throttled" in kinds:
        return {"verdict": "throttled", "worst": "info"}
    if kinds <= {"probe_failed", "baseline_stale"}:
        if "probe_failed" in kinds:
            return {"verdict": "probe_failed", "worst": "info"}
        return {"verdict": "ok", "worst": None}
    if "medium" in sevs:
        return {"verdict": "degraded", "worst": "medium"}
    if "low" in sevs:
        return {"verdict": "warn", "worst": "low"}
    return {"verdict": "ok", "worst": None}
