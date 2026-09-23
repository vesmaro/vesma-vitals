"""Dynamism zone (phase B, zone 4) — is the context REALLY dynamic?

docs/methodology.md §3.4 (ArchCom `7ec9dda3` + `061398fe`, ADR-0026):

  context_dynamism_ratio = 1 - static_share
    static block := the SAME (memory_id, block fingerprint) pair appears
    in >= 80% of the session's assemblies (canon: "отпечаток в >= 80%
    сборок сессии"); static_share aggregates per-pair verdicts as the
    share of the session's block occurrences belonging to static pairs
    (what fraction of the injected material was static);
  dynamic_uniqueness = mean(1 - Jaccard(...)) over intra-session pairs
    of assemblies, pairs sampled deterministically for long sessions;
  explicit_hint_share — the precondition metric (recall.query_source);
  structural falsifier: the share of intra-session pairs with ZERO
    uniqueness (identical block sets) above a preregistered level
    => FAIL.

Storage resolution (the born-final contract):
  The five tables' column tuples are pinned by tests/test_canary_c1.py —
  no column may be added, so the per-block keyed-HMAC fingerprint is
  stored at WRITE time inside the existing ``injection_blocks.ccr_origin``
  TEXT JSON payload, extended from ``["hash", ...]`` to
  ``{"hashes": [...], "block_fp": "<hmac>"}``. The born-final pin covers
  COLUMNS (tests/test_canary_c1.py), not payload shapes; raw text still
  never enters the sidecar — only keyed-HMAC digests. This file READS
  both payload shapes (pre-phase-B rows carry the legacy array) so the
  analyzer tolerates a retention window straddling the upgrade.

Uniqueness approximation (v1, this phase):
  The canon computes Jaccard over HMAC word 5-shingles of the ASSEMBLED
  texts. The sidecar stores no assembled text (that is the point), and
  storing per-assembly shingle sets would blow the storage budget (a
  2000-token assembly is ~500 shingles). Per the budget-conscious
  resolution sanctioned for phase B, uniqueness is computed at the
  BLOCK level: an assembly is represented by its SET of block
  fingerprints, and pair uniqueness = 1 - Jaccard(block_fp_set_i,
  block_fp_set_j). Blocks joined by blank lines approximate the
  assembly's shingle set. This v1 block-set approximation must be
  REVALIDATED against text-shingle Jaccard in phase D (replay of real
  sessions) before any corridor on dynamic_uniqueness is trusted.

Determinism and privacy:
  - all report-time work reads ONLY the sidecar (sqlite), never raw
    text — raw text does not exist there;
  - pair sampling is deterministic per session: pairs are ranked by
    blake2b digests keyed on the session id and canonical pair
    ordinals — the same session always yields the same pairs, no RNG
    state anywhere;
  - session-scoped reads only; every failure degrades to a NO-DATA
    report — the analyzer must never raise into a caller that sits on
    a server path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from itertools import combinations
from typing import Any

logger = logging.getLogger("mnemos_vitals.dynamism")

#: Static-block threshold (canon §3.4): the (memory_id, block_fp) pair
#: must appear in >= 80% of the session's assemblies to count as static.
STATIC_SHARE_THRESHOLD = 0.80

#: Pair sampling: sessions with more assemblies than this are sampled.
PAIR_FULL_LIMIT = 20
#: Cap on sampled pairs (n <= 20 => the full C(n,2) is at most 190 pairs).
PAIR_SAMPLE_CAP = 190


@dataclass(frozen=True)
class _Assembly:
    """One sidecar assemble row, reduced to the dynamism inputs."""

    metrics_id: int
    query_source: str
    block_pairs: frozenset[tuple[str, str]]  # (memory_id, block_fp)


def _load_ccr_origin(raw: str | None) -> tuple[list[str], str | None]:
    """Tolerant reader for the ``ccr_origin`` payload.

    Returns (ccr_hashes, block_fp). Pre-phase-B rows carry a JSON array
    ``["hash", ...]`` (block_fp = None — excluded from block-set math);
    phase-B rows carry ``{"hashes": [...], "block_fp": "<hmac>"}``.
    Anything else (drift, garbage) degrades to no data, never an error.
    """
    if raw is None:
        return [], None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return [], None
    if isinstance(payload, dict):
        hashes = payload.get("hashes")
        block_fp = payload.get("block_fp")
        clean = [h for h in hashes if isinstance(h, str)] if isinstance(hashes, list) else []
        return clean, block_fp if isinstance(block_fp, str) else None
    if isinstance(payload, list):  # legacy pre-phase-B shape
        return [h for h in payload if isinstance(h, str)], None
    return [], None


def corridor_gate(
    report: dict[str, Any], *, min_dynamism: float | None, max_zero_share: float | None
) -> dict[str, Any]:
    """Corridor gate over a session report — thresholds are PARAMETERS.

    The preregistration (docs/experiments/s5-memory-value.md, phase B1
    lane) freezes the values at run time; there are NO library defaults
    — the library refuses to guess (methodology §1: thresholds are set
    after baseline, never before data).

    Statuses (verdict family — never blocks merges, methodology §1):
      NO-DATA  assemblies < 2 (no pairs can form) or a metric the gate
               reads (ratio / zero-share / explicit_hint_share) is
               undefined;
      FAIL     context_dynamism_ratio < min_dynamism OR
               zero_uniqueness_pair_share > max_zero_share;
      PASS     neither breach holds.
    """
    if min_dynamism is None or max_zero_share is None:
        raise ValueError("thresholds are mandatory parameters — preregister them first")
    ratio = report.get("context_dynamism_ratio")
    zero_share = report.get("zero_uniqueness_pair_share")
    hint = report.get("explicit_hint_share")
    if (
        int(report.get("assemblies") or 0) < 2
        or not isinstance(ratio, (int, float))
        or not isinstance(zero_share, (int, float))
        or hint is None  # the precondition metric must be PRESENT (0.0 is a value)
    ):
        return {"status": "NO-DATA", "reasons": ["assemblies < 2 or metrics undefined"]}
    reasons: list[str] = []
    if ratio < min_dynamism:
        reasons.append(f"context_dynamism_ratio {ratio:.4f} < min_dynamism {min_dynamism}")
    if zero_share > max_zero_share:
        reasons.append(
            f"zero_uniqueness_pair_share {zero_share:.4f} > max_zero_share {max_zero_share}"
        )
    return {"status": "FAIL", "reasons": reasons} if reasons else {"status": "PASS", "reasons": []}


def falsifier_level(report: dict[str, Any]) -> float | None:
    """The preregistered structural falsifier input: zero-pair share.

    Canon (translated): the share of intra-session pairs with zero
    uniqueness above the preregistered level => FAIL.
    ``None`` when no pairs exist (nothing can breach).
    """
    share = report.get("zero_uniqueness_pair_share")
    return float(share) if isinstance(share, (int, float)) else None


class DynamismAnalyzer:
    """Session-level dynamism report computed from the sidecar only.

    Non-fatal by contract: any read error degrades to a NO-DATA report,
    never an exception — the host may call this from a server path.
    """

    def __init__(self, store: Any) -> None:
        # store: MetricsStore — typed Any to keep this module import-light
        # and the read surface explicit (the store's own _conn()).
        self._store = store

    # ── Reads (sidecar-only, session-scoped) ──────────────────────────────

    def _read_assemblies(self, session: str, project: str | None) -> list[_Assembly]:
        conn = self._store._conn()  # same-package read plane, not the host contract
        if conn is None:
            raise RuntimeError("sidecar unavailable")
        if project is None:
            rows = conn.execute(
                "SELECT id, query_source FROM assemble_metrics WHERE session = ? ORDER BY ts, id",
                (session,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, query_source FROM assemble_metrics"
                " WHERE session = ? AND project = ? ORDER BY ts, id",
                (session, project),
            ).fetchall()
        out: list[_Assembly] = []
        for r in rows:
            block_rows = conn.execute(
                "SELECT memory_id, ccr_origin FROM injection_blocks WHERE metrics_id = ?",
                (r["id"],),
            ).fetchall()
            pairs: set[tuple[str, str]] = set()
            for b in block_rows:
                _, block_fp = _load_ccr_origin(b["ccr_origin"])
                if block_fp is not None:  # legacy rows (no fp) are excluded
                    pairs.add((str(b["memory_id"]), block_fp))
            out.append(_Assembly(int(r["id"]), str(r["query_source"]), frozenset(pairs)))
        return out

    # ── Report ────────────────────────────────────────────────────────────

    def session_report(self, session: str, *, project: str | None = None) -> dict[str, Any]:
        """Compute the dynamism report for ONE session. Never raises.

        All keys are always present; value keys carry ``None`` when the
        data does not support them (NO-DATA stays loud, never zero).
        """
        try:
            return self._session_report(session, project=project)
        except (sqlite3.Error, RuntimeError, ValueError, TypeError, KeyError) as exc:
            logger.warning("vitals: session_report degraded to NO-DATA: %s", exc)
            return {
                "session": session,
                "project": project,
                "assemblies": 0,
                "static_share": None,
                "context_dynamism_ratio": None,
                "explicit_hint_share": None,
                "pair_uniqueness_mean": None,
                "zero_uniqueness_pair_share": None,
                "pairs_sampled": 0,
                "sampling": "none",
                "status": "NO-DATA",
                "reasons": [f"analyzer degraded: {type(exc).__name__}"],
            }

    def _session_report(self, session: str, *, project: str | None) -> dict[str, Any]:
        assemblies = self._read_assemblies(session, project)
        n = len(assemblies)
        # key-set contract: EVERY report shape carries every key (OK reports
        # carry None placeholders for axes it cannot compute; NO-DATA/degraded
        # carry an empty reasons list) — the docstring promise is exact.
        report: dict[str, Any] = {
            "session": session,
            "project": project,
            "assemblies": n,
            "static_share": None,
            "context_dynamism_ratio": None,
            "explicit_hint_share": None,
            "pair_uniqueness_mean": None,
            "zero_uniqueness_pair_share": None,
            "pairs_sampled": 0,
            "sampling": "none",
            "static_pairs": None,
            "distinct_pairs": None,
            "reasons": [],
        }
        if n == 0:
            report["status"] = "NO-DATA"
            report["reasons"] = ["no assemblies recorded for the session"]
            return report

        # explicit_hint_share — precondition metric (canon §3.4).
        report["explicit_hint_share"] = (
            sum(1 for a in assemblies if a.query_source == "explicit") / n
        )

        if all(len(a.block_pairs) == 0 for a in assemblies):
            # No block fingerprints at all (pre-phase-B corpus): the
            # static and uniqueness axes are undefined — loud NO-DATA.
            report["status"] = "NO-DATA"
            report["reasons"] = ["no block fingerprints (pre-phase-B corpus)"]
            return report

        # ── static_share at the block level ──────────────────────────────
        # Per-pair rule (canon): a pair is static when it appears in
        # >= 80% of the session's assemblies. The session aggregate is
        # the share of BLOCK OCCURRENCES belonging to static pairs
        # (sum of static-pair counts / total block occurrences) — i.e.
        # what fraction of the session's injected material was static.
        counts: dict[tuple[str, str], int] = {}
        for a in assemblies:
            for pair in a.block_pairs:
                counts[pair] = counts.get(pair, 0) + 1
        static_counts = [c for p, c in counts.items() if c / n >= STATIC_SHARE_THRESHOLD]
        total_occurrences = sum(counts.values())
        if not total_occurrences:  # unreachable: the all-empty case returned above —
            # kept LOUD so a future edit cannot fabricate ratio=1.0 (§3.4)
            raise RuntimeError("static axis invariant broken: zero occurrences past guard")
        static_share = sum(static_counts) / total_occurrences
        report["static_share"] = static_share
        report["context_dynamism_ratio"] = 1.0 - static_share
        report["static_pairs"] = len(static_counts)
        report["distinct_pairs"] = len(counts)

        # ── pairwise uniqueness (block-set Jaccard, sampled) ─────────────
        sampled, total_pairs = self._sample_pairs(n, session)
        report["pairs_sampled"] = len(sampled)
        report["sampling"] = (
            "full" if len(sampled) == total_pairs else f"deterministic {len(sampled)}/{total_pairs}"
        )
        uniques: list[float] = []
        zero_pairs = 0
        for i, j in sampled:
            inter = len(assemblies[i].block_pairs & assemblies[j].block_pairs)
            union = len(assemblies[i].block_pairs | assemblies[j].block_pairs)
            if union == 0:  # both sides empty — excluded, not counted as 1.0
                continue
            jaccard = inter / union
            uniques.append(1.0 - jaccard)
            if jaccard == 1.0:
                zero_pairs += 1
        if uniques:
            report["pair_uniqueness_mean"] = sum(uniques) / len(uniques)
            report["zero_uniqueness_pair_share"] = zero_pairs / len(uniques)
        report["status"] = "OK"
        return report

    # ── Deterministic pair sampling ───────────────────────────────────────

    def _sample_pairs(self, n: int, session: str) -> tuple[list[tuple[int, int]], int]:
        """Deterministic pairwise sample: full for small sessions.

        n <= PAIR_FULL_LIMIT: all C(n,2) pairs. Above: at most
        PAIR_SAMPLE_CAP pairs chosen by a canonical deterministic order —
        pairs ranked by blake2b(f"{session}|i|j") digests, lowest first.
        No RNG state: the same session always yields the same pairs.
        Returns (ordered pairs, total pair count).
        """
        indexes = range(n)
        all_pairs = list(combinations(indexes, 2))
        # (n > PAIR_FULL_LIMIT) => len(all_pairs) >= C(21,2) = 210 > 190, so the
        # second clause is dead today; kept as a guard so a future limit change
        # (e.g. full up to 25) cannot silently overflow the sampled branch.
        if n <= PAIR_FULL_LIMIT or len(all_pairs) <= PAIR_SAMPLE_CAP:
            return all_pairs, len(all_pairs)
        seed = hashlib.blake2b(session.encode("utf-8"), digest_size=8).digest()

        # Seed only reorders, never drops; digest of seed+ordinals is a
        # stable total order across runs and processes.
        def pair_key(pair: tuple[int, int]) -> bytes:
            i, j = pair
            return hashlib.blake2b(seed + f"|{i}|{j}".encode("ascii"), digest_size=8).digest()

        ranked = sorted(all_pairs, key=pair_key)
        # Canonical output order (by ordinals) keeps downstream diffs stable.
        return sorted(ranked[:PAIR_SAMPLE_CAP]), len(all_pairs)
