"""Public aggregate style profiles per model family.

These are *aggregate statistics* (mean feature vectors + variance), built
offline by us by probing reference models. They contain no user data and
are safe to serve publicly.

FEATURES (client computes the same vector per response, no key needed):
  0 token_char_ratio      completion_tokens / max(len(text),1)
  1 hedging_rate          hedging markers per 1000 chars
  2 markdown_rate         markdown structural chars per 1000 chars
  3 list_rate             list-item lines per 100 lines
  4 avg_sentence_len      mean chars per sentence

STATUS: illustrative placeholders. The real profiles are built offline
post-acceptance with Muse Spark (see docs/DESIGN.md §5); each profile then
carries built_with / built_at / n_samples provenance.
"""
from __future__ import annotations

FEATURE_NAMES = (
    "token_char_ratio",
    "hedging_rate",
    "markdown_rate",
    "list_rate",
    "avg_sentence_len",
)

# Placeholder aggregates -- replaced by the offline build before launch.
STYLE_PROFILES: tuple = (
    {
        "family": "example-family-a",
        "placeholder": True,
        "mean_vector": [0.25, 1.2, 8.5, 3.0, 95.0],
        "variance": [0.01, 0.4, 4.0, 2.0, 400.0],
        "n_samples": 0,
        "built_with": "placeholder",
        "built_at": None,
    },
    {
        "family": "example-family-b",
        "placeholder": True,
        "mean_vector": [0.31, 4.8, 22.0, 9.0, 70.0],
        "variance": [0.01, 1.2, 9.0, 5.0, 300.0],
        "n_samples": 0,
        "built_with": "placeholder",
        "built_at": None,
    },
)


def get_profiles() -> list:
    return [dict(p) for p in STYLE_PROFILES]


def cosine_distance(a: list, b: list) -> float:
    """1 - cosine similarity. Pure function; also used client-side."""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 1.0
    return 1.0 - dot / (na * nb)


def nearest_family(vector: list) -> dict:
    """Return {family, distance} of the closest reference profile."""
    best = None
    for p in STYLE_PROFILES:
        d = cosine_distance(vector, p["mean_vector"])
        if best is None or d < best["distance"]:
            best = {"family": p["family"], "distance": d,
                    "placeholder": p["placeholder"]}
    return best
