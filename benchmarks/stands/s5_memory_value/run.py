#!/usr/bin/env python
"""S5 'memory-value' stand — two-arm replay runner (phase B, zone 5).

Usage:

    python benchmarks/stands/s5_memory_value/run.py --workload <tape.jsonl>
    python benchmarks/stands/s5_memory_value/run.py --db <mnemos store.sqlite>
    python benchmarks/stands/s5_memory_value/run.py --freeze-thresholds \
        --thresholds delta_task_success=0.10,min_dynamism=...,max_zero_uniqueness_share=...

Implements the FROZEN pre-registration
(``docs/experiments/s5-memory-value.md``): the owner question «how many
tokens do we ACTUALLY save through deliberate context assembly?» is
answered only by a two-arm replay with symmetric comparator legs:

  M (memory-on)        writes go through the memory store; every query is
                       served by ``assemble_context`` semantics — the
                       window is assembled from retrieved blocks;
  B0-naive             the FULL transcript so far (writes + hints), no
                       memory — the headline baseline;
  B0-file              a static curated file sized to M's MEAN assembled
                       token count (equal-budget comparator, «killer of
                       pretty words») — plain file OUTSIDE any store,
                       recipe printed in the report;
  B0-full              ALL history (reference only, overestimates
                       savings; excluded from verdicts).

Thresholds stay PARAMETERS (``--thresholds`` / ``--freeze-thresholds``):
they freeze after the baseline run, never before data (methodology §1.3).
A baseline run stamps ``thresholds: unfrozen`` into the report and exits
0 even on breaches; a frozen run exits 1 when any frozen threshold
breaches (the generator alarms, it does not edit). H1-H3 are measured
against PREREGISTERED constants (δ = 10 пп MDE mirror) at any run state;
only the H4 corridor thresholds (min_dynamism, max_zero_uniqueness_share)
have an UNFROZEN state.

Honesty (pre-registration §Честность отчёта):
  - every number traces to a measured task in the workload;
  - comparator legs are symmetric: the same task list, ONE token
    estimator for all legs, one success definition;
  - NO wall-clock value enters any metric — logical turn index only;
  - NO-DATA is loud: never green, never zero;
  - the store under test is read-only for the runner: with ``--db`` the
    source is cloned once via the SQLite backup API (S4 precedent) and
    the M arm recalls over the CLONE; stand writes are always
    ``actor=benchmark``-marked rows in the arm's own s5_* tables — the
    source's tables are never written;
  - raw text never enters the sidecar; the B0-file curated file lives
    outside any store (recipe + measured size printed in the report);
  - the savings headline is valid ONLY at task_success(M) >=
    task_success(B0-naive) - delta (H2 mandate) — at ANY thresholds
    state, including the unfrozen baseline; otherwise the report prints
    "SAVINGS HEADLINE INVALID (H2)" and net_savings is reported
    informationally only.

Determinism: one run = one logical clock stamp (``LOGICAL_CLOCK``),
fixed BLAKE2b bootstrap streams, no wall-clock anywhere in the report;
two runs of the same workload and flags emit byte-identical JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from statistics import median
from typing import Any

STAND_ROOT = Path(__file__).resolve().parent
REPO_ROOT = STAND_ROOT.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.stands.s5_memory_value.workload import (  # noqa: E402
    Workload,
    WorkloadError,
    load_workload,
    synthetic_workload,
    workload_summary,
    write_workload,
)
from mnemos_vitals.dynamism import DynamismAnalyzer, corridor_gate  # noqa: E402
from mnemos_vitals.sink import MetricsStore  # noqa: E402

STAND_VERSION = "s5-2"

#: Legs — the preregistered comparator set; removing one post-factum is
#: not allowed (symmetric reporting), so all legs always run.
LEGS: tuple[str, ...] = ("M", "B0-naive", "B0-file", "B0-full")

#: Session id / project stamped on every sidecar row the M arm produces.
PROJECT = "s5-memory-value"
AGENT = "benchmark"

#: Fixed logical clock for the report (determinism: no wall-clock in
#: metrics; run metadata carries a CONSTANT stamp, tests pin byte-stable
#: reports).
LOGICAL_CLOCK = "logical-clock-v1"

#: Token estimator: ONE estimator for all legs (symmetry — swapping it
#: per-leg would rig the comparison). ``ESTIMATOR`` names the heuristic
#: so the report carries what was measured WITH what.
ESTIMATOR = "words-and-chars-per-4-v1"

#: Replay-fp-rate proxy threshold (v1): a served block counts as
#: relevant when its retrieval score >= 0.35 (pre-registration §Метрики:
#: relevance = share of tasks whose used blocks the judge confirmed; the
#: v1 proxy replaces the judge with the retrieval score, task-level rule:
#: ALL served blocks of the task must clear the proxy). NOT a verdict
#: threshold — a measured-definition constant.
FP_SCORE_PROXY = 0.35

#: Bootstrap resamples for CI95 (pre-registration: 10 000, session-
#: stratified cluster bootstrap; draws from a fixed blake2b stream —
#: no RNG state). Engineering runs may pass fewer via run_s5(); the CLI
#: always uses the preregistered 10 000.
BOOTSTRAP_RESAMPLES = 10_000

#: Preregistered δ for H2/H3 (MDE mirror, 10 percentage points) — a
#: CONSTANT of the frozen pre-registration, not a to-be-frozen threshold.
PREREG_DELTA = 0.10

#: Default M-arm window budget for assemble (tokens) — a parameter, not
#: a threshold; the default workload's median window stays far below it.
DEFAULT_BUDGET = 1200

DEFAULT_WORKLOAD_PATH = STAND_ROOT / "workloads" / "default.jsonl"
DEFAULT_REPORT_DIR = REPO_ROOT / "reports" / "local"
CURATED_FILE_NAME = "b0file-curated.txt"


# ── Token estimator (ONE for all legs) ───────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Deterministic token estimate: max(words, ceil(chars / 4)).

    Documented heuristic (runner-wide, symmetric, ONE for all legs):
    whitespace-word count floored by a chars/4 count — a cheap lower-
    bound pair robust for ASCII prose (real BPE counts sit between the
    two). It never sees a model tokenizer; what is measured is the
    ESTIMATE, identically for every leg. Any estimator swap must re-run
    the baseline (the workload fingerprint does not change; the
    estimator name in the report does).
    """
    if not text:
        return 0
    words = len(text.split())
    chars4 = -(-len(text) // 4)
    return max(words, chars4)


# ── Recall query guard (FTS5 MATCH syntax; stopword degeneracy) ─────────────

#: FTS5 query-syntax specials — stripped before a token is quoted, so no
#: operator syntax can reach MATCH (M15.2 hardening pattern, vesmaro
#: sqlite_store precedent).
_FTS_STRIP_RE = re.compile('["()\\:*^]')

#: Degenerate high-frequency tokens (issue-#314 pattern: they match every
#: row and collapse the ranking). English-only — the v1 corpus is ASCII.
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "was", "are", "what", "which", "who", "how", "why",
        "when", "where", "about", "tell", "has", "have", "had", "does", "did",
        "that", "this", "with", "from", "into", "onto", "over", "under", "then",
        "than", "them", "they", "been", "being", "were", "will", "would", "can",
        "could", "shall", "should", "may", "might", "must", "also", "just",
        "only", "some", "any", "all", "far", "its", "our", "your", "their",
        "there", "here",
    }
)


def _fts_terms(query: str) -> list[str]:
    """Sanitised FTS terms: quoted literals, degenerate tokens dropped."""
    out: list[str] = []
    for word in query.lower().split():
        token = _FTS_STRIP_RE.sub("", word)
        if len(token) > 2 and token not in _STOPWORDS:
            out.append(token)
    return out


# ── Store layer (vesmaro-independent) ────────────────────────────────────────

class SqliteMemoryArm:
    """M arm: isolated sqlite store + MetricsStore sidecar + recall/assemble.

    Two storage modes, one recall path:

      * fresh mode (no ``--db``): a stand-owned schema (``s5_memories`` +
        FTS5) in a temp-dir sqlite file;
      * host mode (``--db``): the runner first clones the source store
        via the SQLite backup API (read-only source, S4 precedent); the
        arm opens the CLONE, creates its OWN ``s5_*`` tables next to the
        source's (namespaced — source rows are never touched) and recall
        UNIONs the source's ``memories_fts`` with the stand's index. The
        memory under test is therefore really exercised; every stand
        write is an ``actor='benchmark'`` row in ``s5_memories`` and is
        traced in ``s5_traces`` (audit plane, S4 precedent).

    The sidecar is written through the library's own sink (raw text never
    crosses; block fingerprints ride ``ccr_origin``) — the S5 dynamism
    metrics come from the REAL analyzer on REAL sidecar rows.
    """

    DDL = (
        "CREATE TABLE IF NOT EXISTS s5_memories ("
        " id TEXT PRIMARY KEY, sid TEXT NOT NULL, mid TEXT NOT NULL UNIQUE,"
        " content TEXT NOT NULL, actor TEXT NOT NULL, created_turn INTEGER NOT NULL)",
        "CREATE VIRTUAL TABLE IF NOT EXISTS s5_memories_fts USING fts5("
        " id UNINDEXED, content, tokenize='unicode61')",
        "CREATE TABLE IF NOT EXISTS s5_traces ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, turn INTEGER NOT NULL, verb TEXT NOT NULL,"
        " actor TEXT NOT NULL, tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL)",
    )

    def __init__(self, db_path: Path, *, sidecar_path: Path | None) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        for stmt in self.DDL:
            self.conn.execute(stmt)
        self.conn.commit()
        self._host_store = self._detect_host_store()
        self.source_memories = self._count_source_memories()
        self.sidecar = (
            MetricsStore(sidecar_path, hmac_key=b"s5-stand-install-key-0000000000")
            if sidecar_path is not None
            else None
        )
        self.turn = 0

    def _detect_host_store(self) -> bool:
        """True when the opened file carries a vesmaro-shaped memory table.

        The stand then reads it (never writes) and unions it into recall.
        Any other sqlite file degrades to fresh-mode semantics loudly
        reported via ``source_memories = 0``.
        """
        names = {
            str(r[0])
            for r in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            ).fetchall()
        }
        if "memories" not in names or "memories_fts" not in names:
            return False
        cols = {str(r[1]) for r in self.conn.execute("PRAGMA table_info(memories)").fetchall()}
        return "content" in cols and "id" in cols

    def _count_source_memories(self) -> int:
        if not self._host_store:
            return 0
        row = self.conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()
        return int(row["n"])

    def put(self, text: str, mid: str, sid: str) -> str:
        """Persist one memory (idempotent by workload marker ``mid``).

        The store id is derived from the marker (BLAKE2b), so a replayed
        tape cannot duplicate rows; the row is ``actor='benchmark'``.
        """
        existing = self.conn.execute(
            "SELECT id FROM s5_memories WHERE mid = ?", (mid,)
        ).fetchone()
        if existing is not None:  # idempotent replay: same marker → same row
            return str(existing["id"])
        mem_id = "s5-" + hashlib.blake2b(f"{mid}".encode(), digest_size=8).hexdigest()
        self.conn.execute(
            "INSERT INTO s5_memories (id, sid, mid, content, actor, created_turn)"
            " VALUES (?,?,?,?,?,?)",
            (mem_id, sid, mid, text, AGENT, self.turn),
        )
        self.conn.execute(
            "INSERT INTO s5_memories_fts (id, content) VALUES (?,?)", (mem_id, text)
        )
        self._trace("write", text, text)
        self.conn.commit()
        return mem_id

    def serve(self, sid: str, query: str, budget: int) -> dict[str, Any]:
        """Serve the assembled window for one query turn (assemble shape)."""
        self.turn += 1
        blocks = self._recall(query, budget)
        text = "\n\n".join(str(b["content"]) for b in blocks)
        result = {
            "session": sid,
            "project": PROJECT,
            "agent": AGENT,
            "file": None,
            "mode": "sync",
            "text": text,
            "blocks": [
                {
                    "memory_id": b["memory_id"],
                    "content_type": b["content_type"],
                    "score": b["score"],
                    "tokens": b["tokens"],
                    "redactions": 0,
                    "ccr_expanded": False,
                    "ccr_hashes": [],
                    "content": b["content"],
                }
                for b in blocks
            ],
            "tokens": {"budget": budget, "estimated": estimate_tokens(text)},
            "stats": {"stages": ["recall", "budget"], "recall": {"query_source": "explicit"}},
        }
        if self.sidecar is not None:
            self.sidecar.record_assemble(result)
        return result

    def _recall(self, query: str, budget: int) -> list[dict[str, Any]]:
        """FTS recall (source UNION stand index), lexical-overlap scored, budgeted.

        Retrieval mirrors the host's assemble semantics at v1 fidelity:
        candidate recall via FTS5 (each term a quoted literal), a lexical
        overlap relevance score, deterministic tie-break by id, then a
        budget-bounded assembly (blocks that no longer fit are skipped,
        the next candidate is tried). The SCORE uses the full query
        vocabulary (generic words discriminate — a candidate sharing only
        the topical terms of a wide query ranks lower), while the FTS
        MATCH uses the sanitised term set (operator safety).
        """
        terms = _fts_terms(query)
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        candidates: list[tuple[str, str]] = []
        if self._host_store:
            rows = self.conn.execute(
                "SELECT m.id AS src_id, m.content AS src_content FROM memories_fts f"
                " JOIN memories m ON m.id = f.id WHERE memories_fts MATCH ?"
                " ORDER BY rank LIMIT 50",
                (match,),
            ).fetchall()
            candidates += [(str(r["src_id"]), str(r["src_content"])) for r in rows]
        rows = self.conn.execute(
            "SELECT m.id AS own_id, m.content AS own_content FROM s5_memories_fts f"
            " JOIN s5_memories m ON m.id = f.id WHERE s5_memories_fts MATCH ?"
            " ORDER BY rank LIMIT 50",
            (match,),
        ).fetchall()
        candidates += [(str(r["own_id"]), str(r["own_content"])) for r in rows]
        q_words = {w for w in query.lower().split() if _FTS_STRIP_RE.sub("", w)}
        scored: list[tuple[float, str, str]] = []
        for mem_id, content in candidates:
            doc_words = {
                t
                for t in (_FTS_STRIP_RE.sub("", w) for w in content.lower().split())
                if t
            }
            if not doc_words:
                continue
            score = len(q_words & doc_words) / len(q_words)
            scored.append((score, mem_id, content))
        scored.sort(key=lambda p: (-p[0], p[1]))
        blocks: list[dict[str, Any]] = []
        used = 0
        for score, mem_id, content in scored:
            tokens = estimate_tokens(content)
            if used + tokens > budget:
                continue  # budget-bounded: skip, try the next candidate
            blocks.append(
                {
                    "memory_id": mem_id,
                    "content_type": "memory",
                    "score": round(score, 6),
                    "tokens": tokens,
                    "content": content,
                }
            )
            used += tokens
        return blocks

    def _trace(self, verb: str, text_in: str, text_out: str) -> None:
        self.conn.execute(
            "INSERT INTO s5_traces (turn, verb, actor, tokens_in, tokens_out) VALUES (?,?,?,?,?)",
            (self.turn, verb, AGENT, estimate_tokens(text_in), estimate_tokens(text_out)),
        )

    def write_cost_tokens(self) -> int:
        """Σ traces(tokens_in + tokens_out) over the stand's write path.

        Per the pre-registration: ``write_cost = Σ traces(tokens_in +
        tokens_out)`` over write-path calls — the ingestion touch (in)
        plus the persisted content (out) per write, actor=benchmark rows
        only. Source-store rows (if any) are never traced here: the
        source's own historical write cost is NOT the stand's spend.
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(tokens_in + tokens_out), 0) AS total FROM s5_traces"
            " WHERE verb = 'write' AND actor = ?",
            (AGENT,),
        ).fetchone()
        return int(row["total"] or 0)

    def close(self) -> None:
        if self.sidecar is not None:
            self.sidecar.close()
        self.conn.close()


class StaticWindowArm:
    """B0 legs — the window is a deterministic slice of fixed text.

    transcript-so-far (B0-naive): every write+hint SO FAR, unbounded —
    the naive harness's actual window (the headline baseline);
    whole-tape (B0-full): every write+hint of the whole tape at every
    query — reference only, overestimates savings;
    curated-file (B0-file): one static file OUTSIDE any store, sized to
    M's mean assembled tokens — the equal-budget comparator.

    Success is judged on the WINDOW (what that harness would serve);
    token cost is the window size. Blocks carry ``score = 1.0`` by
    construction (nothing was retrieved), so replay-fp-rate is
    UNDEFINED for B0 legs — reported as NO-DATA, never as 0.
    """

    def __init__(
        self,
        *,
        mode: str,
        curated_name: str | None = None,
        curated_path: Path | None = None,
        full_text: str | None = None,
    ) -> None:
        if mode not in ("transcript-so-far", "whole-tape", "curated-file"):
            raise ValueError(f"unknown static-window mode {mode!r}")
        if mode == "curated-file" and (curated_path is None or curated_name is None):
            raise ValueError("curated-file mode requires the curated file path")
        if mode == "whole-tape" and full_text is None:
            raise ValueError("whole-tape mode requires the full transcript text")
        self.mode = mode
        self.curated_name = curated_name
        self.curated_path = curated_path
        self.full_text = full_text
        self.transcript: list[str] = []
        self.turn = 0

    def put(self, text: str, mid: str, sid: str) -> str:
        self.transcript.append(text)
        return mid

    def serve(self, sid: str, query: str, budget: int) -> dict[str, Any]:
        self.turn += 1
        if self.mode == "curated-file":
            window = self.curated_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
        elif self.mode == "whole-tape":
            window = str(self.full_text)
        else:
            window = "\n".join(self.transcript)
        content_type = "curated" if self.mode == "curated-file" else "transcript"
        blocks = [
            {
                "memory_id": f"{sid}:line-{i}",
                "content_type": content_type,
                "score": 1.0,
                "tokens": estimate_tokens(part),
                "content": part,
            }
            for i, part in enumerate(window.split("\n"))
            if part
        ]
        return {
            "session": sid,
            "project": PROJECT,
            "agent": AGENT,
            "file": self.curated_name if self.mode == "curated-file" else None,
            "mode": "sync",
            "text": window,
            "blocks": blocks,
            "tokens": {"budget": budget, "estimated": estimate_tokens(window)},
            "stats": {"stages": ["window"], "recall": {"query_source": "explicit"}},
        }

    def close(self) -> None:  # symmetric arm interface; nothing to release
        return None


def clone_source_store(source: Path, target_dir: Path) -> Path:
    """Backup-API copy of a source mnemos store into ``target_dir``.

    The source is opened READ-ONLY (``mode=ro`` URI) and cloned page by
    page — never a file copy (WAL-unsafe, S4 precedent). Returns the
    copy's path.
    """
    target = target_dir / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)  # consistent snapshot incl. committed WAL
        finally:
            dst.close()
    finally:
        src.close()
    return target


# ── Replay engine ────────────────────────────────────────────────────────────

def _window_success(window_text: str, expect: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """The ONE success definition for every leg.

    fact/rule/session tasks: the required markers must appear in the
    served window with share >= min_ratio;
    negative tasks: no crash AND the window must not claim the absent
    markers (any absent marker present = failure — wallpaper signal).
    """
    markers = list(expect.get("markers") or [])
    absent = list(expect.get("absent") or [])
    min_ratio = float(expect.get("min_ratio") or 1.0)
    hits = [m for m in markers if m in window_text]
    share = (len(hits) / len(markers)) if markers else 1.0
    absent_present = [m for m in absent if m in window_text]
    ok = share >= min_ratio and not absent_present
    return ok, {
        "required": len(markers),
        "found": len(hits),
        "share": round(share, 6),
        "min_ratio": min_ratio,
        "absent_present": len(absent_present),
    }


def _task_relevance(blocks: list[dict[str, Any]]) -> float | None:
    """v1 judge proxy: ALL served blocks must clear the score proxy.

    Defined ONLY over retrieved blocks (``content_type == 'memory'``):
    static-window legs have no retrieval, so there is no false-positive
    claim to make — None (NO-DATA), never a fabricated 0.
    """
    if not blocks or any(b.get("content_type") != "memory" for b in blocks):
        return None
    return 1.0 if min(float(b.get("score") or 0.0) for b in blocks) >= FP_SCORE_PROXY else 0.0


def _replay_arm(arm: Any, workload: Workload, *, label: str, budget: int) -> dict[str, Any]:
    """Replay the whole tape against one arm; collect per-task rows.

    Symmetry: the SAME event order, the SAME estimator, the SAME
    success definition for every leg. Writes/hints are audited
    ``actor=benchmark`` inside the arm; the sidecar session axis is the
    workload ``sid`` (dynamism inputs are per-session).
    """
    tasks: list[dict[str, Any]] = []
    for event in workload.events:
        kind = event["kind"]
        sid = event["sid"]
        if kind == "write":
            arm.put(event["text"], event["mid"], sid)
        elif kind == "hint":
            arm.put(event["text"], f"hint-{event['t']}", sid)
        else:  # query — the measured turn
            result = arm.serve(sid, event["text"], budget)
            window = str(result.get("text") or "")
            ok, detail = _window_success(window, event["expect"])
            blocks = list(result.get("blocks") or [])
            tasks.append(
                {
                    "qid": event["qid"],
                    "sid": sid,
                    "stratum": event["stratum"],
                    "turn": event["t"],
                    "success": ok,
                    "detail": detail,
                    "prompt_tokens": estimate_tokens(window),
                    "blocks": len(blocks),
                    "relevance": _task_relevance(blocks),
                }
            )
    return {"label": label, "tasks": tasks}


# ── Metrics (per pre-registration: one name — one family) ────────────────────

def _draw_stream(key: str, resample: int, n_draws: int) -> list[int]:
    """A fixed deterministic draw stream for one bootstrap resample.

    blake2b counter chain keyed on (key, resample): the same inputs
    always yield the same draws — no RNG module state anywhere.
    """
    out: list[int] = []
    produced = 0
    chunk = 0
    while produced < n_draws:
        blob = hashlib.blake2b(
            f"{key}|{resample}|{chunk}".encode(), digest_size=64
        ).digest()
        for off in range(0, 64, 4):
            if produced >= n_draws:
                break
            out.append(int.from_bytes(blob[off : off + 4], "big"))
            produced += 1
        chunk += 1
    return out


def _stat_value(values: list[float], stat: str) -> float:
    if stat == "mean":
        return sum(values) / len(values)
    if stat == "median":
        return float(median(values))
    if stat == "sum":
        return float(sum(values))
    raise ValueError(f"unknown stat {stat!r}")


def _cluster_stat(
    groups: dict[str, list[float]],
    *,
    stat: str,
    key: str,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict[str, Any]:
    """Point statistic + session-cluster bootstrap CI95 (pre-registration).

    ``groups`` maps session id -> that session's per-task values; a
    resample redraws whole SESSIONS with replacement (the session-
    stratified cluster bootstrap, 10 000 resamples per the frozen
    design) and recomputes ``stat`` over the pooled values. Draws come
    from a blake2b counter stream keyed on ``key`` — byte-stable runs.
    Stats: ``mean`` (rates), ``median`` (distributions), ``sum``
    (aggregate totals).
    """
    sessions = sorted(groups)
    n_sessions = len(sessions)
    all_values = [v for s in sessions for v in groups[s]]
    if not all_values:
        return {
            "point": None, "p25": None, "p75": None, "ci95": None, "n": 0, "n_sessions": 0,
        }
    point = _stat_value(all_values, stat)
    boot: list[float] = []
    if stat == "mean":
        sums = [float(sum(groups[s])) for s in sessions]
        sizes = [len(groups[s]) for s in sessions]
        for r in range(resamples):
            total = 0.0
            count = 0
            for d in _draw_stream(key, r, n_sessions):
                j = d % n_sessions
                total += sums[j]
                count += sizes[j]
            boot.append(total / count if count else 0.0)
    else:
        per_session = [groups[s] for s in sessions]
        for r in range(resamples):
            pooled: list[float] = []
            for d in _draw_stream(key, r, n_sessions):
                pooled.extend(per_session[d % n_sessions])
            boot.append(_stat_value(pooled, stat))
    boot.sort()
    lo = boot[int(0.025 * (len(boot) - 1))]
    hi = boot[int(0.975 * (len(boot) - 1))]
    out: dict[str, Any] = {
        "point": round(float(point), 6),
        "ci95": [round(lo, 6), round(hi, 6)],
        "n": len(all_values),
        "n_sessions": n_sessions,
        "p25": None,
        "p75": None,
    }
    if stat == "median":
        ordered = sorted(all_values)
        out["p25"] = round(ordered[int(0.25 * (len(ordered) - 1))], 6)
        out["p75"] = round(ordered[int(0.75 * (len(ordered) - 1))], 6)
    return out


def _group_by_session(tasks: list[dict[str, Any]], value_fn: Any) -> dict[str, list[float]]:
    groups: dict[str, list[float]] = {}
    for t in tasks:
        groups.setdefault(t["sid"], []).append(float(value_fn(t)))
    return groups


def _leg_metrics(leg: dict[str, Any], seed_key: str, resamples: int) -> dict[str, Any]:
    """Aggregate one leg: success rate, token medians, per-strata, fp-rate."""
    tasks = leg["tasks"]
    if not tasks:
        return {
            "status": "NO-DATA",
            "n_tasks": 0,
            "task_success": {"point": None, "ci95": None, "n": 0, "n_sessions": 0},
            "prompt_tokens": {
                "point": None, "p25": None, "p75": None, "ci95": None, "n": 0, "n_sessions": 0,
            },
            "tokens_total": 0,
            "tokens_per_success": None,
            "replay_fp_rate": None,
            "per_stratum": {},
        }
    success_groups = _group_by_session(tasks, lambda t: 1.0 if t["success"] else 0.0)
    token_groups = _group_by_session(tasks, lambda t: t["prompt_tokens"])
    tokens_total = sum(float(t["prompt_tokens"]) for t in tasks)
    n_success = sum(1 for t in tasks if t["success"])
    relevances = [t["relevance"] for t in tasks if t["relevance"] is not None]
    strata = sorted({t["stratum"] for t in tasks})
    per_stratum: dict[str, Any] = {}
    for stratum in strata:
        subset = [t for t in tasks if t["stratum"] == stratum]
        per_stratum[stratum] = {
            "n": len(subset),
            "task_success": _cluster_stat(
                _group_by_session(subset, lambda t: 1.0 if t["success"] else 0.0),
                stat="mean",
                key=f"{seed_key}|succ|{stratum}",
                resamples=resamples,
            ),
            "prompt_tokens": _cluster_stat(
                _group_by_session(subset, lambda t: t["prompt_tokens"]),
                stat="median",
                key=f"{seed_key}|tok|{stratum}",
                resamples=resamples,
            ),
        }
    return {
        "status": "OK",
        "n_tasks": len(tasks),
        "task_success": _cluster_stat(
            success_groups, stat="mean", key=f"{seed_key}|succ", resamples=resamples
        ),
        "prompt_tokens": _cluster_stat(
            token_groups, stat="median", key=f"{seed_key}|tok", resamples=resamples
        ),
        "tokens_total": round(tokens_total, 6),
        "tokens_per_success": round(tokens_total / n_success, 6) if n_success else None,
        "replay_fp_rate": (
            round(1.0 - sum(relevances) / len(relevances), 6) if relevances else None
        ),
        "per_stratum": per_stratum,
    }


# ── Dynamism (M arm sidecar — the real analyzer) ─────────────────────────────

def _dynamism_section(sidecar_path: Path | None, sessions: list[str]) -> dict[str, Any]:
    """Corridor metrics via DynamismAnalyzer on the M arm's sidecar rows.

    The corridor is defined over sessions with >= 2 assemblies
    (``corridor_gate`` refuses fewer — no pairs can form); singletons
    stay visible per-session with their NO-DATA status. Aggregates over
    zero pair-capable sessions (or a missing/empty sidecar) are loud
    NO-DATA — never green, never zero.
    """
    if sidecar_path is None or not Path(sidecar_path).exists():
        return {"status": "NO-DATA", "reasons": ["no metrics.sqlite sidecar on this run"]}
    store = MetricsStore(sidecar_path)
    try:
        analyzer = DynamismAnalyzer(store)
        reports = [analyzer.session_report(sid, project=PROJECT) for sid in sessions]
    finally:
        store.close()
    pair_capable = [
        r
        for r in reports
        if int(r.get("assemblies") or 0) >= 2 and r.get("context_dynamism_ratio") is not None
    ]
    if not pair_capable:
        return {
            "status": "NO-DATA",
            "reasons": [
                "no session with >= 2 assemblies — dynamism is undefined for"
                " single-assembly sessions",
            ],
            "sessions_total": len(reports),
            "pair_capable_sessions": 0,
        }
    ratios = [r["context_dynamism_ratio"] for r in pair_capable]
    zero_shares = [
        r["zero_uniqueness_pair_share"]
        for r in pair_capable
        if r.get("zero_uniqueness_pair_share") is not None
    ]
    hints = [
        r["explicit_hint_share"]
        for r in pair_capable
        if r.get("explicit_hint_share") is not None
    ]
    return {
        "status": "OK",
        "sessions_total": len(reports),
        "pair_capable_sessions": len(pair_capable),
        "context_dynamism_ratio": {
            "median": round(median(ratios), 6),
            "min": round(min(ratios), 6),
            "max": round(max(ratios), 6),
        },
        # A straddling retention window can leave zero-share undefined
        # while the ratio exists — the axis degrades to None, never zero.
        "zero_uniqueness_pair_share": {
            "median": round(median(zero_shares), 6) if zero_shares else None,
            "max": round(max(zero_shares), 6) if zero_shares else None,
        },
        "explicit_hint_share_median": round(median(hints), 6) if hints else None,
        "per_session": [
            {
                k: r.get(k)
                for k in (
                    "session", "assemblies", "status", "context_dynamism_ratio",
                    "zero_uniqueness_pair_share", "explicit_hint_share",
                )
            }
            for r in reports
        ],
    }


# ── The run ──────────────────────────────────────────────────────────────────

def _build_curated_file(path: Path, workload: Workload, target_tokens: int) -> int:
    """Curated B0-file: the tape's write texts, sized to ``target_tokens``.

    Recipe (deterministic, printed in the report): the workload's write
    texts joined in tape order; the sequence repeats whole when shorter
    than the target; the result is word-truncated to the longest prefix
    whose token estimate fits the target. The file lives OUTSIDE any
    store (a plain file next to the report). Returns the measured token
    count — the report carries the MEASURED budget, never the intent.
    """
    lines = [str(e["text"]) for e in workload.events if e["kind"] == "write"]
    if not lines or target_tokens <= 0:
        path.write_text("", encoding="utf-8")
        return 0
    base = "\n".join(lines)
    text = base
    while estimate_tokens(text) < target_tokens:  # whole-sequence repeats
        text = f"{text}\n{base}"
    words = text.split()
    kept: list[str] = []
    for word in words:
        candidate = " ".join([*kept, word])
        if estimate_tokens(candidate) > target_tokens:
            break
        kept.append(word)
    result = " ".join(kept)
    path.write_text(result, encoding="utf-8")
    return estimate_tokens(result)


def _hypothesis_verdict(ok: bool | None, evidence: str) -> dict[str, Any]:
    """H1-H3 verdict family: preregistered constants -> PASS/FAIL/NO-DATA.

    There is no UNFROZEN state: δ is a frozen constant of the
    pre-registration, and H1's bound (CI95 lower > 0) is parameter-free.
    """
    if ok is None:
        return {"status": "NO-DATA", "reasons": [f"missing data: {evidence}"]}
    return {"status": "PASS" if ok else "FAIL", "reasons": [f"measured: {evidence}"]}


def _h4_verdict(
    dyn: dict[str, Any], frozen: bool, min_dynamism: Any, max_zero: Any
) -> dict[str, Any]:
    """H4 — the dynamism guardrail, via the library's corridor gate.

    Unlike H1-H3, H4's thresholds have an UNFROZEN state: they freeze
    after the baseline run, before the decisive one (methodology §1.3).
    """
    if dyn.get("status") == "NO-DATA":
        return {
            "status": "NO-DATA",
            "reasons": dyn.get("reasons", ["dynamism section is NO-DATA"]),
        }
    if min_dynamism is None or max_zero is None:
        return {
            "status": "UNFROZEN",
            "reasons": ["H4 thresholds (min_dynamism, max_zero_uniqueness_share) not frozen"],
        }
    gate = corridor_gate(
        {
            "assemblies": max(
                (int(s.get("assemblies") or 0) for s in dyn.get("per_session", [])),
                default=0,
            ),
            "context_dynamism_ratio": dyn["context_dynamism_ratio"]["median"],
            "zero_uniqueness_pair_share": dyn["zero_uniqueness_pair_share"]["median"],
            "explicit_hint_share": dyn.get("explicit_hint_share_median"),
        },
        min_dynamism=float(min_dynamism),
        max_zero_share=float(max_zero),
    )
    if not frozen:
        return {"status": "UNFROZEN", "reasons": [f"measured: {gate['status']}"] + gate["reasons"]}
    return {"status": gate["status"], "reasons": gate["reasons"]}


def run_s5(
    workload: Workload,
    *,
    db: Path | None,
    out_dir: Path,
    thresholds: dict[str, Any] | None,
    freeze: bool,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> tuple[dict[str, Any], int]:
    """One two-arm S5 pass. Returns (report, exit_code).

    Isolation: with a source store, the M arm replays on its OWN
    backup-API copy inside a temp dir; the source is opened read-only
    once to clone and never written. Without a source (smoke mode), the
    M arm gets a fresh sqlite file — same isolation semantics. B0 legs
    touch no store at all (transcripts + one curated file).

    ``resamples`` shrinks the bootstrap for engineering runs only; the
    CLI always replays with the preregistered 10 000.
    """
    if db is not None and not Path(db).exists():
        raise FileNotFoundError(f"source store not found: {db}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="mnemos-s5-") as tmp_name:
        tmp = Path(tmp_name)
        m_sidecar_path = tmp / "m-arm" / "metrics.sqlite"

        # ── M arm: isolated store (backup-API copy when --db) ────────
        copied_from: str | None = None
        if db is not None:
            m_db_path = clone_source_store(Path(db), tmp / "m-arm")
            copied_from = str(Path(db).resolve())
        else:
            m_db_path = tmp / "m-arm" / "store.sqlite"
        m_arm = SqliteMemoryArm(m_db_path, sidecar_path=m_sidecar_path)

        # ── replay: M, B0-naive, B0-full (B0-file after M sizes it) ──
        legs: dict[str, dict[str, Any]] = {
            "M": _replay_arm(m_arm, workload, label="M", budget=DEFAULT_BUDGET),
        }
        b0n_arm = StaticWindowArm(mode="transcript-so-far")
        legs["B0-naive"] = _replay_arm(b0n_arm, workload, label="B0-naive", budget=DEFAULT_BUDGET)
        b0n_arm.close()
        full_text = "\n".join(
            str(e["text"]) for e in workload.events if e["kind"] in ("write", "hint")
        )
        b0full_arm = StaticWindowArm(mode="whole-tape", full_text=full_text)
        legs["B0-full"] = _replay_arm(b0full_arm, workload, label="B0-full", budget=0)
        b0full_arm.close()

        # ── write cost: Σ traces(tokens_in + tokens_out), M arm only ─
        write_cost = m_arm.write_cost_tokens()
        source_memories = m_arm.source_memories
        m_arm.close()

        # ── B0-file equal budget: M's mean assembled tokens ──────────
        # The curated file is BUILT to M's mean assembled token count so
        # the comparator is equal-budget BY CONSTRUCTION (pre-reg:
        # «файл равного среднего бюджета»); the report carries the
        # MEASURED size and the deterministic recipe.
        m_tokens = [t["prompt_tokens"] for t in legs["M"]["tasks"]]
        m_mean = round(sum(m_tokens) / len(m_tokens), 6) if m_tokens else 0.0
        target_tokens = round(m_mean)
        curated_path = out_dir / CURATED_FILE_NAME
        curated_measured = _build_curated_file(curated_path, workload, target_tokens)
        b0file_arm = StaticWindowArm(
            mode="curated-file",
            curated_name=CURATED_FILE_NAME,
            curated_path=curated_path,
        )
        legs["B0-file"] = _replay_arm(b0file_arm, workload, label="B0-file", budget=0)
        b0file_arm.close()

        # ── per-leg metrics ──────────────────────────────────────────
        leg_metrics = {
            label: _leg_metrics(legs[label], f"s5|{label}", resamples) for label in LEGS
        }

        # ── F8 value family (pre-registration §Метрики) ──────────────
        # Paired per-task deltas (B0-naive minus M) in tape order; grouped by
        # session for the cluster bootstrap. The paired counts are the
        # McNemar discordant-pair inputs for the phase-D analysis.
        b0n_tasks = legs["B0-naive"]["tasks"]
        m_tasks = legs["M"]["tasks"]
        paired = {"both_ok": 0, "m_only": 0, "b0n_only": 0, "neither": 0}
        delta_groups: dict[str, list[float]] = {}
        if len(b0n_tasks) == len(m_tasks):
            for m_task, b_task in zip(m_tasks, b0n_tasks, strict=True):
                delta = float(b_task["prompt_tokens"]) - float(m_task["prompt_tokens"])
                delta_groups.setdefault(m_task["sid"], []).append(delta)
                m_ok, b_ok = m_task["success"], b_task["success"]
                key = ("both_ok" if m_ok and b_ok else "m_only" if m_ok else
                       "b0n_only" if b_ok else "neither")
                paired[key] += 1
        deltas = [d for group in delta_groups.values() for d in group]
        gross_total = round(sum(deltas), 6) if m_tasks else None
        gross_per_session = _cluster_stat(
            delta_groups or {"_": [0.0]}, stat="median", key="s5|gross|median",
            resamples=resamples,
        ) if m_tasks else {
            "point": None, "p25": None, "p75": None, "ci95": None, "n": 0, "n_sessions": 0,
        }
        gross_sum_ci = _cluster_stat(
            delta_groups or {"_": [0.0]}, stat="sum", key="s5|gross|sum",
            resamples=resamples,
        )["ci95"] if m_tasks else None
        net_savings = (
            round(gross_total - write_cost, 6) if gross_total is not None else None
        )
        net_ci95 = (
            [round(gross_sum_ci[0] - write_cost, 6), round(gross_sum_ci[1] - write_cost, 6)]
            if gross_sum_ci is not None
            else None
        )
        b0n_tokens_total = leg_metrics["B0-naive"]["tokens_total"]
        value_ratio = (
            round(net_savings / b0n_tokens_total, 6)
            if net_savings is not None and b0n_tokens_total
            else None
        )

        m_dyn = _dynamism_section(m_sidecar_path, workload.sessions())

        # ── hypothesis verdicts ──────────────────────────────────────
        frozen = bool(freeze) and bool(thresholds)
        delta = float((thresholds or {}).get("delta_task_success", PREREG_DELTA))
        min_dynamism = (thresholds or {}).get("min_dynamism")
        max_zero = (thresholds or {}).get("max_zero_uniqueness_share")

        def _rate(label: str) -> float | None:
            metrics = leg_metrics[label]
            return metrics["task_success"]["point"] if metrics["n_tasks"] else None

        succ_m = _rate("M")
        succ_b0n = _rate("B0-naive")
        succ_b0file = _rate("B0-file")

        h1_ok = None if net_ci95 is None else net_ci95[0] > 0
        h2_ok = None if succ_m is None or succ_b0n is None else succ_m >= succ_b0n - delta
        h3_ok = (
            None if succ_m is None or succ_b0file is None else succ_m >= succ_b0file - delta
        )
        h1 = _hypothesis_verdict(h1_ok, f"net_savings CI95 lower bound {net_ci95}")
        h2 = _hypothesis_verdict(
            h2_ok, f"task_success(M) {succ_m} vs B0-naive {succ_b0n} (delta {delta})"
        )
        h3 = _hypothesis_verdict(
            h3_ok, f"task_success(M) {succ_m} vs B0-file {succ_b0file} (delta {delta})"
        )
        h4 = _h4_verdict(m_dyn, frozen, min_dynamism, max_zero)
        hypotheses = {"H1": h1, "H2": h2, "H3": h3, "H4": h4}

        # The mandate: H2 fails → the savings headline is suppressed,
        # whatever the freeze state (baseline included).
        headline_invalid = h2["status"] == "FAIL"
        headline = None
        if not headline_invalid and h1["status"] == "PASS" and net_savings is not None:
            headline = f"net_savings {net_savings:.0f} tokens (H1+H2 valid)"
        if headline_invalid:
            print(
                "SAVINGS HEADLINE INVALID (H2): task_success(M) < task_success(B0-naive) -"
                " delta; net_savings reported informationally only",
                file=sys.stderr,
            )

        exit_code = 0
        if frozen:
            for h in hypotheses.values():
                if h["status"] == "FAIL":
                    exit_code = 1

        report = _build_report(
            workload=workload,
            legs=legs,
            leg_metrics=leg_metrics,
            gross_total=gross_total,
            gross_per_session=gross_per_session,
            write_cost=write_cost,
            net_savings=net_savings,
            net_ci95=net_ci95,
            value_ratio=value_ratio,
            paired=paired,
            m_mean_tokens=m_mean,
            curated_measured=curated_measured,
            dynamism=m_dyn,
            hypotheses=hypotheses,
            headline=headline,
            headline_invalid=headline_invalid,
            thresholds=thresholds,
            frozen=frozen,
            delta=delta,
            copied_from=copied_from,
            source_memories=source_memories,
        )
        _write_reports(out_dir, report)
        return report, exit_code


def _build_report(
    *,
    workload: Workload,
    legs: dict[str, dict[str, Any]],
    leg_metrics: dict[str, dict[str, Any]],
    gross_total: float,
    gross_per_session: dict[str, Any],
    write_cost: int,
    net_savings: float | None,
    net_ci95: list[float] | None,
    value_ratio: float | None,
    paired: dict[str, int],
    m_mean_tokens: float,
    curated_measured: int,
    dynamism: dict[str, Any],
    hypotheses: dict[str, dict[str, Any]],
    headline: str | None,
    headline_invalid: bool,
    thresholds: dict[str, Any] | None,
    frozen: bool,
    delta: float,
    copied_from: str | None,
    source_memories: int,
) -> dict[str, Any]:
    """The canonical JSON report (one shape, tests pin byte-stability)."""
    return {
        "stand": "s5-memory-value",
        "stand_version": STAND_VERSION,
        "single_look": "baseline",
        "logical_clock": LOGICAL_CLOCK,
        "estimator": ESTIMATOR,
        "workload": workload_summary(workload),
        "thresholds": {
            "status": "frozen" if frozen else "unfrozen",
            "values": dict(thresholds) if thresholds else {},
            "preregistered_delta_task_success": PREREG_DELTA,
            "effective_delta_task_success": delta,
            "note": (
                "H1-H3 are measured against preregistered constants at any run; H4 corridor"
                " thresholds freeze AFTER the baseline run, BEFORE the decisive run"
                " (pre-registration §Дизайн)"
            ),
        },
        "isolation": {
            "mode": "backup-api-copy" if copied_from else "fresh-tmp-store",
            "source_store": copied_from,
            "source_memories": source_memories,
            "actor": AGENT,
        },
        "legs": {
            label: {"metrics": leg_metrics[label], "tasks": legs[label]["tasks"]}
            for label in LEGS
        },
        "value": {
            "gross_savings_tokens_total": gross_total,
            "gross_savings_per_session": gross_per_session,
            "write_cost_tokens": write_cost,
            "net_savings_tokens": net_savings,
            "net_savings_ci95": net_ci95,
            "value_ratio": value_ratio,
            "m_mean_assembled_tokens": m_mean_tokens,
            "b0_file": {
                "file": CURATED_FILE_NAME,
                "budget_target_tokens": round(m_mean_tokens),
                "measured_tokens": curated_measured,
                "recipe": (
                    "workload write texts in tape order, whole-sequence repeats, word-truncated"
                    " to the target; plain file OUTSIDE any store, next to this report"
                ),
            },
            "paired_counts_m_vs_b0naive": paired,
            "headline": headline,
            "headline_invalid_h2": headline_invalid,
        },
        "dynamism": dynamism,
        "metrics_notes": [
            "ONE token estimator for every leg: words-and-chars-per-4-v1 (max(words, chars/4));",
            "replay-fp-rate is defined only over retrieved blocks (v1 judge proxy: every served"
            " block's retrieval score >= 0.35) — static-window legs report NO-DATA there;",
            "task_success is the per-task rate, session-cluster bootstrapped (10k resamples);",
            "write_cost = Σ traces(tokens_in + tokens_out) over the stand's actor=benchmark"
            " write path on the M arm only.",
        ],
        "hypotheses": hypotheses,
        "status_legend": {
            "PASS": "hypothesis holds at its (preregistered or frozen) threshold",
            "FAIL": "hypothesis breaches its threshold",
            "UNFROZEN": "measured, thresholds not frozen yet (baseline run)",
            "NO-DATA": "no data — loud, never green, never zero",
        },
    }


def _write_reports(out_dir: Path, report: dict[str, Any]) -> None:
    """Write JSON + MD summary to the --out dir."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "s5-report.json"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    md_path = out_dir / "s5-report.md"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"s5: report → {json_path}", file=sys.stderr)
    print(f"s5: summary → {md_path}", file=sys.stderr)


# ── Human-readable MD summary ────────────────────────────────────────────────

def _fmt(value: Any) -> str:
    if value is None:
        return "NO-DATA"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    """The MD summary: verdicts first, tables second, honesty stamps."""
    hyp = report["hypotheses"]
    wl = report["workload"]
    legs = report["legs"]
    lines: list[str] = [
        "# S5 memory-value — report",
        "",
        f"- stand version: {report['stand_version']} · single-look: "
        f"{report['single_look']} · thresholds: {report['thresholds']['status']}",
        f"- workload fingerprint: `{wl['fingerprint']}`",
        f"- workload: {wl['events']} events / {wl['queries']} queries / "
        f"{wl['writes']} writes / {wl['hints']} hints",
        f"- estimator: {report['estimator']} · logical clock: {report['logical_clock']}",
        f"- isolation: {report['isolation']['mode']}"
        + (
            f" (source: `{report['isolation']['source_store']}`,"
            f" {report['isolation']['source_memories']} source memories)"
            if report["isolation"]["source_store"]
            else ""
        ),
        "",
        "## Hypotheses",
        "",
        "| H | Verdict | Evidence |",
        "|---|---|---|",
    ]
    for name in ("H1", "H2", "H3", "H4"):
        h = hyp[name]
        evidence = "; ".join(h["reasons"]) or "-"
        lines.append(f"| {name} | {h['status']} | {evidence} |")
    if report["value"]["headline_invalid_h2"]:
        lines.append("")
        lines.append(
            "**SAVINGS HEADLINE INVALID (H2)** — net_savings reported informationally only."
        )
    elif report["value"]["headline"]:
        lines.append("")
        lines.append(f"**Headline:** {report['value']['headline']}")
    lines += [
        "",
        "## Legs",
        "",
        "| Leg | tasks | task_success (rate) | prompt_tokens (median) | tokens/success"
        " | replay-fp-rate |",
        "|---|---|---|---|---|---|",
    ]
    for label in LEGS:
        m = legs[label]["metrics"]
        ts, pt = m["task_success"], m["prompt_tokens"]
        lines.append(
            f"| {label} | {m['n_tasks']} | {_fmt(ts['point'])} | {_fmt(pt['point'])} | "
            f"{_fmt(m['tokens_per_success'])} | {_fmt(m['replay_fp_rate'])} |"
        )
    value = report["value"]
    ci = value["net_savings_ci95"]
    lines += [
        "",
        "## F8 value family",
        "",
        f"- gross_savings (total): {_fmt(value['gross_savings_tokens_total'])} tokens;"
        f" per-session median {_fmt(value['gross_savings_per_session']['point'])}"
        f" (p25 {_fmt(value['gross_savings_per_session']['p25'])},"
        f" p75 {_fmt(value['gross_savings_per_session']['p75'])})",
        f"- net_savings: {_fmt(value['net_savings_tokens'])} tokens"
        f" (CI95 {_fmt(ci[0]) if ci else 'NO-DATA'}..{_fmt(ci[1]) if ci else 'NO-DATA'})"
        + (" — informational only (H2 invalid)" if value["headline_invalid_h2"] else ""),
        f"- write_cost: {_fmt(value['write_cost_tokens'])} tokens;",
        f"- value_ratio: {_fmt(value['value_ratio'])};",
        f"- B0-file: {value['b0_file']['measured_tokens']} tokens measured"
        f" (target {value['b0_file']['budget_target_tokens']},"
        f" {value['b0_file']['recipe']})",
        "",
        "## Dynamism (M arm, sidecar)",
        "",
    ]
    dyn = report["dynamism"]
    if dyn.get("status") == "NO-DATA":
        lines.append(f"- NO-DATA: {'; '.join(dyn.get('reasons', []))}")
    else:
        lines.append(
            f"- pair-capable sessions: {dyn['pair_capable_sessions']}"
            f" / {dyn['sessions_total']} (singletons stay NO-DATA per session)"
        )
        lines.append(
            f"- context_dynamism_ratio median {_fmt(dyn['context_dynamism_ratio']['median'])}"
            f" (min {_fmt(dyn['context_dynamism_ratio']['min'])},"
            f" max {_fmt(dyn['context_dynamism_ratio']['max'])})"
        )
        lines.append(
            f"- zero_uniqueness_pair_share median"
            f" {_fmt(dyn['zero_uniqueness_pair_share']['median'])}"
            f" (max {_fmt(dyn['zero_uniqueness_pair_share']['max'])})"
        )
        lines.append(f"- explicit_hint_share median {_fmt(dyn.get('explicit_hint_share_median'))}")
    lines += [
        "",
        "## Per-strata (task_success rates)",
        "",
        "| Stratum | n | M | B0-naive | B0-file | B0-full |",
        "|---|---|---|---|---|---|",
    ]
    strata = sorted({s for label in LEGS for s in legs[label]["metrics"]["per_stratum"]})
    for stratum in strata:
        cells = []
        n = 0
        for label in LEGS:
            entry = legs[label]["metrics"]["per_stratum"].get(stratum)
            if entry:
                n = entry["n"]
                cells.append(_fmt(entry["task_success"]["point"]))
            else:
                cells.append("NO-DATA")
        lines.append(f"| {stratum} | {n} | " + " | ".join(cells) + " |")
    lines += ["", "## Failed tasks per leg", ""]
    for label in LEGS:
        tasks = legs[label]["tasks"]
        fails = [t for t in tasks if not t["success"]]
        lines.append(f"### {label} — {len(fails)} failed / {len(tasks)}")
        lines.append("")
        if fails:
            lines += [
                "| qid | stratum | found/required | absent present |",
                "|---|---|---|---|",
            ]
            for t in fails:
                d = t["detail"]
                lines.append(
                    f"| {t['qid']} | {t['stratum']} | {d['found']}/{d['required']}"
                    f" | {d['absent_present']} |"
                )
            lines.append("")
    lines += [
        "---",
        "",
        "Statuses: PASS / FAIL / UNFROZEN / NO-DATA — NO-DATA is loud, never green, never zero.",
        "Full per-task tables: `s5-report.json` (`legs.<leg>.tasks`).",
        "v1 disclaimer (pre-registration): synthetic corpus — figures do not extrapolate to real"
        " sessions.",
        "",
    ]
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_thresholds(raw: str | None) -> dict[str, Any] | None:
    """Parse ``k=v`` comma pairs (k in the frozen-threshold allowlist)."""
    if raw is None:
        return None
    allowed = {"delta_task_success", "min_dynamism", "max_zero_uniqueness_share"}
    out: dict[str, Any] = {}
    for part in raw.split(","):
        if not part:
            continue
        key, _, value = part.partition("=")
        if key not in allowed:
            raise ValueError(f"unknown threshold key {key!r} (allowed: {sorted(allowed)})")
        out[key] = float(value)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S5 memory-value stand (mnemos-vitals phase B)")
    parser.add_argument(
        "--workload",
        type=Path,
        default=None,
        help="path to a JSONL workload tape (default: generate the v1 corpus and pin it)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="path to a source mnemos sqlite store (backup-API copy for the M arm;"
        " default: fresh temp store)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_REPORT_DIR,
        help=f"report output dir (default: {DEFAULT_REPORT_DIR})",
    )
    parser.add_argument(
        "--thresholds",
        type=str,
        default=None,
        help="thresholds: delta_task_success=...,min_dynamism=...,max_zero_uniqueness_share=...",
    )
    parser.add_argument(
        "--freeze-thresholds",
        action="store_true",
        help="stamp these thresholds as FROZEN into the report (decisive run; exit 1 on breach)",
    )
    args = parser.parse_args(argv)

    try:
        thresholds = parse_thresholds(args.thresholds)
    except ValueError as exc:
        print(f"s5: {exc}", file=sys.stderr)
        return 2
    if args.freeze_thresholds and not thresholds:
        print(
            "s5: --freeze-thresholds requires --thresholds values (thresholds freeze"
            " as EXPLICIT values, never defaults)",
            file=sys.stderr,
        )
        return 2

    try:
        if args.workload is not None:
            workload = load_workload(args.workload)
        else:
            # Pin the v1 tape to the repo path so the fingerprint is
            # stable across runs (and across machines with the repo).
            DEFAULT_WORKLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
            workload = write_workload(DEFAULT_WORKLOAD_PATH, synthetic_workload())
        report, exit_code = run_s5(
            workload,
            db=args.db,
            out_dir=args.out,
            thresholds=thresholds,
            freeze=args.freeze_thresholds,
        )
    except WorkloadError as exc:
        print(f"s5: workload refused (loud): {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"s5: {exc}", file=sys.stderr)
        return 2
    stamp = (
        "s5: "
        f"fp={report['workload']['fingerprint'][:12]} "
        f"H1={report['hypotheses']['H1']['status']} "
        f"H2={report['hypotheses']['H2']['status']} "
        f"H3={report['hypotheses']['H3']['status']} "
        f"H4={report['hypotheses']['H4']['status']}"
    )
    print(stamp, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
