"""Append-only JSONL ledger + baseline management (stdlib only)."""
from __future__ import annotations

import json
import os


def append_run(ledger_path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(ledger_path)), exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


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
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return runs


def load_baseline(baseline_path: str) -> dict | None:
    if not os.path.exists(baseline_path):
        return None
    with open(baseline_path, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError:
            return None


def save_baseline(baseline_path: str, baseline: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(baseline_path)), exist_ok=True)
    with open(baseline_path, "w", encoding="utf-8") as fh:
        json.dump(baseline, fh, ensure_ascii=False, indent=2)


def build_baseline(runs: list) -> dict:
    """Aggregate successful probe runs into per-probe baseline stats."""
    by_probe: dict = {}
    for run in runs:
        if run.get("status") != "ok":
            continue
        pid = run["probe_id"]
        by_probe.setdefault(pid, []).append(run)
    baseline: dict = {}
    for pid, pruns in by_probe.items():
        ttfts = sorted(r["ttft_s"] for r in pruns if r.get("ttft_s") is not None)
        lens = sorted(r["output_chars"] for r in pruns)
        baseline[pid] = {
            "n": len(pruns),
            "fingerprint": pruns[-1]["fingerprint"],
            "sample_text": pruns[-1].get("text", "")[:500],
            "ttft_median": ttfts[len(ttfts) // 2] if ttfts else None,
            "len_median": lens[len(lens) // 2] if lens else None,
        }
    return baseline
