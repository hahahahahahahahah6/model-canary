"""Built-in canary probe set.

Each probe is a small prompt with a known-good answer shape. The probes are
drawn from the folk "canary tests" that users invented to detect silent model
degradation (pelican-on-a-bicycle SVG, candy arithmetic, simple factual
anchors, instruction-following drills).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Probe:
    id: str
    prompt: str
    kind: str  # identity | reasoning | factual | instruction
    # expected_substrings: at least one must appear (case-insensitive) for a PASS.
    # Empty tuple = no hard expectation (fingerprint drift is the signal).
    expected_substrings: tuple = ()
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
        expected_substrings=("2",),
        description="Trivial reasoning anchor; a degraded model fumbles even this.",
    ),
    Probe(
        id="instruction-repeat",
        prompt=(
            "Repeat the word 'blue' exactly five times, separated by commas. "
            "Output nothing else."
        ),
        kind="instruction",
        expected_substrings=("blue, blue, blue, blue, blue",),
        description="Instruction-following drill; degradation shows as extra chatter or wrong counts.",
    ),
    Probe(
        id="factual-anchor",
        prompt=(
            "Who is the current Prime Minister of Japan? "
            "Answer with just the name, nothing else."
        ),
        kind="factual",
        expected_substrings=(),
        description=(
            "Factual anchor with no hard expectation: the signal is drift "
            "against your own baseline, not absolute correctness."
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
