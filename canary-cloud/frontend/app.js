/* Canary Cloud frontend — scaffold.
 *
 * BYO-KEY INVARIANT: keys are read from localStorage and attached ONLY to
 * requests aimed at the user's own provider / Meta endpoints. The function
 * callVerdictApi() below reads no keys at all (a backend unit test pins the
 * server side of this: any key-like field in a POST body -> 400).
 *
 * No third-party scripts. No eval. No innerHTML on untrusted strings.
 */
"use strict";

const LS = {
  providerBase: "canary.provider_base",
  providerKey: "canary.provider_key",
  metaBase: "canary.meta_base",
  metaKey: "canary.meta_key",
  baselines: "canary.baselines",
  bank: "canary.private_bank",
};

const FOLK_PROBES = [
  { id: "identity-svg", kind: "identity",
    prompt: "Draw a pelican riding a bicycle as SVG. Output ONLY the SVG code, nothing else." },
  { id: "candy-arithmetic", kind: "reasoning",
    prompt: "If you have 3 candies and I take 1 away, how many candies are left? Answer with just the number, nothing else." },
  { id: "instruction-repeat", kind: "instruction",
    prompt: "Repeat the word 'blue' exactly five times, separated by commas. Output nothing else." },
];

/* ---------- request proof log ---------- */
function logRequest(method, dest, purpose, keyed) {
  const li = document.createElement("li");
  const host = (() => { try { return new URL(dest).host; } catch { return dest; } })();
  li.innerHTML = "";
  const b = document.createElement("b");
  b.textContent = keyed ? "[KEYED → your host] " : "[keyless] ";
  const s = document.createElement("span");
  s.textContent = `${method} ${host} — ${purpose}`;
  li.append(b, s);
  li.className = keyed ? "keyed" : "plain";
  document.getElementById("reqLog").prepend(li);
}

/* Keyed fetch: ONLY ever called with user-configured provider/Meta URLs. */
async function keyedFetch(url, apiKey, purpose, body) {
  logRequest("POST", url, purpose, true);
  const t0 = performance.now();
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Authorization": "Bearer " + apiKey },
    body: JSON.stringify(body),
  });
  const ttft = (performance.now() - t0) / 1000;
  return { resp, ttft };
}

/* ---------- settings ---------- */
function loadSettings() {
  for (const [id, k] of [["providerBase", LS.providerBase], ["metaBase", LS.metaBase]]) {
    const v = localStorage.getItem(k);
    if (v) document.getElementById(id).value = v;
  }
  const hasKeys = !!(localStorage.getItem(LS.providerKey) || localStorage.getItem(LS.metaKey));
  setStatus(hasKeys ? "Keys present in this browser only." : "No keys stored yet.");
}

function setStatus(msg) {
  document.getElementById("keyStatus").textContent = msg;
}

document.getElementById("saveKeys").addEventListener("click", () => {
  const pb = document.getElementById("providerBase").value.trim();
  const pk = document.getElementById("providerKey").value;
  const mb = document.getElementById("metaBase").value.trim() || "https://api.meta.ai/v1";
  const mk = document.getElementById("metaKey").value;
  if (pb) localStorage.setItem(LS.providerBase, pb);
  if (pk) localStorage.setItem(LS.providerKey, pk);
  localStorage.setItem(LS.metaBase, mb);
  if (mk) localStorage.setItem(LS.metaKey, mk);
  // Never echo key material back into the DOM.
  document.getElementById("providerKey").value = "";
  document.getElementById("metaKey").value = "";
  setStatus("Saved to localStorage on this device. Nothing was sent anywhere.");
  logRequest("NONE", "(no network)", "keys saved locally — zero requests made", false);
});

document.getElementById("forgetKeys").addEventListener("click", () => {
  for (const k of [LS.providerKey, LS.metaKey]) localStorage.removeItem(k);
  setStatus("Keys forgotten.");
});

/* ---------- probe runner ---------- */
async function chatOnce(base, apiKey, prompt) {
  const { resp, ttft } = await keyedFetch(
    base.replace(/\/$/, "") + "/chat/completions", apiKey,
    "canary probe", {
      model: "auto", messages: [{ role: "user", content: prompt }],
      temperature: 0, max_tokens: 1500,
      stream_options: { include_usage: true },
    });
  const data = await resp.json();
  if (!resp.ok) {
    return { ok: false, http_status: resp.status, error_body: JSON.stringify(data).slice(0, 2000), text: "" };
  }
  const choice = (data.choices && data.choices[0]) || {};
  const text = (choice.message && choice.message.content) || "";
  const usage = data.usage || {};
  return { ok: true, text, ttft_s: ttft,
           completion_tokens: typeof usage.completion_tokens === "number" ? usage.completion_tokens : null };
}

async function callVerdictApi(baseline, runs) {
  // NOTE: reads no keys. Ever.
  logRequest("POST", "/api/verdict", "keyless verdict computation", false);
  const resp = await fetch("/api/verdict", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ baseline, runs }),
  });
  return resp.json();
}

function getBaselines() {
  try { return JSON.parse(localStorage.getItem(LS.baselines) || "{}"); }
  catch { return {}; }
}

document.getElementById("runProbes").addEventListener("click", async () => {
  const out = document.getElementById("results");
  out.textContent = "Running…";
  const base = localStorage.getItem(LS.providerBase);
  const key = localStorage.getItem(LS.providerKey);
  if (!base || !key) { out.textContent = "Set your provider base URL + key first (§1)."; return; }
  const baselines = getBaselines();
  const runs = [];
  for (const p of FOLK_PROBES) {
    try {
      const r = await chatOnce(base, key, p.prompt);
      runs.push(Object.assign({ probe_id: p.id, kind: p.kind }, r));
    } catch (e) {
      runs.push({ probe_id: p.id, kind: p.kind, ok: false, text: "", error_body: String(e).slice(0, 500) });
    }
  }
  const baseline = { probes: FOLK_PROBES.map(p => {
    const b = baselines[p.id];
    return b || { probe_id: p.id };
  }) };
  const firstRun = FOLK_PROBES.every(p => !baselines[p.id]);
  const verdict = await callVerdictApi(baseline, runs);
  if (firstRun) {
    // Seed baselines from this run (scaffold: single-sample; production uses n>=5).
    const nb = {};
    for (const r of runs) {
      if (!r.ok) continue;
      nb[r.probe_id] = { probe_id: r.probe_id, sample_text: r.text,
        sim_threshold: 0.55, ttft_median: r.ttft_s || 0.5,
        len_median: r.text.trim().length,
        tok_ratio_median: r.completion_tokens != null ? r.completion_tokens / Math.max(r.text.trim().length, 1) : null };
    }
    localStorage.setItem(LS.baselines, JSON.stringify(nb));
  }
  out.textContent = "";
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(verdict, null, 2);
  out.append(pre);
  const p = document.createElement("p");
  p.className = "note";
  p.textContent = firstRun
    ? "First run: baselines seeded locally (scaffold uses 1 sample; production requires ≥5)."
    : "Compared against your local baselines. Raw texts never left your browser except to your provider.";
  out.append(p);
});

/* ---------- private question generation (Meta, BYO) ---------- */
document.getElementById("genQuestions").addEventListener("click", async () => {
  const out = document.getElementById("results");
  out.textContent = "Generating…";
  const mb = localStorage.getItem(LS.metaBase) || "https://api.meta.ai/v1";
  const mk = localStorage.getItem(LS.metaKey);
  if (!mk) { out.textContent = "Set your Meta Model API key first (§1)."; return; }
  logRequest("GET", "/api/question-templates", "fetch public generation templates", false);
  const tplResp = await fetch("/api/question-templates");
  const { templates } = await tplResp.json();
  let seed = localStorage.getItem("canary.seed");
  if (!seed) {
    seed = crypto.randomUUID();
    localStorage.setItem("canary.seed", seed);
  }
  const bank = [];
  for (const t of templates.slice(0, 2)) {  // scaffold: 2 templates
    const prompt = t.template.replace("{seed}", seed);
    try {
      const { resp } = await keyedFetch(mb.replace(/\/$/, "") + "/chat/completions",
        mk, "private question generation (Muse Spark)",
        { model: "muse-spark", messages: [{ role: "user", content: prompt }],
          temperature: 0.7, max_tokens: 800 });
      const data = await resp.json();
      const text = ((data.choices || [])[0] || {}).message?.content || "";
      const m = text.match(/\{[\s\S]*\}/);
      if (!m) continue;
      const probe = JSON.parse(m[0]);
      if (probe.prompt && probe.kind) {
        probe.id = "private-" + t.id + "-" + seed.slice(0, 8);
        probe.uses_left = 3;
        bank.push(probe);
      }
    } catch { /* skip failed generations */ }
  }
  localStorage.setItem(LS.bank, JSON.stringify(bank));
  out.textContent = "";
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify({ generated: bank.length, bank }, null, 2);
  out.append(pre);
});

loadSettings();
