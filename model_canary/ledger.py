"""Append-only JSONL ledger + baseline management (stdlib only).

Concurrency safety:
- Appends use O_APPEND | LOCK_EX and a single os.write() call, so
  overlapping cron runs cannot interleave partial lines.
- Baselines are written to a temp file and atomically os.replace()d.
- A corrupt baseline raises instead of silently returning None (which
  used to trigger a quiet full-ledger baseline rebuild).
- `run_lock()` serializes `model-canary run` invocations.
"""
from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager

from . import detect as _detect  # local import at module level is safe: detect imports nothing from ledger

MIN_BASELINE_SAMPLES = 5
LEDGER_ROTATE_BYTES = 10 * 1024 * 1024


def _line_size_guard(record: dict) -> bytes:
    return (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")


def append_run(ledger_path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(ledger_path)), exist_ok=True)
    _maybe_rotate(ledger_path)
    data = _line_size_guard(record)
    fd = os.open(ledger_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # One os.write: with O_APPEND this is atomic w.r.t. other appenders.
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _maybe_rotate(ledger_path: str) -> None:
    """Rotate the ledger when it grows past 10MB (keeps one backup)."""
    try:
        if os.path.getsize(ledger_path) > LEDGER_ROTATE_BYTES:
            os.replace(ledger_path, ledger_path + ".1")
    except OSError:
        pass


def read_runs(ledger_path: str) -> list:
    runs: list = []
    if not os.path.exists(ledger_path):
        return runs
    with open(ledger_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Skip corrupt rows instead of crashing downstream on KeyError.
            if not isinstance(obj, dict) or not obj.get("probe_id"):
                continue
            runs.append(obj)
    return runs


def load_baseline(baseline_path: str) -> dict | None:
    """Return the baseline dict, None if no baseline was ever written.

    Raises ValueError on a corrupt file -- a half-written baseline must
    never silently trigger a rebuild from the full ledger.
    """
    if not os.path.exists(baseline_path):
        return None
    with open(baseline_path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt baseline at {baseline_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"corrupt baseline at {baseline_path}: not a JSON object")
    return data


def save_baseline(baseline_path: str, baseline: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(baseline_path)), exist_ok=True)
    tmp = baseline_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(baseline, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, baseline_path)


@contextmanager
def run_lock(state_dir: str):
    """Serialize `run` invocations; raises RuntimeError if one is active."""
    os.makedirs(state_dir, exist_ok=True)
    lock_path = os.path.join(state_dir, "run.lock")
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise RuntimeError(f"another model-canary run is in progress ({lock_path})")
    try:
        yield
    finally:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


def _prompt_sha(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def _medoid(texts: list) -> tuple[int, float]:
    """Index of the medoid (highest mean pairwise similarity) and the
    minimum pairwise similarity. O(n^2); n is small (samples per probe)."""
    n = len(texts)
    if n == 1:
        return 0, 1.0
    sims = [[0.0] * n for _ in range(n)]
    min_sim = 1.0
    for i in range(n):
        for j in range(i + 1, n):
            s = _detect.identity_similarity(texts[i], texts[j])
            sims[i][j] = sims[j][i] = s
            min_sim = min(min_sim, s)
    best = max(range(n), key=lambda i: sum(sims[i]) / (n - 1))
    return best, min_sim


def _median(xs: list):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def build_baseline(runs: list, min_samples: int = MIN_BASELINE_SAMPLES,
                   since: str | None = None) -> dict:
    """Aggregate probe runs into per-probe baseline stats.

    - Only `ok` runs are candidates.
    - Two passes: build a provisional baseline, detect() every candidate
      against it, drop runs with HIGH/MEDIUM findings (a poisoned baseline
      would blind us forever), then rebuild.
    - Runs are grouped by (model, endpoint, prompt_sha); only the latest
      cohort per probe is used, so a model switch starts a fresh baseline
      instead of mixing cohorts.
    - Probes with fewer than min_samples runs are left out (still baselining).
    - `since`: ISO-8601 cutoff; older runs are ignored (for --since rebuilds).
    """
    cands = [r for r in runs if r.get("status") == "ok" and r.get("probe_id")]
    if since:
        cands = [r for r in cands if (r.get("ts") or "") >= since]

    by_probe: dict = {}
    for r in cands:
        by_probe.setdefault(r["probe_id"], []).append(r)

    baseline: dict = {}
    for pid, pruns in by_probe.items():
        # Latest cohort only: group by (model, endpoint, prompt_sha), keep
        # the cohort of the most recent run.
        pruns.sort(key=lambda r: r.get("ts") or "")
        latest = pruns[-1]
        cohort_key = (latest.get("model"), latest.get("endpoint"), latest.get("prompt_sha"))
        cohort = [r for r in pruns
                  if (r.get("model"), r.get("endpoint"), r.get("prompt_sha")) == cohort_key]
        if len(cohort) < min_samples:
            continue

        provisional = _aggregate(pid, cohort, latest)
        clean = [r for r in cohort
                 if not _has_high_or_medium(_detect.detect(r, {pid: provisional}))]
        if len(clean) < min_samples:
            # Too poisoned to trust; leave this probe baselining.
            continue
        baseline[pid] = _aggregate(pid, clean, latest)
    return baseline


def _has_high_or_medium(findings: list) -> bool:
    return any(f["severity"] in ("high", "medium") for f in findings)


def _aggregate(pid: str, pruns: list, latest: dict) -> dict:
    texts = [_detect.normalize_identity(r.get("text", "")) for r in pruns]
    medoid_idx, min_sim = _medoid(texts)
    ttfts = [r["ttft_s"] for r in pruns if r.get("ttft_s") is not None]
    lens = [len(t.strip()) for t in texts]
    ratios = [r["completion_tokens"] / max(len(t.strip()), 1) for r, t in zip(pruns, texts)
              if isinstance(r.get("completion_tokens"), int)]
    threshold = min(0.95, max(0.30, min_sim - _detect.SIMILARITY_MARGIN)) \
        if len(texts) > 1 else _detect.DEFAULT_SIMILARITY_THRESHOLD
    return {
        "model": latest.get("model"),
        "endpoint": latest.get("endpoint"),
        "prompt_sha": latest.get("prompt_sha"),
        "created_at": _now(),
        "n": len(pruns),
        "sample_text": texts[medoid_idx],
        "sim_threshold": round(threshold, 3),
        "ttft_median": _median(ttfts),
        "len_median": _median(lens),
        "tok_ratio_median": round(_median(ratios), 4) if ratios else None,
    }


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
