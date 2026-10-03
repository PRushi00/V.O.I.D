"""T0.11: the coverage-loss guard must itself be proven to catch what it exists to catch."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_test_guard", ROOT / "scripts" / "check_test_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

LIMITS = {"min_collected": 5, "max_skipped": 1, "max_xfailed": 1}


def _junit(tmp_path, cases):
    """cases: list of ('pass'|'fail'|'error'|'skip'|'xfail')."""
    body = []
    for i, kind in enumerate(cases):
        inner = {"pass": "", "fail": "<failure message='x'/>", "error": "<error message='x'/>",
                 "skip": "<skipped type='pytest.skip' message='s'/>",
                 "xfail": "<skipped type='pytest.xfail' message='x'/>"}[kind]
        body.append(f"<testcase classname='t' name='n{i}'>{inner}</testcase>")
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites><testsuite>{''.join(body)}</testsuite></testsuites>", encoding="utf-8")
    return path


def test_baseline_file_is_well_formed_and_complete():
    ids = guard.read_baseline()
    # 883 at V1. One more since: test_factory_defaults_to_sapi_on_windows was renamed and split in two
    # when the Windows TTS default changed from "sapi" to "sapi_stream" (2026-10-02), with both halves
    # preserving the behavioural requirement it existed for - Windows defaults to local speech, never
    # silence - plus a test that the previous provider stays selectable as a rollback.
    #
    # The number is an exact count on purpose: the guard's protection is that no baseline id ever
    # disappears, and a count that drifted upward silently would hide a swap of one test for another.
    assert len(ids) == 884, "the baseline is 883 V1 tests plus one documented rename-and-split"
    assert all("::" in i and i.startswith("tests/") for i in ids)


def test_limits_file_is_valid_and_matches_reality_direction():
    limits = guard.read_limits()
    assert limits["min_collected"] >= 883 and limits["max_skipped"] >= 0 and limits["max_xfailed"] >= 0


def test_a_removed_baseline_test_is_caught():
    baseline = {"tests/a.py::t1", "tests/a.py::t2", "tests/b.py::t3"}
    assert guard.check_collection(baseline, baseline, 3) == []
    problems = guard.check_collection(baseline - {"tests/a.py::t2"}, baseline, 2)
    assert len(problems) == 1 and "tests/a.py::t2" in problems[0]


def test_a_renamed_baseline_test_is_caught_even_when_the_count_is_unchanged():
    baseline = {"tests/a.py::old", "tests/a.py::keep"}
    collected = {"tests/a.py::new", "tests/a.py::keep"}
    assert any("no longer collected" in p for p in guard.check_collection(collected, baseline, 2))


def test_new_tests_are_fine_and_a_low_count_is_caught():
    baseline = {"tests/a.py::t1"}
    assert guard.check_collection(baseline | {"tests/a.py::t2"}, baseline, 2) == []
    assert any("floor" in p for p in guard.check_collection(baseline, baseline, 5))


def test_many_missing_tests_are_summarised():
    baseline = {f"tests/a.py::t{i}" for i in range(30)}
    (problem,) = guard.check_collection(set(), baseline, 0)
    assert "30 baseline" in problem and "more" in problem


@pytest.mark.parametrize("cases,ok", [
    (["pass"] * 5, True),
    (["pass"] * 4 + ["skip"], True),                    # at the skip ceiling
    (["pass"] * 3 + ["skip", "skip"], False),           # one skip too many
    (["pass"] * 4 + ["xfail"], True),
    (["pass"] * 3 + ["xfail", "xfail"], False),
    (["pass"] * 4 + ["fail"], False),
    (["pass"] * 4 + ["error"], False),
    (["pass"] * 4, False),                              # below the floor
])
def test_result_checks(tmp_path, cases, ok):
    problems = guard.check_results(guard.summarize_junit(_junit(tmp_path, cases)), LIMITS)
    assert (problems == []) is ok, problems


def test_xfail_and_skip_are_told_apart(tmp_path):
    counts = guard.summarize_junit(_junit(tmp_path, ["pass", "skip", "xfail", "xfail", "fail"]))
    assert counts == {"tests": 5, "failed": 1, "skipped": 1, "xfailed": 2}


def test_limits_validation_rejects_garbage(tmp_path):
    for bad in ({}, {"min_collected": -1, "max_skipped": 0, "max_xfailed": 0},
                {"min_collected": "5", "max_skipped": 0, "max_xfailed": 0}):
        p = tmp_path / "l.json"
        p.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError):
            guard.read_limits(p)


def test_pytest_is_scoped_to_this_repositorys_own_tests():
    """Bare `pytest` from the repository root once collected the untracked wakeword-training/tests package, whose
    `tests` name collides with ours, and aborted the whole run. Collection must stay scoped to tests/.

    Asserts the PROPERTY rather than the mechanism. The previous version read ``pytest.ini`` from the root
    and broke when the owner deliberately moved their configuration into ``workspace/`` - it was testing
    where a file lived, not whether collection was actually scoped. Scoping now comes from the root
    ``conftest.py``, which is pytest's own mechanism for this and, unlike ``testpaths``, also holds when a
    path argument is given. Checking the ignore list directly keeps the test true under either mechanism.
    """
    import conftest

    ignored = {Path(entry).name for entry in getattr(conftest, "collect_ignore", ())}
    assert "wakeword-training" in ignored, (
        "the sibling project's colliding tests package must be excluded from collection")
    # Scoping must not be accidental: the module has to say what it excludes and why.
    assert "wakeword-training" in conftest.NOT_OURS
