# Canary Cloud — Design Document

**Status:** scaffold (pre-acceptance). Branch `canary-cloud`; `main` untouched.
**Context:** hosted web-app version of [model-canary](https://github.com/hahahahahahahahah6/model-canary)
(the local CLI drift sentinel). Target: Meta Global AI Developer Hackathon,
"Build for Your World" track — *paid API users cannot verify they are getting
the model they paid for.*

Validated with Claude (2026-10-21): topic A wins if reframed globally, with
BYO key, private rotating question bank, and the Meta model as a core
component rather than a peripheral judge.

---

## 1. Architecture

```
┌──────────────────────── BROWSER (user's machine) ────────────────────────┐
│                                                                          │
│  localStorage — BYO secrets, NEVER leave the browser:                    │
│    canary.provider_key   canary.meta_key   canary.baselines               │
│    canary.private_bank   canary.seed                                       │
│                                                                          │
│  ┌──────────────┐  ┌───────────────────┐  ┌────────────────────────┐      │
│  │ Settings UI  │  │ Probe Runner      │  │ Bank Generator         │      │
│  │ keys in,     │  │ fetch() →         │  │ fetch() → Meta Model   │      │
│  │ never out    │  │ provider's        │  │ API (Muse Spark) with  │      │
│  │              │  │ OpenAI-compatible │  │ the USER's key         │      │
│  │ Proof panel: │  │ endpoint, user's  │  │                        │      │
│  │ every out-   │  │ key in            │  │ → private probes,      │      │
│  │ bound fetch  │  │ Authorization     │  │    stored locally      │      │
│  │ logged       │  └────────┬──────────┘  └───────────┬────────────┘      │
│  └──────────────┘           │ probe results (NO key)  │                   │
│                             ▼                         │                   │
│                  ┌────────────────────────────────────▼──────────┐        │
│                  │ Verdict UI: baseline compare, style           │        │
│                  │ fingerprint, history                          │        │
│                  └───────────────────────────────────────────────┘        │
└──────────────────────────────┬───────────────────────────────────────────┘
                               │ HTTPS, keyless JSON only
                               ▼
┌──────────────────────── CANARY CLOUD SERVER ─────────────────────────────┐
│  GET  /api/health                    → {ok: true, version}                │
│  GET  /api/question-templates        → public generation prompt templates│
│  GET  /api/style-profiles            → public aggregate style profiles   │
│  POST /api/verdict                   → {baseline, runs} → verdict        │
│  (+ static hosting of the frontend)                                       │
│                                                                           │
│  INVARIANT: the server has NO endpoint that accepts, stores, or forwards │
│  API keys. Request bodies are schema-validated; any key-like field        │
│  (api_key, apikey, authorization, bearer, secret) → 400, loudly.          │
└───────────────────────────────────────────────────────────────────────────┘
```

The keyed network edges (browser → provider, browser → Meta) never touch our
origin. The unkeyed edge (browser → our server) never carries secrets. The
two graphs are disjoint by construction.

## 2. BYO key — data flow and proof chain

**Data flow.**
1. User pastes provider key + Meta key into Settings. `saveKeys()` writes
   them to `localStorage` under `canary.provider_key` / `canary.meta_key`.
2. Probe Runner reads the provider key from `localStorage` and puts it in the
   `Authorization` header of `fetch()` calls whose URL host is the
   user-configured provider base URL. The backend origin is never a target.
3. Bank Generator does the same with the Meta key against the Meta Model API
   base URL (`https://api.meta.ai/v1` by default, user-overridable).
4. `callVerdictApi()` POSTs `{baseline, runs}` to the backend. It reads no
   keys; a code comment and a unit test pin this.

**Proof chain** (how we *demonstrate* it, not just claim it):
1. **Static.** `grep -riE "api_key|apikey|bearer|authorization" backend/`
   returns nothing; this is a CI gate, not a hope.
2. **Schema fail-closed.** Every POST handler validates the body against a
   fixed schema and returns **400** on any key-like field. We would rather
   loudly refuse a misbehaving client than silently swallow a secret.
3. **Runtime proof panel.** The Settings UI logs every outbound `fetch` this
   session: timestamp, method, destination host, and purpose — with headers
   and bodies redacted. Users can cross-check in DevTools → Network.
4. **Demo.** The hackathon video shows the Network tab during a full run:
   keyed requests go to the provider/Meta hosts; our host sees only keyless
   JSON.

**Residual risk (stated openly):** XSS in our own frontend would break the
invariant. Mitigations: zero third-party scripts, strict Content-Security-Policy,
no `eval`/`innerHTML` on untrusted strings, and the proof panel itself.

## 3. Question generator interface

The server never generates questions (that needs a model + a key). It serves
**public generation prompt templates**; generation happens in the browser.

- `GET /api/question-templates` → `[{id, version, kind, template, output_schema, description}]`.
  Templates are versioned public goods, e.g. `identity-svg-v1`:
  > "Invent a NEW visual-identity probe: ask for a simple object drawn as SVG
  > with an unusual constraint, such that the correct output is checkable by
  > regex/shape rules. Return JSON: {prompt, kind:'identity', check_regex,
  > why_discriminative}."
- Client-side contract:
  ```
  generatePrivateProbe({template, seed, metaKey, metaBase}) -> Probe
  Probe = {id, prompt, kind, check_regex, generated_at, uses_left}
  ```
  The user's private `seed` (random per user, local only) is embedded in the
  generation prompt so banks differ per user. Returned JSON is
  schema-validated by `validate_probe()` (mirrored in `question_bank.py`
  for tests); invalid output is discarded, never retried blindly.
- The generator model is **Muse Spark** via the Meta Model API, using the
  user's own key (hackathon participants get $150 in Meta Model API credits).

## 4. Private rotation strategy

- **Per-user uniqueness:** seed = `crypto.randomUUID()` at first run, stored
  locally, embedded in every generation prompt. Two users never share a bank.
- **Rotation:** each scheduled run draws a fresh subset
  (`select_rotation(seed, bank, count)` — deterministic shuffle, stdlib
  `hashlib`). A question retires after `K` uses (default 3); retired
  questions are never re-served, so a gaming provider cannot learn the bank.
- **TTL:** generated banks expire after 30 days → re-generate. Stale banks
  are labeled in the UI.
- **Server blindness:** private questions never leave the browser. There is
  no upload endpoint; there will never be one.
- **Honest fallback:** without a Meta key, the app falls back to the built-in
  public folk probes (pelican SVG, candy arithmetic…) with a visible
  "public bank — weaker guarantee" badge. We do not pretend otherwise.

## 5. Style fingerprint scheme

Per-response feature vector, computed **client-side** (no key, no server):

| feature | why it discriminates |
|---|---|
| token/char ratio | tokenizer-shaped; strong family signal |
| length + sentence-length distribution | decoding/sampling habits |
| formatting rates (headers, bold, code fences, list style) | instruction-tuning fingerprint |
| hedging marker rate ("as an AI", "however", "it's important to note") | RLHF style |
| refusal/hedge templates | safety-tuning fingerprint |
| first-token distribution on fixed prompts | cheap, surprisingly stable |

- `GET /api/style-profiles` → public **aggregate** profiles per model family:
  `{family, mean_vector, variance, n_samples, built_at, built_with}`.
  Built **offline by us** (never from user data): we probe reference models,
  cluster, and publish the aggregates.
- Matching: cosine distance to family centroids in the browser. Ambiguous
  cases escalate to **Muse Spark as judge** (client-side, user key):
  "given sample A (known family X) and sample B, same family?" — this is the
  second core use of the Meta model.
- Privacy: v1 sends nothing back. (Future, opt-in only: contribute aggregate
  vectors. Off by default, and raw text never leaves.)

## 6. Where the Meta model is used

The hackathon rewards genuine, central use — ours is:

1. **Private question generation** (Muse Spark): the generator behind the
   rotating bank. This is the product's differentiator, not a bolt-on.
2. **Style judge** (Muse Spark): resolves ambiguous fingerprint matches.
3. **Offline profile construction**: reference style profiles built with Meta
   models; `built_with` is recorded in every profile for transparency.
4. **Demo**: the submission video runs the full loop on Meta models.

## 7. Build breakdown (assuming acceptance ~10/27; submit 11/04)

| window | work |
|---|---|
| 10/21–10/25 | scaffold + design (this branch); hackathon application |
| 10/27–10/29 | deploy keyless backend; template pipeline; offline style-profile build with Muse Spark |
| 10/30–11/01 | frontend probe runner; baselines in IndexedDB; verdict UI; proof panel |
| 11/02–11/03 | style fingerprint UI; bank rotation UX; demo script; record video (via web Claude per standing rule) |
| 11/04 | submit |

## 8. Risks & non-goals

- **CORS.** Browsers cannot call every provider API directly. We support
  OpenAI-compatible endpoints with CORS enabled and document a 20-line
  user-run relay (their machine, their key — still BYO) as fallback.
- **Event-window rules.** Many hackathons require work produced during the
  event. This scaffold is pre-work; all product code is written
  post-acceptance, and the application states exactly what is new.
- **Non-goals for v1:** user accounts, server-side scheduling, mobile app,
  community data sharing.
