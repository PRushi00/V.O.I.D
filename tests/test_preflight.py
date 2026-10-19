"""``void preflight``: the harness that executes the live chains, and the rules it must obey.

The tests here are mostly *about the harness itself*, because a harness that lies is worse than no
harness. So: a healthy installation passes with no failures; a broken one fails on the specific thing
that is broken and names it; an environment limit WARNS rather than FAILS, or the owner learns to
ignore the output; a probe's own bug becomes one FAIL rather than taking the run down; and nothing it
does is destructive, outward-facing or left behind.
"""
import os
import stat
from pathlib import Path

import pytest

from void import preflight
from void.config import Config
from void.preflight import FAIL, OK, WARN, Report, exit_code, format_report, run_preflight
from void.security import ownership


@pytest.fixture
def home(tmp_path) -> Path:
    """A plausible installation: allowed roots, a notes folder with notes, its own state dir."""
    notes = tmp_path / "home" / "Notes"
    (notes / "Projects").mkdir(parents=True)
    (notes / "Projects" / "Sideview Pricing.md").write_text(
        "# Sideview Pricing\nThe retainer is 2400 a month.\n", encoding="utf-8")
    (notes / "Retainer Terms.md").write_text(
        "# Retainer Terms\nThirty days notice either way.\n", encoding="utf-8")
    return tmp_path / "home"


def config_for(home: Path, **over) -> Config:
    data = {"app": {"state_dir": str(home / ".void")},
            "security": {"allowed_roots": [str(home)]},
            "memory": {"enabled": True},
            "vault": {"notes_root": str(home / "Notes")},
            "llm": {"primary": "gemini", "fallback": ["local"],
                    "gemini": {"model": "gemini-3.8-flash"},
                    "local": {"model": "qwen3:8b"}}}
    data.update(over)
    return Config(data)


def by_name(report: Report) -> dict:
    return {p.name: p for p in report.probes}


# ================================================================== a healthy installation

def test_a_healthy_installation_has_no_failures(home):
    report = run_preflight(config_for(home), offline=True)
    failures = [f"{p.name}: {p.detail}" for p in report.probes if p.status == FAIL]
    assert failures == []
    assert exit_code(report) == 0


def test_the_summary_is_the_one_line_the_packs_ask_for(home):
    report = run_preflight(config_for(home), offline=True)
    assert report.summary == (f"{report.count(OK)} pass, {report.count(FAIL)} fail, "
                              f"{report.count(WARN)} warn")
    assert report.count(OK) >= 10


def test_every_chain_that_matters_is_actually_probed(home):
    """The list of probes is the contract; a silently dropped probe is a silently dropped guarantee."""
    names = set(by_name(run_preflight(config_for(home), offline=True)))
    for chain in ("assembly", "source on disk", "state file permissions", "engine protection",
                  "path confinement", "task store", "kill switch", "risk gate", "receipt judge",
                  "model refusal", "vault index", "vault recall", "vault honesty",
                  "notes question route", "vault capture", "memory round trip",
                  "focus sessions", "provider credential",
                  "model reachable"):
        assert chain in names, f"{chain} is no longer probed"


def test_the_live_chains_really_ran_rather_than_being_described(home):
    """Real calls, not mocks: the probes leave evidence that they executed."""
    report = run_preflight(config_for(home), offline=True)
    probes = by_name(report)
    assert "5 notes" not in probes["vault index"].detail      # 2 notes in this fixture
    assert "2 notes indexed" in probes["vault index"].detail
    assert probes["vault recall"].detail.startswith("answered with cited notes")
    assert (home / ".void" / "tasks.sqlite").exists()         # assembly really built an Assistant


# ================================================================== honest statuses

def test_an_environment_limit_warns_and_never_fails(home):
    """A laptop with no credential must not fail preflight, or its owner stops reading it."""
    report = run_preflight(config_for(home), offline=True)
    probes = by_name(report)
    assert probes["provider credential"].status == WARN
    assert probes["model reachable"].status == WARN
    assert exit_code(report) == 0
    assert exit_code(report, strict=True) == 1               # CI can still demand a clean run


def test_no_notes_folder_configured_is_a_warning_not_a_failure(home):
    report = run_preflight(config_for(home, vault={"notes_root": None}), offline=True)
    probes = by_name(report)
    assert probes["vault index"].status == WARN
    assert "vault.notes_root" in probes["vault index"].detail
    assert exit_code(report) == 0


def test_a_notes_folder_that_cannot_be_read_is_a_failure(home, tmp_path):
    """Configured but outside the allowed roots is a misconfiguration, and must say which."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    report = run_preflight(config_for(home, vault={"notes_root": str(outside)}), offline=True)
    probe = by_name(report)["vault index"]
    assert probe.status == FAIL and str(outside) in probe.detail


def test_memory_disabled_is_a_warning(home):
    report = run_preflight(config_for(home, memory={"enabled": False}), offline=True)
    assert by_name(report)["memory round trip"].status == WARN


# ================================================================== it catches real breakage

def test_a_world_readable_state_file_fails_loudly(home):
    """The defect this harness found on its first real run."""
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not describe access on Windows")
    state = home / ".void"
    state.mkdir(parents=True, exist_ok=True)
    leaky = state / "tasks.sqlite"
    leaky.write_text("", encoding="utf-8")
    os.chmod(leaky, 0o644)

    probes = by_name(run_preflight(config_for(home), offline=True,
                                   probes=(preflight._check_secrets_are_not_in_the_repository,)))
    # The Assistant has not been built in this run, so nothing re-hardened the file first.
    probe = probes["state file permissions"]
    assert probe.status == FAIL and "tasks.sqlite" in probe.detail and "chmod 600" in probe.detail


def test_a_key_shaped_value_in_configuration_fails(home, tmp_path, monkeypatch):
    leaked = tmp_path / "local_config.yaml"
    leaked.write_text('llm:\n  gemini:\n    api_key: "AIzaSyB0000000000000000000000000000"\n',
                      encoding="utf-8")
    monkeypatch.setattr("void.config.local_config_path", lambda: leaked)
    probes = by_name(run_preflight(config_for(home), offline=True,
                                   probes=(preflight._check_secrets_are_not_in_the_repository,)))
    probe = probes["no keys in config"]
    assert probe.status == FAIL and "api_key" in probe.detail and "rotate" in probe.detail
    assert "AIza" not in probe.detail                        # and it does not print the key back


def test_a_broken_judge_would_be_caught(home, monkeypatch):
    monkeypatch.setattr("void.orchestration.judge.screen",
                        lambda answer, receipts: type("R", (), {"blocked": False})())
    probes = by_name(run_preflight(config_for(home), offline=True,
                                   probes=(preflight._check_judge,)))
    assert probes["receipt judge"].status == FAIL


def test_a_judge_that_blocks_honest_answers_would_be_caught(home, monkeypatch):
    monkeypatch.setattr("void.orchestration.judge.screen",
                        lambda answer, receipts: type("R", (), {"blocked": True})())
    probe = by_name(run_preflight(config_for(home), offline=True,
                                  probes=(preflight._check_judge,)))["receipt judge"]
    assert probe.status == FAIL and "over-firing" in probe.detail


def test_a_catalog_that_approximates_a_model_name_would_be_caught(home, monkeypatch):
    from void.providers.catalog import ModelCatalog, Resolution
    monkeypatch.setattr(ModelCatalog, "resolve",
                        lambda self, spoken: Resolution(provider="gemini", model="whatever"))
    probe = by_name(run_preflight(config_for(home), offline=True,
                                  probes=(preflight._check_model_refusal,)))["model refusal"]
    assert probe.status == FAIL and "refuse" in probe.detail


def test_a_second_copy_of_the_package_on_the_path_would_be_caught(home, monkeypatch):
    import void
    monkeypatch.setattr(void, "__file__", "/somewhere/else/void/__init__.py")
    probe = by_name(run_preflight(config_for(home), offline=True,
                                  probes=(preflight._check_source_is_what_runs,)))["source on disk"]
    assert probe.status == FAIL and "not what runs" in probe.detail


def test_a_probe_that_raises_becomes_one_failure_and_the_rest_still_run(home):
    def explodes(ctx):
        raise RuntimeError("the probe itself is broken")

    report = run_preflight(config_for(home), offline=True,
                           probes=(explodes, preflight._check_risk_gate))
    assert report.count(FAIL) == 1 and report.count(OK) == 1
    assert "the probe itself raised RuntimeError" in report.probes[0].detail


# ================================================================== it is safe to run

def test_the_probe_note_is_removed_from_the_owners_vault(home):
    run_preflight(config_for(home), offline=True)
    leftovers = [str(p) for p in (home / "Notes").rglob("*") if p.is_file()
                 and preflight.PROBE_NOTE_TITLE.split()[0] in p.name]
    assert leftovers == [], f"preflight left files behind: {leftovers}"


def test_read_only_writes_no_note_at_all(home):
    report = run_preflight(config_for(home), offline=True, read_only=True)
    probe = by_name(report)["vault capture"]
    assert probe.status == WARN and "read-only" in probe.detail
    assert list((home / "Notes").rglob("captures")) == []


def test_the_owners_memory_store_is_never_written(home):
    """The memory probe runs in a scratch store; the owner's own memory must be untouched."""
    run_preflight(config_for(home), offline=True)
    probe = by_name(run_preflight(config_for(home), offline=True))["memory round trip"]
    assert probe.status in (OK, WARN)
    if probe.status == OK:
        assert "the owner's memory was not written" in probe.detail
    from void.memory.service import MemoryService
    owners = MemoryService.from_config(config_for(home))
    assert not any("zygal" in item.text for item in owners.list())


def test_the_kill_switch_probe_never_leaves_a_real_stop_engaged(home):
    run_preflight(config_for(home), offline=True)
    assert not (home / ".void" / "STOP").exists()


def test_the_scratch_directory_is_removed(home, tmp_path):
    before = set(Path(os.environ.get("TMPDIR", "/tmp")).glob("void-preflight-*"))
    run_preflight(config_for(home), offline=True)
    after = set(Path(os.environ.get("TMPDIR", "/tmp")).glob("void-preflight-*"))
    assert after <= before


def test_nothing_is_launched_or_sent(home):
    """Structural: the probe list contains no probe that could reach the outside world."""
    source = Path(preflight.__file__).read_text(encoding="utf-8")
    for forbidden in ("launch_app", "open_path", "navigate", "click_", "type_into",
                      "describe_screen", "look("):
        assert forbidden not in source, f"a preflight probe references {forbidden}"


# ================================================================== reporting

def test_the_report_renders_a_tick_or_a_cross_per_check_and_ends_with_the_summary(home):
    report = run_preflight(config_for(home), offline=True)
    text = format_report(report)
    assert "[ ok ]" in text and "[WARN]" in text
    assert text.splitlines()[-1] == report.summary
    for probe in report.probes:
        assert probe.name in text and probe.detail.splitlines()[0][:40] in text


def test_the_report_is_machine_readable_too(home):
    payload = run_preflight(config_for(home), offline=True).as_dict()
    assert set(payload) == {"probes", "summary", "pass", "fail", "warn"}
    assert payload["pass"] + payload["fail"] + payload["warn"] == len(payload["probes"])
    for probe in payload["probes"]:
        assert set(probe) == {"name", "status", "detail", "seconds"}


def test_the_exit_code_is_non_zero_only_on_a_failure():
    clean, warned, failed = Report(), Report(), Report()
    clean.add("a", OK, "")
    warned.add("a", OK, "")
    warned.add("b", WARN, "")
    failed.add("a", FAIL, "")
    assert exit_code(clean) == 0 and exit_code(warned) == 0 and exit_code(failed) == 2
    assert exit_code(warned, strict=True) == 1


# ================================================================== the hardening it prompted

def test_the_state_directory_and_its_files_are_created_owner_only(home):
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not describe access on Windows")
    from void.app import Assistant
    from void.core.task import Status, Task
    assistant = Assistant(config=config_for(home))
    assistant.store.save(Task(goal="a private goal", id="probe", status=Status.PAUSED))
    state = home / ".void"
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    for path in state.iterdir():
        if path.is_file():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


def test_an_installation_that_is_already_wrong_is_fixed_at_boot(home):
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not describe access on Windows")
    from void.app import Assistant
    state = home / ".void"
    state.mkdir(parents=True, exist_ok=True)
    (state / "tasks.sqlite").write_text("", encoding="utf-8")
    os.chmod(state / "tasks.sqlite", 0o644)
    os.chmod(state, 0o755)
    Assistant(config=config_for(home))
    assert stat.S_IMODE((state / "tasks.sqlite").stat().st_mode) == 0o600
    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_restricting_is_best_effort_and_never_raises(tmp_path):
    assert ownership.restrict(tmp_path / "does-not-exist") in (ownership.MISSING,
                                                              ownership.NOT_APPLICABLE)
    target = tmp_path / "f"
    target.write_text("x", encoding="utf-8")
    assert ownership.restrict(target) in (ownership.APPLIED, ownership.NOT_APPLICABLE)


def test_windows_is_not_pretended_about(tmp_path, monkeypatch):
    """On NTFS the mode bits would be a lie, so the module says so instead of claiming success."""
    monkeypatch.setattr(os, "name", "nt")
    target = tmp_path / "f"
    target.write_text("x", encoding="utf-8")
    assert ownership.restrict(target) == ownership.NOT_APPLICABLE
    assert ownership.too_open(target) is None
