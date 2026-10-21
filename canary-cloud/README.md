# Canary Cloud — scaffold

Hosted web-app version of [model-canary](https://github.com/hahahahahahahahah6/model-canary).
Target: Meta Global AI Developer Hackathon, "Build for Your World".

**Status: scaffold only** (branch `canary-cloud`; `main` untouched). Built
pre-acceptance to nail the architecture; the full product is built
post-acceptance. See [docs/DESIGN.md](docs/DESIGN.md) for the full design.

## The one invariant

**Your API keys never leave your browser.** The backend (`backend/app.py`)
has no endpoint that accepts keys — a key-like field in any request body
gets a loud `400`. Keys live in `localStorage`; keyed requests go only to
*your* provider / Meta endpoints. The Settings page has a live Proof panel
logging every outbound fetch.

## Layout

```
canary-cloud/
  docs/DESIGN.md          # architecture, BYO proof chain, build plan
  backend/
    app.py                # stdlib HTTP server, keyless JSON API (+ static frontend)
    verdict.py            # drift verdict engine (ported from model-canary core)
    question_bank.py      # public generation templates, rotation, probe schema
    style_profiles.py     # public aggregate style profiles per model family
    tests/test_backend.py # 21 tests, stdlib unittest
  frontend/
    index.html            # settings (BYO keys), probe runner, proof panel
    app.js                # no third-party scripts, no eval
    styles.css
```

## Run it

```bash
cd canary-cloud/backend
python3 app.py            # http://127.0.0.1:8077  (API + frontend)
python3 -m unittest discover -s tests -v   # backend tests
```

Open http://127.0.0.1:8077, paste your provider + Meta keys (§1 — they stay
in localStorage), run probes (§2), watch the Proof panel (§3).

## What's stubbed for post-acceptance

- Style profiles are placeholders; the real aggregates get built offline
  with Muse Spark (provenance recorded per profile).
- Baselines seed from 1 sample in the scaffold; production requires ≥5
  (mirrors the CLI's anti-pollution rule).
- No IndexedDB history charts, no scheduled runs, no opt-in aggregate
  contribution — all roadmap, see DESIGN.md §7.
