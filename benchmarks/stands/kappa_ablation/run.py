#!/usr/bin/env python
"""Kappa ablation runner — the FROZEN pre-registration executed (phase C).

Implements exactly the §7 checklist of
``docs/experiments/touched-rate-kappa.md`` on the S5 synthetic v1 stand:

  - T = 50 turns sampled STRATIFIED from the committed S5 default
    workload, seed ``blake2b("kappa-ablation-sampling")`` (largest-
    remainder proportional allocation over strata; in-stratum picks from
    a BLAKE2b chain — no RNG state);
  - unit = one BLOCK DECISION (turn, candidate block): the turn replays
    twice — full assembly vs the block removed from the CANDIDATE list
    BEFORE budget-assembly (no post-hoc text surgery) — on isolated
    store copies (SQLite backup API when a source is given,
    ``actor=benchmark``, S5 precedent; the live store is never touched);
  - ground truth = 1 iff ``task_success(full) = 1`` AND
    ``task_success(ablated) = 0`` under the stand's own validator
    (``_window_success``); turns with ``task_success(full) = 0`` are
    pre-registered exclusions (degradation below zero is impossible);
  - predicted = the harness touched report for that block through the
    REAL phase-C usage contract — ``MetricsStore.record_assemble``
    writes the assembly, the stand implements the SAME shingle-echo the
    integration annex uses (word 5-shingle intersection; blocks >= 40
    tokens are reportable — the canon signal domain), the touched
    ordinals go through ``record_usage`` and the prediction label is
    read BACK from ``usage_reports``: the calibration consumes what the
    plane actually stored;
  - Cohen's kappa (unweighted, block-level) + turn-cluster bootstrap
    CI95 (10 000 resamples, percentile, blocks nested in turns, draws
    from the S5 ``_draw_stream`` pattern keyed on the tape
    fingerprint); prevalence + PI + BI + PABAK (Byrt 1993) printed as
    sensitivity numbers, never a gate;
  - verdict per the frozen rule: κ̂ >= 0.6 AND CI95 lower >= 0.5 →
    corridor-eligible; otherwise NOT-CALIBRATED (H-K0 — a valid,
    equally-honest outcome); degenerate marginals (either estimator
    constant) → κ undefined → NOT-CALIBRATED; zero evaluable decisions
    → loud NO-DATA (never a κ from too few points); decisions < 60 keep
    the frozen rule as-is with a printed power warning; sampled T < 60
    warns;
  - single decisive look: engineering runs (<= 10 turns) NEVER compute
    κ (mechanically absent from their artifacts); decisive mode runs
    ONCE per tape fingerprint — the ledger stamps "decisive done for fp
    X" BEFORE the ablation loop and refuses a second look (exit 1);
  - tape fingerprint = BLAKE2b over (workload fingerprint + sampled
    turn fingerprints + candidates per turn + sampling seed + mode +
    turn count + the TOUCHED-SIGNAL IMPLEMENTATION HASH) — signal drift
    invalidates the calibration by mismatch (exit 1); the expected
    workload fingerprint is pinned (the committed S5 tape) and a
    mismatch is an abort.

Honesty: NO-DATA is loud, never green, never zero; the report prints
whatever the numbers are; the generator alarms (exit 1), never edits.
Determinism: fixed BLAKE2b chains, a constant logical-clock stamp, no
wall-clock in any artifact — two runs of the same tape and flags emit
byte-identical JSON/MD/JSONL. Raw text never enters artifacts and the
runner writes NOTHING outside its --out dir.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

STAND_ROOT = Path(__file__).resolve().parent
REPO_ROOT = STAND_ROOT.parents[2]
_SRC_ROOT = REPO_ROOT / "src"
for _p in (str(_SRC_ROOT), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from benchmarks.stands.s5_memory_value import run as s5_run  # noqa: E402
from benchmarks.stands.s5_memory_value.workload import (  # noqa: E402
    Workload,
    WorkloadError,
    canonical_line,
    load_workload,
)
from mnemos_vitals.sink import MetricsStore  # noqa: E402

STAND_VERSION = "kappa-ablation-1"
#: Report dir default — the gitignored reports/local tree (prereg §7.5).
DEFAULT_REPORT_DIR = REPO_ROOT / "reports" / "local" / "s5-ablation-kappa"
#: The single-look ledger's sanctioned home — FIXED, not a CLI flag: a
#: decisive stamp must be found by any later rerun regardless of --out.
SANCTIONED_LEDGER_DIR = DEFAULT_REPORT_DIR
#: The committed S5 workload — the read-only input the calibration rides.
DEFAULT_WORKLOAD_PATH = s5_run.STAND_ROOT / "workloads" / "default.jsonl"
#: Sidecar HMAC key for the run-scoped usage sink (test-plane local, a
#: fixed constant keeps runs byte-stable; never a secret of any install).
USAGE_HMAC_KEY = b"kappa-ablation-usage-key-000000"

# ── Frozen constants (docs/experiments/touched-rate-kappa.md) ────────────────

#: Preregistered decisive sample size (T).
TURNS = 50
#: Engineering runs may never exceed this many turns (prereg §Анти-HARKing).
ENGINEERING_MAX_TURNS = 10
#: Evaluable block decisions under the floor: rule as-is + power warning.
DECISIONS_FLOOR = 60
#: Sampled turns under this many warn (prereg's ~50; T < 60 warning).
TURNS_FLOOR_WARN = 60
#: κ̂ gate (methodology §3.2 canon; frozen).
KAPPA_GATE = 0.6
#: CI95 lower-bound gate (frozen).
CI95_FLOOR = 0.5
#: Cluster bootstrap resamples (frozen).
BOOTSTRAP_RESAMPLES = 10_000
#: The shingle-echo signal domain: reportable blocks only (frozen).
SIGNAL_DOMAIN_MIN_TOKENS = 40
#: Prevalence beyond these forces the explicit κ-interpretability warning.
PREVALENCE_WARN_HI = 0.9
PREVALENCE_WARN_LO = 0.1
#: Bias index beyond this forces the asymmetric-marginals warning.
BIAS_WARN = 0.2

#: Fixed sampling seed — a frozen constant, never a CLI parameter.
SAMPLING_SEED = "kappa-ablation-sampling"
#: Bootstrap draw-stream key (prereg: keyed on the tape fingerprint).
BOOTSTRAP_KEY = "kappa-bootstrap"

LOGICAL_CLOCK = "logical-clock-v1"
#: ONE token estimator for every leg — reusing the S5 stand's function
#: keeps the token domain identical to the fingerprinted corpus.
estimate_tokens = s5_run.estimate_tokens
#: M-arm window budget shared with the S5 stand.
DEFAULT_BUDGET = s5_run.DEFAULT_BUDGET

#: Shingle-echo signal tokenizer — the canon's word tokenizer (same
#: regex family as the sidecar's dynamism shingling).
SHINGLE_SIZE = 5
_WORD_RE = re.compile(r"\w+", re.UNICODE)


# ── The touched signal (frozen v1 implementation, hash-pinned) ───────────────


def touched_signal(answer: str, block_text: str) -> bool:
    """Shingle-echo touched predicate (the v1 harness-report signal).

    The block is REPORTED as touched iff at least one word 5-shingle of
    the block text also occurs in the model answer's word 5-shingles.
    Domain gate: the caller applies ``in_signal_domain`` (\u200b>= 40
    tokens) BEFORE calling this, mirroring the integration's
    post_llm_call annex contract. The FUNCTION SOURCE is fingerprinted
    (``signal_implementation_hash``) and the hash folds into the tape
    fingerprint: any edit here is a DIFFERENT signal — the old
    calibration cannot be reused (a new pre-registration is required).
    """
    a_words = _WORD_RE.findall(answer)
    b_words = _WORD_RE.findall(block_text)
    if len(a_words) < SHINGLE_SIZE or len(b_words) < SHINGLE_SIZE:
        return False
    a_set = {
        tuple(a_words[i : i + SHINGLE_SIZE]) for i in range(len(a_words) - SHINGLE_SIZE + 1)
    }
    b_set = {
        tuple(b_words[i : i + SHINGLE_SIZE]) for i in range(len(b_words) - SHINGLE_SIZE + 1)
    }
    return bool(a_set & b_set)


def in_signal_domain(tokens: int) -> bool:
    """The frozen >= 40-token reportability gate (methodology §3.2)."""
    return tokens >= SIGNAL_DOMAIN_MIN_TOKENS


def signal_implementation_hash() -> str:
    """BLAKE2b over the signal function's source (drift = fp mismatch)."""
    return hashlib.blake2b(
        inspect.getsource(touched_signal).encode("utf-8"), digest_size=32
    ).hexdigest()


# ── Deterministic sampling (frozen stratified draw) ─────────────────────────


def _stable_int(key: str, n: int) -> int:
    """One stable draw in [0, n) from a BLAKE2b chain — no RNG state."""
    return int.from_bytes(
        hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest(), "big"
    ) % max(1, n)


def stratum_quota(sizes: dict[str, int], t: int) -> dict[str, int]:
    """Largest-remainder allocation of ``t`` proportional to stratum sizes.

    Deterministic (no RNG): quotas floor the proportional shares, the
    remainder goes to the largest fractional parts, ties broken by
    stratum name — byte-stable on any machine.
    """
    total = sum(sizes.values())
    if total == 0:
        return {s: 0 for s in sizes}
    exact = {s: t * size / total for s, size in sizes.items()}
    quota = {s: int(v) for s, v in exact.items()}
    order = sorted(sizes, key=lambda s: (-(exact[s] - quota[s]), s))
    for s in order[: t - sum(quota.values())]:
        quota[s] += 1
    return quota


def turn_fingerprint(workload: Workload, query: dict[str, Any]) -> str:
    """Content fingerprint of one sampled turn: session context + the query.

    The context is every write/hint of the query's session (the store
    content the assembly draws from); canonical-line hashing makes any
    sample mutation a new fingerprint — i.e. a new tape.
    """
    sid = query["sid"]
    lines = [
        canonical_line(e).decode("utf-8")
        for e in workload.events
        if e["sid"] == sid and e["kind"] in ("write", "hint")
    ]
    lines.append(canonical_line(query).decode("utf-8"))
    blob = "\n".join(sorted(lines))
    return hashlib.blake2b(blob.encode("utf-8"), digest_size=16).hexdigest()


def sample_turns(workload: Workload, t: int = TURNS) -> list[dict[str, Any]]:
    """The frozen stratified T-turn sample (unit: query turns).

    Quotas are proportional to the tape's stratum composition; the
    in-stratum picks are a contiguous block of ``take`` queries starting
    at a BLAKE2b draw keyed on ``(SAMPLING_SEED, stratum)`` — stable
    across runs. The sample is fixed at the look; post-hoc sample
    extension is a preregistered breach (anti-HARKing).
    """
    by_stratum: dict[str, list[dict[str, Any]]] = {}
    for q in workload.queries():
        by_stratum.setdefault(q["stratum"], []).append(q)
    quota = stratum_quota({s: len(qs) for s, qs in by_stratum.items()}, t)
    sampled: list[dict[str, Any]] = []
    for s in sorted(by_stratum):
        qs = by_stratum[s]
        take = quota[s]
        if take >= len(qs):
            sampled.extend(qs)
        else:
            start = _stable_int(f"{SAMPLING_SEED}|{s}", len(qs))
            sampled.extend(qs[(start + j) % len(qs)] for j in range(take))
    return sampled


# ── Store layer (S5 sibling semantics, untouched S5 files) ───────────────────


class KappaAblationArm:
    """M-arm wrapper: full vs block-ablated assembly on one store copy.

    The wrapped :class:`s5_run.SqliteMemoryArm` supplies the store
    (fresh temp file, or the caller's backup-API clone path), the write
    path and the FTS recall — the calibration exercises the stand's
    REAL recall/score/budget semantics, not a reimplementation.

    Ablation semantics (prereg §Ground truth): the candidate block is
    removed from the CANDIDATE list BEFORE budget-assembly. The full
    leg assembles the same candidate list with the stand's budget; the
    ablated leg assembles the list minus the candidate — the freed
    budget can admit the next candidate, exactly as a genuinely
    pre-assembly removal would. No post-hoc text surgery anywhere.
    """

    def __init__(self, db_path: Path) -> None:
        self.arm = s5_run.SqliteMemoryArm(db_path, sidecar_path=None)
        #: The phase-C usage-loop sink (wired by the runner before
        #: replay: every serve path refuses without it).
        self.usage: MetricsStore | None = None
        self._candidates_budget = 10**9  # candidate recall is unbounded

    def replay_context(self, workload: Workload) -> None:
        """Replay writes/hints in tape order (store state == S5 M arm's)."""
        for event in workload.events:
            if event["kind"] == "write":
                self.arm.put(event["text"], event["mid"], event["sid"])
            elif event["kind"] == "hint":
                self.arm.put(event["text"], f"hint-{event['t']}", event["sid"])

    def candidates(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        """The FTS candidate list (sorted, budget-free) — assembly INPUT."""
        return self.arm._recall(query["text"], self._candidates_budget)

    @staticmethod
    def assemble(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Budget-bounded assembly — the stand's own loop, same order."""
        blocks: list[dict[str, Any]] = []
        used = 0
        for b in candidates:
            tokens = int(b["tokens"])
            if used + tokens > DEFAULT_BUDGET:
                continue  # budget-bounded: skip, try the next candidate
            blocks.append(b)
            used += tokens
        return blocks

    def serve_full(self, workload: Workload, query: dict[str, Any]) -> dict[str, Any]:
        """The full (production-shaped) assembly for one sampled turn."""
        blocks = self.assemble(self.candidates(query))
        text = "\n\n".join(str(b["content"]) for b in blocks)
        result = self._build_result(workload, query, blocks, text)
        if self.usage is None:
            raise SystemExit("INTERNAL: usage sink not wired before serve_full")
        metrics_id = self.usage.record_assemble(result)
        if metrics_id is None:
            raise SystemExit(
                "SINK GUARD: record_assemble failed on the sidecar — the"
                " usage-loop contract must hold for the calibration to be"
                " valid (loud refusal, never a fabricated report)"
            )
        return {"metrics_id": metrics_id, "blocks": blocks, "text": text}

    def serve_ablated(self, workload: Workload, query: dict[str, Any], memory_id: str) -> str:
        """The ablated assembly: ONE candidate removed pre-assembly."""
        candidates = [
            b
            for b in self.candidates(query)
            if str(b["memory_id"]) != memory_id
        ]
        blocks = self.assemble(candidates)
        return "\n\n".join(str(b["content"]) for b in blocks)

    def _build_result(
        self,
        workload: Workload,
        query: dict[str, Any],
        blocks: list[dict[str, Any]],
        text: str,
    ) -> dict[str, Any]:
        """The assemble result shape the sink contract consumes."""
        return {
            "session": query["sid"],
            "project": s5_run.PROJECT,
            "agent": s5_run.AGENT,
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
            "tokens": {"budget": DEFAULT_BUDGET, "estimated": estimate_tokens(text)},
            "stats": {
                "stages": ["recall", "budget"],
                "recall": {"query_source": "explicit"},
            },
        }

    def close(self) -> None:
        self.arm.close()


def _scripted_answer(window_text: str) -> str:
    """The S5 stand's scripted 'model': the answer ECHOES the served window.

    v1 synthetic honesty: the stand has no model — the validator judges
    the window, and the shingle-echo needs an answer derived from what
    the call received. The v1 calibration therefore tests the SIGNAL
    itself (shingle-echo against ablation), per the pre-registration's
    disclaimer — never the behavior of external harnesses.
    """
    return window_text


# ── Single-look ledger ───────────────────────────────────────────────────────


class FingerprintLedger:
    """A decisive look is stamped per tape fingerprint — exactly once.

    The sanctioned single-look mechanism (assignment + prereg §Анти-
    HARKing): the ledger records decisive-done stamps keyed on the tape
    fingerprint BEFORE any ablation is executed; a second decisive run
    on the same fingerprint refuses loudly (exit 1).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def check(self, tape_fp: str) -> str | None:
        """The prior stamp for ``tape_fp``, or None when free."""
        done = self._read().get("decisive_done", {})
        prior = done.get(tape_fp)
        return json.dumps(prior, sort_keys=True) if prior is not None else None

    def stamp(self, tape_fp: str) -> None:
        data = self._read()
        done = data.setdefault("decisive_done", {})
        if self.check(tape_fp) is not None:
            return  # never rewrite an existing stamp
        done[tape_fp] = {
            "stand_version": STAND_VERSION,
            "signal_hash": signal_implementation_hash(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        return data if isinstance(data, dict) else {}


def enforce_single_look(out_dir: Path, tape_fp: str) -> None:
    """Exit 1 when the decisive look for ``tape_fp`` already happened."""
    prior = FingerprintLedger(Path(out_dir) / "fingerprint-ledger.json").check(tape_fp)
    if prior is not None:
        raise SystemExit(
            "SINGLE-LOOK GUARD: a decisive look for tape fingerprint"
            f" {tape_fp[:12]}.. was already stamped ({prior[:24]}..) — the"
            " single look is exhausted; recalibration requires a NEW"
            " pre-registration"
        )


# ── The run ──────────────────────────────────────────────────────────────────


def run_ablation(
    workload: Workload,
    *,
    out_dir: Path,
    mode: str,
    turns: int = TURNS,
    resamples: int = BOOTSTRAP_RESAMPLES,
    source_db: Path | None = None,
    expected_workload_fp: str | None = None,
) -> tuple[dict[str, Any], int]:
    """One ablation pass. Returns (report, exit_code).

    ``expected_workload_fp`` pins the tape (default: the committed S5
    default workload's fingerprint — the only preregistered corpus;
    tests pass their tiny tape's fp to exercise the decisive paths
    without burning the real single look).

    Order of operations encodes the honnesty guards: the tape
    fingerprint is computed from the sampled turns + candidates, the
    single-look ledger stamps BEFORE the ablation loop, and κ is only
    ever computed in decisive mode with at least one evaluable
    decision.
    """
    if mode not in ("engineering", "decisive"):
        raise ValueError(f"unknown mode {mode!r}")
    if mode == "engineering" and turns > ENGINEERING_MAX_TURNS:
        raise SystemExit(
            f"ENGINEERING-RUN GUARD: engineering mode replays <= "
            f"{ENGINEERING_MAX_TURNS} turns (got {turns}) — the full-T look"
            " belongs to decisive mode"
        )
    if mode == "decisive" and turns != TURNS and expected_workload_fp is None:
        raise SystemExit(
            f"DECISIVE-RUN GUARD: decisive mode replays the preregistered"
            f" T={TURNS} (got {turns}) — a different T is a new"
            " pre-registration"
        )
    expected_fp = (
        expected_workload_fp if expected_workload_fp is not None else committed_fp()
    )
    if workload.fingerprint != expected_fp:
        raise SystemExit(
            "FINGERPRINT GUARD: workload fingerprint "
            f"{workload.fingerprint[:12]}.. != expected {expected_fp[:12]}.."
            " — the calibration rides the FROZEN tape; drift is an abort"
        )

    signal_hash = signal_implementation_hash()
    sampled = sample_turns(workload, turns)

    # ── phase 1: candidate discovery (the cheap pre-look pass) ───────
    full_legs: dict[str, dict[str, Any]] = {}
    excluded_full_zero: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mnemos-kappa-") as tmp_name:
        tmp = Path(tmp_name)
        if source_db is not None:
            if not Path(source_db).exists():
                raise FileNotFoundError(f"source store not found: {source_db}")
            m_db_path = s5_run.clone_source_store(Path(source_db), tmp / "m-arm")
        else:
            m_db_path = tmp / "m-arm" / "store.sqlite"
        usage = MetricsStore(tmp / "usage" / "metrics.sqlite", hmac_key=USAGE_HMAC_KEY)
        arm = KappaAblationArm(m_db_path)
        try:
            arm.usage = usage
            arm.replay_context(workload)
            for query in sampled:
                if query["qid"] in full_legs:
                    raise SystemExit(
                        f"sampling guard: duplicate qid {query['qid']!r} in"
                        " the sample — sampling draw is corrupt"
                    )
                full = arm.serve_full(workload, query)
                ok_full, _ = s5_run._window_success(full["text"], query["expect"])
                full_legs[query["qid"]] = full | {"success_full": ok_full}
                if not ok_full:
                    excluded_full_zero.append(query["qid"])

            # ── tape fingerprint (sampled turns + candidates + seed +
            #    signal hash) — computable BEFORE the ablation loop,
            #    so a second decisive look never reaches the data ─────
            candidates_by_turn = {
                qid: [str(b["memory_id"]) for b in leg["blocks"]]
                for qid, leg in full_legs.items()
            }
            tape_fp = _tape_fingerprint(
                workload,
                turns=turns,
                mode=mode,
                signal_hash=signal_hash,
                candidates=candidates_by_turn,
            )
            if mode == "decisive":
                # The decisive look is stamped ONCE — in the SANCTIONED
                # ledger (the preregistered report dir), never in a
                # caller-supplied scratch dir: a rerun against a fresh
                # --out must still find the stamp and refuse.
                enforce_single_look(SANCTIONED_LEDGER_DIR, tape_fp)
                FingerprintLedger(
                    SANCTIONED_LEDGER_DIR / "fingerprint-ledger.json"
                ).stamp(tape_fp)
        finally:
            arm.close()
            usage.close()

    # ── phase 2: the look — ablations, labels, (maybe) kappa ─────────
    decisions: list[dict[str, Any]] = []
    provenance: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="mnemos-kappa-ab-") as tmp_name:
        tmp = Path(tmp_name)
        if source_db is not None:
            m_db_path = s5_run.clone_source_store(Path(source_db), tmp / "m-arm")
        else:
            m_db_path = tmp / "m-arm" / "store.sqlite"
        usage = MetricsStore(tmp / "usage" / "metrics.sqlite", hmac_key=USAGE_HMAC_KEY)
        arm = KappaAblationArm(m_db_path)
        try:
            arm.usage = usage
            arm.replay_context(workload)
            for query in sampled:
                full = arm.serve_full(workload, query)
                ok_full, _ = s5_run._window_success(full["text"], query["expect"])
                if not ok_full:
                    # Preregistered exclusion: with task_success(full)=0
                    # no degradation is possible below zero.
                    provenance.append(
                        {
                            "qid": query["qid"],
                            "stratum": query["stratum"],
                            "status": "excluded_full_zero",
                        }
                    )
                    continue
                answer = _scripted_answer(full["text"])
                ordinals_touched = _touched_ordinals(usage, full, answer)
                for i, block in enumerate(full["blocks"]):
                    if not in_signal_domain(int(block["tokens"])):
                        continue  # preregistered exclusion: below the domain
                    ablated_text = arm.serve_ablated(
                        workload, query, str(block["memory_id"])
                    )
                    ok_ab, _ = s5_run._window_success(ablated_text, query["expect"])
                    decisions.append(
                        _decision(
                            query=query,
                            block=block,
                            index=i,
                            success_full=True,
                            success_ablated=ok_ab,
                            pred=1 if f"{full['metrics_id']}:{i}" in ordinals_touched else 0,
                        )
                    )
                provenance.append(
                    {"qid": query["qid"], "stratum": query["stratum"], "status": "evaluated"}
                )
        finally:
            arm.close()
            usage.close()

    report = _build_report(
        workload=workload,
        mode=mode,
        turns=turns,
        expected_fp=expected_fp,
        signal_hash=signal_hash,
        tape_fp=tape_fp,
        sampled=sampled,
        decisions=decisions,
        candidates_by_turn=candidates_by_turn,
        excluded_full_zero=excluded_full_zero,
        resamples=resamples,
    )
    _write_reports(out_dir, report)
    return report, _decisive_exit_code(report)


def _touched_ordinals(
    usage: MetricsStore, full: dict[str, Any], answer: str
) -> set[str]:
    """The harness report via the REAL usage contract: record → read back.

    For each injected block inside the signal domain, the stand's
    shingle-echo decides touched; the touched ordinals
    (``"<metrics_id>:<i>"`` — the sink's born-final opaque block ids)
    go through ``record_usage`` and the prediction labels are read BACK
    from ``usage_reports``: the calibration consumes what the plane
    actually stored, never a private side computation.
    """
    ordinals = [
        f"{full['metrics_id']}:{i}"
        for i, block in enumerate(full["blocks"])
        if in_signal_domain(int(block["tokens"]))
        and touched_signal(answer, str(block["content"]))
    ]
    if ordinals:
        recorded = usage.record_usage(
            full["metrics_id"],
            block_ids_touched=ordinals,
            tokens_out=estimate_tokens(answer),
        )
        if recorded is None:
            raise SystemExit(
                "SINK GUARD: record_usage refused a well-formed report —"
                " the usage contract must hold for calibration (loud abort)"
            )
    return set(_read_touched_json(usage, int(full["metrics_id"])))


def _read_touched_json(usage: MetricsStore, metrics_id: int) -> list[str]:
    """The stored report, read back through the sink's own connection."""
    conn = usage._conn()
    if conn is None:
        raise SystemExit("SINK GUARD: sidecar unavailable for the report read-back")
    row = conn.execute(
        "SELECT block_ids_touched_json FROM usage_reports WHERE metrics_id = ?",
        (metrics_id,),
    ).fetchone()
    if row is None:
        return []
    touched = json.loads(str(row[0]))
    if not isinstance(touched, list):
        raise SystemExit("SINK GUARD: unreadable touched JSON on read-back")
    return [str(x) for x in touched]


def _decision(
    *,
    query: dict[str, Any],
    block: dict[str, Any],
    index: int,
    success_full: bool,
    success_ablated: bool,
    pred: int,
) -> dict[str, Any]:
    """One CONFUSION row: truth (ablation) vs report (shingle echo)."""
    return {
        "turn_id": query["qid"],
        "stratum": query["stratum"],
        "block_id": str(block["memory_id"]),
        "block_ordinal": index,
        "tokens": int(block["tokens"]),
        "score": float(block.get("score") or 0.0),
        "success_full": 1 if success_full else 0,
        "success_ablated": 1 if success_ablated else 0,
        "gt": 1 if (success_full and not success_ablated) else 0,
        "pred": pred,
    }


# ── Kappa statistics (frozen definitions) ────────────────────────────────────


def confusion_matrix(rows: list[dict[str, Any]]) -> dict[str, int]:
    """TP/FN/FP/TN over block decisions (rows = truth, columns = report)."""
    return {
        "tp": sum(1 for r in rows if r["gt"] == 1 and r["pred"] == 1),
        "fn": sum(1 for r in rows if r["gt"] == 1 and r["pred"] == 0),
        "fp": sum(1 for r in rows if r["gt"] == 0 and r["pred"] == 1),
        "tn": sum(1 for r in rows if r["gt"] == 0 and r["pred"] == 0),
    }


def kappa_from_confusion(cm: dict[str, int]) -> dict[str, Any]:
    """Cohen's κ (unweighted, block-level) from one confusion matrix.

    kappa = (p_o - p_e) / (1 - p_e); p_o = (TP+TN)/N, p_e from the
    marginals. Degenerate marginals (either rater constant) → κ None →
    the frozen rule sees an undefined κ → NOT-CALIBRATED (H-K0).
    """
    n = cm["tp"] + cm["fn"] + cm["fp"] + cm["tn"]
    if n == 0:
        return {
            "kappa_hat": None,
            "observed_agreement": None,
            "expected_agreement": None,
            "degenerate": False,
            "undefined_reason": "empty matrix",
        }
    p_o = (cm["tp"] + cm["tn"]) / n
    p_gt = (cm["tp"] + cm["fn"]) / n
    p_pred = (cm["tp"] + cm["fp"]) / n
    p_e = p_gt * p_pred + (1 - p_gt) * (1 - p_pred)
    degenerate = p_gt in (0.0, 1.0) or p_pred in (0.0, 1.0)
    kappa_hat = None if degenerate or (1 - p_e) == 0 else (p_o - p_e) / (1 - p_e)
    return {
        "kappa_hat": None if kappa_hat is None else round(kappa_hat, 6),
        "observed_agreement": round(p_o, 6),
        "expected_agreement": round(p_e, 6),
        "degenerate": degenerate,
        "undefined_reason": (
            None
            if kappa_hat is not None
            else ("degenerate marginals" if degenerate else "p_e == 1")
        ),
    }


def sensitivity_from_confusion(cm: dict[str, int]) -> dict[str, Any]:
    """Prevalence + PI + BI + PABAK (Byrt 1993) — printed, never a gate.

    PABAK = 2 * p_o - 1 is the prevalence-independent agreement index;
    the frozen rule NEVER substitutes it for κ (prereg §Статистика).
    """
    n = cm["tp"] + cm["fn"] + cm["fp"] + cm["tn"]
    p_o = (cm["tp"] + cm["tn"]) / n
    prevalence = (cm["tp"] + cm["fn"]) / n
    return {
        "prevalence_truth": round(prevalence, 6),
        "prevalence_index_pi": round(abs(cm["tp"] * cm["tn"] - cm["fn"] * cm["fp"]) / (n * n), 6),
        "bias_index_bi": round(abs((cm["tp"] + cm["fp"]) / n - prevalence), 6),
        "pabak": round(2 * p_o - 1, 6),
        "reference": "Byrt et al. 1993: PABAK = 2 * p_o - 1 (sensitivity only)",
    }


def kappa_with_ci(
    rows: list[dict[str, Any]],
    *,
    tape_fp: str,
    resamples: int,
) -> dict[str, Any]:
    """κ̂ + turn-cluster bootstrap CI95 (frozen constants and stream).

    Blocks are nested in turns: a resample redraws whole TURNS with
    replacement (resampling blocks alone would understate the variance).
    Draws come from the S5 ``_draw_stream`` counter chain keyed on
    ``(BOOTSTRAP_KEY, tape_fingerprint)`` — byte-stable runs. Resamples
    with degenerate marginals yield no κ and are dropped; < 2 surviving
    draws → CI None → the frozen rule finds the floor unmet.
    """
    cm = confusion_matrix(rows)
    point = kappa_from_confusion(cm)
    turns_ordered = sorted({r["turn_id"] for r in rows})
    by_turn: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_turn.setdefault(r["turn_id"], []).append(r)
    n_t = len(turns_ordered)
    boot: list[float] = []
    if n_t and len(rows) > 0:
        key = f"{BOOTSTRAP_KEY}|{tape_fp}"
        for r in range(resamples):
            pooled: list[dict[str, Any]] = []
            for draw in s5_run._draw_stream(key, r, n_t):
                pooled.extend(by_turn[turns_ordered[draw % n_t]])
            if not pooled:
                continue  # pragma: no cover - n_t draw never yields empty
            b_cm = confusion_matrix(pooled)
            b_point = kappa_from_confusion(b_cm)
            if b_point["kappa_hat"] is not None:
                boot.append(float(b_point["kappa_hat"]))
    boot.sort()
    ci = (
        [
            round(boot[int(0.025 * (len(boot) - 1))], 6),
            round(boot[int(0.975 * (len(boot) - 1))], 6),
        ]
        if len(boot) >= 2
        else None
    )
    out = dict(point)
    out.update(
        {
            "ci95": ci,
            "n": len(rows),
            "n_turns": n_t,
            "resamples": resamples,
            "confusion": cm,
            "sensitivity": sensitivity_from_confusion(cm) if rows else None,
        }
    )
    return out


# ── Tape fingerprint ─────────────────────────────────────────────────────────


def _tape_fingerprint(
    workload: Workload,
    *,
    turns: int,
    mode: str,
    signal_hash: str,
    candidates: dict[str, list[str]],
) -> str:
    """BLAKE2b over the ablation tape's defining constants (frozen inputs).

    Per the prereg §Анти-HARKing: sampled turns + candidates per turn +
    seed + the touched-signal implementation hash. Mode and turn count
    are bound too so engineering and decisive tapes are distinct
    objects and sample mutation is detectable (different fp = different
    tape = the decisive stamp does not carry over).
    """
    per_turn: list[str] = []
    for q in sample_turns(workload, turns):
        qid = q["qid"]
        per_turn.append(
            f"{qid}|{turn_fingerprint(workload, q)}|{','.join(candidates.get(qid, []))}"
        )
    blob = "\n".join(
        [
            "kappa-ablation-tape-v1",
            workload.fingerprint,
            SAMPLING_SEED,
            f"turns={turns}",
            f"mode={mode}",
            f"signal={signal_hash}",
            *sorted(per_turn),
        ]
    )
    return hashlib.blake2b(blob.encode("utf-8"), digest_size=32).hexdigest()


# ── Verdict + report ─────────────────────────────────────────────────────────


def _build_report(
    *,
    workload: Workload,
    mode: str,
    turns: int,
    expected_fp: str,
    signal_hash: str,
    tape_fp: str,
    sampled: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    candidates_by_turn: dict[str, list[str]],
    excluded_full_zero: list[str],
    resamples: int,
) -> dict[str, Any]:
    """The canonical report (one shape; tests pin byte-stability)."""
    evaluable_turns = len(sampled) - len(excluded_full_zero)
    n_candidates = sum(len(v) for v in candidates_by_turn.values())
    by_stratum: dict[str, int] = {}
    for q in sampled:
        by_stratum[q["stratum"]] = by_stratum.get(q["stratum"], 0) + 1
    notes: list[str] = []
    if mode == "engineering":
        kappa_section: dict[str, Any] | None = None
        sensitivity: dict[str, Any] | None = None
        verdict = {
            "status": "ENGINEERING",
            "reasons": [
                "engineering run: kappa is NEVER computed or published"
                " (frozen pre-registration anti-HARKing clause)"
            ],
        }
        notes.append("engineering run: runner health only — no kappa anywhere")
    elif not decisions or evaluable_turns == 0:
        kappa_section, sensitivity = None, None
        verdict = {
            "status": "NO-DATA",
            "reasons": [
                (
                    "zero evaluable block decisions"
                    " (no in-domain candidates on evaluable turns)"
                    if evaluable_turns > 0
                    else "zero evaluable turns (task_success(full) = 0 everywhere)"
                ),
                "touched_rate is NOT corridor-eligible under this frozen protocol",
            ],
        }
        notes.append(
            "NO-DATA is loud: no kappa exists for this tape — never a kappa"
            " from too few points"
        )
    else:
        computed = kappa_with_ci(decisions, tape_fp=tape_fp, resamples=resamples)
        kappa_section = computed
        sensitivity = computed["sensitivity"]
        verdict, notes = _decisive_verdict(computed)
    if mode == "decisive" and 0 < len(decisions) < DECISIONS_FLOOR:
        notes.append(f"POWER WARNING: {len(decisions)} evaluable decisions < {DECISIONS_FLOOR}")
    if mode == "decisive" and turns < TURNS_FLOOR_WARN:
        notes.append(f"TURNS WARNING: {turns} sampled turns < 60 (preregistered ~50)")
    return {
        "stand": "kappa-ablation",
        "stand_version": STAND_VERSION,
        "logical_clock": LOGICAL_CLOCK,
        "mode": mode,
        "single_look": mode,
        "preregistration": "docs/experiments/touched-rate-kappa.md",
        "workload_fingerprint": workload.fingerprint,
        "workload_is_committed_s5_default": workload.fingerprint == committed_fp(),
        "signal_implementation_hash": signal_hash,
        "tape_fingerprint": tape_fp,
        "sampling": {
            "seed": SAMPLING_SEED,
            "requested_turns": turns,
            "sampled_turns": len(sampled),
            "turns_by_stratum": dict(sorted(by_stratum.items())),
            "evaluable_turns": evaluable_turns,
            "excluded_full_zero_turns": len(excluded_full_zero),
            "excluded_full_zero_qids": sorted(excluded_full_zero),
            "evaluable_turn_share": (
                round(evaluable_turns / len(sampled), 6) if sampled else None
            ),
        },
        "candidates": {
            "total": n_candidates,
            "in_signal_domain": len(decisions),
            "below_domain_excluded": n_candidates - len(decisions),
        },
        "decisions": {"n": len(decisions), "rows": decisions},
        "kappa": kappa_section,
        "sensitivity": sensitivity,
        "verdict": verdict,
        "status_notes": notes,
        "honesty": [
            "NO-DATA is loud: never green, never zero;",
            "the frozen rule uses kappa only — PABAK prints as sensitivity;",
            "raw text never enters artifacts (ids and counts only);",
            "single decisive look per tape fingerprint (ledger-enforced).",
        ],
    }


def _decisive_verdict(computed: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Apply the FROZEN decision rule to one computed κ section."""
    notes: list[str] = []
    kappa_hat = computed["kappa_hat"]
    ci = computed["ci95"]
    if kappa_hat is None:
        notes.append(
            "degenerate marginals: an estimator is constant — kappa undefined"
        )
        return (
            {
                "status": "NOT-CALIBRATED",
                "reasons": ["kappa undefined (degenerate marginals)", "H-K0 holds"],
            },
            notes,
        )
    if ci is None:  # a computed kappa always carries a CI in decisive mode
        raise SystemExit("INTERNAL: kappa computed without a CI — report would be ungateable")
    eligible = kappa_hat >= KAPPA_GATE and ci[0] >= CI95_FLOOR
    if eligible:
        notes.append("touched_rate is corridor-eligible per the frozen rule")
    else:
        notes.append("touched_rate is NOT a corridor metric (H-K0) — a valid outcome")
    prevalence = (computed["sensitivity"] or {}).get("prevalence_truth")
    bias = (computed["sensitivity"] or {}).get("bias_index_bi")
    if prevalence is not None and (
        prevalence > PREVALENCE_WARN_HI or prevalence < PREVALENCE_WARN_LO
    ):
        notes.append(
            f"prevalence {prevalence} is outside [{PREVALENCE_WARN_LO},"
            f" {PREVALENCE_WARN_HI}] — kappa's interpretability is limited"
            " (printed warning, rule unchanged)"
        )
    if bias is not None and bias > BIAS_WARN:
        notes.append(f"bias index {bias} > {BIAS_WARN} — marginals asymmetric (warning)")
    return (
        {
            "status": "CORRIDOR-ELIGIBLE" if eligible else "NOT-CALIBRATED",
            "reasons": [
                f"kappa_hat {kappa_hat} vs gate {KAPPA_GATE};"
                f" CI95 lower {ci[0]} vs floor {CI95_FLOOR}"
            ],
        },
        notes,
    )


def _decisive_exit_code(report: dict[str, Any]) -> int:
    """The generator alarms, never edits: exit 1 on any non-pass verdict."""
    if report["mode"] == "engineering":
        return 0
    return 0 if report["verdict"]["status"] == "CORRIDOR-ELIGIBLE" else 1


def _write_reports(out_dir: Path, report: dict[str, Any]) -> None:
    """Write tape.jsonl + report.json + report.md (ids/counts only)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tape.jsonl").write_text(
        "".join(
            json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n"
            for r in report["decisions"]["rows"]
        ),
        encoding="utf-8",
    )
    (out_dir / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    print(f"kappa-ablation: report -> {out_dir / 'report.json'}", file=sys.stderr)


def render_markdown(report: dict[str, Any]) -> str:
    """The MD summary: verdict first, honesty stamps loud, no raw text."""
    sampling = report["sampling"]
    verdict = report["verdict"]
    kappa = report["kappa"]
    lines: list[str] = [
        "# Kappa ablation — touched-signal calibration (phase C)",
        "",
        f"- pre-registration: `{report['preregistration']}` (frozen)",
        f"- mode / single-look: {report['mode']} / {report['single_look']}",
        f"- workload fingerprint: `{report['workload_fingerprint']}`"
        + (" (committed S5 tape)" if report["workload_is_committed_s5_default"] else ""),
        f"- tape fingerprint: `{report['tape_fingerprint']}`",
        f"- signal implementation hash: `{report['signal_implementation_hash']}`",
        f"- sampled turns: {sampling['sampled_turns']}"
        f" (evaluable {sampling['evaluable_turns']}, excluded full-zero"
        f" {sampling['excluded_full_zero_turns']}) by stratum"
        f" {sampling['turns_by_stratum']}",
        f"- block decisions: {report['decisions']['n']}"
        f" (candidates {report['candidates']['total']}, below-domain excluded"
        f" {report['candidates']['below_domain_excluded']})",
        "",
    ]
    for note in report["status_notes"]:
        lines += [f"> {note}", ""]
    lines += ["## Verdict", "", f"**{verdict['status']}**", ""]
    lines += [f"- {reason}" for reason in verdict["reasons"]] or ["- (none)"]
    lines += ["", "## Kappa", ""]
    if kappa is None or kappa.get("kappa_hat") is None:
        lines.append(
            "- NO-DATA (engineering mode never computes kappa; decisive mode"
            " with no evaluable decisions or degenerate marginals)"
        )
    else:
        ci = kappa["ci95"]
        sens = kappa["sensitivity"]
        lines += [
            f"- kappa_hat: {kappa['kappa_hat']} (n = {kappa['n']} decisions,"
            f" {kappa['n_turns']} turns)",
            "- CI95 (turn-cluster bootstrap"
            f", {kappa['resamples']} resamples): "
            + (f"[{ci[0]}, {ci[1]}]" if ci else "NO-DATA"),
            "- observed agreement: "
            f"{kappa['observed_agreement']} · expected: {kappa['expected_agreement']}",
            "- confusion (truth → report): "
            f"TP {kappa['confusion']['tp']}, FN {kappa['confusion']['fn']},"
            f" FP {kappa['confusion']['fp']}, TN {kappa['confusion']['tn']}",
            "",
            "## Sensitivity (printed, never a gate)",
            "",
            f"- prevalence (truth share): {sens['prevalence_truth']}",
            f"- prevalence index PI: {sens['prevalence_index_pi']}",
            f"- bias index BI: {sens['bias_index_bi']}",
            f"- PABAK (Byrt 1993, 2*p_o - 1): {sens['pabak']}",
        ]
    lines += [
        "",
        "v1 disclaimer (pre-registration): synthetic S5 corpus — figures do NOT"
        " extrapolate to real sessions (phase D).",
        "",
    ]
    return "\n".join(lines)


# ── Constants resolved from the committed tape (read-only) ───────────────────


def committed_fp() -> str:
    """The committed S5 default workload's fingerprint (read-only input)."""
    return load_workload(DEFAULT_WORKLOAD_PATH).fingerprint


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    """CLI — engineering (default) or the single decisive look."""
    parser = argparse.ArgumentParser(
        description="Kappa ablation runner (mnemos-vitals phase C, frozen prereg)",
    )
    parser.add_argument("--mode", choices=("engineering", "decisive"), default="engineering")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_REPORT_DIR,
        help=f"report dir (default: {DEFAULT_REPORT_DIR})",
    )
    parser.add_argument(
        "--turns",
        type=int,
        default=None,
        help="engineering-only sampled-turn override (decisive stays T=50)",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="source mnemos store (backup-API clone; never written)",
    )
    args = parser.parse_args(argv)
    if args.mode == "decisive" and args.turns is not None:
        print(
            "kappa-ablation: --turns is engineering-only (decisive is frozen T=50)",
            file=sys.stderr,
        )
        return 2
    turns = TURNS if args.mode == "decisive" else (args.turns or ENGINEERING_MAX_TURNS)
    try:
        workload = load_workload(DEFAULT_WORKLOAD_PATH)
        report, exit_code = run_ablation(
            workload,
            out_dir=args.out,
            mode=args.mode,
            turns=turns,
            source_db=args.db,
        )
    except WorkloadError as exc:
        print(f"kappa-ablation: workload refused (loud): {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:
        # guard breaches (engineering cap, decisive T, fingerprint pin,
        # single look) alarm through the CLI — a refusal, never a repair
        print(f"kappa-ablation: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"kappa-ablation: {exc}", file=sys.stderr)
        return 2
    print(
        "kappa-ablation: "
        f"mode={report['mode']} n={report['decisions']['n']}"
        f" verdict={report['verdict']['status']}"
        f" fp={report['tape_fingerprint'][:12]}",
        file=sys.stderr,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
