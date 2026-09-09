"""S5 'memory-value' stand — two-arm replay (phase B).

Arms: memory-on / memory-off on an isolated store copy (SQLite backup API).
Comparator legs: B0-naive (full transcript, headline), B0-file (static curated
file of equal budget), B0-full (reference only). The 'saved X' headline is
valid only at task_success(M) >= task_success(B0-naive).

v1 is synthetic-only; real sessions (phase D) require per-capture opt-in and
fail-closed sanitisation at the capture boundary.
"""
