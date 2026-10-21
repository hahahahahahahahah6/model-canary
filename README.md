# model-canary

A degradation watchdog for LLM APIs. Your provider can silently swap, throttle, or degrade the model behind your API key — and nothing tells you. `model-canary` periodically probes your endpoint with community-standard canary prompts, records output fingerprints, time-to-first-token, length, and token usage into an append-only ledger, and alerts when the model appears degraded.

Zero dependencies. Python 3.9+. Your API key never touches disk.

## The problem

In October 2026, Chinese AI communities went through repeated waves of "degradation panic": users on paid tiers discovered their models had been quietly swapped or throttled — no notice, no dashboard indicator. Users invented their own detection rituals: drawing a pelican riding a bicycle as SVG, candy-counting questions, "who is the prime minister of Japan" fingerprint tests, checking support chats for hidden flags. One-off fingerprint sites exist, but nobody was doing **continuous** canary monitoring linked to quota burn. That's what this does.

## Quickstart

```bash
pip install model-canary

export MODEL_CANARY_API_KEY="sk-..."   # never written to disk

model-canary init --endpoint https://api.openai.com/v1 --model gpt-6.1-sol
model-canary run        # runs 4 canary probes, writes baseline on first run
model-canary check      # exit 0 = ok, 2 = degraded, 3 = probe infra failure
model-canary report     # markdown drift report of recent runs
```

Works with any OpenAI-compatible endpoint (OpenAI, relays, DeepSeek-compatible, local servers).

Put `run` + `check` on a cron schedule (hourly is plenty) and have `check` page you when it exits 2.

## Canary probes

| probe | kind | what it catches |
|---|---|---|
| `identity-svg` | identity | Pelican-on-a-bicycle SVG — the community gold standard. A silently swapped model draws a recognizably different picture (fingerprint drift). |
| `candy-arithmetic` | reasoning | "3 candies, take 1" — a degraded model fumbles even trivial reasoning. |
| `instruction-repeat` | instruction | Repeat "blue" five times — catches instruction-following decay. |
| `factual-anchor` | factual | Stable factual question — signal is drift against *your* baseline, not absolute correctness. |

## Detection rules

Against your baseline, `check` reports:

- **fingerprint_drift** (high) — identity-probe output similarity to baseline below 0.55: the model was probably swapped.
- **quality_drift** (medium) — a probe with a known-good answer now fails it.
- **error_signal** (high) — the provider returned a known throttle/degradation marker (`model is at capacity`, `subscription_sharing_user_not_eligible`, rate-limit strings, …).
- **latency_drift** (low) — TTFT more than 3× the baseline median.
- **length_drift** (low) — output length drifted more than 60% from baseline.

Transport failures are recorded as `probe_failed` and are **never** reported as degradation — a dead network is not a degraded model (fail-open on alerting).

Every run also logs token usage, so `report` shows exactly what the watchdog itself costs you (quota linkage).

## Commands

```
model-canary init --endpoint URL --model ID [--api-key-env NAME] [--timeout S]
model-canary run [--probe ID] [--baseline]   # --baseline rebuilds baseline from ledger
model-canary check                            # exit codes: 0 ok / 2 degraded / 3 infra failure
model-canary report [--last N] [--output FILE]
model-canary probes                           # list built-in probes
```

State lives in `./.model-canary/` (override with `--dir`): `config.json`, `baseline.json`, `ledger.jsonl`.

## How it differs

- **vs one-off fingerprint sites** (e.g. ModelTrace): those answer "which model is this right now". model-canary answers "has my model changed since yesterday, and how much quota did the check cost".
- **vs model-side evaluators** (e.g. Laminar): those detect silent agent failures from the inside. This watches the provider from the outside, with the user's own key, on the user's own schedule.

## Limitations

- Probes cost tokens on every run — keep the cadence sane (hourly, not minutely).
- Fingerprint heuristics are statistical, not proofs; treat alerts as "go look", not verdicts.
- A provider that degrades *everyone* identically won't trip the identity probe — that's what the reasoning/instruction probes are for.

## License

MIT
