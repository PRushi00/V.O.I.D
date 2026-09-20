"""The privacy-safe performance stream (V2.0 T0.6).

Guarantee under test: telemetry carries numbers, booleans, enum values, random ids and
short program-controlled names - NEVER transcripts, audio, file contents, tool
arguments, paths, secrets or memory text. Anything else is dropped and counted."""
import json
import random
import string
import threading
import time
from pathlib import Path

import pytest

from void import perf
from void.perf import schema


@pytest.fixture
def sink(tmp_path):
    path = perf.configure(tmp_path / "perf")
    assert path is not None
    yield path
    perf.shutdown()


def _lines(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def test_emit_is_a_cheap_noop_until_configured():
    perf.shutdown()
    assert perf.emit("activation", source="cli") is False
    t0 = time.perf_counter()
    for _ in range(100_000):
        perf.emit("activation", source="cli")
    assert time.perf_counter() - t0 < 1.5                 # instrumentation costs ~nothing when off


def test_valid_event_is_written_as_one_json_line(sink):
    assert perf.emit("tool", name="launch_app", risk="LOW", duration_s=0.06, ok=True) is True
    rec = _lines(sink)[0]
    assert rec["event"] == "tool" and rec["name"] == "launch_app" and rec["risk"] == "LOW"
    assert rec["duration_s"] == 0.06 and rec["ok"] is True and isinstance(rec["ts"], float)


def test_unknown_event_is_dropped_and_counted(sink):
    before = perf.stats()["unknown_event"]
    assert perf.emit("transcript", text="hello") is False
    assert perf.stats()["unknown_event"] == before + 1
    assert not sink.exists() or _lines(sink) == []


@pytest.mark.parametrize("event,field,bad", [
    ("endpoint", "reason", "open notepad please"),                 # transcript-like
    ("stt", "backend", r"C:\Users\someone\secret.txt"),            # path
    ("stt", "backend", "/home/user/notes.txt"),                    # path
    ("tool", "name", "AIzaSy" + "A" * 33),                         # API-key shaped
    ("tool", "name", "a" * 32 + "b" * 32),                         # long token
    ("tool", "name", "read file now"),                             # spaces
    ("llm", "error_class", "x" * 100),                             # over length
    ("llm", "attempt", True),                                      # bool is not an int
    ("stt", "audio_s", float("nan")),
    ("stt", "audio_s", float("inf")),
    ("complete", "status", "hello world"),                         # not in the enum
    ("tool", "risk", "CRITICAL"),                                  # not in the enum
    ("mic", "attempts", "3"),                                      # wrong type
])
def test_content_like_or_mistyped_values_are_dropped(sink, event, field, bad):
    before = perf.stats()["dropped_fields"]
    perf.emit(event, **{field: bad})
    rec = _lines(sink)[-1]
    assert field not in rec, f"{field}={bad!r} reached the telemetry file"
    assert perf.stats()["dropped_fields"] == before + 1


def test_reserved_envelope_keys_cannot_be_used_as_fields_and_never_raise(sink):
    assert perf.emit("mic", state="unavailable", ts=1, event="hijack", attempts=2) is True
    rec = _lines(sink)[-1]
    assert rec["event"] == "mic" and rec["ts"] != 1 and rec["state"] == "unavailable" and rec["attempts"] == 2


def test_unlisted_field_names_are_dropped(sink):
    perf.emit("activation", source="wake", transcript="secret words", path=r"C:\x", arguments="{}")
    rec = _lines(sink)[-1]
    assert set(rec) == {"ts", "event", "source"}


def test_interaction_id_is_attached_from_context_and_validated(sink):
    with perf.interaction("0123456789abcdef") as iid:
        assert perf.current_interaction_id() == iid
        perf.emit("route", provider="gemini", reason="select")
    assert perf.current_interaction_id() is None
    assert _lines(sink)[-1]["interaction_id"] == "0123456789abcdef"
    perf.emit("route", provider="gemini", reason="select", interaction_id="not an id!")
    assert "interaction_id" not in _lines(sink)[-1]               # malformed id dropped


def test_context_is_per_thread_so_ids_never_leak_between_interactions(sink):
    seen = {}

    def worker(n):
        with perf.interaction(f"{n:016x}"):
            time.sleep(0.01)
            seen[n] = perf.current_interaction_id()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 9)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(seen[n] == f"{n:016x}" for n in seen)


def test_concurrent_emits_are_all_valid_json_lines(sink):
    def worker():
        for i in range(200):
            perf.emit("llm", attempt=1, duration_s=i / 100, ok=True, tool_calls=1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(_lines(sink)) == 1600


def test_seeded_fuzz_never_lets_content_reach_the_file(sink):
    """Push canary content and random junk through EVERY event/field; nothing that is
    not schema-valid may be written, and no canary may appear anywhere in the file."""
    rng = random.Random(20260921)
    canaries = ["CANARY-TRANSCRIPT open my resume", r"C:\Users\CANARYUSER\Desktop\plan.docx",
                "AIzaSyCANARYKEY" + "x" * 24, "hello world this is a sentence",
                "line1\nline2", "https://attacker.example/?d=CANARY"]
    alphabet = string.ascii_letters + string.digits + " _.:-/\\\n\t\u00e9\u4e2d"
    for _ in range(3000):
        event = rng.choice(list(schema.EVENTS) + ["nope"])
        fields = {}
        for _f in range(rng.randint(1, 5)):
            key = rng.choice(list(schema.EVENTS.get(event, {"x": 0})) + ["text", "path", "args"])
            kind = rng.random()
            if kind < 0.3:
                value = rng.choice(canaries)
            elif kind < 0.6:
                value = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
            elif kind < 0.8:
                value = rng.choice([rng.random() * 100, rng.randint(-10, 10**13), float("nan"), None, [1], {"a": 1}])
            else:
                value = rng.choice([True, False, "LOW", "wake", "completed"])
            fields[key] = value
        perf.emit(event, **fields)
    text = Path(sink).read_text(encoding="utf-8")
    assert "CANARY" not in text and "attacker" not in text and "Users" not in text
    for rec in _lines(sink):
        spec = schema.EVENTS[rec["event"]]
        for key, value in rec.items():
            if key in ("ts", "event"):
                continue
            check = spec.get(key) or schema.COMMON.get(key)
            assert check is not None and check(value), f"schema-invalid {key}={value!r} was written"


def test_perf_file_is_size_bounded_by_rotation(tmp_path):
    perf.shutdown()
    path = perf.configure(tmp_path / "p", max_bytes=4000, backup_count=2)
    try:
        for i in range(2000):
            perf.emit("llm", attempt=1, duration_s=1.5, ok=True, tool_calls=0)
        files = list(Path(path).parent.glob("perf.jsonl*"))
        assert 1 < len(files) <= 3                                # live + <=2 backups
        assert sum(f.stat().st_size for f in files) < 3 * 4000 + 2000
    finally:
        perf.shutdown()


def test_configure_is_idempotent_and_shutdown_detaches(tmp_path):
    first = perf.configure(tmp_path / "a")
    second = perf.configure(tmp_path / "b")                        # ignored: already configured
    assert first == second
    perf.shutdown()
    assert perf.emit("activation", source="cli") is False
    assert not (tmp_path / "b").exists()
