"""Prometheus hybrid exposition (phase A2).

Counters mnemos_verb_calls_total{surface,verb,status} and p50/p95 quantiles
from the hourly rollup only (survives restarts); volume gauges stay sourced
from the host dashboard_stats(). avg is removed — means hide tails.

Precondition: mnemos #249 (_METRICS_BYPASS) must be fixed BEFORE any new
exposition ships. Public exposition: zero endpoint/project/detector labels
on security metrics (RL-S2); per-principal is forbidden everywhere.
"""
