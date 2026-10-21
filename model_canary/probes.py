"""Built-in canary probe set.

Each probe is a small prompt with a known-good answer shape. The probes are
drawn from the folk "canary tests" that users invented to detect silent model
degradation (pelican-on-a-bicycle SVG, candy arithmetic, simple factual
anchors, instruction-following drills).

Hard expectations use `expected_regex` (anchored patterns), not substrings:
"12" must not pass the candy probe, and six blues must not pass the
five-blue drill.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Probe:
    id: str
    prompt: str
    kind: str  # identity | reasoning | factual | instruction
    # expected_substrings: legacy fallback; at least one must appear
    # (case-insensitive) for a PASS. Empty tuple = no hard expectation.
    expected_substrings: tuple = ()
    # expected_regex: anchored pattern the stripped output must match.
    # Preferred over expected_substrings; empty = unused.
    expected_regex: str = ""
    description: str = ""


PROBES: tuple = (
    Probe(
        id="identity-svg",
        prompt=(
            "Draw a pelican riding a bicycle as SVG. "
            "Output ONLY the SVG code, nothing else."
        ),
        kind="identity",
        expected_substrings=("<svg",),
        description=(
            "Community gold-standard canary: a healthy model of a given "
            "family produces a recognizably similar SVG; a silently swapped "
            "model produces a wildly different one."
        ),
    ),
    Probe(
        id="candy-arithmetic",
        prompt=(
            "If you have 3 candies and I take 1 away, how many candies are "
            "left? Answer with just the number, nothing else."
        ),
        kind="reasoning",
        expected_regex=r"^\s*2\s*\.?$",
        description="Trivial reasoning anchor; a degraded model fumbles even this.",
    ),
    Probe(
        id="instruction-repeat",
        prompt=(
            "Repeat the word 'blue' exactly five times, separated by commas. "
            "Output nothing else."
        ),
        kind="instruction",
        expected_regex=r"(?i)^\s*blue(\s*,\s*blue){4}\s*\.?\s*$",
        description="Instruction-following drill; degradation shows as extra chatter or wrong counts.",
    ),
    Probe(
        id="factual-anchor",
        prompt=(
            "How many days are in a leap year? "
            "Answer with just the number, nothing else."
        ),
        kind="factual",
        expected_regex=r"^\s*366\s*\.?\s*$",
        description=(
            "Timeless factual anchor (no knowledge-cutoff dependence): the "
            "signal is drift against your own baseline, not absolute correctness."
        ),
    ),
)


def get_probe(probe_id: str) -> Probe:
    for p in PROBES:
        if p.id == probe_id:
            return p
    raise KeyError(f"unknown probe: {probe_id}")


def list_probes() -> list:
    return list(PROBES)


def probe_map() -> dict:
    """probe_id -> probe dict (for detect's expectation lookup)."""
    return {
        p.id: {
            "expected_regex": p.expected_regex,
            "expected_substrings": p.expected_substrings,
        }
        for p in PROBES
    }
