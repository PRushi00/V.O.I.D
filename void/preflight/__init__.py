"""``void preflight``: run every live chain end to end and print a verdict.

Four of the seven Jarvis prompt packs carry this harness and each calls it "the one that matters",
for a reason worth repeating in full: *the failures that hurt are the ones where every unit test
passes and a real chain is dead.* V.O.I.D has 4 300 hermetic tests, and hermetic is the point of them
- no network, no credential store, no microphone, no model. That is exactly why they cannot answer
"does this installation work right now", and why this exists beside them rather than instead of them.

How it differs from ``void doctor``, which it does not replace:

* ``doctor`` is **read-only** and asks whether the *running* process is healthy - heartbeat, mic,
  gateway, task store, log size. It executes nothing and repairs nothing.
* ``preflight`` **executes**: it builds a real Assistant, indexes the real vault, makes a real
  minimal provider call, asks the Models API for the configured model id, writes and removes a real
  note, engages and clears the real kill switch. It is the "is it actually wired up" pass.

Three rules hold over every probe here:

**Real, but never outward-facing and never destructive.** Nothing in this harness sends a message,
places a call, launches an application, writes outside the owner's own vault captures folder, or
touches the owner's memory store - the memory round trip runs in a scratch store and says so. Every
artefact a probe creates, it removes, and a cleanup that fails is reported rather than hidden.

**WARN and FAIL mean different things, and the difference is the whole value.** FAIL is "this
installation is broken" - code, configuration, permissions. WARN is "this cannot be checked from
here" - no credential, no notes folder configured, no hardware. A harness that fails because a laptop
has no microphone teaches its owner to ignore it, and then it protects nothing. So the exit code is
non-zero only on a FAIL, and ``--strict`` is there for a CI that wants warnings to count.

**Every detail line is specific enough to act on.** "The model named in configuration is not
reachable" is useless; "gemini-3.9-flash: 404 NOT_FOUND from models.get - the key cannot reach that
id" is a fix. The pack's rule, kept verbatim: fail with the id in the message.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"

#: Name of the note a probe writes into the owner's vault, and removes. Deliberately unmistakable:
#: if a cleanup ever fails, the owner should be able to see at a glance what the file is.
PROBE_NOTE_TITLE = "void preflight probe"

#: Question the vault probe asks after capturing, built so that only the probe note can answer it.
PROBE_PHRASE = "preflight probe sentinel phrase zygal"


@dataclass
class Probe:
    """One check, with the verdict and a detail line the owner can act on."""

    name: str
    status: str
    detail: str
    #: Seconds the probe took, for the slow ones (a live provider call, indexing a large vault).
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail,
                "seconds": round(self.seconds, 3)}


@dataclass
class Report:
    probes: list[Probe] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, seconds: float = 0.0) -> Probe:
        probe = Probe(name, status, detail, seconds)
        self.probes.append(probe)
        return probe

    def count(self, status: str) -> int:
        return sum(1 for p in self.probes if p.status == status)

    @property
    def summary(self) -> str:
        """The one line the packs ask for, and the line to paste when reporting "done"."""
        return (f"{self.count(OK)} pass, {self.count(FAIL)} fail, {self.count(WARN)} warn")

    def as_dict(self) -> dict:
        return {"probes": [p.as_dict() for p in self.probes], "summary": self.summary,
                "pass": self.count(OK), "fail": self.count(FAIL), "warn": self.count(WARN)}


def exit_code(report: Report, strict: bool = False) -> int:
    """Non-zero on any failure - and, with ``strict``, on any warning too.

    Deliberately NOT ``void doctor``'s mapping, which returns 1 for warnings. "Done means preflight
    passed" has to be a usable rule, and a laptop with no microphone would otherwise never pass.
    """
    if report.count(FAIL):
        return 2
    if strict and report.count(WARN):
        return 1
    return 0


def _ms(t0: float) -> float:
    return time.monotonic() - t0


# ======================================================================== the probes

def _check_assembly(ctx) -> None:
    """Does the application assemble at all, with every layer wired?

    The pack's "the server is up and serving" check. V.O.I.D has no server; the equivalent question
    is whether a real Assistant can be constructed from this configuration and whether the tool
    registry came out of it populated, because every other chain below runs through one.
    """
    t0 = time.monotonic()
    try:
        assistant = ctx.assistant()
    except Exception as exc:
        ctx.report.add("assembly", FAIL,
                       f"Assistant(config) raised {type(exc).__name__}: {exc}", _ms(t0))
        return
    names = assistant.tools.names()
    if not names:
        ctx.report.add("assembly", FAIL, "the tool registry is empty: no capability is reachable",
                       _ms(t0))
        return
    ctx.report.add("assembly", OK,
                   f"{len(names)} tools registered, state dir {assistant.config.state_dir()}",
                   _ms(t0))


def _check_source_is_what_runs(ctx) -> None:
    """Is the code being imported the code on disk, here?

    The pack's "the files the browser is actually SERVED match the files on disk", which it calls the
    most common cause of "I fixed it but nothing changed". V.O.I.D's version of that failure is a
    second copy of the ``void`` package earlier on ``sys.path`` - an installed wheel, a stale
    site-packages copy, a sibling checkout - so an edit to this repository changes nothing and every
    test still passes against the wrong tree.
    """
    import void
    loaded = Path(void.__file__).resolve().parent
    expected = Path(__file__).resolve().parent.parent
    if loaded != expected:
        ctx.report.add("source on disk", FAIL,
                       f"'import void' resolves to {loaded}, not {expected}: edits to this "
                       f"repository are not what runs. Remove the other copy from sys.path or "
                       f"uninstall it (pip uninstall void).")
        return
    ctx.report.add("source on disk", OK, f"import void -> {loaded}")


def _check_secrets_are_not_in_the_repository(ctx) -> None:
    """The one that must fail loudly: no secret is readable from a file in the tree.

    The packs' "config.json is not reachable from the browser" and "keys in config files, state in
    .gitignore". V.O.I.D keeps keys in the OS credential store, so the equivalent failure is a key
    that has leaked into a tracked file, a local override that is not ignored, or a state file
    readable by other users on the machine. Each is checked separately, because each has a different
    fix.
    """
    from void.config import local_config_path
    repo = Path(__file__).resolve().parent.parent.parent

    ignore = repo / ".gitignore"
    patterns = ignore.read_text(encoding="utf-8", errors="replace") if ignore.is_file() else ""
    if "local_config" not in patterns:
        ctx.report.add("local config ignored", FAIL,
                       f"{ignore} does not cover local_config: a machine-local override with real "
                       f"paths or settings could be committed")
    else:
        ctx.report.add("local config ignored", OK, "config/local_config.yaml* is gitignored")

    # A key-shaped string in configuration at all. The default config documents that keys live in the
    # credential store; a value that looks like a key means someone put one here anyway.
    suspects = []
    for path in (repo / "config" / "default_config.yaml", local_config_path()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            lowered = line.strip().lower()
            if lowered.startswith("#") or ":" not in lowered:
                continue
            _k, _, value = line.partition(":")
            value = value.strip().strip("\"'")
            if len(value) >= 24 and any(value.startswith(p) for p in
                                        ("sk-", "AIza", "ghp_", "xoxb-", "key-", "Bearer ")):
                suspects.append(f"{path.name}: {_k.strip()}")
    if suspects:
        ctx.report.add("no keys in config", FAIL,
                       "a key-shaped value is in configuration: " + ", ".join(suspects) +
                       " - move it to the credential store (python -m void set-key) and rotate it")
    else:
        ctx.report.add("no keys in config", OK, "no key-shaped value in the config files")

    state_dir = ctx.config.state_dir()
    if os.name == "nt":
        ctx.report.add("state file permissions", WARN,
                       "POSIX mode bits do not describe access on Windows; the state directory sits "
                       "under the user profile and inherits its ACL")
    else:
        loose = []
        for path in sorted(state_dir.glob("*")):
            if not path.is_file():
                continue
            mode = path.stat().st_mode & 0o777
            if mode & 0o077:
                loose.append(f"{path.name} is {oct(mode)}")
        if loose:
            ctx.report.add("state file permissions", FAIL,
                           "readable by other users: " + ", ".join(loose) + " - chmod 600 them")
        else:
            ctx.report.add("state file permissions", OK,
                           f"every file in {state_dir} is owner-only")


def _check_engine_protection(ctx) -> None:
    """V.O.I.D's own state and the credential store refuse the file tools, really."""
    assistant = ctx.assistant()
    targets = [str(ctx.config.state_dir() / "tasks.sqlite"),
               str(Path(__file__).resolve()),
               str(Path(ctx.config.state_dir()) / ".." / ".void" / "STOP")]
    refused, allowed = 0, []
    for target in targets:
        out = assistant.tools.execute("read_file", {"path": target})
        if out.ok:
            allowed.append(target)
        else:
            refused += 1
    if allowed:
        ctx.report.add("engine protection", FAIL,
                       "a file tool read a path that must be protected: " + ", ".join(allowed))
        return
    ctx.report.add("engine protection", OK,
                   f"{refused} protected paths refused by the file layer (state, own source)")


def _check_confinement(ctx) -> None:
    """A path outside the allowed roots is refused, really."""
    assistant = ctx.assistant()
    outside = "/etc/passwd" if os.name != "nt" else "C:/Windows/System32/drivers/etc/hosts"
    out = assistant.tools.execute("read_file", {"path": outside})
    if out.ok:
        ctx.report.add("path confinement", FAIL,
                       f"read_file returned content for {outside}, which is outside "
                       f"security.allowed_roots")
        return
    roots = ", ".join(str(r) for r in ctx.config.allowed_roots()) or "none configured"
    ctx.report.add("path confinement", OK, f"{outside} refused; allowed roots: {roots}")


def _check_task_store(ctx) -> None:
    """A task really round-trips through SQLite, so a paused task can really be resumed."""
    from void.core.task import Status, Task, TaskStore
    t0 = time.monotonic()
    store = TaskStore(ctx.scratch / "preflight-tasks.sqlite")
    task = Task(goal="preflight round trip", id="preflight", status=Status.PAUSED)
    try:
        store.save(task)
        loaded = store.load("preflight")
    except Exception as exc:
        ctx.report.add("task store", FAIL, f"{type(exc).__name__}: {exc}", _ms(t0))
        return
    if loaded is None or loaded.goal != task.goal or loaded.status != Status.PAUSED:
        ctx.report.add("task store", FAIL,
                       "a saved task did not come back identical: resume would lose work", _ms(t0))
        return
    ctx.report.add("task store", OK, "save -> load round trip preserved goal and status", _ms(t0))


def _check_kill_switch(ctx) -> None:
    """The off switch really stops a run, and really clears again.

    Uses a scratch stop file, never the owner's: a preflight that left a real stop engaged would be
    the worst possible outcome of running a diagnostic.
    """
    from void.core.kill_switch import KillSwitch
    t0 = time.monotonic()
    switch = KillSwitch(stop_file=ctx.scratch / "STOP")
    switch.engage("preflight")
    if not switch.engaged:
        ctx.report.add("kill switch", FAIL, "engage() did not engage the switch", _ms(t0))
        return
    if not (ctx.scratch / "STOP").exists():
        ctx.report.add("kill switch", FAIL,
                       "no stop file was written: another process could not see the stop", _ms(t0))
        return
    switch.reset()
    if switch.engaged or (ctx.scratch / "STOP").exists():
        ctx.report.add("kill switch", FAIL, "reset() left the stop in place", _ms(t0))
        return
    ctx.report.add("kill switch", OK,
                   "engaged (in process and by file), then cleared, in a scratch directory", _ms(t0))


def _check_risk_gate(ctx) -> None:
    """A HIGH-risk action with nobody to ask is refused, not guessed."""
    from void.security.risk import RiskGate, RiskLevel
    gate = RiskGate(confirm_at_or_above="high", confirm_fn=None)
    if gate.authorize(RiskLevel.HIGH, "preflight probe"):
        ctx.report.add("risk gate", FAIL,
                       "a HIGH-risk action was authorized with no confirmer present")
        return
    if not gate.authorize(RiskLevel.LOW, "preflight probe"):
        ctx.report.add("risk gate", FAIL, "a LOW-risk action was refused: autonomy is broken")
        return
    ctx.report.add("risk gate", OK, "HIGH refused unattended, LOW allowed")


def _check_judge(ctx) -> None:
    """An answer claiming an action with no receipt really is blocked."""
    from void.orchestration.judge import Receipt, screen
    fabricated = screen("Done. I have sent the invoice to Mike.", [])
    honest = screen("Three notes mention pricing.", [])
    backed = screen("I deleted the old report.", [Receipt("delete_file", True)])
    if not fabricated.blocked:
        ctx.report.add("receipt judge", FAIL,
                       "a claim to have sent something with no receipt was not blocked")
        return
    if honest.blocked or backed.blocked:
        ctx.report.add("receipt judge", FAIL,
                       "an honest answer was blocked: the judge is over-firing and will be "
                       "switched off")
        return
    ctx.report.add("receipt judge", OK,
                   "unbacked claim blocked; an ordinary answer and a backed claim untouched")


def _check_model_refusal(ctx) -> None:
    """A model name this installation does not know is refused, not approximated."""
    from void.providers.catalog import ModelCatalog
    catalog = ModelCatalog.from_config(ctx.config)
    known = catalog.all_ids()
    if not known:
        ctx.report.add("model refusal", WARN,
                       "no model id is configured, so there is nothing to resolve against")
        return
    invented = catalog.resolve("gemini 9.9 ultra")
    if invented.ok:
        ctx.report.add("model refusal", FAIL,
                       f"an invented model name resolved to {invented.model}: a near miss must "
                       f"refuse, never fall back")
        return
    real = catalog.resolve(known[0])
    if not real.ok:
        ctx.report.add("model refusal", FAIL,
                       f"the configured id {known[0]} does not resolve through the catalog")
        return
    ctx.report.add("model refusal", OK,
                   f"{len(known)} known ids; an invented name refused, {known[0]} resolved")


def _check_vault(ctx) -> None:
    """The vault indexes, answers with cited notes, and is honest about absence."""
    assistant = ctx.assistant()
    if assistant.vault is None:
        ctx.report.add("vault index", WARN,
                       "no notes folder is configured (vault.notes_root): nothing to index")
        return
    from void.vault import Verdict, VaultUnavailable
    t0 = time.monotonic()
    try:
        count = assistant.vault.reload()
    except VaultUnavailable as exc:
        ctx.report.add("vault index", FAIL, str(exc), _ms(t0))
        return
    if count == 0:
        ctx.report.add("vault index", WARN,
                       f"{assistant.vault.root} holds no .md files", _ms(t0))
        return
    ctx.report.add("vault index", OK, f"{count} notes indexed from {assistant.vault.root}", _ms(t0))

    titles = [n.title for n in assistant.vault.notes()[:3]]
    recall = assistant.vault.search(" ".join(titles))
    if not recall.ids:
        ctx.report.add("vault recall", FAIL,
                       "a question built from the vault's own note titles matched no note")
    else:
        ctx.report.add("vault recall", OK,
                       f"answered with cited notes {recall.ids} (verdict {recall.verdict})")

    absent = assistant.vault.search("zygal quintaped nonexistent sentinel topic")
    if absent.verdict != Verdict.ABSENT:
        ctx.report.add("vault honesty", FAIL,
                       f"a question the notes cannot answer returned '{absent.verdict}' with "
                       f"{len(absent.notes)} notes: V.O.I.D would improvise an answer")
    else:
        ctx.report.add("vault honesty", OK,
                       "a question the notes do not cover returned absent, with no excerpts")


def _check_vault_capture(ctx) -> None:
    """A captured note is written AND retrievable immediately - then removed.

    The packs' sharpest point about capture: writing a file is not the same as indexing it. So this
    probe captures for real, asks a question only the new note can answer, and deletes it. A failed
    deletion is reported, never swallowed.
    """
    assistant = ctx.assistant()
    if assistant.vault is None:
        ctx.report.add("vault capture", WARN, "no notes folder is configured")
        return
    if ctx.read_only:
        ctx.report.add("vault capture", WARN, "--read-only: no note was written")
        return
    t0 = time.monotonic()
    out = assistant.vault.capture(PROBE_PHRASE, title=PROBE_NOTE_TITLE)
    if not out.ok:
        ctx.report.add("vault capture", FAIL, f"the note was not written: {out.detail}", _ms(t0))
        return
    recall = assistant.vault.search(PROBE_PHRASE)
    retrievable = out.note is not None and out.note.id in recall.ids
    removed, why = _remove(Path(out.path))
    if not retrievable:
        ctx.report.add("vault capture", FAIL,
                       "the note was written but the very next question did not find it: a capture "
                       "that needs a restart to be searchable reads like a lost capture", _ms(t0))
    else:
        ctx.report.add("vault capture", OK,
                       f"written and retrievable in the same breath; probe note "
                       f"{'removed' if removed else 'NOT removed'}", _ms(t0))
    if not removed:
        ctx.report.add("vault cleanup", FAIL,
                       f"the probe note is still on disk at {out.path}: {why}")


def _check_memory(ctx) -> None:
    """Memory really stores and really recalls - in a scratch store, not the owner's."""
    from void.memory.crypto import MemoryUnavailable
    from void.memory.service import MemoryService
    if not ctx.config.get("memory.enabled", True):
        ctx.report.add("memory round trip", WARN, "memory.enabled is false")
        return
    t0 = time.monotonic()
    try:
        service = MemoryService(ctx.scratch / "preflight-memory.db")
        written = service.remember("the preflight sentinel phrase is zygal", channel="cli")
        if not written.stored:
            ctx.report.add("memory round trip", FAIL,
                           f"remember() did not store: {written}", _ms(t0))
            return
        found = service.retrieve("what is the preflight sentinel phrase")
    except MemoryUnavailable as exc:
        ctx.report.add("memory round trip", WARN,
                       f"the memory key is unavailable here: {exc}", _ms(t0))
        return
    except Exception as exc:
        ctx.report.add("memory round trip", FAIL, f"{type(exc).__name__}: {exc}", _ms(t0))
        return
    if not any("zygal" in item.text for item in found):
        ctx.report.add("memory round trip", FAIL,
                       "a fact was stored and could not be recalled by a question about it", _ms(t0))
        return
    ctx.report.add("memory round trip", OK,
                   "stored and recalled in a scratch store; the owner's memory was not written",
                   _ms(t0))


def _check_provider_credential(ctx) -> None:
    """One real, minimal call with the stored credential. The pack's "is the key actually valid"."""
    if ctx.offline:
        ctx.report.add("provider credential", WARN, "--offline: no network call was made")
        return
    from void.providers.base import ProviderUnavailable
    assistant = ctx.assistant()
    try:
        provider = assistant.providers.select()
    except ProviderUnavailable as exc:
        ctx.report.add("provider credential", WARN,
                       f"no provider has a usable credential here: {exc}")
        return
    t0 = time.monotonic()
    try:
        response = provider.generate([{"role": "user", "content": "Reply with the word OK."}])
    except Exception as exc:
        ctx.report.add("provider credential", FAIL,
                       f"{provider.name} ({getattr(provider, 'model', '?')}) raised "
                       f"{type(exc).__name__}: {exc}", _ms(t0))
        return
    text = (response.text or "").strip()
    if not text:
        ctx.report.add("provider credential", FAIL,
                       f"{provider.name} ({getattr(provider, 'model', '?')}) answered with no text",
                       _ms(t0))
        return
    ctx.report.add("provider credential", OK,
                   f"{provider.name} ({getattr(provider, 'model', '?')}) answered "
                   f"{text[:30]!r}", _ms(t0))


def _check_model_is_reachable(ctx) -> None:
    """Ask the Models API for the configured id, and fail with the id in the message.

    Straight from the packs, and worth the extra call: a model that has been retired answers every
    unit test and then 404s on the first real request. The id is in the message because a message
    without it sends the owner reading configuration instead of fixing it.
    """
    if ctx.offline:
        ctx.report.add("model reachable", WARN, "--offline: no network call was made")
        return
    assistant = ctx.assistant()
    gemini = assistant.providers.get("gemini")
    if gemini is None or not gemini.available():
        ctx.report.add("model reachable", WARN,
                       "no Gemini credential here, so the Models API cannot be asked")
        return
    t0 = time.monotonic()
    model = gemini.model
    try:
        credential = gemini._pool().get_next_available()        # noqa: SLF001 - the provider's own pool
        client = gemini._client_for(credential)                 # noqa: SLF001
        info = client.models.get(model=model)
    except Exception as exc:
        ctx.report.add("model reachable", FAIL,
                       f"{model}: {type(exc).__name__} from models.get - the credential cannot "
                       f"reach that id ({exc})", _ms(t0))
        return
    ctx.report.add("model reachable", OK,
                   f"{model} answered models.get ({getattr(info, 'name', model)})", _ms(t0))


def _check_optional_dependencies(ctx) -> None:
    """Which optional capability layers are installed here. Never a failure: each is opt-in."""
    wanted = {"playwright": "browser automation", "uiautomation": "desktop automation",
              "docx": "DOCX artifacts", "pptx": "PPTX artifacts", "openpyxl": "XLSX artifacts",
              "reportlab": "PDF artifacts", "faster_whisper": "local speech to text",
              "sounddevice": "microphone capture", "openwakeword": "wake word",
              "cv2": "camera frames", "mcp": "MCP server"}
    missing = []
    for module, what in sorted(wanted.items()):
        try:
            __import__(module)
        except Exception:
            missing.append(f"{module} ({what})")
    if missing:
        ctx.report.add("optional layers", WARN,
                       f"{len(wanted) - len(missing)}/{len(wanted)} present; absent: "
                       + ", ".join(missing))
    else:
        ctx.report.add("optional layers", OK, f"all {len(wanted)} optional layers importable")


def _remove(path: Path) -> tuple[bool, str]:
    try:
        path.unlink()
        return True, ""
    except OSError as exc:
        return False, str(exc)


#: Every probe, in the order they run. Cheapest and most structural first, so a broken installation
#: fails on "assembly" rather than 40 seconds later on a network call. Add one every time something
#: breaks in the field: a harness that grows one check per real incident is the most valuable file in
#: a project within a month.
PROBES = (
    _check_assembly,
    _check_source_is_what_runs,
    _check_secrets_are_not_in_the_repository,
    _check_engine_protection,
    _check_confinement,
    _check_task_store,
    _check_kill_switch,
    _check_risk_gate,
    _check_judge,
    _check_model_refusal,
    _check_vault,
    _check_vault_capture,
    _check_memory,
    _check_optional_dependencies,
    _check_provider_credential,
    _check_model_is_reachable,
)


@dataclass
class Context:
    """What the probes share: the configuration, a scratch directory, and the flags."""

    config: object
    scratch: Path
    report: Report
    offline: bool = False
    read_only: bool = False
    _assistant: object = None

    def assistant(self):
        """One Assistant, built on first use and reused: assembling it IS one of the probes."""
        if self._assistant is None:
            from void.app import Assistant
            self._assistant = Assistant(config=self.config)
        return self._assistant


def run_preflight(config=None, *, offline: bool = False, read_only: bool = False,
                  probes=PROBES) -> Report:
    """Run every probe and return the report. Never raises: a broken probe becomes a FAIL."""
    from void.config import Config
    config = config or Config.load()
    report = Report()
    scratch = Path(tempfile.mkdtemp(prefix="void-preflight-"))
    ctx = Context(config=config, scratch=scratch, report=report,
                  offline=offline, read_only=read_only)
    try:
        for probe in probes:
            try:
                probe(ctx)
            except Exception as exc:                  # a probe's own bug must not hide the rest
                report.add(getattr(probe, "__name__", "probe").lstrip("_"), FAIL,
                           f"the probe itself raised {type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return report


def format_report(report: Report) -> str:
    """A tick or a cross per check, then the one summary line."""
    mark = {OK: "[ ok ]", WARN: "[WARN]", FAIL: "[FAIL]"}
    width = max((len(p.name) for p in report.probes), default=10)
    lines = [f"V.O.I.D preflight - {platform.system()} {platform.release()}, "
             f"Python {sys.version.split()[0]}", ""]
    for probe in report.probes:
        took = f" ({probe.seconds:.2f}s)" if probe.seconds >= 0.05 else ""
        lines.append(f"{mark[probe.status]} {probe.name:{width}s}  {probe.detail}{took}")
    lines += ["", report.summary]
    return "\n".join(lines)
