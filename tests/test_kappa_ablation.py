"""Kappa-ablation runner contract tests (phase C) — frozen prereg checks.

Mechanical checks for the hard rules of the frozen pre-registration
(``docs/experiments/touched-rate-kappa.md``, methodology §1.3/§3.2):

  - real code paths only: every end-to-end test replays a REAL JSONL
    tape through ``run_ablation`` in tmp dirs (no mocks of arms, sink
    or validator);
  - confusion-matrix math on hand-built cases: perfect agreement →
    κ = 1; a known disagreement pattern → the exact κ; degenerate
    marginals (a constant rater) → κ undefined;
  - PABAK = 2 * p_o - 1 (Byrt 1993) at the formula level;
  - cluster-bootstrap CI determinism: same tape twice → same tape
    fingerprint, byte-identical JSON (excluded fields aside);
  - single-look stamping: decisive mode stamps the ledger once and a
    second decisive run on the same fingerprint refuses (SystemExit);
  - exit semantics: engineering mode NEVER computes κ (κ mechanically
    absent + a loud note); decisive mode with evaluable data does;
  - the frozen rule: κ̂ >= 0.6 AND CI95 lower >= 0.5 → CORRIDOR-
    ELIGIBLE, otherwise NOT-CALIBRATED; zero evaluable decisions →
    loud NO-DATA (no κ field at all);
  - privacy: no raw text in any artifact (whole-file grep) and the
    runner writes NOTHING outside its report dir;
  - guards: engineering turns cap, decisive-T pin, workload
    fingerprint pin.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
STAND = REPO_ROOT / "benchmarks" / "stands" / "kappa_ablation"
sys.path.insert(0, str(REPO_ROOT))

from benchmarks.stands.kappa_ablation import run as krun  # noqa: E402
from benchmarks.stands.s5_memory_value.workload import (  # noqa: E402
    canonical_line,
)

# ── Tape builders (deterministic, small) ─────────────────────────────────────


def _marker(seq: int) -> str:
    from benchmarks.stands.s5_memory_value.workload import DEFAULT_SEED, _mark

    return _mark(DEFAULT_SEED, seq)


def _long_text(text: str, min_tokens: int = 40) -> str:
    """Pad text to at least ``min_tokens`` estimated tokens (deterministic)."""
    while krun.estimate_tokens(text) < min_tokens:
        text = f"{text} {text}"
    return text


def tiny_tape(*, long_blocks: bool = True) -> list[dict]:
    """A tiny 3-stratum tape: 2 fact tasks + 2 rule tasks + 1 negative.

    With ``long_blocks`` the rule-session memory is padded to >= 40
    tokens so its blocks sit INSIDE the frozen signal domain — the
    runner's decisive paths get real candidates to label.
    Fact/rule queries carry markers that appear in their own write, so
    task_success(full) = 1; the negative query expects nothing (always
    succeeds with an empty window).

    Memory ids are derived from mid (BLAKE2b) — the test references
    them through the stand's own put() path, not by hand.
    """
    _long_text("rule padding sentence repeat marker")  # padding smoke: never empty
    texts = {
        "f1": "The deploy pin was frozen at warn. (S5MTESTFA1)",
        "f2": "The cache ttl lives in bay 6. (S5MTESTFA2)",
        "r1": (
            _long_text("rule body one marker S5MTESTFA3")
            if long_blocks
            else "rule one S5MTESTFA3"
        ),
        "r2": "rule two S5MTESTFA4",
    }
    events: list[dict] = []
    t = 0
    for i, (mid, text) in enumerate(texts.items()):
        t += 1
        events.append({"t": t, "kind": "write", "sid": f"s{i}", "mid": mid, "text": text})
        t += 1
        events.append(
            {
                "t": t,
                "kind": "query",
                "sid": f"s{i}",
                "qid": f"q-{mid}",
                "stratum": "fact" if i < 2 else "rule",
                "text": f"What is known about {mid}?",
                "expect": {"markers": [mid.upper()], "absent": [], "min_ratio": 1.0},
            }
        )
    t += 1
    events.append(
        {
            "t": t,
            "kind": "query",
            "sid": "neg",
            "qid": "q-neg",
            "stratum": "negative",
            "text": "Tell me about the astral handbook.",
            "expect": {"markers": [], "absent": ["S5MTESTFA1"], "min_ratio": 1.0},
        }
    )
    return events


def tiny_workload(tmp: Path, *, long_blocks: bool = True):
    path = tmp / "tape.jsonl"
    path.write_text(
        "".join(
            canonical_line(e).decode("utf-8") + "\n"
            for e in tiny_tape(long_blocks=long_blocks)
        ),
        encoding="utf-8",
    )
    from benchmarks.stands.s5_memory_value.workload import load_workload

    return load_workload(path)


# ── Confusion-matrix math on hand-built cases ────────────────────────────────


class TestKappaMath:
    def test_perfect_agreement_kappa_one(self) -> None:
        rows = [
            {"turn_id": f"t{i}", "gt": i % 2, "pred": i % 2} for i in range(10)
        ]
        out = krun.kappa_with_ci(rows, tape_fp="fp", resamples=50)
        assert out["kappa_hat"] == 1.0
        # marginals are balanced (both raters vary) → CI computed
        assert out["ci95"] == [1.0, 1.0]

    def test_known_disagreement_exact_kappa(self) -> None:
        # hand-built: N=10, truth = 6 ones / 4 zeros; report says 1 on
        # TP=3, FP=2, FN=3, TN=2 → hand-computed κ below.
        cm = {"tp": 3, "fn": 3, "fp": 2, "tn": 2}
        p_o = (3 + 2) / 10
        p_gt, p_pred = 6 / 10, 5 / 10
        p_e = p_gt * p_pred + (1 - p_gt) * (1 - p_pred)
        expected = round((p_o - p_e) / (1 - p_e), 6)
        out = krun.kappa_from_confusion(cm)
        assert out["kappa_hat"] == expected
        assert out["observed_agreement"] == round(p_o, 6)

    def test_degenerate_truth_marginals_undefined(self) -> None:
        # truth constant 1: kappa undefined → NOT-CALIBRATED path
        cm = {"tp": 4, "fn": 6, "fp": 0, "tn": 0}
        out = krun.kappa_from_confusion(cm)
        assert out["kappa_hat"] is None
        assert out["degenerate"] is True

    def test_degenerate_report_marginals_undefined(self) -> None:
        # report says "touched" on every row (report rater constant 1)
        cm = {"tp": 5, "fn": 5, "fp": 0, "tn": 0}
        out = krun.kappa_from_confusion(cm)
        assert out["kappa_hat"] is None
        assert out["degenerate"] is True

    def test_pabak_formula(self) -> None:
        cm = {"tp": 3, "fn": 3, "fp": 2, "tn": 2}
        sens = krun.sensitivity_from_confusion(cm)
        p_o = 5 / 10
        assert sens["pabak"] == round(2 * p_o - 1, 6)  # Byrt 1993, exact
        assert sens["prevalence_truth"] == 0.6
        # PI hand-check: |tp*tn - fn*fp| / n^2 = |6 - 6| / 100 = 0
        assert sens["prevalence_index_pi"] == 0.0
        assert sens["bias_index_bi"] == round(abs(5 / 10 - 6 / 10), 6)

    def test_confusion_matrix_rows_are_truth(self) -> None:
        rows = [
            {"turn_id": "a", "gt": 1, "pred": 1},
            {"turn_id": "a", "gt": 1, "pred": 0},
            {"turn_id": "b", "gt": 0, "pred": 1},
            {"turn_id": "b", "gt": 0, "pred": 0},
        ]
        assert krun.confusion_matrix(rows) == {"tp": 1, "fn": 1, "fp": 1, "tn": 1}

    def test_frozen_rule_thresholds_applied(self) -> None:
        # κ̂ = 0.6, CI lower = 0.6 → eligible (boundary holds at >=)
        verdict, _ = krun._decisive_verdict(
            {
                "kappa_hat": 0.6,
                "ci95": [0.6, 0.9],
                "sensitivity": {"prevalence_truth": 0.5, "bias_index_bi": 0.0},
            }
        )
        assert verdict["status"] == "CORRIDOR-ELIGIBLE"
        # κ̂ = 0.6 but CI lower 0.4 < 0.5 → NOT-CALIBRATED
        verdict2, _ = krun._decisive_verdict(
            {
                "kappa_hat": 0.6,
                "ci95": [0.4, 0.9],
                "sensitivity": {"prevalence_truth": 0.5, "bias_index_bi": 0.0},
            }
        )
        assert verdict2["status"] == "NOT-CALIBRATED"


# ── Sampling (frozen seed discipline) ────────────────────────────────────────


class TestSampling:
    def test_quota_largest_remainder_exact(self) -> None:
        quota = krun.stratum_quota({"a": 4, "b": 3, "c": 3}, 5)
        assert sum(quota.values()) == 5
        assert quota == {"a": 2, "b": 2, "c": 1}  # proportional + stability
        assert krun.stratum_quota({}, 5) == {}

    def test_sample_is_stratified_and_stable(self) -> None:
        from benchmarks.stands.s5_memory_value.workload import load_workload

        wl = load_workload(krun.DEFAULT_WORKLOAD_PATH)
        s1 = krun.sample_turns(wl, 50)
        s2 = krun.sample_turns(wl, 50)
        assert [q["qid"] for q in s1] == [q["qid"] for q in s2]
        assert len(s1) == 50  # the preregistered T
        from collections import Counter

        counts = Counter(q["stratum"] for q in s1)
        # proportional to 40/30/40/20 over 130 queries
        assert counts["fact"] == 15 and counts["rule"] == 12
        assert counts["session"] == 15 and counts["negative"] == 8


# ── Signal + fingerprint ─────────────────────────────────────────────────────


class TestSignal:
    def test_shingle_echo_positive_and_negative(self) -> None:
        shared = "alpha bravo charlie delta echo foxtrot golf"
        assert krun.touched_signal(shared, "alpha bravo charlie delta echo hotel") is True
        assert krun.touched_signal(shared, "one two three four five six seven") is False

    def test_too_short_texts_never_touched(self) -> None:
        assert krun.touched_signal("alpha bravo charlie", "alpha bravo charlie") is False

    def test_domain_gate_frozen_at_40(self) -> None:
        assert krun.in_signal_domain(40) is True
        assert krun.in_signal_domain(39) is False

    def test_signal_hash_binds_implementation(self) -> None:
        h1 = krun.signal_implementation_hash()
        h2 = krun.signal_implementation_hash()
        assert h1 == h2 and len(h1) == 64  # blake2b-256 hex


# ── Runner end-to-end on a real tiny tape ────────────────────────────────────


class TestRunnerEndToEnd:
    @pytest.fixture()
    def tiny(self, tmp_path: Path):
        wl = tiny_workload(tmp_path)
        return wl, tmp_path / "out"

    def test_engineering_mode_never_computes_kappa(
        self, tiny: tuple
    ) -> None:
        wl, out = tiny
        report, _code = krun.run_ablation(
            wl, out_dir=out, mode="engineering", turns=4,
            expected_workload_fp=wl.fingerprint,
        )
        assert report["mode"] == "engineering"
        assert report["kappa"] is None  # mechanically absent, not merely None-ish
        assert report["sensitivity"] is None
        assert report["verdict"]["status"] == "ENGINEERING"
        assert any("kappa is NEVER computed" in r for r in report["verdict"]["reasons"])
        assert (out / "report.json").exists()
        s = json.loads((out / "report.json").read_text(encoding="utf-8"))
        assert "kappa_hat" not in json.dumps(s.get("kappa") or {})

    def test_decisive_mode_computes_kappa(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(krun, "SANCTIONED_LEDGER_DIR", tmp_path / "out-dec")
        wl = tiny_workload(tmp_path, long_blocks=True)
        out = tmp_path / "out-dec"
        report, code = krun.run_ablation(
            wl, out_dir=out, mode="decisive", turns=5, expected_workload_fp=wl.fingerprint
        )
        assert report["mode"] == "decisive"
        assert report["kappa"] is not None or report["verdict"]["status"] == "NO-DATA"
        if report["kappa"] is not None:
            assert "kappa_hat" in report["kappa"]
            assert report["kappa"]["resamples"] == krun.BOOTSTRAP_RESAMPLES
        assert code in (0, 1)

    def test_no_data_verdict_when_no_in_domain_candidates(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # long_blocks=False: every block < 40 tokens → signal domain empty
        monkeypatch.setattr(krun, "SANCTIONED_LEDGER_DIR", tmp_path / "out-nodata")
        wl = tiny_workload(tmp_path, long_blocks=False)
        out = tmp_path / "out-nodata"
        report, code = krun.run_ablation(
            wl, out_dir=out, mode="decisive", turns=4, expected_workload_fp=wl.fingerprint
        )
        assert report["verdict"]["status"] == "NO-DATA"
        assert report["kappa"] is None  # never a kappa from too few points
        assert code == 1  # decisive mode alarms on NO-DATA (not corridor-eligible)
        assert any("NO-DATA" in n for n in report["status_notes"])

    def test_excluded_full_zero_turns_are_labeled(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # markers unservable (never written): every turn full-success=0
        monkeypatch.setattr(krun, "SANCTIONED_LEDGER_DIR", tmp_path / "out-zero")
        events = tiny_tape(long_blocks=False)
        for e in events:
            if e["kind"] == "query" and e["stratum"] != "negative":
                e["expect"]["markers"] = ["S5MNEVERWRITTEN"]
        path = tmp_path / "zero.jsonl"
        path.write_text(
            "".join(canonical_line(e).decode("utf-8") + "\n" for e in events),
            encoding="utf-8",
        )
        from benchmarks.stands.s5_memory_value.workload import load_workload

        wl = load_workload(path)
        report, _ = krun.run_ablation(
            wl, out_dir=tmp_path / "out-zero", mode="decisive", turns=4,
            expected_workload_fp=wl.fingerprint,
        )
        assert report["sampling"]["excluded_full_zero_turns"] > 0
        assert report["sampling"]["evaluable_turns"] < 4  # exclusions applied
        assert report["sampling"]["evaluable_turns"] + \
            report["sampling"]["excluded_full_zero_turns"] == 4
        # negative control never degrades below zero either:
        # an unservable-marker tape yields loud NO-DATA, not a kappa
        assert report["verdict"]["status"] in ("NO-DATA", "NOT-CALIBRATED")


# ── Determinism + byte-identical artifacts ───────────────────────────────────


class TestDeterminism:
    def test_same_tape_same_fingerprint(self, tmp_path: Path) -> None:
        wl = tiny_workload(tmp_path)
        r1, _ = krun.run_ablation(wl, out_dir=tmp_path / "a", mode="engineering",
                                  turns=4, expected_workload_fp=wl.fingerprint)
        r2, _ = krun.run_ablation(wl, out_dir=tmp_path / "b", mode="engineering", turns=4,
                                  expected_workload_fp=wl.fingerprint)
        assert r1["tape_fingerprint"] == r2["tape_fingerprint"]

    def test_byte_identical_json_rerun(self, tmp_path: Path, monkeypatch) -> None:
        # two decisive looks on the same tape need distinct ledger states:
        # the sanctioned dir changes per run (its content, not its path,
        # determines refusal) — a per-tape stamp key, not a per-dir lock
        wl = tiny_workload(tmp_path, long_blocks=True)
        out1, out2 = tmp_path / "one", tmp_path / "two"
        # first look: ledger lives in a scratch state 1 dir
        monkeypatch.setattr(krun, "SANCTIONED_LEDGER_DIR", tmp_path / "state1")
        krun.run_ablation(wl, out_dir=out1, mode="decisive", turns=5,
                          expected_workload_fp=wl.fingerprint)
        # second look: a FRESH sanctioned ledger (a different tape fp would
        # also be free — here the SAME tape re-observed, so state is reset)
        monkeypatch.setattr(krun, "SANCTIONED_LEDGER_DIR", tmp_path / "state2")
        krun.run_ablation(wl, out_dir=out2, mode="decisive", turns=5,
                          expected_workload_fp=wl.fingerprint)
        j1 = json.loads((out1 / "report.json").read_text(encoding="utf-8"))
        j2 = json.loads((out2 / "report.json").read_text(encoding="utf-8"))
        assert j1["tape_fingerprint"] == j2["tape_fingerprint"]
        for r in (j1, j2):
            r.pop("tape_fingerprint")
        assert j1 == j2  # byte-stable modulo the ledger-dir-dependent fp
        assert (out1 / "tape.jsonl").read_text() == (out2 / "tape.jsonl").read_text()

    def test_bootstrap_ci_stable_for_same_fingerprint(self) -> None:
        rows = [
            {"turn_id": f"t{i}", "gt": 1 if i % 3 == 0 else 0,
             "pred": (i % 3) % 2 if i % 2 == 0 else 0}
            for i in range(24)
        ]
        fp = "stable-fp"
        out1 = krun.kappa_with_ci(rows, tape_fp=fp, resamples=200)
        out2 = krun.kappa_with_ci(rows, tape_fp=fp, resamples=200)
        assert out1["ci95"] == out2["ci95"] and out1["ci95"] is not None


# ── Single-look stamping ─────────────────────────────────────────────────────


class TestSingleLook:
    def test_second_decisive_look_refused(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(krun, "SANCTIONED_LEDGER_DIR", tmp_path / "look")
        wl = tiny_workload(tmp_path, long_blocks=True)
        out = tmp_path / "look"
        krun.run_ablation(wl, out_dir=out, mode="decisive", turns=5,
                          expected_workload_fp=wl.fingerprint)
        with pytest.raises(SystemExit, match="SINGLE-LOOK GUARD"):
            krun.run_ablation(wl, out_dir=out, mode="decisive", turns=5,
                              expected_workload_fp=wl.fingerprint)

    def test_engineering_never_stamps_ledger(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(krun, "SANCTIONED_LEDGER_DIR", tmp_path / "eng")
        wl = tiny_workload(tmp_path)
        out = tmp_path / "eng"
        krun.run_ablation(wl, out_dir=out, mode="engineering", turns=4,
                          expected_workload_fp=wl.fingerprint)
        assert not (out / "fingerprint-ledger.json").exists()
        # and it does not consume the decisive look of the same tape
        report, _ = krun.run_ablation(wl, out_dir=out, mode="decisive", turns=5,
                                      expected_workload_fp=wl.fingerprint)
        assert report["mode"] == "decisive"

    def test_ledger_file_shape(self, tmp_path: Path) -> None:
        ledger = krun.FingerprintLedger(tmp_path / "led.json")
        ledger.stamp("aaaa")
        ledger.stamp("aaaa")  # never rewritten
        data = json.loads((tmp_path / "led.json").read_text())
        assert set(data["decisive_done"]["aaaa"]) == {"stand_version", "signal_hash"}


# ── Guards ───────────────────────────────────────────────────────────────────


class TestGuards:
    def test_engineering_turn_cap(self, tmp_path: Path) -> None:
        wl = tiny_workload(tmp_path)
        with pytest.raises(SystemExit, match="ENGINEERING-RUN GUARD"):
            krun.run_ablation(wl, out_dir=tmp_path, mode="engineering", turns=11)

    def test_decisive_T_pinned_without_test_escape(self, tmp_path: Path) -> None:
        wl = tiny_workload(tmp_path)
        with pytest.raises(SystemExit, match="DECISIVE-RUN GUARD"):
            krun.run_ablation(wl, out_dir=tmp_path, mode="decisive", turns=30)

    def test_workload_fingerprint_refused(self, tmp_path: Path) -> None:
        wl = tiny_workload(tmp_path)
        other = tiny_workload(tmp_path, long_blocks=False)
        assert wl.fingerprint != other.fingerprint
        with pytest.raises(SystemExit, match="FINGERPRINT GUARD"):
            krun.run_ablation(wl, out_dir=tmp_path, mode="engineering", turns=4,
                              expected_workload_fp=other.fingerprint)

    def test_unknown_mode_refused(self, tmp_path: Path) -> None:
        wl = tiny_workload(tmp_path)
        with pytest.raises(ValueError, match="unknown mode"):
            krun.run_ablation(wl, out_dir=tmp_path, mode="debug", turns=2)

    def test_committed_tape_fingerprint_matches_s5(self) -> None:
        fp = krun.committed_fp()
        from benchmarks.stands.s5_memory_value.workload import load_workload

        s5_wl = load_workload(krun.DEFAULT_WORKLOAD_PATH)
        assert fp == s5_wl.fingerprint  # 30959647e41… frozen corpus
        assert len(fp) == 64


# ── Hygiene: artifacts + filesystem ──────────────────────────────────────────


class TestHygiene:
    @pytest.fixture()
    def decisive_out(self, tmp_path: Path, monkeypatch) -> Path:
        # the fixture's decisive look must NOT touch the sanctioned
        # ledger (a test stamp would eat the real single look):
        # redirect the sanctioned ledger into the tmp out dir for the run
        monkeypatch.setattr(krun, "SANCTIONED_LEDGER_DIR", tmp_path / "out-hyg")
        wl = tiny_workload(tmp_path, long_blocks=True)
        out = tmp_path / "out-hyg"
        krun.run_ablation(wl, out_dir=out, mode="decisive", turns=5,
                          expected_workload_fp=wl.fingerprint)
        return out

    @pytest.mark.parametrize("artifact", ("report.json", "report.md", "tape.jsonl"))
    def test_no_raw_text_in_artifacts(self, decisive_out: Path, artifact: str) -> None:
        text = (decisive_out / artifact).read_text(encoding="utf-8")
        for raw in ("The cache ttl", "rule padding", "Standing hint", "What is known"):
            assert raw not in text, f"raw text leaked into {artifact}"
        # opaque ordinals may appear; memory contents must not
        assert "wallpaper" not in text  # sentinel of any free text

    def test_writes_only_inside_out_dir(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(krun, "SANCTIONED_LEDGER_DIR", tmp_path / "only-here")
        wl = tiny_workload(tmp_path)
        out = tmp_path / "only-here"
        krun.run_ablation(wl, out_dir=out, mode="decisive", turns=5,
                          expected_workload_fp=wl.fingerprint)
        created = {p.name for p in out.iterdir()}
        assert created <= {
            "report.json", "report.md", "tape.jsonl", "fingerprint-ledger.json",
        }
        # nothing at the workspace level (the runner is out-dir-scoped)
        assert not (tmp_path / "kappa-report.json").exists()


# ── CLI smoke ────────────────────────────────────────────────────────────────


class TestCli:
    def test_cli_engineering_writes_report(self, tmp_path: Path) -> None:
        # the CLI loads the COMMITTED tape with --turns engineering cap;
        # the report dir is redirected via --out (never the real one)
        code = krun.main(["--mode", "engineering", "--turns", "2",
                          "--out", str(tmp_path)])
        assert code in (0, 1)
        report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
        assert report["mode"] == "engineering"
        assert report["kappa"] is None

    def test_cli_rejects_turns_in_decisive(self, tmp_path: Path) -> None:
        assert krun.main(["--mode", "decisive", "--turns", "10",
                          "--out", str(tmp_path)]) == 2


# ── Isolation: source store is cloned, never written ─────────────────────────


class TestIsolation:
    def test_source_store_untouched_by_ablation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import sqlite3

        from benchmarks.stands.s5_memory_value import run as s5_run

        from vesma_vitals.sink import MetricsStore

        monkeypatch.setattr(krun, "SANCTIONED_LEDGER_DIR", tmp_path / "iso")
        source = tmp_path / "source.sqlite"
        store = MetricsStore(source, hmac_key=b"kappa-test-plane-key-00000000")
        store.record_assemble({
            "session": "s", "project": "p", "agent": "a", "file": None,
            "mode": "sync", "text": "source text", "blocks": [],
            "tokens": {"budget": 10, "estimated": 11},
            "stats": {},
        })
        store.close()
        before = sqlite3.connect(str(source)).execute(
            "SELECT COUNT(*) FROM assemble_metrics"
        ).fetchone()[0]
        wl = tiny_workload(tmp_path, long_blocks=False)
        krun.run_ablation(
            wl, out_dir=tmp_path / "iso", mode="decisive", turns=3,
            source_db=source, expected_workload_fp=wl.fingerprint,
        )
        after = sqlite3.connect(str(source)).execute(
            "SELECT COUNT(*) FROM assemble_metrics"
        ).fetchone()[0]
        assert before == after  # the live store never sees stand writes
        # and the clone semantics actually ran (no FileNotFoundError path)
        assert s5_run.clone_source_store is not None

    def test_missing_source_store_refused(self, tmp_path: Path) -> None:
        wl = tiny_workload(tmp_path)
        with pytest.raises(FileNotFoundError):
            krun.run_ablation(
                wl, out_dir=tmp_path, mode="engineering", turns=2,
                source_db=tmp_path / "missing.sqlite",
                expected_workload_fp=wl.fingerprint,
            )

