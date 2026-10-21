"""Public question-bank templates for Canary Cloud.

The server never generates questions (that needs a model + a user key).
It serves versioned *generation prompt templates*; generation happens in
the browser with the user's own Meta API key (BYO). Private questions
never leave the browser -- there is no upload endpoint and never will be.
"""
from __future__ import annotations

import hashlib
import re

# A template tells the generator model (Muse Spark) how to invent a NEW
# private canary probe. Templates are public goods; the generated probes
# are per-user private.
QUESTION_TEMPLATES: tuple = (
    {
        "id": "identity-svg-v1",
        "version": 1,
        "kind": "identity",
        "description": (
            "Invent a NEW visual-identity probe in the spirit of the folk "
            "'pelican on a bicycle' canary: ask for a simple object drawn "
            "as SVG under an unusual constraint, with a checkable shape."
        ),
        "template": (
            "You invent canary probes that detect silent LLM swaps. "
            "Invent ONE new 'identity' probe: ask the model under test to "
            "draw a simple, specific object as inline SVG with one unusual "
            "constraint (e.g. only <circle> elements, or a limited palette). "
            "The correct output must be checkable by a regex or by counting "
            "SVG elements. User seed (make this probe unique): {seed}. "
            "Return JSON ONLY: {{\"prompt\": \"...\", \"kind\": \"identity\", "
            "\"check_regex\": \"...\", \"why_discriminative\": \"...\"}}. "
            "No prose, no markdown fences."
        ),
        "output_schema": ["prompt", "kind", "check_regex",
                          "why_discriminative"],
    },
    {
        "id": "reasoning-anchor-v1",
        "version": 1,
        "kind": "reasoning",
        "description": (
            "Invent a NEW trivial-reasoning anchor: a question any healthy "
            "model answers identically, with an exact-match check."
        ),
        "template": (
            "You invent canary probes that detect silent LLM degradation. "
            "Invent ONE new 'reasoning' probe: a trivial question (arithmetic, "
            "counting, or ordering) whose correct answer is a single short "
            "string, checkable by exact regex. It must be so easy that any "
            "non-degraded model answers it identically. "
            "User seed (make this probe unique): {seed}. "
            "Return JSON ONLY: {{\"prompt\": \"...\", \"kind\": \"reasoning\", "
            "\"check_regex\": \"...\", \"why_discriminative\": \"...\"}}. "
            "No prose, no markdown fences."
        ),
        "output_schema": ["prompt", "kind", "check_regex",
                          "why_discriminative"],
    },
    {
        "id": "instruction-drill-v1",
        "version": 1,
        "kind": "instruction",
        "description": (
            "Invent a NEW instruction-following drill: an exact-format "
            "task where deviations reveal sampling/behavior drift."
        ),
        "template": (
            "You invent canary probes that detect silent LLM degradation. "
            "Invent ONE new 'instruction' probe: ask the model under test to "
            "repeat/emit an exact short string with precise formatting rules "
            "(separator, count, casing). Any deviation reveals drift. "
            "User seed (make this probe unique): {seed}. "
            "Return JSON ONLY: {{\"prompt\": \"...\", \"kind\": \"instruction\", "
            "\"check_regex\": \"...\", \"why_discriminative\": \"...\"}}. "
            "No prose, no markdown fences."
        ),
        "output_schema": ["prompt", "kind", "check_regex",
                          "why_discriminative"],
    },
    {
        "id": "style-probe-v1",
        "version": 1,
        "kind": "style",
        "description": (
            "Invent a NEW open-ended style probe: a prompt whose answer "
            "reveals family-level stylistic habits (formatting, hedging, "
            "structure) for the style-fingerprint matcher."
        ),
        "template": (
            "You invent canary probes that reveal a model's stylistic "
            "fingerprint. Invent ONE new open-ended prompt (explain a simple "
            "concept, or give advice on a mundane topic) where different "
            "model families are known to differ in formatting habits, "
            "hedging, and structure. "
            "User seed (make this probe unique): {seed}. "
            "Return JSON ONLY: {{\"prompt\": \"...\", \"kind\": \"style\", "
            "\"check_regex\": \"\", \"why_discriminative\": \"...\"}}. "
            "No prose, no markdown fences."
        ),
        "output_schema": ["prompt", "kind", "check_regex",
                          "why_discriminative"],
    },
)


def get_templates() -> list:
    """Return the public template list (safe to serve to anyone)."""
    return [dict(t) for t in QUESTION_TEMPLATES]


def get_template(template_id: str) -> dict | None:
    for t in QUESTION_TEMPLATES:
        if t["id"] == template_id:
            return dict(t)
    return None


def validate_probe(obj: object) -> tuple[bool, str]:
    """Schema-check a client-generated probe. (bool, reason)."""
    if not isinstance(obj, dict):
        return False, "probe must be a JSON object"
    for field in ("prompt", "kind"):
        if not isinstance(obj.get(field), str) or not obj[field].strip():
            return False, f"missing or empty field: {field}"
    if obj["kind"] not in ("identity", "reasoning", "instruction", "style"):
        return False, f"unknown kind: {obj['kind']!r}"
    rx = obj.get("check_regex", "")
    if rx:
        try:
            re.compile(rx)
        except re.error as exc:
            return False, f"invalid check_regex: {exc}"
    return True, "ok"


def select_rotation(seed: str, bank: list, count: int) -> list:
    """Deterministically pick `count` probe ids for this run.

    Same seed + same bank -> same order, so rotation is stable per user
    without any server state. Uses SHA-256, stdlib only.
    """
    if not bank or count <= 0:
        return []
    scored = []
    for i, probe in enumerate(bank):
        pid = probe.get("id", str(i))
        h = hashlib.sha256(f"{seed}:{pid}".encode()).hexdigest()
        scored.append((h, pid))
    scored.sort()
    return [pid for _, pid in scored[:count]]
