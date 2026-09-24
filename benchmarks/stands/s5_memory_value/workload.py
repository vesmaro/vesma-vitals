"""S5 workload tape — the JSONL event stream every S5 leg replays.

The tape is the shared input of ALL comparator legs (M, B0-naive,
B0-file, B0-full): symmetry starts here. One line = one event:

    {"t": <turn index>, "kind": "write"|"query"|"hint", ...}

Event shapes (closed sets — the workload fingerprint depends on the
exact content, so anything outside the closed sets fails LOUD):

  write   {"t", "kind": "write", "sid", "mid", "text"}
          one memory write into the store (M arm) / the transcript
          (B0 legs); ``mid`` is the workload-stable marker id the
          validator greps for — NOT a store id;
  query   {"t", "kind": "query", "sid", "qid", "stratum", "text",
           "expect": {"markers": [...], "absent": [...], "min_ratio"}}
          one served-context task (the only LLM turn of the leg);
          ``markers`` must appear in the served window (share >=
          min_ratio) AND every ``absent`` marker must stay OUT of the
          window for the task to succeed. ``sid`` is the session axis:
          per-session task_success is stratified (and bootstrapped) BY
          session, never pooled.
  hint    {"t", "kind": "hint", "sid", "text"}
          a standing instruction the naive harness would carry in its
          window — it counts toward B0 legs' transcripts, never toward
          M's (the memory holds it instead).

Determinism: the synthetic generator is keyed by a BLAKE2b stream
derived from a FIXED constant seed — no ``random`` module state, no
wall-clock anywhere; the same call signature yields a byte-identical
tape (tests pin this). The default call ``synthetic_workload()`` IS
the preregistered v1 corpus (~100 tasks x 3 strata + 20 negatives);
its tape ships as ``workloads/default.jsonl`` so the fingerprint is
stable across runs. Non-default seeds/strata are for tests and
engineering runs only — they are NOT preregistered corpora.

Corpus size (v1 defaults): 40 fact + 30 rule + 20 session tasks with
interleaved writes => 40+30+60=130 writes, 40+30+40=110 queries, 20
hints; plus 20 negative queries => 300 events, 130 queries total
(~100 positive tasks + 20 negatives, per the pre-registration).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Event kinds — a closed set (unknown kind = loud refusal).
KINDS: tuple[str, ...] = ("write", "query", "hint")

#: Query strata of the v1 corpus (pre-registration: fact-insertion /
#: rule-query / multi-turn session) + the negative-control stratum.
STRATA: tuple[str, ...] = ("fact", "rule", "session", "negative")

#: Marker prefix — workload-stable ids, greppable in any served window.
MARKER_PREFIX = "S5M"

#: Default stratum sizes of the preregistered v1 corpus: 40 fact tasks,
#: 30 rule tasks, 20 multi-turn sessions (each session = 2 query tasks:
#: a mid-checkpoint query and a final summarize query — two assemblies
#: make the session pair-capable for the dynamism corridor).
DEFAULT_STRATA: tuple[int, int, int] = (40, 30, 20)
#: Negative control size (pre-registration: 20).
DEFAULT_NEGATIVES = 20

#: Fixed generator seed (documented constant — the default seed IS the
#: preregistered v1 corpus; a different seed is a different corpus).
DEFAULT_SEED = "s5-memory-value-v1"

#: Maximum accepted turn index (guards against a corrupted tape).
MAX_TURN = 10_000_000

#: Writes per multi-turn session task (each session ends with 1 query).
SESSION_WRITES = 4


@dataclass(frozen=True)
class Workload:
    """A validated S5 workload tape (immutable; fingerprint over content)."""

    events: tuple[dict[str, Any], ...]
    fingerprint: str

    @property
    def n_writes(self) -> int:
        return sum(1 for e in self.events if e["kind"] == "write")

    @property
    def n_queries(self) -> int:
        return sum(1 for e in self.events if e["kind"] == "query")

    @property
    def n_hints(self) -> int:
        return sum(1 for e in self.events if e["kind"] == "hint")

    def queries(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e["kind"] == "query"]

    def writes(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e["kind"] == "write"]

    def hints(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e["kind"] == "hint"]

    def sessions(self) -> list[str]:
        """Session ids in first-appearance order (stable reporting axis)."""
        seen: dict[str, None] = {}
        for e in self.events:
            seen.setdefault(e["sid"], None)
        return list(seen)


# ── Validation (fail loud — the fingerprint depends on exact content) ────────

_WRITE_FIELDS = frozenset({"t", "kind", "sid", "mid", "text"})
_HINT_FIELDS = frozenset({"t", "kind", "sid", "text"})
_QUERY_FIELDS = frozenset({"t", "kind", "sid", "qid", "stratum", "text", "expect"})
_EXPECT_FIELDS = frozenset({"markers", "absent", "min_ratio"})


class WorkloadError(ValueError):
    """A workload tape failed validation — always loud, never repaired."""


def _require_str(obj: dict[str, Any], key: str, where: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise WorkloadError(f"{where}: field {key!r} must be a non-empty string")
    return value


def _validate_event(raw: dict[str, Any], lineno: int) -> dict[str, Any]:
    """Validate one tape line against its kind's closed shape."""
    where = f"line {lineno}"
    if not isinstance(raw, dict):
        raise WorkloadError(f"{where}: event must be a JSON object")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise WorkloadError(f"{where}: unknown kind {kind!r} (allowed: {KINDS})")
    t = raw.get("t")
    if not isinstance(t, int) or isinstance(t, bool) or not 0 <= t <= MAX_TURN:
        raise WorkloadError(f"{where}: field 't' must be an int in [0, {MAX_TURN}]")
    allowed = {"write": _WRITE_FIELDS, "hint": _HINT_FIELDS, "query": _QUERY_FIELDS}[kind]
    unknown = set(raw) - allowed
    if unknown:
        raise WorkloadError(f"{where}: unknown field(s) {sorted(unknown)} for kind {kind!r}")
    missing = allowed - set(raw)
    if missing:
        raise WorkloadError(f"{where}: missing field(s) {sorted(missing)} for kind {kind!r}")
    _require_str(raw, "sid", where)
    if kind == "write":
        _require_str(raw, "mid", where)
        _require_str(raw, "text", where)
    elif kind == "hint":
        _require_str(raw, "text", where)
    else:  # query
        _require_str(raw, "qid", where)
        _require_str(raw, "text", where)
        stratum = raw.get("stratum")
        if stratum not in STRATA:
            raise WorkloadError(f"{where}: unknown stratum {stratum!r} (allowed: {STRATA})")
        expect = raw.get("expect")
        if not isinstance(expect, dict):
            raise WorkloadError(f"{where}: 'expect' must be an object")
        unknown_exp = set(expect) - _EXPECT_FIELDS
        if unknown_exp:
            raise WorkloadError(f"{where}: unknown expect field(s) {sorted(unknown_exp)}")
        markers = expect.get("markers")
        absent = expect.get("absent")
        if not isinstance(markers, list) or not all(isinstance(m, str) and m for m in markers):
            raise WorkloadError(f"{where}: expect.markers must be a list of non-empty strings")
        if not isinstance(absent, list) or not all(isinstance(m, str) and m for m in absent):
            raise WorkloadError(f"{where}: expect.absent must be a list of non-empty strings")
        min_ratio = expect.get("min_ratio", 1.0)
        if (
            not isinstance(min_ratio, (int, float))
            or isinstance(min_ratio, bool)
            or not 0.0 <= float(min_ratio) <= 1.0
        ):
            raise WorkloadError(f"{where}: expect.min_ratio must be a number in [0, 1]")
    return raw


# ── Loading + fingerprint ────────────────────────────────────────────────────

def fingerprint_bytes(canonical_lines: list[bytes]) -> str:
    """BLAKE2b-256 over the sorted-join of the tape's canonical line bytes.

    "Sorted-join" per the design contract: canonical lines (compact
    separators, sorted object keys) are hashed as a sorted multiset so
    object-key order and blank separator lines cannot move the hash.
    Turn ORDER stays outside the hash by this construction — the
    semantic order is carried by the ``t`` field, which IS validated
    to be non-decreasing across the tape.
    """
    digest = hashlib.blake2b(digest_size=32)
    for chunk in sorted(canonical_lines):
        digest.update(chunk)
        digest.update(b"\n")
    return digest.hexdigest()


def canonical_line(event: dict[str, Any]) -> bytes:
    """The canonical byte form of one event (sort_keys, compact)."""
    return json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")


def load_workload(path: str | Path) -> Workload:
    """Load and validate a JSONL tape. Fail loud on any deviation."""
    path = Path(path)
    canonical: list[bytes] = []
    events: list[dict[str, Any]] = []
    with path.open("rb") as f:
        for lineno, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line:
                continue  # blank separators are ignored, not hashed
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkloadError(f"line {lineno}: invalid JSON: {exc}") from exc
            event = _validate_event(obj, lineno)
            canonical.append(canonical_line(event))
            events.append(event)
    if not events:
        raise WorkloadError("workload tape is empty — nothing to replay (NO-DATA by design)")
    _validate_turn_monotonic(events)
    _validate_unique_ids(events)
    return Workload(events=tuple(events), fingerprint=fingerprint_bytes(canonical))


def fingerprint(workload: Workload) -> str:
    """BLAKE2b hex of the workload's canonical bytes (already computed)."""
    return workload.fingerprint


def _validate_turn_monotonic(events: list[dict[str, Any]]) -> None:
    """Turn order is semantic: ``t`` is non-decreasing across the tape."""
    prev = -1
    for e in events:
        if e["t"] < prev:
            raise WorkloadError(f"turn index went backwards at t={e['t']}")
        prev = e["t"]


def _validate_unique_ids(events: list[dict[str, Any]]) -> None:
    """``qid`` is the per-task identity (per-strata tables key on it).

    A duplicated qid would silently collapse two tasks in the report —
    refused loudly at load time, not discovered at read time.
    """
    seen: set[str] = set()
    for e in events:
        if e["kind"] == "query":
            qid = e["qid"]
            if qid in seen:
                raise WorkloadError(f"duplicate query id {qid!r} at t={e['t']}")
            seen.add(qid)


# ── Synthetic generator (fixed-seed, no RNG state) ──────────────────────────

#: Vocabulary of the v1 synthetic world. Content is deliberately dull:
#: short declarative facts with unique machine markers, rule statements
#: with scope labels, session chatter. Nothing secret, nothing real.

_FACT_TOPICS = (
    "deploy pin", "schema rollback", "cache ttl", "retry budget", "quota window",
    "index rebuild", "log rotation", "backup schedule", "drain timeout", "probe cadence",
    "artifact retention", "token quota", "rate limiter", "secret rotation",
    "queue depth", "batch size", "health check", "snapshot interval",
    "fallback path", "circuit breaker",
)

_FACT_TAILS = (
    "was pinned to build 4711", "is scheduled on nightly-02", "must stay under 300 requests",
    "expires after 14 days", "is owned by team obsidian", "reports to the amber cache",
    "runs in region eu-central", "defaults to port 8443", "was frozen at level warn",
    "is allowed only from the green channel", "lives in the north queue",
    "is capped at tier two", "follows the quiet pool schedule", "binds to lane three",
    "uses the old registry", "deployed to cluster delta", "waits in bay 6",
    "syncs with window w9", "answers to the on-call pager", "mirrors the shadow pool",
)

_RULE_SCOPES = (
    "payments service", "auth service", "search service", "export job",
    "ingest pipeline", "notify worker",
)

_RULE_BODIES = (
    "roll out canary before full deploy",
    "double the timeout before scaling replicas",
    "freeze schema changes during release week",
    "route overflow traffic to the standby pool",
    "require two approvers for production changes",
    "replay the dead letter queue before the nightly window",
)

_SESSION_SUBJECTS = (
    "incident bridge", "capacity review", "onboarding walk", "vendor audit",
    "postmortem draft", "budget sync",
)

_SESSION_FACTS = (
    "opened with the timeline recap",
    "assigned the follow-up to the platform pair",
    "logged the decision in the running notes",
    "closed with a checkpoint summary",
)

_ABSENT_DOMAINS = (
    "astral navigation", "deep sea welding", "alpine beekeeping",
    "orbital plumbing", "quantum gardening", "volcanic accounting",
)


def _mark(seed: str, seq: int) -> str:
    """Workload-stable marker: S5M-<hex> — greppable, opaque, unique."""
    digest = hashlib.blake2b(f"{seed}|marker|{seq}".encode(), digest_size=8)
    return f"{MARKER_PREFIX}-{digest.hexdigest()[:12].upper()}"


def _stable_pick(seed: str, domain: str, seq: int, choices: tuple[str, ...]) -> str:
    """Deterministic domain-scoped pick: no RNG, just a keyed digest."""
    digest = hashlib.blake2b(f"{seed}|{domain}|{seq}".encode(), digest_size=8).digest()
    return choices[digest[0] % len(choices)]


def _paraphrase(text: str) -> str:
    """A cheap deterministic paraphrase: same words, reordered prefix."""
    words = text.split()
    if len(words) <= 3:
        return "asking again about " + text
    head, rest = words[:2], words[2:]
    return " ".join([*rest, "-", *head])


def synthetic_workload(
    strata: tuple[int, int, int] = DEFAULT_STRATA,
    negatives: int = DEFAULT_NEGATIVES,
    *,
    seed: str = DEFAULT_SEED,
) -> list[dict[str, Any]]:
    """Build the S5 corpus. Deterministic: same arguments → same tape.

    Seed discipline (preregistration §Дизайн): the tape is derived from
    a BLAKE2b stream keyed by ``seed``; the DEFAULT constant is part of
    the v1 corpus definition and is the only preregistered corpus.
    Non-default seeds/strata are engineering/test shapes — never cited
    as S5 evidence.

    Strata: ``strata[0]`` fact-insertion tasks (write a unique-marker
    fact, then query it by paraphrase), ``strata[1]`` rule-query tasks
    (write a rule-like memory, then ask which applies), ``strata[2]``
    multi-turn session tasks (facts interleaved with queries), plus
    ``negatives`` control queries about things never written, carrying
    ``absent`` markers of unrelated facts (the M arm must NOT degrade
    on them — wallpaper-memory signal; a leg whose window carries the
    absent markers FAILS the task).

    Turn discipline: a single global counter gives strictly increasing
    ``t`` in generation order (write before its query; hint before its
    session's writes; the query last in its session block).
    """
    if min(strata) < 0 or negatives < 0:
        raise ValueError("strata/negatives must be non-negative")
    events: list[dict[str, Any]] = []
    turn = 0
    marker_seq = 0
    fact_markers: list[str] = []

    # ── stratum 1: fact-insertion ────────────────────────────────────
    for i in range(strata[0]):
        topic = _stable_pick(seed, "fact-topic", i, _FACT_TOPICS)
        tail = _stable_pick(seed, "fact-tail", i, _FACT_TAILS)
        marker = _mark(seed, marker_seq)
        marker_seq += 1
        fact_markers.append(marker)
        turn += 1
        events.append(
            {
                "t": turn,
                "kind": "write",
                "sid": f"fact-{i}",
                "mid": marker,
                "text": f"The {topic} {tail}. ({marker})",
            }
        )
        turn += 1
        events.append(
            {
                "t": turn,
                "kind": "query",
                "sid": f"fact-{i}",
                "qid": f"fact-q-{i}",
                "stratum": "fact",
                "text": _paraphrase(f"What is the {topic}?"),
                "expect": {"markers": [marker], "absent": [], "min_ratio": 1.0},
            }
        )

    # ── stratum 2: rule-query ────────────────────────────────────────
    for i in range(strata[1]):
        service = _stable_pick(seed, "rule-service", i, _RULE_SCOPES)
        body = _stable_pick(seed, "rule-body", i, _RULE_BODIES)
        marker = _mark(seed, marker_seq)
        marker_seq += 1
        turn += 1
        events.append(
            {
                "t": turn,
                "kind": "write",
                "sid": f"rule-{i}",
                "mid": marker,
                "text": f"{service} rule: {body}. ({marker})",
            }
        )
        turn += 1
        events.append(
            {
                "t": turn,
                "kind": "query",
                "sid": f"rule-{i}",
                "qid": f"rule-q-{i}",
                "stratum": "rule",
                "text": _paraphrase(f"Which rule applies to the {service}?"),
                "expect": {"markers": [marker], "absent": [], "min_ratio": 1.0},
            }
        )

    # ── stratum 3: multi-turn session ────────────────────────────────
    # Facts interleaved with queries: 2 writes → mid checkpoint query
    # (first two markers) → 2 more writes → final summarize query (all
    # four markers, min_ratio 0.5). Two queries per session make the
    # session PAIR-CAPABLE for the dynamism corridor (assemblies >= 2).
    for i in range(strata[2]):
        subject = _stable_pick(seed, "session-subject", i, _SESSION_SUBJECTS)
        turn += 1
        events.append(
            {
                "t": turn,
                "kind": "hint",
                "sid": f"session-{i}",
                "text": f"Standing hint for the {subject}: keep answers terse.",
            }
        )
        session_markers: list[str] = []
        for step in range(SESSION_WRITES):
            detail = _stable_pick(seed, "session-fact", i * 16 + step, _SESSION_FACTS)
            marker = _mark(seed, marker_seq)
            marker_seq += 1
            session_markers.append(marker)
            turn += 1
            events.append(
                {
                    "t": turn,
                    "kind": "write",
                    "sid": f"session-{i}",
                    "mid": marker,
                    "text": f"{subject} step {step}: {detail}. ({marker})",
                }
            )
            if step == 1:  # mid checkpoint query — facts interleaved
                turn += 1
                events.append(
                    {
                        "t": turn,
                        "kind": "query",
                        "sid": f"session-{i}",
                        "qid": f"session-q-{i}-mid",
                        "stratum": "session",
                        "text": f"What has happened in the {subject} so far?",
                        "expect": {
                            "markers": session_markers[:2],
                            "absent": [],
                            "min_ratio": 1.0,
                        },
                    }
                )
        turn += 1
        events.append(
            {
                "t": turn,
                "kind": "query",
                "sid": f"session-{i}",
                "qid": f"session-q-{i}-final",
                "stratum": "session",
                "text": _paraphrase(f"Summarize the {subject} so far."),
                "expect": {"markers": session_markers, "absent": [], "min_ratio": 0.5},
            }
        )

    # ── negative control: queries about things NEVER written ─────────
    for i in range(negatives):
        domain = _stable_pick(seed, "absent-domain", i, _ABSENT_DOMAINS)
        # absent := 3 deterministic fact-strata markers (written about
        # OTHER topics — never this domain): a leg whose window carries
        # them is surfacing unrelated memory (wallpaper signal).
        stride = max(1, len(fact_markers) // 3)
        absent = (
            [fact_markers[(i * stride + k) % len(fact_markers)] for k in range(3)]
            if fact_markers
            else []
        )
        turn += 1
        events.append(
            {
                "t": turn,
                "kind": "query",
                "sid": f"negative-{i}",
                "qid": f"negative-q-{i}",
                "stratum": "negative",
                "text": _paraphrase(f"Tell me about the {domain} handbook."),
                "expect": {"markers": [], "absent": absent, "min_ratio": 1.0},
            }
        )

    _validate_turn_monotonic(events)
    _validate_unique_ids(events)
    return events


def write_workload(path: str | Path, events: list[dict[str, Any]]) -> Workload:
    """Serialize a workload to JSONL (canonical) and verify round-trip."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for e in events:
            f.write(canonical_line(e).decode("utf-8") + "\n")
    # The written tape must survive its own loader unchanged — a
    # generator bug that emits an invalid event fails HERE, not at
    # replay time.
    workload = load_workload(path)
    if len(workload.events) != len(events):
        raise WorkloadError("workload round-trip lost events")
    return workload


def workload_summary(workload: Workload) -> dict[str, Any]:
    """Counts by kind and stratum — the report's workload header."""
    by_stratum: dict[str, int] = {}
    for q in workload.queries():
        by_stratum[q["stratum"]] = by_stratum.get(q["stratum"], 0) + 1
    return {
        "fingerprint": workload.fingerprint,
        "events": len(workload.events),
        "writes": workload.n_writes,
        "queries": workload.n_queries,
        "hints": workload.n_hints,
        "sessions": len(workload.sessions()),
        "queries_by_stratum": dict(sorted(by_stratum.items())),
    }
