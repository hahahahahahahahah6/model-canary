"""model-canary: a degradation watchdog for LLM APIs.

Periodically probes a chat-completions endpoint with community-standard
canary prompts, records output fingerprints / TTFT / length / token usage
into an append-only ledger, and alerts when the model appears silently
degraded, swapped, or throttled.
"""

__version__ = "0.1.0"
