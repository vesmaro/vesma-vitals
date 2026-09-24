"""S5 runner contract tests (phase B, zone 5) — end-to-end, tiny workloads.

Mechanical checks for every hard rule (pre-registration
``docs/experiments/s5-memory-value.md``, methodology §1/§3.5):

  - real code paths only: every test replays a real JSONL tape through
    ``run_s5`` in tmp dirs (no mocks of the arms or the metrics);
  - the H2 mandate: task_success(M) >= task_success(B0-naive) - δ is
    REQUIRED for the savings headline — a failing H2 suppresses the
    headline and prints the loud banner, at ANY freeze state;
  - NO-DATA is loud: an empty workload refuses loudly; static-window
    legs have no replay-fp-rate (never a fabricated zero);
  - determinism: same seed → same fingerprint and same medians; two
    runs → byte-identical JSON reports (fixed logical clock);
  - symmetry: B0-full tokens > B0-naive on the same tape; one estimator
    for all legs; the B0-file budget is M's mean assembled tokens;
  - freeze discipline: unfrozen baseline exits 0 with the unfrozen
    stamp; a frozen run exits 1 on breach and 0 when thresholds hold;
  - isolation: a --db source store is cloned via the backup API (source
    bytes untouched) and host memories enter recall without stand rows
    polluting the source;
  - privacy: raw text never lands in the sidecar (whole-file grep).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STAND = REPO_ROOT / "benchmarks" / "stands" / "s5_memory_value"
sys.path.insert(0, str(REPO_ROOT))

from benchmarks.stands.s5_memory_value import run as run_mod  # noqa: E402
from benchmarks.stands.s5_memory_value.run import (  # noqa: E402
    AGENT,
    CURATED_FILE_NAME,
    DEFAULT_BUDGET,
    PREREG_DELTA,
    estimate_tokens,
    main,
    parse_thresholds,
    run_s5,
)
from benchmarks.stands.s5_memory_value.workload import (  # noqa: E402
    MARKER_PREFIX,
    WorkloadError,
    load_workload,
    synthetic_workload,
    write_workload,
)

# ── Tape builders (deterministic, small, fail-loud on drift) ─────────────────

def _marker(seq: int) -> str:
    from benchmarks.stands.s5_memory_value.workload import DEFAULT_SEED, _mark

    return _mark(DEFAULT_SEED, seq)


def tiny_tape(
    *,
    good_windows: bool = True,
    sessions: int = 2,
) -> list[dict]:
    """A 3-stratum tape: 2 fact tasks + ``sessions`` sessions + 1 negative.

    Each session carries hint + 2 writes + 2 queries (mid checkpoint +
    final) — pair-capable for the dynamism corridor.

    ``good_windows=False`` swaps in markers that NO leg can serve (the M
    arm recalls nothing for them) — drives task_success(M) to 0 for the
    H2-mandate-failure tests while keeping the tape valid.
    """
    m1, m2 = _marker(101), _marker(102)
    ghost1, ghost2 = _marker(901), _marker(902)
    events: list[dict] = []
    t = 0
    for i, (marker, ghost) in enumerate(((m1, ghost1), (m2, ghost2))):
        t += 1
        events.append(
            {"t": t, "kind": "write", "sid": f"fact-{i}", "mid": marker,
             "text": f"The alpha probe {i} reports to the amber cache. ({marker})"}
        )
        t += 1
        events.append(
            {"t": t, "kind": "query", "sid": f"fact-{i}", "qid": f"fact-q-{i}",
             "stratum": "fact",
             "text": "What is the alpha probe?" if good_windows else "What is the omega drift?",
             "expect": {"markers": [ghost if not good_windows else marker], "absent": [],
                        "min_ratio": 1.0}}
        )
    for s in range(sessions):
        s1, s2 = _marker(200 + s * 2), _marker(201 + s * 2)
        t += 1
        events.append(
            {"t": t, "kind": "hint", "sid": f"session-{s}",
             "text": "Standing hint for the session: keep answers terse."}
        )
        for step, marker in enumerate((s1, s2)):
            t += 1
            events.append(
                {"t": t, "kind": "write", "sid": f"session-{s}", "mid": marker,
                 "text": f"session step {step}: logged the checkpoint. ({marker})"}
            )
        t += 1
        events.append(
            {"t": t, "kind": "query", "sid": f"session-{s}", "qid": f"session-q-{s}-mid",
             "stratum": "session", "text": "What has happened in the session so far?",
             "expect": {"markers": [s1, s2], "absent": [], "min_ratio": 1.0}}
        )
        t += 1
        events.append(
            {"t": t, "kind": "query", "sid": f"session-{s}", "qid": f"session-q-{s}-final",
             "stratum": "session", "text": "Summarize the session so far.",
             "expect": {"markers": [s1, s2], "absent": [], "min_ratio": 0.5}}
        )
    t += 1
    events.append(
        {"t": t, "kind": "query", "sid": "negative-0", "qid": "negative-q-0",
         "stratum": "negative", "text": "Tell me about the orbital plumbing handbook.",
         "expect": {"markers": [], "absent": [m1, m2], "min_ratio": 1.0}}
    )
    return events


@pytest.fixture()
def tape_path(tmp_path: Path) -> Path:
    path = tmp_path / "tape.jsonl"
    write_workload(path, tiny_tape())
    return path


@pytest.fixture()
def out_dir(tmp_path: Path) -> Path:
    return tmp_path / "out"


def _run(workload_path: Path | None, out: Path, *extra: str) -> tuple[int, dict, Path]:
    """CLI entry with the default tape pinned OUT of the repo (tmp copy)."""
    argv: list[str] = []
    if workload_path is not None:
        argv += ["--workload", str(workload_path)]
    argv += ["--out", str(out), *extra]
    code = main(argv)
    report = json.loads((out / "s5-report.json").read_text(encoding="utf-8"))
    return code, report, out


# ── End-to-end (real code paths, tmp dirs) ───────────────────────────────────

class TestEndToEnd:
    def test_cli_replay_full_pipeline(self, tape_path: Path, out_dir: Path) -> None:
        code, report, out = _run(tape_path, out_dir)
        assert code == 0  # baseline run: unfrozen thresholds never exit 1
        assert report["stand"] == "s5-memory-value"
        assert report["thresholds"]["status"] == "unfrozen"
        assert report["isolation"]["mode"] == "fresh-tmp-store"
        assert report["isolation"]["actor"] == AGENT
        # all four preregistered legs always present (symmetric reporting)
        assert set(report["legs"]) == {"M", "B0-naive", "B0-file", "B0-full"}
        for leg in report["legs"].values():
            assert leg["metrics"]["n_tasks"] == report["workload"]["queries"]
        # the two report files exist
        assert (out / "s5-report.json").exists()
        assert (out / "s5-report.md").exists()

    def test_m_arm_serves_real_assembles_and_sidecar(
        self, tape_path: Path, out_dir: Path
    ) -> None:
        _, report, _ = _run(tape_path, out_dir)
        m = report["legs"]["M"]["metrics"]
        assert m["status"] == "OK"
        # the M arm really recalled: negative control passes (markers of
        # unrelated facts are NOT in its window) and fact tasks pass
        per_stratum = m["per_stratum"]
        assert per_stratum["fact"]["task_success"]["point"] == 1.0
        assert per_stratum["negative"]["task_success"]["point"] == 1.0
        # prompt tokens far below the B0-naive transcript sizes
        b0n_med = report["legs"]["B0-naive"]["metrics"]["prompt_tokens"]["point"]
        assert m["prompt_tokens"]["point"] < b0n_med

    def test_b0_naive_serves_full_transcript(self, tape_path: Path, out_dir: Path) -> None:
        _, report, _ = _run(tape_path, out_dir)
        tasks = report["legs"]["B0-naive"]["tasks"]
        # the transcript GROWS: later queries carry more tokens than early
        tokens = [t["prompt_tokens"] for t in tasks]
        assert tokens[-2] > tokens[0]  # session query after 3 writes+hint
        # the naive harness CANNOT pass the negative control: its window
        # carries the earlier markers (wallpaper transcript signal)
        neg = [t for t in tasks if t["stratum"] == "negative"]
        assert neg and neg[0]["success"] is False

    def test_b0_full_exceeds_b0_naive_tokens(self, tape_path: Path, out_dir: Path) -> None:
        _, report, _ = _run(tape_path, out_dir)
        full_med = report["legs"]["B0-full"]["metrics"]["prompt_tokens"]["point"]
        naive_med = report["legs"]["B0-naive"]["metrics"]["prompt_tokens"]["point"]
        assert full_med > naive_med  # sanity: whole tape > transcript-so-far

    def test_b0_file_equal_budget_by_construction(
        self, tape_path: Path, out_dir: Path
    ) -> None:
        _, report, out = _run(tape_path, out_dir)
        b0file = report["value"]["b0_file"]
        m_mean = report["value"]["m_mean_assembled_tokens"]
        assert b0file["budget_target_tokens"] == round(m_mean)
        assert b0file["measured_tokens"] <= b0file["budget_target_tokens"]
        # the curated file is a plain file OUTSIDE any store, next to the report
        curated = out / CURATED_FILE_NAME
        assert curated.exists()
        assert estimate_tokens(curated.read_text(encoding="utf-8")) == b0file["measured_tokens"]
        # every B0-file window has the SAME size (static file)
        sizes = {t["prompt_tokens"] for t in report["legs"]["B0-file"]["tasks"]}
        assert len(sizes) == 1

    def test_replay_fp_rate_m_only(self, tape_path: Path, out_dir: Path) -> None:
        _, report, _ = _run(tape_path, out_dir)
        m = report["legs"]["M"]["metrics"]
        assert m["replay_fp_rate"] is not None
        for label in ("B0-naive", "B0-file", "B0-full"):
            assert report["legs"][label]["metrics"]["replay_fp_rate"] is None

    def test_dynamism_from_real_sidecar_rows(self, tape_path: Path, out_dir: Path) -> None:
        _, report, _ = _run(tape_path, out_dir)
        dyn = report["dynamism"]
        assert dyn["status"] == "OK"
        assert dyn["sessions_total"] == report["workload"]["sessions"]
        # both tape sessions are pair-capable (2 assemblies each)
        assert dyn["pair_capable_sessions"] == 2
        assert 0.0 <= dyn["context_dynamism_ratio"]["median"] <= 1.0
        assert dyn["explicit_hint_share_median"] is not None

    def test_write_cost_positive_and_net_below_gross(
        self, tape_path: Path, out_dir: Path
    ) -> None:
        _, report, _ = _run(tape_path, out_dir)
        value = report["value"]
        assert value["write_cost_tokens"] > 0
        assert value["net_savings_tokens"] == round(
            value["gross_savings_tokens_total"] - value["write_cost_tokens"], 6
        )

    def test_h2_mandate_passes_and_headline_printed(
        self, tape_path: Path, out_dir: Path
    ) -> None:
        _, report, _ = _run(tape_path, out_dir)
        assert report["hypotheses"]["H2"]["status"] == "PASS"
        assert report["value"]["headline_invalid_h2"] is False
        # The headline additionally requires H1 (CI95 lower > 0): the tiny
        # 5-task tape has a wide CI and a write cost of the same order as
        # its savings, so H1 may be honestly unresolved here — the
        # headline discipline is what this test pins.
        if report["hypotheses"]["H1"]["status"] == "PASS":
            assert report["value"]["headline"] is not None
        else:
            assert report["value"]["headline"] is None  # H1 gate respected

    def test_source_db_mode_isolated_clone(self, tmp_path: Path, out_dir: Path) -> None:
        source = tmp_path / "source.sqlite"
        conn = sqlite3.connect(str(source))
        conn.execute(
            "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE memories_fts USING fts5(id UNINDEXED, content,"
            " tokenize='unicode61')"
        )
        conn.execute(
            "INSERT INTO memories (id, content) VALUES ('host-1',"
            " 'the alpha probe reports to the amber cache')"
        )
        conn.execute("INSERT INTO memories_fts (id, content) VALUES ('host-1',"
                     " 'the alpha probe reports to the amber cache')")
        conn.commit()
        conn.close()
        before = source.read_bytes()
        code, report, _ = _run(None, out_dir, "--db", str(source))
        assert code == 0
        assert report["isolation"]["mode"] == "backup-api-copy"
        assert report["isolation"]["source_store"] == str(source.resolve())
        assert report["isolation"]["source_memories"] == 1
        # the source store was never written (byte-identical after the run)
        assert source.read_bytes() == before

    def test_missing_db_fails_loud(self, tmp_path: Path, out_dir: Path, capsys) -> None:
        code = main(["--db", str(tmp_path / "nope.sqlite"), "--out", str(out_dir)])
        assert code == 2
        assert "source store not found" in capsys.readouterr().err

    def test_unknown_threshold_key_fails_loud(self, out_dir: Path, capsys) -> None:
        code = main(["--out", str(out_dir), "--thresholds", "bogus_key=0.5"])
        assert code == 2
        assert "unknown threshold key" in capsys.readouterr().err

    def test_freeze_without_values_fails_loud(self, out_dir: Path, capsys) -> None:
        code = main(["--out", str(out_dir), "--freeze-thresholds"])
        assert code == 2
        assert "requires --thresholds" in capsys.readouterr().err


# ── H2 mandate (the savings headline discipline) ─────────────────────────────

class TestH2Mandate:
    def test_empty_window_arm_fails_h2_and_suppresses_headline(
        self, tmp_path: Path
    ) -> None:
        """An M arm serving NOTHING must lose H2 and lose the headline."""
        workload = load_workload(self._write_tape(tmp_path, good_windows=False))
        with pytest.MonkeyPatch.context() as mp:
            # a broken M arm: serves an empty window for every query —
            # the honest-est possible "memory does not help" scenario
            mp.setattr(
                run_mod.SqliteMemoryArm, "_recall", lambda self, q, b: [], raising=True
            )
            report, _code = run_s5(
                workload, db=None, out_dir=tmp_path / "out", thresholds=None, freeze=False
            )
        assert report["hypotheses"]["H2"]["status"] == "FAIL"
        assert report["value"]["headline_invalid_h2"] is True
        assert report["value"]["headline"] is None
        assert report["value"]["net_savings_tokens"] is not None  # informational only
        # with an empty window the M arm's gross savings are zero minus
        # write cost → H1 fails too: the report never pretends otherwise
        assert report["hypotheses"]["H1"]["status"] == "FAIL"

    def test_h2_banner_goes_to_stderr(self, tmp_path: Path, capsys) -> None:
        workload = load_workload(self._write_tape(tmp_path, good_windows=False))
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(run_mod.SqliteMemoryArm, "_recall", lambda self, q, b: [], raising=True)
            run_s5(
                workload, db=None, out_dir=tmp_path / "out", thresholds=None, freeze=False
            )
        err = capsys.readouterr().err
        assert "SAVINGS HEADLINE INVALID (H2)" in err

    def test_m_smaller_window_but_correct_markers_keeps_headline(
        self, tmp_path: Path
    ) -> None:
        """M's window is tiny; B0-naive's is huge — H2 holds (rate, not size)."""
        workload = load_workload(self._write_tape(tmp_path, good_windows=True))
        report, _code = run_s5(
            workload, db=None, out_dir=tmp_path / "out", thresholds=None, freeze=False
        )
        m_rate = report["legs"]["M"]["metrics"]["task_success"]["point"]
        b0n_rate = report["legs"]["B0-naive"]["metrics"]["task_success"]["point"]
        assert m_rate >= b0n_rate - PREREG_DELTA
        assert report["hypotheses"]["H2"]["status"] == "PASS"
        assert report["value"]["headline_invalid_h2"] is False

    def test_h2_uses_prereg_delta_by_default(self, tmp_path: Path) -> None:
        """δ comes from the frozen pre-registration (10 пп), not from air."""
        workload = load_workload(self._write_tape(tmp_path, good_windows=True))
        report, _ = run_s5(
            workload, db=None, out_dir=tmp_path / "out", thresholds=None, freeze=False
        )
        assert report["thresholds"]["preregistered_delta_task_success"] == PREREG_DELTA
        assert report["thresholds"]["effective_delta_task_success"] == PREREG_DELTA

    def _write_tape(self, tmp_path: Path, *, good_windows: bool) -> Path:
        path = tmp_path / f"tape-{good_windows}.jsonl"
        write_workload(path, tiny_tape(good_windows=good_windows))
        return path


# ── NO-DATA semantics (loud, never green, never zero) ────────────────────────

class TestNoData:
    def test_empty_workload_refused_loudly(self, tmp_path: Path, capsys) -> None:
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        code = main(["--workload", str(empty), "--out", str(tmp_path / "out")])
        assert code == 2
        assert "workload refused" in capsys.readouterr().err

    def test_no_queries_no_leg_metrics(self, tmp_path: Path) -> None:
        """A tape with writes only: legs have NO-DATA metrics, not zeros."""
        path = tmp_path / "writes-only.jsonl"
        path.write_text(
            '{"t":1,"kind":"write","sid":"fact-0","mid":"'
            + _marker(7)
            + '","text":"the alpha probe reports to the amber cache. ('
            + _marker(7)
            + ')"}\n',
            encoding="utf-8",
        )
        workload = load_workload(path)
        report, code = run_s5(
            workload, db=None, out_dir=tmp_path / "out", thresholds=None, freeze=False
        )
        assert code == 0
        for leg in report["legs"].values():
            m = leg["metrics"]
            assert m["status"] == "NO-DATA"
            assert m["task_success"]["point"] is None
            assert m["n_tasks"] == 0

    def test_dynamism_no_sidecar_is_loud(self, tmp_path: Path) -> None:
        section = run_mod._dynamism_section(None, ["session-0"])
        assert section["status"] == "NO-DATA"
        assert section["reasons"]
        section2 = run_mod._dynamism_section(tmp_path / "absent.sqlite", ["session-0"])
        assert section2["status"] == "NO-DATA"

    def test_dynamism_single_assembly_sessions_are_loud(
        self, tmp_path: Path
    ) -> None:
        """No session with >= 2 assemblies → NO-DATA, not ratio 0."""
        path = tmp_path / "singletons.jsonl"
        marker = _marker(55)
        events = [
            {"t": 1, "kind": "write", "sid": "fact-0", "mid": marker,
             "text": f"the alpha probe reports. ({marker})"},
            {"t": 2, "kind": "query", "sid": "fact-0", "qid": "q-0", "stratum": "fact",
             "text": "What is the alpha probe?",
             "expect": {"markers": [marker], "absent": [], "min_ratio": 1.0}},
        ]
        write_workload(path, events)
        workload = load_workload(path)
        report, _ = run_s5(
            workload, db=None, out_dir=tmp_path / "out", thresholds=None, freeze=False
        )
        assert report["dynamism"]["status"] == "NO-DATA"
        assert report["hypotheses"]["H4"]["status"] == "NO-DATA"


# ── Determinism (fingerprints, medians, byte-identical reports) ──────────────

class TestDeterminism:
    def test_same_seed_same_fingerprint_and_medians(
        self, tmp_path: Path
    ) -> None:
        p1, p2 = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        wl1 = write_workload(p1, synthetic_workload())
        wl2 = write_workload(p2, synthetic_workload())
        assert wl1.fingerprint == wl2.fingerprint
        assert p1.read_bytes() == p2.read_bytes()
        r1, _ = run_s5(wl1, db=None, out_dir=tmp_path / "o1",
                       thresholds=None, freeze=False, resamples=200)
        r2, _ = run_s5(wl2, db=None, out_dir=tmp_path / "o2",
                       thresholds=None, freeze=False, resamples=200)
        for label in run_mod.LEGS:
            m1 = r1["legs"][label]["metrics"]["prompt_tokens"]
            m2 = r2["legs"][label]["metrics"]["prompt_tokens"]
            assert m1["point"] == m2["point"]
            assert m1["ci95"] == m2["ci95"]
        assert r1["value"]["net_savings_tokens"] == r2["value"]["net_savings_tokens"]

    def test_byte_identical_json_reports(self, tape_path: Path, tmp_path: Path) -> None:
        out1, out2 = tmp_path / "r1", tmp_path / "r2"
        main(["--workload", str(tape_path), "--out", str(out1)])
        main(["--workload", str(tape_path), "--out", str(out2)])
        j1 = (out1 / "s5-report.json").read_bytes()
        j2 = (out2 / "s5-report.json").read_bytes()
        assert j1 == j2
        # the only run-varying path (curated file name) is a constant
        r1 = json.loads(j1)
        assert r1["value"]["b0_file"]["file"] == CURATED_FILE_NAME
        assert r1["logical_clock"] == run_mod.LOGICAL_CLOCK
        assert "2026-" not in j1.decode() and "timestamp" not in json.dumps(r1)

    def test_default_tape_on_disk_matches_generator(self) -> None:
        committed = load_workload(STAND / "workloads" / "default.jsonl")
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            regenerated = write_workload(
                Path(d) / "regen.jsonl", synthetic_workload()
            )
        assert regenerated.fingerprint == committed.fingerprint

    def test_default_corpus_shape_matches_prereg(self) -> None:
        wl = load_workload(STAND / "workloads" / "default.jsonl")
        by_stratum: dict[str, int] = {}
        for q in wl.queries():
            by_stratum[q["stratum"]] = by_stratum.get(q["stratum"], 0) + 1
        assert by_stratum == {"fact": 40, "rule": 30, "session": 40, "negative": 20}
        assert wl.n_queries == 130
        assert wl.n_writes == 150
        assert wl.n_hints == 20
        # every session is pair-capable (2 queries: mid + final)
        for sid in wl.sessions():
            if sid.startswith("session-"):
                n = sum(1 for q in wl.queries() if q["sid"] == sid)
                assert n == 2

    def test_turn_indices_are_strictly_increasing(self) -> None:
        wl = load_workload(STAND / "workloads" / "default.jsonl")
        turns = [e["t"] for e in wl.events]
        assert turns == sorted(turns)
        assert len(set(turns)) == len(turns)


# ── Freeze discipline (exit codes, stamps) ───────────────────────────────────

def medium_tape_path(tmp_path: Path) -> Path:
    """An engineering-shape synthetic tape (NOT the preregistered corpus).

    8 fact + 6 rule + 4 sessions (8 queries) + 4 negatives = 26 tasks —
    enough for a tight-enough CI that H1 (savings >> write cost) passes,
    which the frozen-exit-0 tests need.
    """
    path = tmp_path / "medium.jsonl"
    write_workload(path, synthetic_workload(strata=(8, 6, 4), negatives=4))
    return path


class TestFreeze:
    def test_baseline_run_exits_zero_and_stamps_unfrozen(
        self, tape_path: Path, out_dir: Path
    ) -> None:
        code, report, _ = _run(tape_path, out_dir)
        assert code == 0
        assert report["thresholds"]["status"] == "unfrozen"
        assert "thresholds: unfrozen" in (out_dir / "s5-report.md").read_text(
            encoding="utf-8"
        )

    def test_frozen_run_passes_exit_zero(self, tmp_path: Path, out_dir: Path) -> None:
        tape = medium_tape_path(tmp_path)
        code = main([
            "--workload", str(tape), "--out", str(out_dir),
            "--thresholds",
            "delta_task_success=0.10,min_dynamism=0.01,max_zero_uniqueness_share=0.9",
            "--freeze-thresholds",
        ])
        assert code == 0
        report = json.loads((out_dir / "s5-report.json").read_text(encoding="utf-8"))
        assert report["thresholds"]["status"] == "frozen"
        assert report["thresholds"]["values"] == {
            "delta_task_success": 0.10,
            "min_dynamism": 0.01,
            "max_zero_uniqueness_share": 0.9,
        }
        assert all(h["status"] == "PASS" for h in report["hypotheses"].values())

    def test_frozen_run_breach_exits_one(self, tape_path: Path, out_dir: Path) -> None:
        """An impossible dynamism minimum breaches H4 → exit 1 (alarm)."""
        code = main([
            "--workload", str(tape_path), "--out", str(out_dir),
            "--thresholds",
            "delta_task_success=0.10,min_dynamism=0.99,max_zero_uniqueness_share=0.0",
            "--freeze-thresholds",
        ])
        assert code == 1
        report = json.loads((out_dir / "s5-report.json").read_text(encoding="utf-8"))
        assert report["hypotheses"]["H4"]["status"] == "FAIL"

    def test_unfrozen_breach_still_exits_zero(self, tape_path: Path, out_dir: Path) -> None:
        code = main([
            "--workload", str(tape_path), "--out", str(out_dir),
            "--thresholds", "min_dynamism=0.99,max_zero_uniqueness_share=0.0",
        ])
        assert code == 0  # measured, not frozen — the alarm waits for the freeze
        report = json.loads((out_dir / "s5-report.json").read_text(encoding="utf-8"))
        assert report["hypotheses"]["H4"]["status"] == "UNFROZEN"

    def test_frozen_run_without_h4_values_leaves_h4_unfrozen(
        self, tmp_path: Path, out_dir: Path
    ) -> None:
        """Freezing δ only (no H4 corridor) must NOT mark H4 frozen."""
        tape = medium_tape_path(tmp_path)
        code = main([
            "--workload", str(tape), "--out", str(out_dir),
            "--thresholds", "delta_task_success=0.10", "--freeze-thresholds",
        ])
        assert code == 0  # only H1-H3 frozen; they PASS on this tape
        report = json.loads((out_dir / "s5-report.json").read_text(encoding="utf-8"))
        assert report["thresholds"]["status"] == "frozen"
        assert report["hypotheses"]["H4"]["status"] == "UNFROZEN"


# ── Privacy + audit (sidecar and store hygiene) ──────────────────────────────

class TestPrivacyAndAudit:
    def test_raw_text_never_enters_sidecar(self, tape_path: Path, tmp_path: Path) -> None:
        # run in-process so the sidecar stays inspectable (CLI runs in a
        # tempdir that is removed afterwards)
        workload = load_workload(tape_path)
        sidecar = tmp_path / "metrics.sqlite"
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            arm = run_mod.SqliteMemoryArm(
                Path(d) / "m.sqlite", sidecar_path=sidecar
            )
            run_mod._replay_arm(arm, workload, label="M", budget=DEFAULT_BUDGET)
            arm.close()
        blob = sidecar.read_bytes()
        assert b"alpha probe".lower() not in blob.lower()
        assert b"amber cache".lower() not in blob.lower()
        assert MARKER_PREFIX.encode() not in blob

    def test_stand_writes_are_actor_benchmark(self, tape_path: Path, tmp_path: Path) -> None:
        out = tmp_path / "out"
        main(["--workload", str(tape_path), "--out", str(out)])
        # find the M arm store (temp dirs are gone after run; re-run in-process)
        workload = load_workload(tape_path)
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            arm = run_mod.SqliteMemoryArm(
                Path(d) / "m.sqlite", sidecar_path=Path(d) / "side.sqlite"
            )
            run_mod._replay_arm(arm, workload, label="M", budget=DEFAULT_BUDGET)
            rows = arm.conn.execute(
                "SELECT DISTINCT actor FROM s5_memories"
            ).fetchall()
            assert [str(r["actor"]) for r in rows] == [AGENT]
            traces = arm.conn.execute(
                "SELECT DISTINCT actor FROM s5_traces"
            ).fetchall()
            assert [str(r["actor"]) for r in traces] == [AGENT]
            arm.close()

    def test_host_mode_stand_tables_are_namespaced(self, tmp_path: Path) -> None:
        source = tmp_path / "source.sqlite"
        conn = sqlite3.connect(str(source))
        conn.execute(
            "CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE VIRTUAL TABLE memories_fts USING fts5(id UNINDEXED, content,"
            " tokenize='unicode61')"
        )
        conn.commit()
        conn.close()
        arm = run_mod.SqliteMemoryArm(source, sidecar_path=None)
        assert arm._host_store is True
        # the stand's tables are s5_*: the source's rows stay untouched
        names = {
            str(r[0])
            for r in arm.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "s5_memories" in names and "s5_traces" in names
        arm.put("stand content", _marker(3), "fact-0")
        count = arm.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        assert count == 0  # no rows added to the SOURCE table
        arm.close()


# ── Workload module contract (validation fail-loud) ──────────────────────────

class TestWorkloadValidation:
    def test_unknown_kind_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text('{"t":1,"kind":"gossip","sid":"x"}\n', encoding="utf-8")
        with pytest.raises(WorkloadError, match="unknown kind"):
            load_workload(path)

    def test_unknown_field_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text(
            '{"t":1,"kind":"write","sid":"x","mid":"m","text":"t","extra":1}\n',
            encoding="utf-8",
        )
        with pytest.raises(WorkloadError, match="unknown field"):
            load_workload(path)

    def test_duplicate_qid_refused(self, tmp_path: Path) -> None:
        marker = _marker(20)
        line = (
            '{"t":%(t)d,"kind":"query","sid":"s","qid":"q","stratum":"fact",'
            '"text":"q","expect":{"markers":["%(m)s"],"absent":[],"min_ratio":1.0}}'
        )
        path = tmp_path / "dup.jsonl"
        path.write_text(
            (line % {"t": 1, "m": marker}) + "\n" + (line % {"t": 2, "m": marker}) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(WorkloadError, match="duplicate query id"):
            load_workload(path)

    def test_backwards_turn_refused(self, tmp_path: Path) -> None:
        marker = _marker(21)
        path = tmp_path / "back.jsonl"
        path.write_text(
            '{"t":5,"kind":"write","sid":"s","mid":"m1","text":"x (m1)"}\n'
            '{"t":4,"kind":"write","sid":"s","mid":"m2","text":"y (m2)"}\n'.replace(
                "m1", marker
            ).replace("m2", _marker(22)),
            encoding="utf-8",
        )
        with pytest.raises(WorkloadError, match="backwards"):
            load_workload(path)

    def test_invalid_json_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text("{not json}\n", encoding="utf-8")
        with pytest.raises(WorkloadError, match="invalid JSON"):
            load_workload(path)

    def test_estimator_is_symmetric_and_deterministic(self) -> None:
        assert estimate_tokens("") == 0
        assert estimate_tokens("word") == 1
        text = "one two three four five six seven eight"  # 8 words, 39 chars
        assert estimate_tokens(text) == max(8, -(-39 // 4))  # 10


# ── Module shape ─────────────────────────────────────────────────────────────

class TestModuleShape:
    def test_parse_thresholds_allowlist(self) -> None:
        parsed = parse_thresholds("delta_task_success=0.2,min_dynamism=0.5")
        assert parsed == {"delta_task_success": 0.2, "min_dynamism": 0.5}
        assert parse_thresholds(None) is None

    def test_standalone_cli_subprocess(self, tape_path: Path, tmp_path: Path) -> None:
        """The documented invocation works as a plain script."""
        proc = subprocess.run(  # noqa: S603 - fixed argv, repo-local script
            [sys.executable, str(STAND / "run.py"), "--workload", str(tape_path),
             "--out", str(tmp_path / "out")],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(REPO_ROOT),
        )
        assert proc.returncode == 0, proc.stderr
        assert "fp=" in proc.stderr
        assert (tmp_path / "out" / "s5-report.json").exists()
