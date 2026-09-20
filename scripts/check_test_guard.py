"""Guard against silent loss of test coverage (V2.0 T0.11).

A green run proves little if tests quietly disappeared or started skipping. This checks:

  1. every test id from the V1 baseline (``tests/baseline_v1_test_ids.txt``) is still collected;
  2. the number of collected tests has not fallen below ``min_collected``;
  3. (with ``--junit``) nothing failed or errored, and the number of SKIPPED and XFAILED tests
     has not risen above ``max_skipped`` / ``max_xfailed``.

The limits live in ``tests/guard_limits.json`` and only ratchet the good way: ``min_collected``
goes up as tests are added, ``max_xfailed`` goes down as known defects are fixed. Changing a
limit the wrong way, or editing the baseline list, is a visible, reviewable diff.

Standard library only; run from the repository root:

    python -m pytest --junitxml=junit.xml
    python scripts/check_test_guard.py --junit junit.xml
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "tests" / "baseline_v1_test_ids.txt"
LIMITS = ROOT / "tests" / "guard_limits.json"


def read_baseline(path: Path = BASELINE) -> set[str]:
    return {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")}


def read_limits(path: Path = LIMITS) -> dict:
    limits = json.loads(path.read_text(encoding="utf-8"))
    for key in ("min_collected", "max_skipped", "max_xfailed"):
        if not isinstance(limits.get(key), int) or limits[key] < 0:
            raise ValueError(f"{path}: {key!r} must be a non-negative integer")
    return limits


def collect_ids(python: str = sys.executable, cwd: Path = ROOT) -> set[str]:
    proc = subprocess.run([python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
                          cwd=cwd, capture_output=True, text=True)
    ids = {ln.strip() for ln in proc.stdout.splitlines() if "::" in ln and not ln.startswith(" ")}
    if proc.returncode not in (0,) or not ids:
        raise RuntimeError(f"test collection failed (exit {proc.returncode}):\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
    return ids


def check_collection(collected: set[str], baseline: set[str], min_collected: int) -> list[str]:
    problems = []
    missing = sorted(baseline - collected)
    if missing:
        shown = ", ".join(missing[:10]) + (f" ... (+{len(missing) - 10} more)" if len(missing) > 10 else "")
        problems.append(f"{len(missing)} baseline test(s) are no longer collected: {shown}")
    if len(collected) < min_collected:
        problems.append(f"only {len(collected)} tests collected; the floor is {min_collected}")
    return problems


def summarize_junit(path: Path) -> dict:
    root = ET.parse(path).getroot()
    counts = {"tests": 0, "failed": 0, "skipped": 0, "xfailed": 0}
    for case in root.iter("testcase"):
        counts["tests"] += 1
        if case.find("failure") is not None or case.find("error") is not None:
            counts["failed"] += 1
            continue
        skipped = case.find("skipped")
        if skipped is not None:
            # pytest reports xfail as <skipped type="pytest.xfail">; a real skip is "pytest.skip"
            kind = "xfailed" if "xfail" in (skipped.get("type") or "") else "skipped"
            counts[kind] += 1
    return counts


def check_results(counts: dict, limits: dict) -> list[str]:
    problems = []
    if counts["failed"]:
        problems.append(f"{counts['failed']} test(s) failed or errored")
    if counts["skipped"] > limits["max_skipped"]:
        problems.append(f"{counts['skipped']} tests skipped; the ceiling is {limits['max_skipped']} "
                        "(a new skip hides coverage)")
    if counts["xfailed"] > limits["max_xfailed"]:
        problems.append(f"{counts['xfailed']} tests xfailed; the ceiling is {limits['max_xfailed']}")
    if counts["tests"] < limits["min_collected"]:
        problems.append(f"only {counts['tests']} tests ran; the floor is {limits['min_collected']}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--junit", type=Path, help="junit XML from the run to check")
    args = ap.parse_args(argv)
    limits = read_limits()
    problems = check_collection(collect_ids(), read_baseline(), limits["min_collected"])
    if args.junit:
        counts = summarize_junit(args.junit)
        problems += check_results(counts, limits)
        print(f"run: {counts}")
    for p in problems:
        print(f"GUARD FAIL: {p}")
    if not problems:
        print("guard ok: no baseline test lost, skip/xfail ceilings respected")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
