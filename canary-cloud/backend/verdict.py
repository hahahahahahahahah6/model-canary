"""Keyless verdict engine for Canary Cloud.

Adapted from model-canary's local drift detector (model_canary/detect.py).
Pure functions over plain dicts: no network, no keys, no I/O.

The cloud server exposes this via POST /api/verdict; the browser may also
bundle an equivalent implementation, but the canonical algorithm lives here
so it stays versioned in one place.

Verdicts: ok | warn | degraded | throttled | probe_failed | baselining
"""
from __future__ import annotations

import difflib
import hashlib
import re

# --- structured error classification --------------------------------------
# Only explicit markers are trusted. Broad substring matches like "capacity"
# or "rate limit" are deliberately NOT trusted: they appear in benign text.
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
# Narrow message hints, consulted only after structured fields came up empty.
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
    """Canonical form for identity comparison (both sides, always)."""
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
    """Return (is_account_flag, is_throttled) from structured error data."""
    body = run.get("error_body") or ""
    lowered = body.lower()
    for marker in ACCOUNT_FLAG_MARKERS:
        if marker in lowered:
            return True, False
    if run.get("http_status") in THROTTLE_HTTP_STATUSES:
        return False, True
    code = (run.get("error_code") or "").lower()
    etype = (run.get("error_type") or "").lower()
    if code in THROTTLE_CODES or etype in THROTTLE_TYPES:
        return False, True
    for hint in THROTTLE_MESSAGE_HINTS:
        if hint in lowered:
            return False, True
    return False, False


def compare_run(baseline: dict, run: dict) -> list:
    """Compare one probe run against its baseline entry -> findings list.

    `baseline`: {"probe_id":..., "sample_text":..., "sim_threshold":...,
                  "ttft_median":..., "len_median":..., "tok_ratio_median":...}
    `run`: {"probe_id":..., "kind":..., "text":..., "ttft_s":...,
            "completion_tokens":..., "ok": bool, "error_body":...,
            "http_status":..., "error_code":..., "error_type":...}
    """
    pid = run.get("probe_id", "?")
    findings: list = []

    # Fail-open: infrastructure failures never degrade.
    if not run.get("ok", True):
        account_flag, throttled = _error_markers(run)
        if account_flag:
            findings.append(_finding("high", "account_flag", pid,
                                     "account-level marker in error body"))
        elif throttled:
            findings.append(_finding("info", "throttled", pid,
                                     "request was throttled; not degradation"))
        else:
            findings.append(_finding("info", "probe_failed", pid,
                                     "probe infrastructure failed (fail-open)"))
        return findings

    if baseline.get("probe_prompt_sha") and run.get("probe_prompt_sha"):
        if baseline["probe_prompt_sha"] != run["probe_prompt_sha"]:
            return [_finding("info", "baseline_stale", pid,
                             "probe prompt changed; rebuild the baseline")]

    text = run.get("text", "") or ""

    # Fingerprint drift on the identity probe = likely model swap.
    if run.get("kind") == "identity" and baseline.get("sample_text"):
        sim = identity_similarity(text, baseline["sample_text"])
        threshold = baseline.get("sim_threshold", DEFAULT_SIMILARITY_THRESHOLD)
        if sim < threshold:
            findings.append(
                _finding(
                    "high", "fingerprint_drift", pid,
                    f"identity-probe similarity to baseline {sim:.2f} < "
                    f"{threshold:.2f} (model may have been silently swapped)",
                )
            )

    # Latency drift: relative AND absolute -- tiny absolute jitter is noise.
    ttft = run.get("ttft_s")
    base_ttft = baseline.get("ttft_median")
    if ttft is not None and base_ttft:
        if (ttft > base_ttft * TTFT_DRIFT_FACTOR
                and (ttft - base_ttft) > TTFT_MIN_ABS_DELTA_S):
            findings.append(
                _finding(
                    "low", "latency_drift", pid,
                    f"TTFT {ttft:.2f}s vs baseline median {base_ttft:.2f}s",
                )
            )

    # Length drift: short baselines use an absolute threshold.
    olen = len(text.strip())
    base_len = baseline.get("len_median")
    if base_len:
        if base_len < SHORT_BASELINE_CHARS:
            limit = max(20, LENGTH_DRIFT_RATIO * base_len)
            if abs(olen - base_len) > limit:
                findings.append(
                    _finding(
                        "low", "length_drift", pid,
                        f"output length {olen} vs baseline median {base_len}",
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

    # Token-ratio drift: completion_tokens/output_chars is tokenizer-shaped.
    ct = run.get("completion_tokens")
    base_ratio = baseline.get("tok_ratio_median")
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

    degraded: a HIGH, or a confirmed MEDIUM.
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


def verdict_for(baseline: dict, runs: list) -> dict:
    """Full pipeline: compare every run, return {verdict, worst, findings}."""
    findings: list = []
    by_id = {b.get("probe_id"): b for b in baseline.get("probes", [])}
    for run in runs:
        base = by_id.get(run.get("probe_id"), {})
        findings.extend(compare_run(base, run))
    result = summarize(findings)
    result["findings"] = findings
    return result
