"""Owner report: traffic light per family, PASS / FAIL / NO-DATA.

NO-DATA is never zero and never green. The generator exits 1 on breach —
it fails loud, it never edits thresholds. Verdicts never block merges;
invariants and corridors do (docs/methodology.md §1).
"""
