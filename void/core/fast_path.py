"""Deterministic fast path for "open <known app>" - no model call.

A spoken/typed command that is *exactly* "open / launch / start <app>" for an application V.O.I.D can already
identify is executed without asking an LLM to plan it. This module only DECIDES: it turns the sentence into a
``FastPlan`` (a short list of ``launch_app`` calls with fixed, engine-chosen arguments) or ``None``. It never
launches anything itself, never touches the filesystem, and never imports the risk gate: every call in a plan is run
by ``Agent.run_direct`` through the same single funnel as a model-proposed call (kill switch -> risk level ->
``RiskGate.authorize`` -> execute -> telemetry). What ``launch_app`` will and will not start is decided by
``launch_app`` (alias map / catalog app_id only, no shell), exactly as before.

What the grammar accepts is deliberately tiny, and anything outside it returns ``None`` so the ordinary agent path
handles the sentence unchanged:

* one launch verb (open / launch / start), an optional polite prefix or suffix, and one or more app names of
  letters, digits, spaces and ``. + ' -`` only. Path separators, drive letters, quotes, ``& | ; < > $ % ( ) ~ * ? [``
  and the like can never appear in an app name, so a path, a URL, a shell string or a glob cannot be expressed;
* SEVERAL names may be listed with commas and "and" ("open Chrome, VS Code and File Explorer"). Each one is
  resolved and executed on its own, so a name that is not installed never cancels the others. A listed item that
  carries an instruction verb ("open chrome and delete my files") is not a name, and the WHOLE sentence goes to the
  model - splitting must never silently drop an instruction by reporting it as an application it could not find;
* no other conjunctions / prepositions ("open notepad WITH ...") - a qualified request is not a simple launch;
* no file/script extension ("open setup.exe", "open run.ps1");
* no shells, interpreters or system-administration consoles (cmd, PowerShell, terminal, WSL, regedit, ...): the
  fast path never starts a command interpreter on the strength of a sentence, whatever the catalog contains.

The app name is then matched against the fixed alias map or, through ``AppCatalog.resolve_name``, against the
automatically discovered catalog: exact name, then the same name spaced differently, then a whole-word prefix, then
the name without its vendor word. Never by substring, never fuzzily, never reordered. Zero matches -> ``None`` (the
agent's find_app path). More than one application -> never picked: the fast path answers with a short question
naming the candidates, which is both faster than a model round trip and the only useful answer when the cloud is
down.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from void.actions.apps import _APP_ALIASES
from void.actions.computer import AppCatalog, ComputerBackendError, NameMatch

_log = logging.getLogger("void.fast_path")

_MAX_PHRASE = 40

# The wake word reaches this path spelled several ways: the runtime strips it, but a typed command keeps it and
# speech-to-text writes the initialism out ("v.o.i.d.", "v o i d"). All of them are the same single noise word.
_PREFIX = re.compile(
    r"^(?:(?:hey|hi|hello|ok|okay)[ ,]+)?(?:(?:void|v\.?\s?o\.?\s?i\.?\s?d\.?)[ ,]+)?(?:please[ ,]+)?"
    r"(?:(?:can|could|would|will) you (?:please )?)?(?:i (?:want|need) you to )?(?:please[ ,]+)?")
_LAUNCH = re.compile(
    r"^(?:open|launch|start)(?: up)? (?:the )?(?P<app>.+?)(?: (?P<noun>app|application|program))?(?: please)?$")
_APP_CHARS = re.compile(r"^[a-z0-9][a-z0-9 .+'\-]*$")
_VERBS = ("open", "launch", "start")
#: Items in a list of names, split on commas and "and". Both spellings of the same list separator.
_SPLIT = re.compile(r"\s*,\s*|\s+and\s+")
#: At most this many targets in one sentence: a bound on the work a single command can ask for.
_MAX_TARGETS = 6
#: The launch grammar already drops "the" after the verb; a list item needs the same ("and the calculator app").
_ARTICLE = re.compile(r"^the ")

# A compound or qualified request is not a plain launch. "and" is handled separately: it is the list separator
# between names, so it is removed here and rejected inside each individual target instead.
_CONNECTORS = frozenset({
    "then", "also", "with", "in", "on", "to", "for", "from", "at", "after", "before", "as", "using", "by",
    "into", "onto", "inside", "of", "about", "that", "which", "so", "or", "plus", "while", "when", "if"})
# Verbs that make an item an INSTRUCTION rather than a name. A list containing one of these is not a list of
# applications, so the whole sentence goes to the model - never split, never partially executed. An application
# genuinely called e.g. "Send Anywhere" is still reachable as a single-target command, which is not split at all.
_INSTRUCTION_WORDS = frozenset({
    "delete", "remove", "erase", "wipe", "format", "send", "email", "mail", "post", "share", "upload", "download",
    "install", "uninstall", "update", "upgrade", "buy", "pay", "order", "transfer", "close", "quit", "exit", "kill",
    "stop", "shutdown", "shut", "restart", "reboot", "sleep", "hibernate", "lock", "logout", "log", "sign",
    "disable", "enable", "turn", "set", "change", "move", "copy", "rename", "create", "make", "write", "read",
    "run", "execute", "call", "text", "message", "search", "google", "play", "pause", "mute", "empty", "clear",
    "reset", "revoke", "grant", "allow", "block", "add", "drop", "push", "pull", "commit", "merge", "deploy"})
_EXTENSIONS = frozenset({
    "exe", "com", "bat", "cmd", "ps1", "psm1", "vbs", "vbe", "js", "jse", "wsf", "wsh", "msi", "msc", "scr", "pif",
    "lnk", "url", "dll", "cpl", "reg", "hta", "jar", "py", "sh", "appref-ms", "application"})
# Never started by the fast path, whatever the catalog contains (the model path is unchanged).
#
# The line is drawn at what the program DOES when it starts with no arguments. V.O.I.D cannot pass a command to
# anything: ``launch_app`` starts a catalog-chosen identity and supplies no arguments of its own, so opening a
# terminal window is exactly as consequential as opening any other application - it shows a prompt, and the owner
# still has to type into it. What stays refused is the set that ACTS on startup: administration consoles that edit
# the machine's configuration, and the script hosts / LOLBins whose whole purpose is to run something else.
_SHELL_WORDS = frozenset({
    "regedit", "registry", "mmc", "gpedit", "secpol", "diskpart", "bcdedit", "schtasks", "taskschd",
    "task scheduler", "mshta", "wscript", "cscript", "rundll32", "msiexec"})
# ("sc" and "net" were removed: they are console tools that only print usage when started with no arguments, they
#  have no Start-Menu entry to be launched from, and as single generic words they blocked real names - "net flix".)

#: Tiers a name produced by SPLITTING a sentence may resolve through unconditionally. The identity tiers and the
#: vendor tier compare whole names, so a fragment cannot claim a different program through them.
_SPLIT_TIERS = frozenset({"exact", "spacing", "publisher", "sound"})
#: The prefix tier needs this many words from a split fragment. A fragment the engine cut out of a sentence was
#: never offered as a complete name, so letting ONE generic word claim the start of a longer one is a guess
#: ("open Command and Conquer" -> "command" -> Command Prompt). Two leading words are evidence the owner really
#: named that program ("open Opera GX and Notepad" -> "Opera GX Browser"), so they stay allowed.
_SPLIT_PREFIX_MIN_WORDS = 2

# Spoken forms -> alias keys of the engine's fixed alias map (validated against it by a test).
_SPOKEN: dict[str, str] = {
    "vs code": "vscode", "vscode": "vscode", "visual studio code": "vscode", "code": "code",
    "notepad": "notepad", "calculator": "calc", "calc": "calc",
    "file explorer": "explorer", "windows explorer": "explorer", "explorer": "explorer",
    "google chrome": "chrome", "chrome": "chrome",
    "microsoft edge": "edge", "edge": "edge",
    "cursor": "cursor",
}
_DISPLAY: dict[str, str] = {
    "vscode": "VS Code", "code": "VS Code", "notepad": "Notepad", "calc": "Calculator", "explorer": "File Explorer",
    "chrome": "Chrome", "edge": "Edge", "cursor": "Cursor",
}


@dataclass(frozen=True)
class DirectCall:
    """One tool call the engine itself chose. ``arguments`` come from the alias map / catalog, never from speech."""
    name: str
    arguments: dict
    reply: str                       # the short user-facing sentence if THIS call succeeds


@dataclass(frozen=True)
class Target:
    """One thing the owner asked to open, with the ALTERNATIVE ways to open it (alias first, then catalog).

    Alternatives are tried in order until one succeeds - that is a single target, not several. Separate targets are
    separate ``Target`` objects, and the executor runs each one whatever the others did.
    """
    label: str                       # what to call it if it fails, already safe to speak
    alternatives: tuple[DirectCall, ...]


@dataclass(frozen=True)
class FastPlan:
    targets: tuple[Target, ...]
    kind: str                        # "alias" | "catalog" | "multi"

    @property
    def calls(self) -> tuple[DirectCall, ...]:
        """Every call in the plan. For ONE target these are alternatives, which is what ``run_direct`` expects."""
        return tuple(c for t in self.targets for c in t.alternatives)


@dataclass
class Decision:
    """Outcome of ``FastPath.plan``; ``plan`` is None when the sentence is not handled here (``why`` says why)."""
    plan: FastPlan | None
    why: str = ""                    # unknown | ambiguous | excluded | discovery ("" when a plan exists)
    matched: bool = False            # True once the sentence looked like a launch command at all
    reply: str = ""                  # a deterministic answer to SPEAK instead of launching (the ambiguity question)
    failures: tuple[str, ...] = ()   # targets that resolved to nothing, named for the spoken failure


def normalise(text: object) -> str:
    """Lower-case, single-spaced, trailing punctuation removed. Never raises."""
    if not isinstance(text, str):
        return ""
    s = re.sub(r"\s+", " ", text.strip().lower())
    return s.rstrip(" .!?,;")


def _acceptable(phrase: str) -> bool:
    """The safety rules an app phrase must satisfy however it was read out of the sentence."""
    if not phrase or len(phrase) > _MAX_PHRASE or not _APP_CHARS.match(phrase):
        return False
    words = phrase.split(" ")
    if any(w in _CONNECTORS for w in words):
        return False
    return not any("." in w and w.rsplit(".", 1)[-1] in _EXTENSIONS for w in words)


def _deglue(s: str) -> str:
    """A sentence whose launch verb is glued to the name, spaced out again. "" when it is not glued.

    Speech-to-text runs the verb into the name on ordinary, clean audio: "Open ChatGPT." is transcribed
    'OpenChat GPT' and "Open Chrome." is transcribed 'OpenCrow.' (measured, both SAPI voices, normal level). The
    result is offered as an EXTRA reading, never as a replacement, so a program whose name really does start with
    the verb ("OpenOffice", "OpenVPN", "OpenRGB") is still matched as itself first.
    """
    if " " not in s:
        # One word is not evidence that a verb was run into a name: "opennotepad" is not a launch command, and the
        # single-word case that WAS measured ('OpenCrow.') does not resolve after de-gluing either.
        return ""
    first, _, tail = s.partition(" ")
    for verb in _VERBS:
        rest = first[len(verb):]
        if first.startswith(verb) and len(rest) >= 2:
            return (verb + " " + rest + " " + tail).strip()
    return ""


def _readings(phrase: str, noun: str | None) -> list[str]:
    """Both readings of one name: as spoken, and with a trailing "app"/"application"/"program" kept.

    "app", "application" and "program" are usually noise ("open the calculator app"), but they are also the second
    half of a real name a transcript spaced out ("open whats app" -> WhatsApp). Rather than guess which it is, both
    readings are returned and the caller keeps the first that resolves to an installed application. Each reading
    passes the same character, connector and extension rules, so the extra one cannot widen what is expressible.
    """
    return [p for p in (phrase, (phrase + " " + noun) if noun else "") if _acceptable(p)]


def _launch_body(text: object) -> tuple[str, str | None] | None:
    """The part of a launch sentence AFTER the verb, plus a trailing noun. None when it is not a launch command."""
    s = normalise(text)
    if not s or len(s) > 120 or "\x00" in s:
        return None
    s = _PREFIX.sub("", s, count=1).strip()
    m = _LAUNCH.match(s)
    if m is None:
        gl = _deglue(s)
        m = _LAUNCH.match(gl) if gl else None
        if m is None:
            return None
    return m.group("app").strip(), m.group("noun")


def launch_phrases(text: object) -> tuple[str, ...]:
    """Every reading of THE app name in a plain single-target launch command, best first. Pure, side-effect free.

    A sentence listing several names is not a single-target launch, so this returns () for it and the caller uses
    ``launch_targets`` instead. When the verb was glued to the name, the glued spelling is tried first and the
    de-glued one second.
    """
    targets = launch_targets(text)
    return targets[0] if len(targets) == 1 else ()


def launch_targets(text: object) -> tuple[tuple[str, ...], ...]:
    """The targets of a launch command, each as its readings (best first). () when it is not a launch command.

    One target is the ordinary case and behaves exactly as before. Several are produced only by a list written with
    commas and/or "and", and only when EVERY item is a plain name: an item carrying an instruction verb means the
    sentence is not a list of applications at all, and () sends it to the model unchanged.
    """
    body = _launch_body(text)
    if body is None:
        return ()
    phrase, noun = body
    items = [_ARTICLE.sub("", i.strip(), count=1) for i in _SPLIT.split(phrase) if i.strip()]
    items = [i for i in items if i]
    if len(items) <= 1:
        out = tuple(_readings(phrase, noun))
        # The glued spelling is a legitimate name in its own right ("OpenOffice"), so keep it ahead of the
        # de-glued reading rather than instead of it.
        glued = _glued_reading(text)
        if glued and glued not in out:
            out = (glued,) + out
        return (out,) if out else ()
    if len(items) > _MAX_TARGETS:
        return ()
    targets = []
    for n, item in enumerate(items):
        # The trailing noun belongs to the LAST item only ("open chrome and the calculator app").
        readings = tuple(_readings(item, noun if n == len(items) - 1 else None))
        if not readings or any(w in _INSTRUCTION_WORDS for w in item.split(" ")):
            return ()                      # not a list of names: the model owns the whole sentence
        targets.append(readings)
    return tuple(targets)


def whole_readings(text: object) -> tuple[str, ...]:
    """The readings of the ENTIRE name part, unsplit. Used to settle "open Command and Conquer" before splitting."""
    body = _launch_body(text)
    if body is None:
        return ()
    return tuple(_readings(body[0], body[1]))


def _glued_reading(text: object) -> str:
    """The whole first token treated as part of the name, for a sentence whose verb was glued to it."""
    s = normalise(text)
    if not s:
        return ""
    s = _PREFIX.sub("", s, count=1).strip()
    if _LAUNCH.match(s) is not None or not _deglue(s):
        return ""
    first, _, tail = s.partition(" ")
    phrase = (first + (" " + tail if tail else "")).strip()
    return phrase if _acceptable(phrase) else ""


def parse_launch(text: object) -> str | None:
    """The app phrase of a plain single-target launch command, or None. Pure and side-effect free."""
    phrases = launch_phrases(text)
    return phrases[0] if phrases else None


def _is_shell(phrase: str) -> bool:
    text = phrase.lower()
    words = set(re.split(r"[ .+'\-]+", text))
    return bool(words & _SHELL_WORDS) or any(" " in w and w in text for w in _SHELL_WORDS)


# Words that make a phrase about the OWNER'S OWN CONTENT rather than an installed product. "Open my V.O.I.D
# project" and "Open the project folder" are jobs for find_directory and open_path, which the model has and the
# fast path does not - so an unmatched phrase containing one of these is never answered as "not installed".
_POSSESSIVE = frozenset({"my", "our", "your", "his", "her", "their", "its", "this", "that", "these", "those"})
_CONTENT_WORDS = frozenset({
    "project", "projects", "folder", "folders", "directory", "directories", "file", "files", "document",
    "documents", "note", "notes", "workspace", "repo", "repository", "report", "photo", "photos", "download",
    "downloads", "screenshot", "screenshots", "backup", "backups", "spreadsheet", "presentation"})


def _is_about_owner_content(phrase: str) -> bool:
    """True when the phrase names something of the owner's rather than a program."""
    words = set(phrase.lower().split())
    return bool(words & _POSSESSIVE) or bool(words & _CONTENT_WORDS)


def _not_found(phrase: str) -> str:
    """What to say about an application that is not installed, or not discoverable under that name.

    Saying it here rather than asking the model is not a shortcut: ``find_app`` reads the same catalog this just
    searched, so the model cannot find what the resolver could not. Sending it there cost 12 model calls and
    minutes of waiting to arrive at this same sentence.
    """
    return (f"I can't find {_clean_display(phrase)} on this machine. "
            f"If it is installed under a different name, tell me that name.")


def _not_found_many(names) -> str:
    """What to say when several named applications are not installed. One sentence, however many there are."""
    shown = list(names)
    if len(shown) == 1:
        return _not_found(shown[0])
    listed = ", ".join(shown[:-1]) + " or " + shown[-1]
    return (f"I can't find {listed} on this machine. "
            f"If they are installed under different names, tell me those names.")


def opening_sentence(names) -> str:
    """One sentence for what is being opened. Never spoken on success - it is the CLI/transcript line."""
    shown = [str(n) for n in names]
    if len(shown) == 1:
        return f"Opening {shown[0]}."
    return "Opening " + ", ".join(shown[:-1]) + " and " + shown[-1] + "."


def cannot_find_sentence(names) -> str:
    """What to SAY when named applications could not be opened. The only thing said about a partial success."""
    return _not_found_many([str(n) for n in names])


def _clarify(candidates) -> str:
    """The question asked when several installed applications match equally well.

    Names come from the catalog, so they are sanitised exactly like a spoken reply - and this path only ever
    SPEAKS them: no plan is produced, so nothing can be launched on the strength of an ambiguous name.
    """
    shown = [_clean_display(c.name) for c in candidates[:4]]
    if len(candidates) > 4:
        shown.append("or something else")
    return "I found more than one match: " + ", ".join(shown) + ". Which one do you mean?"


def _containing_folder(path: object) -> str:
    """The name of the directory a path sits in - a STRING operation, deliberately.

    This module decides and never touches the filesystem (a test enforces that it imports no path, process or
    security module), so the separator is split here rather than by ``pathlib``. Only this one component is ever
    used, so no question can read a full path out loud.
    """
    parts = [p for p in re.split(r"[\\/]+", str(path or "")) if p]
    return parts[-2] if len(parts) >= 2 else ""


def _clarify_folders(candidates) -> str:
    """The question asked when several of the owner's folders share a name.

    Named by the folder each one sits in: "Downloads in OneDrive" against "Downloads in nanda". The name alone
    ("Downloads, Downloads") is not a question anyone can answer. Only the two last path components are ever spoken,
    so the question cannot read out a full path.
    """
    shown = []
    for c in candidates[:4]:
        parent = _containing_folder(getattr(c, "path", ""))
        label = _clean_display(getattr(c, "name", ""))
        shown.append(f"{label} in {_clean_display(parent)}" if parent else label)
    if len(candidates) > 4:
        shown.append("or something else")
    return "I found more than one folder called that: " + ", ".join(shown) + ". Which one do you mean?"


def _clean_display(name: str) -> str:
    """A catalog name made safe to speak: printable, single-spaced, bounded."""
    s = re.sub(r"[^\w .+'&()\-]", "", str(name or "")).strip()
    return re.sub(r"\s+", " ", s)[:60] or "the app"


@dataclass
class FastPath:
    """Turns a launch sentence into a plan. Holds only read-only references."""
    catalog: AppCatalog | None = None
    aliases: dict = field(default_factory=lambda: _APP_ALIASES)
    #: Optional folder index (``void.actions.folders.FolderCatalog``). Tried only when NO installed application
    #: matches the name and the catalog did not report ambiguity, so a program always wins its own name. Without
    #: one, folder names behave exactly as before: the model resolves them with find_directory / open_path.
    folders: object = None
    #: Answer "open <app>" locally when no installed application matches, instead of handing the sentence to the
    #: model. Off sends unresolvable names back to the model exactly as before.
    answer_unknown: bool = True

    def decide(self, goal: object) -> Decision:
        targets = launch_targets(goal)
        if not targets:
            return Decision(None)
        if len(targets) > 1:
            # RESOLVE FIRST, SPLIT SECOND: an installed "Command and Conquer" is one application, not two.
            whole = whole_readings(goal)
            if any(_is_shell(p) for p in whole):
                return Decision(None, "excluded", matched=True)
            single = self._decide_readings(whole)
            if single.plan is not None:
                return single
            return self._decide_many(targets)
        return self._decide_readings(targets[0])

    def _decide_readings(self, phrases, strict: bool = False) -> Decision:
        if not phrases:
            return Decision(None)
        if any(_is_shell(p) for p in phrases):
            return Decision(None, "excluded", matched=True)
        outcome = Decision(None, "unknown", matched=True)
        for phrase in phrases:
            decision = self._decide_phrase(phrase, strict=strict)
            if decision.plan is not None or decision.why in ("discovery", "excluded"):
                return decision
            if decision.why == "ambiguous" and outcome.why != "ambiguous":
                outcome = decision          # kept only if no other reading of the name resolves outright
        if (outcome.why == "unknown" and self.answer_unknown and self.catalog is not None
                and not any(_is_about_owner_content(p) for p in phrases)):      # noqa: SIM102 - read as written
            # Nothing installed matches, under any reading of the name, and the name is a product rather than
            # something of the owner's. The model cannot do better here - find_app searches this same catalog -
            # so say so now rather than after a dozen round trips.
            outcome = Decision(None, "unknown", matched=True, reply=_not_found(phrases[0]),
                               failures=(_clean_display(phrases[0]),))
        return outcome

    def _folder(self, phrase: str) -> Decision | None:
        """A directory of the owner's, resolved by IDENTITY only. None when there is no folder index or no match.

        The argument handed on is an absolute path from the folder index - the filesystem's own spelling, never the
        transcript's - so ``open_path`` receives an engine-chosen value exactly as ``launch_app`` does, and confines
        it again itself. Two matching directories ask rather than guess: opening the wrong one of the owner's folders
        is not a small mistake.
        """
        if self.folders is None:
            return None
        try:
            m = self.folders.resolve_name(phrase)
        except Exception:                            # noqa: BLE001 - a folder index must never break a command
            _log.exception("FAST_PATH_FOLDERS_FAILED")
            return None
        if m.entry is not None:
            name = _clean_display(m.entry.name)
            return Decision(FastPlan(targets=(Target(label=name,
                                                     alternatives=(DirectCall("open_path",
                                                                              {"target": m.entry.path},
                                                                              f"Opening {name}."),)),),
                                     kind="catalog"), matched=True)
        if m.reason == "ambiguous":
            return Decision(None, "ambiguous", matched=True, reply=_clarify_folders(m.candidates))
        return None

    def _decide_many(self, targets) -> Decision:
        """Several names in one sentence. Each resolves on its own; a miss never cancels the rest.

        A target that is AMBIGUOUS stops the whole command with the same question a single ambiguous name asks:
        launching the others and then asking would leave the owner unable to tell what did and did not happen. A
        target that resolves to nothing is reported, and the ones that resolved are still launched.
        """
        plan_targets: list[Target] = []
        failures: list[str] = []
        for readings in targets:
            if any(_is_shell(p) for p in readings):
                return Decision(None, "excluded", matched=True)
            decision = self._decide_readings(readings, strict=True)
            if decision.why == "discovery":
                return decision                      # the catalog is unusable: the whole sentence goes to the model
            if decision.why == "ambiguous":
                return Decision(None, "ambiguous", matched=True, reply=decision.reply)
            if decision.plan is not None:
                plan_targets.extend(decision.plan.targets)
                continue
            if any(_is_about_owner_content(p) for p in readings):
                # Something of the owner's rather than a program: find_directory / open_path are the model's, and
                # answering "not installed" here would be wrong. The whole sentence goes to the model, unchanged.
                return Decision(None, "unknown", matched=True)
            failures.append(_clean_display(readings[0]))
        if not plan_targets:
            if not failures:
                return Decision(None, "unknown", matched=True)
            return Decision(None, "unknown", matched=True, reply=_not_found_many(failures),
                            failures=tuple(failures))
        return Decision(FastPlan(targets=tuple(plan_targets), kind="multi"), matched=True,
                        failures=tuple(failures))

    def _decide_phrase(self, phrase: str, strict: bool = False) -> Decision:   # noqa: C901 - tier per branch
        calls: list[DirectCall] = []
        kind = "catalog"
        key = _SPOKEN.get(phrase, phrase if phrase in self.aliases else None)
        if key is not None and key in self.aliases:
            kind = "alias"
            shown = _DISPLAY.get(key, key.title())
            calls.append(DirectCall("launch_app", {"name": key}, f"Opening {shown}."))

        match = NameMatch()
        if self.catalog is not None:
            # Spoken names differ from installed ones in ways that are settled deterministically - punctuation
            # ("Opera-GX"), spacing ("Whats App"), letters spelled out by speech-to-text ("Opera G X"), an omitted
            # installed suffix ("Opera GX" vs "Opera GX Browser") or a vendor word nobody says ("Teams"). Sending
            # any of those to the model costs several seconds and several round trips for something the catalog
            # can answer in microseconds.
            try:
                match = self.catalog.resolve_name(phrase)
            except ComputerBackendError:
                if not calls:
                    return Decision(None, "discovery", matched=True)
            except Exception:                        # noqa: BLE001 - discovery must never break a command
                _log.exception("FAST_PATH_CATALOG_FAILED")
                if not calls:
                    return Decision(None, "discovery", matched=True)
        entry = match.entry
        if (entry is not None and strict and match.tier not in _SPLIT_TIERS
                and len(phrase.split(" ")) < _SPLIT_PREFIX_MIN_WORDS):
            entry = None                 # one word of a split sentence may not claim the start of a longer name
        if entry is not None:
            if _is_shell(entry.name):
                if not calls:
                    return Decision(None, "excluded", matched=True)
            else:
                calls.append(DirectCall("launch_app", {"name": entry.app_id},
                                        f"Opening {_clean_display(entry.name)}."))
        if not calls and match.reason != "ambiguous":
            folder = self._folder(phrase)
            if folder is not None:
                return folder
        if not calls:
            if match.reason == "ambiguous":
                # Several real applications match. Never guess between them, and never launch: ask.
                return Decision(None, "ambiguous", matched=True, reply=_clarify(match.candidates))
            return Decision(None, "unknown", matched=True)
        label = _clean_display(entry.name) if entry is not None else _DISPLAY.get(key, str(key).title())
        return Decision(FastPlan(targets=(Target(label=label, alternatives=tuple(calls)),), kind=kind),
                        matched=True)
