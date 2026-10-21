"""model-canary CLI: init / run / check / report / probes.

Exit codes: 0 = ok/warn/baselining, 2 = degraded, 3 = probe_failed, 4 = throttled.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import uuid

from . import __version__, detect, ledger, probes, provider

DEFAULT_DIR = ".model-canary"
CONFIG_NAME = "config.json"
BASELINE_NAME = "baseline.json"
LEDGER_NAME = "ledger.jsonl"

CONFIRM_WINDOW = 10  # check looks at the last N records per probe
CONFIRM_K = 2        # a finding kind must repeat >=K times to count
DEFAULT_SAMPLES = 5  # samples per probe per run (also the baselining quorum)


def _paths(root: str) -> dict:
    d = os.path.abspath(root)
    return {
        "dir": d,
        "config": os.path.join(d, CONFIG_NAME),
        "baseline": os.path.join(d, BASELINE_NAME),
        "ledger": os.path.join(d, LEDGER_NAME),
    }


def _load_config(paths: dict) -> dict:
    if not os.path.exists(paths["config"]):
        sys.exit(f"no config at {paths['config']}; run `model-canary init` first")
    with open(paths["config"], encoding="utf-8") as fh:
        return json.load(fh)


def cmd_init(args: argparse.Namespace) -> int:
    p = _paths(args.dir)
    os.makedirs(p["dir"], exist_ok=True)
    if os.path.exists(p["config"]) and not args.force:
        print(f"config already exists at {p['config']} (use --force to overwrite)")
        return 1
    cfg = {
        "endpoint": args.endpoint,
        "model": args.model,
        "api_key_env": args.api_key_env,
        "timeout_s": args.timeout,
        "probes": [pr.id for pr in probes.list_probes()],
    }
    with open(p["config"], "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    print(f"initialized {p['config']}")
    print(f"set {cfg['api_key_env']} in your environment, then run: model-canary run")
    return 0


def _run_probe(cfg: dict, probe: probes.Probe, run_id: str) -> dict:
    api_key = os.environ.get(cfg["api_key_env"], "")
    base_rec = {
        "probe_id": probe.id,
        "kind": probe.kind,
        "run_id": run_id,
        "model": cfg["model"],
        "endpoint": cfg["endpoint"],
        "prompt_sha": ledger._prompt_sha(probe.prompt),
        "ts": _now(),
    }
    if not api_key:
        return {**base_rec, "status": "probe_failed",
                "error": f"env var {cfg['api_key_env']} is not set"}
    try:
        m = provider.chat_once(
            cfg["endpoint"], api_key, cfg["model"], probe.prompt,
            timeout=cfg.get("timeout_s", 60.0),
        )
    except provider.ProbeError as exc:
        return {**base_rec, "status": "probe_failed",
                "error": str(exc)[:200],
                "error_body": exc.body[:2000],
                "http_status": exc.http_status}
    text = m["text"]
    return {**base_rec, "status": "ok",
            "fingerprint": detect.fingerprint(text),
            "text": text[:4000],
            "expected_regex": probe.expected_regex or None,
            "ttft_s": m["ttft_s"],
            "connect_s": m.get("connect_s"),
            "latency_s": m["latency_s"],
            "output_chars": m["output_chars"],
            "prompt_tokens": m["prompt_tokens"],
            "completion_tokens": m["completion_tokens"],
            "total_tokens": m["total_tokens"],
            "http_status": m["http_status"]}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_run(args: argparse.Namespace) -> int:
    p = _paths(args.dir)
    cfg = _load_config(p)
    try:
        with ledger.run_lock(p["dir"]):
            return _cmd_run_locked(args, p, cfg)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _cmd_run_locked(args: argparse.Namespace, p: dict, cfg: dict) -> int:
    wanted = set(args.probe) if args.probe else set(cfg.get("probes", []))
    run_id = uuid.uuid4().hex[:12]
    samples = max(1, args.samples)
    for pr in probes.list_probes():
        if pr.id not in wanted:
            continue
        for i in range(samples):
            rec = _run_probe(cfg, pr, run_id)
            ledger.append_run(p["ledger"], rec)
            mark = "OK " if rec["status"] == "ok" else "FAIL"
            extra = "" if rec["status"] == "ok" else f" ({rec.get('error')})"
            print(f"[{mark}] {pr.id} sample {i + 1}/{samples}: "
                  f"{rec.get('output_chars', 0)} chars, "
                  f"ttft={rec.get('ttft_s', '?')}s{extra}")
    if args.baseline or not os.path.exists(p["baseline"]):
        runs = ledger.read_runs(p["ledger"])
        base = ledger.build_baseline(runs, since=args.since)
        if base:
            ledger.save_baseline(p["baseline"], base)
            total = sum(v["n"] for v in base.values())
            print(f"baseline written ({total} runs across {len(base)} probes)")
        else:
            print("baselining: not enough clean samples yet "
                  f"(need >={ledger.MIN_BASELINE_SAMPLES} per probe)")
    return 0


def _parse_max_age(spec: str) -> float | None:
    """Parse '7d'/'24h'/'30m'/'0' -> seconds; '0' disables the age filter."""
    if spec == "0":
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([dhm])", spec.strip())
    if not m:
        raise ValueError(f"bad --max-age {spec!r}; use like 7d, 24h, 30m, or 0")
    mult = {"d": 86400, "h": 3600, "m": 60}[m.group(2)]
    return float(m.group(1)) * mult


def _confirm_findings(per_run: list, k: int = CONFIRM_K) -> list:
    """k-of-n: a finding kind counts only if seen in >=k runs of the window.

    Returns one representative finding (latest) per confirmed kind.
    """
    counts: dict = {}
    latest: dict = {}
    for findings in per_run:
        seen = set()
        for f in findings:
            key = (f["kind"], f["probe_id"])
            if key in seen:
                continue
            seen.add(key)
            counts[key] = counts.get(key, 0) + 1
            latest[key] = f
    return [latest[key] for key, c in counts.items() if c >= k]


def cmd_check(args: argparse.Namespace) -> int:
    p = _paths(args.dir)
    _load_config(p)  # validates init
    try:
        base = ledger.load_baseline(p["baseline"])
    except ValueError as exc:
        print(f"baseline error: {exc}", file=sys.stderr)
        return 1
    if base is None:
        print("baselining: no baseline yet; run `model-canary run` "
              f"until each probe has >={ledger.MIN_BASELINE_SAMPLES} samples")
        return 0
    runs = ledger.read_runs(p["ledger"])
    if not runs:
        print("ledger is empty")
        return 1
    try:
        max_age_s = _parse_max_age(args.max_age)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    now = datetime.datetime.now(datetime.timezone.utc)
    pmap = probes.probe_map()

    # Window: last CONFIRM_WINDOW records per probe, within --max-age.
    by_probe: dict = {}
    for r in runs:
        by_probe.setdefault(r["probe_id"], []).append(r)
    all_confirmed: list = []
    for pid in sorted(by_probe):
        window = by_probe[pid][-CONFIRM_WINDOW:]
        fresh = []
        for r in window:
            if max_age_s is not None:
                try:
                    ts = datetime.datetime.strptime(
                        r.get("ts", ""), "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=datetime.timezone.utc)
                except ValueError:
                    continue
                if (now - ts).total_seconds() > max_age_s:
                    continue  # stale record: not evidence of anything
            fresh.append(r)
        per_run = [detect.detect(r, base, pmap) for r in fresh]
        confirmed = _confirm_findings(per_run)
        for f in confirmed:
            all_confirmed.append(f)
            print(f"[{f['severity'].upper():6}] {f['kind']:17} {pid}: {f['detail']}")
    verdict = detect.summarize(all_confirmed)
    print(f"verdict: {verdict['verdict']}")
    return {"degraded": 2, "probe_failed": 3, "throttled": 4}.get(
        verdict["verdict"], 0)


def cmd_report(args: argparse.Namespace) -> int:
    p = _paths(args.dir)
    runs = ledger.read_runs(p["ledger"])
    try:
        base = ledger.load_baseline(p["baseline"])
    except ValueError:
        base = None
    if not runs:
        print("ledger is empty")
        return 1
    runs = runs[-args.last:]
    pmap = probes.probe_map()
    lines = ["# model-canary report", ""]
    lines.append("| time | probe | status | chars | ttft | tokens | drift |")
    lines.append("|---|---|---|---|---|---|---|")
    total_tokens = 0
    degraded = 0
    for r in runs:
        findings = detect.detect(r, base, pmap) if base else []
        kinds = ",".join(sorted({f["kind"] for f in findings
                                 if f["kind"] != "probe_failed"})) or "-"
        if any(f["severity"] in ("high", "medium") for f in findings):
            degraded += 1
        tok = r.get("total_tokens")
        total_tokens += tok if isinstance(tok, int) else 0
        lines.append(
            f"| {r.get('ts', '?')} | {r.get('probe_id')} | {r.get('status')} "
            f"| {r.get('output_chars', '-')} | {r.get('ttft_s', '-')} "
            f"| {tok if tok is not None else '-'} | {kinds} |"
        )
    lines += ["", f"runs: {len(runs)}, degraded: {degraded}, probe tokens spent: {total_tokens}"]
    out = "\n".join(lines)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
        print(f"wrote {args.output}")
    else:
        print(out)
    return 0


def cmd_probes(_args: argparse.Namespace) -> int:
    for pr in probes.list_probes():
        print(f"{pr.id} [{pr.kind}]\n  {pr.description}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="model-canary",
        description="Degradation watchdog for LLM APIs: canary probes, ledger, drift alerts.",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="state directory (default: .model-canary)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="create config in --dir")
    i.add_argument("--endpoint", required=True, help="OpenAI-compatible base URL, e.g. https://api.openai.com/v1")
    i.add_argument("--model", required=True, help="model id to probe")
    i.add_argument("--api-key-env", default="MODEL_CANARY_API_KEY")
    i.add_argument("--timeout", type=float, default=60.0)
    i.add_argument("--force", action="store_true")

    r = sub.add_parser("run", help="run canary probes and append to ledger")
    r.add_argument("--probe", action="append", default=[], help="only run this probe id (repeatable)")
    r.add_argument("--baseline", action="store_true", help="rebuild baseline from ledger after run")
    r.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                   help="samples per probe per run (default: 5)")
    r.add_argument("--since", default=None,
                   help="only use ledger runs at/after this ISO time when rebuilding baseline")

    c = sub.add_parser("check", help="compare recent runs against baseline "
                                     "(exit 2 = degraded, 3 = probe_failed, 4 = throttled)")
    c.add_argument("--max-age", default="7d",
                   help="ignore ledger records older than this (e.g. 7d, 24h, 30m, 0=none)")

    rp = sub.add_parser("report", help="markdown drift report of recent runs")
    rp.add_argument("--last", type=int, default=20)
    rp.add_argument("--output", help="write report to file instead of stdout")

    sub.add_parser("probes", help="list built-in canary probes")
    return ap


def main(argv: list | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    cmds = {
        "init": cmd_init,
        "run": cmd_run,
        "check": cmd_check,
        "report": cmd_report,
        "probes": cmd_probes,
    }
    return cmds[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
