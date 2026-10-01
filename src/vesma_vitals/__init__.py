"""vesma-vitals — honest measurement methodology and instrumentation
for the Mnemos memory server.

Two circuits:
  Health — every server function: called / succeeded / latency / errors.
  Value   — only promised functions; causal value proved by the two-arm
            S5 replay stand ("saved X tokens" from passive data is banned).

Canon: docs/methodology.md. Architecture: docs/architecture.md.
"""

__version__ = "0.0.1"

#: Metric family registry (ADR-0026 mnemos + addendum 061398fe).
#: One value — one name — one family; family = theme or one falsifiable
#: owner question, never a tier.
FAMILY_REGISTRY = {
    "F2": "Accuracy / quality (owner family, mnemos ADR-0020)",
    "F3": "Token economy (compression + redemption)",
    "F5": "Session coherence",
    "F6": "Availability",
    "F7": "Extensibility",
    "F8": "Memory value (core decision 7ec9dda3)",
    "F9": "Subsystem health (addendum 061398fe; health-only forever)",
}

#: Status taxonomy — the honesty core.
STATUS_TAXONOMY = {
    "invariant": "mechanical fact (= 1.000 / = 0); always blocks merges",
    "corridor": "regression vs own baseline - max(0.02; CI95); blocks",
    "verdict": "value threshold; NEVER blocks; PASS / FAIL / NO-DATA",
}
