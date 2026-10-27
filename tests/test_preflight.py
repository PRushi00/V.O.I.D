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
                  "focus sessions", "teardown kit", "teardown routes",
                  "no stray browser", "provider credential",
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


# ----------------------------------------------------------------- the two probes that lied

def test_reading_voids_own_source_is_allowed_but_changing_it_is_not(home):
    """The policy the engine actually implements, pinned so a probe cannot quietly contradict it again.

    ``void.security.protected`` puts V.O.I.D's own package in the WRITE-deny set and says so in as
    many words - "refused for mutation only". Its source is public code; the risk is a tool editing
    the engine underneath itself, not a tool reading it. The preflight probe nonetheless demanded
    that a READ of ``void/preflight/__init__.py`` be refused, and got away with it for as long as
    every configuration in sight had allowed roots too narrow to reach the repository at all.
    """
    from void.app import Assistant
    from void.config import _PKG_DIR

    repo = Path(_PKG_DIR).parent
    assistant = Assistant(config=config_for(home, security={"allowed_roots": [str(repo)]}))
    own_source = str(Path(_PKG_DIR) / "preflight" / "__init__.py")

    assert assistant.tools.execute("read_file", {"path": own_source}).ok, (
        "V.O.I.D's own source is write-denied, not read-protected; refusing the read would be a new "
        "policy, and this test exists so that it cannot arrive by accident")
    refused = assistant.tools.execute(
        "write_file", {"path": str(Path(_PKG_DIR) / "nope.py"), "content": "x"})
    assert not refused.ok and "protected" in (refused.error or "")
    assert not (Path(_PKG_DIR) / "nope.py").exists()


def test_the_engine_protection_probe_rejects_a_refusal_it_did_not_earn(home, monkeypatch):
    """The defect that hid the first one: a refusal used to count however it was reached.

    A path refused for being outside ``security.allowed_roots`` proves confinement, not protection,
    and a probe that cannot tell them apart reports a guarantee it never measured. That is exactly
    what happened here - it passed on this suite's narrow roots for as long as they stayed narrow.
    """
    from void.actions.base import ToolResult
    from void.actions.registry import ToolRegistry

    monkeypatch.setattr(ToolRegistry, "execute", lambda self, name, args: ToolResult.failure(
        f"'{args['path']}' is outside the allowed roots (nowhere)."))

    probe = by_name(run_preflight(config_for(home), offline=True,
                                  probes=(preflight._check_engine_protection,)))["engine protection"]
    assert probe.status == FAIL and "not as protected" in probe.detail


def test_a_write_that_reaches_voids_own_package_fails_loudly(home, monkeypatch):
    from void.actions.base import ToolResult
    from void.actions.registry import ToolRegistry

    real = ToolRegistry.execute

    def allow_the_write(self, name, args):
        if name == "write_file":
            return ToolResult.success("written")
        return real(self, name, args)

    monkeypatch.setattr(ToolRegistry, "execute", allow_the_write)
    probe = by_name(run_preflight(config_for(home), offline=True,
                                  probes=(preflight._check_engine_protection,)))["engine protection"]
    assert probe.status == FAIL and "own package" in probe.detail


def test_allowed_roots_that_cover_a_system_file_warn_rather_than_allege_an_escape(home):
    """Reading a file INSIDE the allowed roots is the configured behaviour, not a breach.

    The probe used to read a hardcoded hosts file and, on success, report it as "outside
    security.allowed_roots" - a claim it never checked. With ``allowed_roots: [C:\\]``, which
    ``void.security.protected`` documents as the owner's own setting and designs around, that file is
    inside the roots. The breadth of the roots is the real finding and belongs in the report as
    itself; a FAIL alleging an escape that did not happen only teaches the owner to stop reading.
    """
    root = "C:/" if os.name == "nt" else "/"
    probe = by_name(run_preflight(config_for(home, security={"allowed_roots": [root]}),
                                  offline=True,
                                  probes=(preflight._check_confinement,)))["path confinement"]
    assert probe.status == WARN
    assert "confinement works" in probe.detail or "cannot be demonstrated" in probe.detail
    assert "returned content" not in probe.detail


def test_confinement_still_fails_when_a_path_outside_the_roots_is_readable(home, monkeypatch):
    from void.actions.base import ToolResult
    from void.actions.registry import ToolRegistry

    monkeypatch.setattr(ToolRegistry, "execute",
                        lambda self, name, args: ToolResult.success("here it is", data="root:x:0:0"))
    probe = by_name(run_preflight(config_for(home), offline=True,
                                  probes=(preflight._check_confinement,)))["path confinement"]
    assert probe.status == FAIL and "returned content" in probe.detail


def test_a_path_outside_the_roots_needs_no_existence_to_test_the_check(home):
    """``_confine`` applies the roots test before it touches the filesystem, which is what lets the
    probe use a path that cannot exist - and therefore cannot be refused for an unrelated reason."""
    root = Path(home).resolve()
    outside = preflight._a_path_outside([root])
    assert outside is not None and not outside.exists()
    assert not outside.is_relative_to(root)
    # Roots that leave nothing outside are reported as such rather than guessed at. On Windows an
    # unused drive letter is still outside C:\, so exhausting the candidates needs every letter.
    everything = ([Path(f"{c}:/") for c in "CQRSTUVWXYZ"] if os.name == "nt" else [Path("/")])
    assert preflight._a_path_outside(everything) is None


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


# ------------------------------------------------------------------------------------------------
# A provider error is not automatically a verdict on the key
# ------------------------------------------------------------------------------------------------
class _Ctx:
    """The two attributes ``_report_provider_failure`` touches."""

    def __init__(self):
        self.report = Report()


@pytest.mark.parametrize("message, category", [
    ("503 UNAVAILABLE. This model is currently experiencing high demand.", "server"),
    ("504 deadline exceeded", "timeout"),
    ("connection refused talking to generativelanguage.googleapis.com", "network"),
    ("429 RESOURCE_EXHAUSTED: too many requests", "rate_limit"),
])
def test_a_service_fault_warns_and_does_not_blame_the_credential(message, category):
    """Observed live: the configured model answered 503 "experiencing high demand" while the key
    was provably valid - a real generation had just succeeded on another id.

    Reported as FAIL, that line reads "your key is broken" and sends the owner to rotate a perfectly
    good credential. The whole point of this harness is not to produce that kind of wrong certainty.
    """
    ctx = _Ctx()
    preflight._report_provider_failure(ctx, "provider credential", "gemini (x)",
                                       RuntimeError(message), 0.1)
    probe = by_name(ctx.report)["provider credential"]
    assert probe.status == WARN
    assert category in probe.detail
    assert "no evidence about the credential" in probe.detail


@pytest.mark.parametrize("message", [
    "401 UNAUTHENTICATED: API key not valid. Please pass a valid API key.",
    "403 PERMISSION_DENIED",
    "quota exceeded for this project",
    "400 INVALID_ARGUMENT: malformed request",
])
def test_a_persistent_fault_still_fails(message):
    """Each of these names something the owner must actually change, and none of them goes away on
    its own. Downgrading these to WARN would be weakening the harness to get a clean run."""
    ctx = _Ctx()
    preflight._report_provider_failure(ctx, "provider credential", "gemini (x)",
                                       RuntimeError(message), 0.1)
    assert by_name(ctx.report)["provider credential"].status == FAIL


def test_a_retired_model_fails_and_carries_the_api_s_own_replacement_advice():
    """Observed live: `gemini-2.5-flash` is still listed by models.list and still answers
    models.get with 200, but refuses generation. Only the error body says what to use instead, so
    it has to survive into the report."""
    ctx = _Ctx()
    exc = RuntimeError("404 NOT_FOUND: This model models/gemini-2.5-flash is no longer available "
                       "to new users. Please update your code to use models/gemini-3.8-flash")
    preflight._report_provider_failure(ctx, "model reachable", "gemini-2.5-flash (models.get)",
                                       exc, 0.1)
    probe = by_name(ctx.report)["model reachable"]
    assert probe.status == FAIL
    assert "gemini-3.8-flash" in probe.detail


def test_an_unrecognised_failure_is_treated_as_real_rather_than_waved_through():
    """`failures.classify` returns 'other' for anything it does not know. For retrying that means
    "try once more"; for this report it must mean FAIL, because a harness that downgrades the
    unknown stops being evidence of anything."""
    ctx = _Ctx()
    preflight._report_provider_failure(ctx, "provider credential", "gemini (x)",
                                       RuntimeError("something nobody has seen before"), 0.1)
    assert by_name(ctx.report)["provider credential"].status == FAIL


def test_the_exact_failure_observed_against_the_live_api_warns_rather_than_fails():
    """The regression this set exists for, taken verbatim from a real run.

    A session with a provably valid key - `models.get` succeeded on the same credential seconds
    earlier, and a real generation had billed tokens on another id - got:

        [FAIL] provider credential  gemini (gemini-3.8-flash) raised ProviderUnavailable:
               Gemini did not answer within 30s. (29.21s)

    reproducibly, twice. The cause was the model returning 503 "experiencing high demand"; the
    30-second deadline hides that body, so the report named the credential instead of the outage.

    The first version of `_TRANSIENT_FAILURES` did NOT catch this: `classify` short-circuits on
    `ProviderUnavailable` before reading the message, so it returns `unavailable` rather than
    `timeout` or `server`. This test pins the real exception, not a plausible one.
    """
    from void.providers.base import ProviderUnavailable
    ctx = _Ctx()
    exc = ProviderUnavailable("Gemini did not answer within 30s.")
    preflight._report_provider_failure(ctx, "provider credential",
                                       "gemini (gemini-3.8-flash)", exc, 29.21)
    probe = by_name(ctx.report)["provider credential"]
    assert probe.status == WARN
    assert "no evidence about the credential" in probe.detail


def test_a_persistent_outage_is_still_made_actionable():
    """WARN must not mean "ignore me". If the model never serves, the id is what to change."""
    from void.providers.base import ProviderUnavailable
    ctx = _Ctx()
    preflight._report_provider_failure(ctx, "provider credential", "gemini (x)",
                                       ProviderUnavailable("did not answer within 30s."), 30.0)
    assert "configured id" in by_name(ctx.report)["provider credential"].detail


def test_having_no_credential_at_all_never_reaches_this_helper():
    """`unavailable` is only safe in the transient set because the no-credential case returns
    earlier, with its own WARN. If that guard were ever removed, a missing key would start
    reporting as a transient outage - so this pins the guard, not the helper."""
    import inspect
    body = inspect.getsource(preflight._check_provider_credential)
    guard = body.index("no provider has a usable credential here")
    call = body.index("provider.generate(")
    assert guard < call, "the no-credential WARN must come before any call that can raise"


def test_the_transient_set_holds_only_categories_the_classifier_defines():
    """A typo here would silently turn a real failure into a warning for ever."""
    from void.providers.failures import POLICY
    assert preflight._TRANSIENT_FAILURES <= set(POLICY)


def test_no_persistent_category_leaked_into_the_transient_set():
    for category in ("auth", "quota", "unsupported_model", "invalid_request"):
        assert category not in preflight._TRANSIENT_FAILURES


# ------------------------------------------------------------------------------------------------
# ... but a pool that was tried and wholly refused IS a verdict on the key
# ------------------------------------------------------------------------------------------------
def test_every_credential_refused_fails_instead_of_warning():
    """The case the set above does not cover, found by reading the merged probes rather than by a run.

    When the pool runs out, ``_call`` raises - and ``classify`` short-circuits on
    ProviderUnavailable before reading anything, so five rejected keys arrived here as
    ``unavailable``, a member of _TRANSIENT_FAILURES. The report then said "no evidence about the
    credential either way" directly beside an exception stating that every credential was rejected,
    as a WARN, which ``exit_code`` passes. A wholly dead credential set produced a clean preflight.

    The transient set is right about what it covers; this is simply not one of those cases, and the
    two are told apart by TYPE rather than by the wording of a message.
    """
    from void.providers.base import CredentialsRejected
    ctx = _Ctx()
    exc = CredentialsRejected(
        "All 5 Gemini credential(s) are exhausted: each was rejected or rate-limited. A rejected "
        "key needs replacing ('python -m void set-key gemini'); a rate-limited one needs time.",
        categories=frozenset({"auth"}))
    preflight._report_provider_failure(ctx, "provider credential",
                                       "gemini (gemini-3.5-flash)", exc, 2.4)
    probe = by_name(ctx.report)["provider credential"]
    assert probe.status == FAIL
    assert "no evidence about the credential" not in probe.detail
    assert "set-key gemini" in probe.detail            # the remedy has to survive into the report


def test_the_refusal_is_recognised_by_type_rather_than_by_its_message():
    """Two exceptions carrying the same sentence, reported differently, because only one of them came
    from a pool that actually tried every credential.

    Matching the sentence instead would be one provider rewording away from silence, and would put
    the live-503 regression above back at risk the first time a timeout message mentioned a
    credential. The cost of deciding by type is that a plain ProviderUnavailable saying the same
    thing still warns - which is safe only because every exhaustion path raises the subclass, and
    the next test pins that.
    """
    from void.providers.base import CredentialsRejected, ProviderUnavailable
    sentence = "All 5 Gemini credential(s) are exhausted: each was rejected or rate-limited."

    typed, untyped = _Ctx(), _Ctx()
    preflight._report_provider_failure(
        typed, "provider credential", "gemini (x)",
        CredentialsRejected(sentence, categories=frozenset({"auth"})), 0.1)
    preflight._report_provider_failure(
        untyped, "provider credential", "gemini (x)", ProviderUnavailable(sentence), 0.1)

    assert by_name(typed.report)["provider credential"].status == FAIL
    assert by_name(untyped.report)["provider credential"].status == WARN


def test_both_providers_raise_the_typed_exception_when_their_pool_runs_out():
    """Checked by source because there are three such raise sites across two providers and no single
    call reaches them all. A site left as a plain ProviderUnavailable would quietly restore the WARN
    that this whole section exists to remove."""
    import ast
    import inspect

    from void.providers import gemini_provider, openai_provider

    for module, marker in ((gemini_provider, "are exhausted"),
                           (openai_provider, "credentials are unavailable")):
        tree = ast.parse(inspect.getsource(module))
        sites = [node for node in ast.walk(tree)
                 if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                 and marker in ast.unparse(node.exc)]
        assert sites, f"{module.__name__}: nothing raises {marker!r} any more - update this test"
        for node in sites:
            raised = ast.unparse(node.exc.func)
            assert raised == "CredentialsRejected", (
                f"{module.__name__} raises {raised} for an exhausted pool, so preflight will "
                f"report it as a transient outage")


def test_a_pool_that_is_only_rate_limited_warns_without_claiming_ignorance():
    """OpenAI can exhaust a pool on rate_limit alone, and that genuinely may clear by itself, so it
    stays a WARN. It must not borrow the transient wording though: "no evidence about the
    credential" is false when every credential was tried and every one answered 429."""
    from void.providers.base import CredentialsRejected
    ctx = _Ctx()
    preflight._report_provider_failure(
        ctx, "provider credential", "openai (gpt-x)",
        CredentialsRejected("All OpenAI credentials are unavailable (last failure: rate_limit).",
                            categories=frozenset({"rate_limit"})), 0.1)
    probe = by_name(ctx.report)["provider credential"]
    assert probe.status == WARN
    assert "rate_limit" in probe.detail
    assert "no evidence about the credential" not in probe.detail


@pytest.mark.parametrize("categories", [
    frozenset(),                            # the provider did not say why: not an excuse
    frozenset({"auth"}),
    frozenset({"quota"}),
    frozenset({"missing_value"}),           # a reference with no stored value; not a POLICY key
    frozenset({"rate_limit", "auth"}),      # one transient cause does not cover for the others
])
def test_anything_short_of_a_wholly_transient_pool_fails(categories):
    from void.providers.base import CredentialsRejected
    ctx = _Ctx()
    preflight._report_provider_failure(
        ctx, "provider credential", "gemini (x)",
        CredentialsRejected("every credential was refused", categories=categories), 0.1)
    assert by_name(ctx.report)["provider credential"].status == FAIL


def test_the_new_type_changes_nothing_about_retry_or_failover():
    """CredentialsRejected subclasses ProviderUnavailable so that nothing outside preflight needs to
    learn about it: classify still answers `unavailable`, the policy is still (1, True), and every
    existing ``except ProviderUnavailable`` still catches it."""
    from void.providers.base import CredentialsRejected, ProviderUnavailable
    from void.providers.failures import classify, policy

    exc = CredentialsRejected("x", categories=frozenset({"auth"}))
    assert isinstance(exc, ProviderUnavailable)
    assert classify(exc) == "unavailable"
    assert policy(classify(exc)) == (1, True)
