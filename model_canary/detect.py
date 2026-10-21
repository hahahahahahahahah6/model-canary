"""Drift detection: compare a probe run against the baseline.

Signals (each returns a finding dict or None):
- fingerprint_drift (HIGH): identity probe output no longer resembles baseline
- quality_drift   (MEDIUM): a probe with a hard expectation now fails it
- error_signal    (HIGH): provider returned a known degradation/throttle marker
- latency_drift   (LOW): TTFT far above baseline median
- length_drift    (LOW): output length far from baseline median

Transport failures are recorded as probe_failed and NEVER reported as
degradation (fail-open on alerting).
"""
from __future__ import annotations

import difflib
import hashlib
import re

# Substrings (lowercased) that providers emit when an account/model is
# throttled, capacity-limited, or flagged for sharing violations — the
# machine-readable side of "am I being degraded?".
ERROR_SIGNALS = (
    "model is at capacity",
    "at capacity. please try a different model",
    "subscription_sharing_user_not_eligible",
    "rate_limit",
    "rate limit",
    "capacity",
    "overloaded",
    "temporarily unavailable",
)

FINGERPRINT_SIMILARITY_THRESHOLD = 0.55
TTFT_DRIFT_FACTOR = 3.0
LENGTH_DRIFT_RATIO = 0.60


def fingerprint(text: str) -> str:
    """Stable fingerprint of a response (whitespace/case normalized)."""
    norm = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _finding(severity: str, kind: str, probe_id: str, detail: str) -> dict:
    return {
        "severity": severity,
        "kind": kind,
        "probe_id": probe_id,
        "detail": detail,
    }


def detect(run: dict, baseline: dict | None, sample_text_lookup: dict | None = None) -> list:
    """Return findings for one ledger run. Empty list = no drift."""
    findings: list = []
    pid = run.get("probe_id", "?")

    if run.get("status") == "probe_failed":
        # Fail-open: infrastructure failure is data, not degradation —
        # UNLESS the error body carries a known degradation/throttle marker.
        findings = [
            _finding(
                "info",
                "probe_failed",
                pid,
                f"probe request failed ({run.get('error', 'unknown')}); not counted as degradation",
            )
        ]
        err_body = (run.get("error_body") or "").lower()
        for sig in ERROR_SIGNALS:
            if sig in err_body:
                findings.append(
                    _finding(
                        "high",
                        "error_signal",
                        pid,
                        f"provider returned degradation/throttle marker {sig!r} "
                        f"(HTTP {run.get('http_status')})",
                    )
                )
                break
        return findings

    # Error-body signals on otherwise-"ok" runs beat everything else.
    err_body = (run.get("error_body") or "").lower()
    http_status = run.get("http_status")
    for sig in ERROR_SIGNALS:
        if sig in err_body:
            findings.append(
                _finding(
                    "high",
                    "error_signal",
                    pid,
                    f"provider returned degradation/throttle marker {sig!r} (HTTP {http_status})",
                )
            )
            break

    # Hard expectation check (probes with known-good answers).
    expected = run.get("expected_substrings") or ()
    text = run.get("text", "")
    lowered = text.lower()
    if expected and not any(s.lower() in lowered for s in expected):
        findings.append(
            _finding(
                "medium",
                "quality_drift",
                pid,
                f"expected one of {list(expected)} in output; got {len(text)} chars starting: {text[:80]!r}",
            )
        )

    if not baseline or pid not in baseline:
        return findings
    base = baseline[pid]

    # Fingerprint drift on the identity probe = likely model swap.
    if run.get("kind") == "identity" and base.get("sample_text"):
        sim = similarity(text, base["sample_text"])
        if sim < FINGERPRINT_SIMILARITY_THRESHOLD:
            findings.append(
                _finding(
                    "high",
                    "fingerprint_drift",
                    pid,
                    f"identity-probe similarity to baseline {sim:.2f} < {FINGERPRINT_SIMILARITY_THRESHOLD} "
                    f"(model may have been silently swapped)",
                )
            )

    ttft = run.get("ttft_s")
    base_ttft = base.get("ttft_median")
    if ttft is not None and base_ttft:
        if ttft > base_ttft * TTFT_DRIFT_FACTOR:
            findings.append(
                _finding(
                    "low",
                    "latency_drift",
                    pid,
                    f"TTFT {ttft:.2f}s vs baseline median {base_ttft:.2f}s (>{TTFT_DRIFT_FACTOR}x)",
                )
            )

    olen = run.get("output_chars", 0)
    base_len = base.get("len_median")
    if base_len:
        drift = abs(olen - base_len) / base_len
        if drift > LENGTH_DRIFT_RATIO:
            findings.append(
                _finding(
                    "low",
                    "length_drift",
                    pid,
                    f"output length {olen} vs baseline median {base_len} (drift {drift:.0%})",
                )
            )

    return findings


def summarize(findings: list) -> dict:
    """Roll findings up to a verdict: ok | degraded | probe_failed."""
    kinds = {f["kind"] for f in findings}
    if kinds == {"probe_failed"} or not findings:
        if not findings:
            return {"verdict": "ok", "worst": None}
        return {"verdict": "probe_failed", "worst": "info"}
    worst = "low"
    order = {"low": 0, "medium": 1, "high": 2}
    for f in findings:
        if f["kind"] == "probe_failed":
            continue
        if order.get(f["severity"], 0) > order[worst]:
            worst = f["severity"]
    return {"verdict": "degraded", "worst": worst}
