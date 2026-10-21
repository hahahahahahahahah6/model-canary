"""model-canary CLI: init / run / check / report / probes."""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

from . import __version__, detect, ledger, probes, provider

DEFAULT_DIR = ".model-canary"
CONFIG_NAME = "config.json"
BASELINE_NAME = "baseline.json"
LEDGER_NAME = "ledger.jsonl"


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


def _run_probe(cfg: dict, probe: probes.Probe) -> dict:
    api_key = os.environ.get(cfg["api_key_env"], "")
    if not api_key:
        return {
            "probe_id": probe.id,
            "kind": probe.kind,
            "status": "probe_failed",
            "error": f"env var {cfg['api_key_env']} is not set",
            "ts": _now(),
        }
    try:
        m = provider.chat_once(
            cfg["endpoint"], api_key, cfg["model"], probe.prompt,
            timeout=cfg.get("timeout_s", 60.0),
        )
    except provider.ProbeError as exc:
        return {
            "probe_id": probe.id,
            "kind": probe.kind,
            "status": "probe_failed",
            "error": str(exc)[:200],
            "error_body": exc.body[:2000],
            "http_status": exc.http_status,
            "ts": _now(),
        }
    text = m["text"]
    return {
        "probe_id": probe.id,
        "kind": probe.kind,
        "status": "ok",
        "model": cfg["model"],
        "fingerprint": detect.fingerprint(text),
        "text": text[:4000],
        "expected_substrings": list(probe.expected_substrings),
        "ttft_s": m["ttft_s"],
        "latency_s": m["latency_s"],
        "output_chars": m["output_chars"],
        "prompt_tokens": m["prompt_tokens"],
        "completion_tokens": m["completion_tokens"],
        "total_tokens": m["total_tokens"],
        "http_status": m["http_status"],
        "ts": _now(),
    }


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_run(args: argparse.Namespace) -> int:
    p = _paths(args.dir)
    cfg = _load_config(p)
    wanted = set(args.probe) if args.probe else set(cfg.get("probes", []))
    results = []
    for pr in probes.list_probes():
        if pr.id not in wanted:
            continue
        rec = _run_probe(cfg, pr)
        ledger.append_run(p["ledger"], rec)
        results.append(rec)
        mark = "OK " if rec["status"] == "ok" else "FAIL"
        print(f"[{mark}] {pr.id}: {rec.get('output_chars', 0)} chars, "
              f"ttft={rec.get('ttft_s', '?')}s" +
              ("" if rec["status"] == "ok" else f" ({rec.get('error')})"))
    if args.baseline or ledger.load_baseline(p["baseline"]) is None:
        runs = ledger.read_runs(p["ledger"])
        base = ledger.build_baseline(runs)
        if base:
            ledger.save_baseline(p["baseline"], base)
            print(f"baseline written ({sum(v['n'] for v in base.values())} runs)")
        else:
            print("no successful runs yet; baseline not written")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    p = _paths(args.dir)
    _load_config(p)  # validates init
    base = ledger.load_baseline(p["baseline"])
    if base is None:
        print("no baseline yet; run `model-canary run` first")
        return 1
    runs = ledger.read_runs(p["ledger"])
    if not runs:
        print("ledger is empty")
        return 1
    # Check the most recent run of each probe.
    latest: dict = {}
    for r in runs:
        latest[r["probe_id"]] = r
    all_findings: list = []
    for pid, run in sorted(latest.items()):
        findings = detect.detect(run, base)
        for f in findings:
            all_findings.append(f)
            print(f"[{f['severity'].upper():6}] {f['kind']:17} {pid}: {f['detail']}")
    verdict = detect.summarize(all_findings)
    print(f"verdict: {verdict['verdict']}")
    if verdict["verdict"] == "degraded":
        return 2
    if verdict["verdict"] == "probe_failed":
        return 3
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    p = _paths(args.dir)
    runs = ledger.read_runs(p["ledger"])
    base = ledger.load_baseline(p["baseline"])
    if not runs:
        print("ledger is empty")
        return 1
    runs = runs[-args.last :]
    lines = ["# model-canary report", ""]
    lines.append("| time | probe | status | chars | ttft | tokens | drift |")
    lines.append("|---|---|---|---|---|---|---|")
    total_tokens = 0
    degraded = 0
    for r in runs:
        findings = detect.detect(r, base) if base else []
        kinds = ",".join(sorted({f["kind"] for f in findings if f["kind"] != "probe_failed"})) or "-"
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

    sub.add_parser("check", help="compare latest runs against baseline (exit 2 = degraded)")

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
