"""Taking a website apart: watch it run, read its code, and write down what is actually there.

The REA pack's second half, built on V.O.I.D's own layers. One spoken sentence ("how does igloo.inc
do that?") becomes a browser V.O.I.D owns, three REA observations, two reads of its own, a stack
named from licence banners, and a note in the owner's vault that cites its evidence.

Four laws hold across this package, and each one is here because the alternative is a teardown nobody
should trust:

**REA observes; it never drives.** It attaches to a browser V.O.I.D launched and looks at it. The
DevTools client in ``void/teardown/browser.py`` has no click, no type and no navigate, so "read-only"
is enforced by absence rather than by policy, and the browser is a brand-new profile with none of the
owner's logins in it.

**Websites only, and only ones the owner may look at.** Every address goes through
``void.browser.safe_url`` - the same single gate the rest of V.O.I.D navigates through - and the whole
capability is deny-by-default behind ``teardown.enabled``.

**V.O.I.D does not install software behind the owner's back.** The kit is *detected*, never installed:
:meth:`Kit.diag` reports what is missing and names the exact commands, and installing is a separate
thing the owner runs on purpose (``void teardown install``), which verifies the package's repository
URL before it fetches anything and passes ``--ignore-scripts`` so no lifecycle script runs. A feature
that silently npm-installs a third-party analyser on someone's machine is not a feature.

**Every line of evidence says who saw it.** REA attaches after a page has loaded, so it reports the
requests it witnessed and not the ones it missed; the browser's own resource-timing log has all of
them. Both are read, and :class:`Line` records which. The pack reports getting this wrong - crediting
REA with something it had read itself - and a teardown's whole value is that its claims are checkable.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from void.teardown import fingerprints
from void.teardown.browser import (CleanBrowser, TeardownBrowserError, find_chrome,
                                   stray_profiles)

_log = logging.getLogger(__name__)

#: The REA version this integration was written and drilled against. PINNED: "latest" at runtime is
#: how a teardown quietly breaks on camera. Upgrade on purpose - bump this, run the drill, then use it.
PINNED_REA = "6.7.0"

#: The repository the package must resolve to. Checked before an install fetches anything, because
#: copycat packages and repositories exist and the name alone is not evidence.
REA_REPOSITORY = "github.com/morluto/rea"

#: Node versions REA declares support for. Checked and reported, never worked around.
MIN_NODE = (22, 19)

#: Where the kit lives: outside the project, so a reinstall of V.O.I.D does not take it with it and a
#: third-party package never sits inside V.O.I.D's own tree.
KIT_ROOT = Path.home() / ".void" / "tools" / "rea"

#: How long each REA command is given. The observation pass is the long one by design.
REA_TIMEOUT_S = 240.0

#: How long REA watches the page while V.O.I.D scrolls it, and how long to wait before scrolling so
#: that the observation has started.
OBSERVE_MS = 7000
SCROLL_AFTER_S = 1.0

#: Ceiling on the digest handed to a model. Big enough for the evidence, small enough to leave room
#: for thinking and an answer in one call.
DIGEST_CHARS = 15_000

#: Largest files reported individually, and how much script source is fingerprinted.
TOP_FILES = 12
MAX_SOURCE_CHARS = 2_000_000

#: Who captured a line of evidence. Three labels and not two, because the third case is the one the
#: pack reports getting wrong: a stack signal is REA's capture of the code AND V.O.I.D's pattern
#: match over it, and crediting either alone is a false claim about where the fact came from.
REA = "REA"
OWN = "V.O.I.D's own browser read"
BOTH = "code captured by REA, pattern matched by V.O.I.D"


class TeardownUnavailable(RuntimeError):
    """The kit is not ready. The message names what to install."""


@dataclass(frozen=True)
class Line:
    """One fact, and who saw it."""

    text: str
    source: str = REA

    def rendered(self) -> str:
        return f"{self.text} [{self.source}]"


@dataclass
class Evidence:
    """Everything one teardown established, with attribution and with its limits."""

    url: str = ""
    host: str = ""
    question: str = ""
    seconds: float = 0.0
    page: dict = field(default_factory=dict)
    #: Script files, each {url, bytes, source_included}
    scripts: list[dict] = field(default_factory=list)
    #: Every file the page loaded, from the browser's own log: {url, kind, bytes, origin}
    assets: list[dict] = field(default_factory=list)
    signals: list[fingerprints.Signal] = field(default_factory=list)
    other_banners: list[str] = field(default_factory=list)
    #: REA evidence ids, so a claim can be traced back to the observation that produced it.
    evidence_ids: list[str] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    screenshot: bytes = b""
    bundle: dict = field(default_factory=dict)

    def totals(self) -> dict:
        by_kind: dict[str, dict] = {}
        total = 0
        for asset in self.assets:
            kind = asset.get("kind") or "other"
            row = by_kind.setdefault(kind, {"count": 0, "bytes": 0})
            row["count"] += 1
            row["bytes"] += int(asset.get("bytes") or 0)
            total += int(asset.get("bytes") or 0)
        origins = sorted({a.get("origin") for a in self.assets if a.get("origin")})
        return {"files": len(self.assets), "bytes": total, "by_kind": by_kind,
                "origins": origins, "third_party": [o for o in origins if o != self.host]}

    def largest(self, limit: int = TOP_FILES) -> list[dict]:
        return sorted(self.assets, key=lambda a: -int(a.get("bytes") or 0))[:limit]


class Kit:
    """Where REA, Node and a browser are, and whether a teardown can run at all.

    Detection only. Nothing in this class installs, downloads or updates anything;
    :meth:`install_commands` returns what the owner would run, and ``void teardown install`` is the
    deliberate act of running it.
    """

    def __init__(self, root: Path | None = None, *, node: str | None = None,
                 chrome: str | None = None):
        self.root = Path(root) if root else KIT_ROOT
        self._node = node
        self._chrome = chrome

    # --- discovery -----------------------------------------------------

    @property
    def script(self) -> Path:
        return self.root / "node_modules" / "rea-agents" / "scripts" / "rea.mjs"

    def node(self) -> str | None:
        """Node on the PATH or in a common install location.

        Looked up rather than assumed because the voice runtime starts without the owner's shell
        environment, which is where "it works in my terminal" teardowns come from.
        """
        if self._node:
            return self._node if Path(self._node).exists() or shutil.which(self._node) else None
        explicit = os.environ.get("VOID_TEARDOWN_NODE", "").strip()
        if explicit and (Path(explicit).exists() or shutil.which(explicit)):
            return explicit
        found = shutil.which("node")
        if found:
            return found
        for candidate in ("/opt/node22/bin/node", "/usr/local/bin/node", "/usr/bin/node",
                          r"C:\Program Files\nodejs\node.exe"):
            if Path(candidate).exists():
                return candidate
        return None

    def node_version(self) -> tuple[int, ...] | None:
        node = self.node()
        if not node:
            return None
        try:
            out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", out.stdout or "")
        return tuple(int(part) for part in match.groups()) if match else None

    def installed_version(self) -> str:
        manifest = self.root / "node_modules" / "rea-agents" / "package.json"
        try:
            return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", ""))
        except (OSError, ValueError):
            return ""

    def chrome(self) -> str | None:
        return self._chrome or find_chrome()

    def install_commands(self) -> list[str]:
        """Exactly what the owner would run. Shown by diag, run only on request."""
        return [f"mkdir -p {self.root}",
                f"cd {self.root} && npm view rea-agents repository.url    "
                f"# must print {REA_REPOSITORY}",
                f"cd {self.root} && npm install rea-agents@{PINNED_REA} --save-exact "
                f"--ignore-scripts"]

    def problems(self) -> list[str]:
        """Everything standing between this machine and a teardown, in plain English."""
        out: list[str] = []
        node_version = self.node_version()
        if self.node() is None:
            out.append(f"Node.js is not installed, or not on the PATH. REA needs "
                       f"{MIN_NODE[0]}.{MIN_NODE[1]} or newer.")
        elif node_version is not None and node_version[:2] < MIN_NODE:
            out.append(f"Node.js {'.'.join(map(str, node_version))} is too old; REA needs "
                       f"{MIN_NODE[0]}.{MIN_NODE[1]} or newer.")
        if not self.script.exists():
            out.append(f"REA is not installed at {self.root}. Install it with: "
                       + " ; ".join(self.install_commands()))
        else:
            installed = self.installed_version()
            if installed and installed != PINNED_REA:
                out.append(f"REA {installed} is installed but this integration was drilled against "
                           f"{PINNED_REA}. Bump the pin deliberately and re-run the drill.")
        if self.chrome() is None:
            out.append("No Chromium-family browser was found. Install Chrome, Edge or Chromium.")
        return out

    @property
    def ready(self) -> bool:
        return not self.problems()

    def diag(self) -> dict:
        """What a caller may ask about the kit. Statuses and versions, no secrets, no paths a model
        needs to reason about beyond the kit's own."""
        node_version = self.node_version()
        return {"ready": self.ready,
                "rea_installed": self.installed_version(),
                "rea_pinned": PINNED_REA,
                "rea_script_present": self.script.exists(),
                "node": ".".join(map(str, node_version)) if node_version else "",
                "node_minimum": ".".join(map(str, MIN_NODE)),
                "chrome_found": bool(self.chrome()),
                "problems": self.problems(),
                "install_commands": self.install_commands(),
                "stray_profiles": len(stray_profiles())}

    # --- running REA ---------------------------------------------------

    def run(self, *args: str, timeout_s: float = REA_TIMEOUT_S) -> dict:
        """One REA command, as JSON. Raises :class:`TeardownUnavailable` when the kit is not ready.

        ``--format json`` and not the ``--json`` the pack's text shows: the installed tool's own help
        is the authority on its flags, and checking beat remembering.
        """
        node = self.node()
        if node is None or not self.script.exists():
            raise TeardownUnavailable("; ".join(self.problems()) or "the REA kit is not installed")
        argv = [node, str(self.script), *args, "--format", "json"]
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s,
                                 cwd=str(self.root))
        except subprocess.TimeoutExpired as exc:
            raise TeardownBrowserError(
                f"rea {args[0] if args else '?'} did not finish within "
                f"{timeout_s:.0f}s") from exc
        except OSError as exc:
            raise TeardownUnavailable(f"REA could not be run: {exc}") from exc
        text = (out.stdout or "").strip()
        if not text:
            raise TeardownBrowserError(
                f"rea {args[0] if args else '?'} produced no output "
                f"({(out.stderr or '').strip()[:200]})")
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise TeardownBrowserError(
                f"rea {args[0] if args else '?'} did not return JSON: {text[:200]}") from exc
        if out.returncode != 0:
            raise TeardownBrowserError(
                f"rea {args[0] if args else '?'}: "
                f"{payload.get('message') or payload.get('code') or 'failed'}")
        return payload


def _normalised(payload: dict) -> dict:
    return (payload or {}).get("normalized_result") or {}


def _script_sources(inspect: dict) -> tuple[list[dict], str]:
    """Script files and their concatenated source text, from a REA page inspection.

    Defensive about shape on purpose: this reads another tool's output, and a tool that changes an
    optional field should degrade the fingerprints rather than raise.
    """
    items = (_normalised(inspect).get("scripts") or {}).get("items") or []
    files, blobs, total = [], [], 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")[:500]
        size = int(item.get("length") or 0)
        source = item.get("source") or {}
        artifact = source.get("artifact") or {} if isinstance(source, dict) else {}
        text = ""
        if isinstance(artifact, dict):
            if isinstance(artifact.get("text"), str):
                text = artifact["text"]
            elif isinstance(artifact.get("data_base64"), str):
                try:
                    text = base64.b64decode(artifact["data_base64"]).decode("utf-8", "replace")
                except (ValueError, TypeError):
                    text = ""
        if not text and isinstance(source, dict) and isinstance(source.get("text"), str):
            text = source["text"]
        files.append({"url": url, "bytes": size, "source_included": bool(text)})
        if text and total < MAX_SOURCE_CHARS:
            blobs.append(text[: MAX_SOURCE_CHARS - total])
            total += len(blobs[-1])
    return files, "\n".join(blobs)


def _trim(text: object, limit: int = 200) -> str:
    """Shorten a borrowed sentence at a word boundary. A limitation cut mid-word reads like a bug."""
    words = " ".join(str(text or "").split())
    if len(words) <= limit:
        return words
    cut = words[:limit].rsplit(" ", 1)[0]
    return (cut or words[:limit]).rstrip(",;:") + "..."


def _evidence_ids(*payloads: dict) -> list[str]:
    out = []
    for payload in payloads:
        value = (payload or {}).get("evidence_id")
        if isinstance(value, str) and value:
            out.append(value)
    return out


def _limitations(*payloads: dict) -> list[str]:
    out: list[str] = []
    for payload in payloads:
        for place in ((payload or {}).get("limitations"), _normalised(payload).get("limitations")):
            if isinstance(place, list):
                out += [_trim(item) for item in place if item]
    seen, unique = set(), []
    for item in out:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique[:12]


class Teardown:
    """Runs one teardown at a time, start to finish.

    One at a time is deliberate and is the pack's rule: a second request while one is running gets
    told so, rather than opening a second browser on the owner's machine.
    """

    def __init__(self, kit: Kit | None = None, *, headless: bool = True,
                 profile_root: Path | None = None, safe_url=None):
        self.kit = kit or Kit()
        self.headless = bool(headless)
        self.profile_root = profile_root
        self._lock = threading.Lock()
        self._running = False
        if safe_url is None:
            from void.browser import safe_url as _safe
            safe_url = _safe
        self._safe_url = safe_url

    @property
    def running(self) -> bool:
        return self._running

    def capture(self, address: str, question: str = "") -> Evidence:
        """Open the site in a clean browser, gather every kind of evidence, and close it.

        The order matters and is the pack's: start REA's observation, then scroll **while it is
        watching**, so that scroll-driven effects and lazily-loaded files happen inside the window
        REA can see. Scroll back to the top before the snapshot, so the picture is of the site rather
        than of wherever the scroll stopped.
        """
        url = self._safe_url(address)                 # one gate for every address V.O.I.D opens
        host = (urlsplit(url).hostname or "").lower()
        if not self.kit.ready:
            raise TeardownUnavailable("; ".join(self.kit.problems()))
        with self._lock:
            if self._running:
                raise TeardownUnavailable("A teardown is already running.")
            self._running = True
        started = time.monotonic()
        evidence = Evidence(url=url, host=host, question=" ".join(str(question or "").split()))
        browser = CleanBrowser(self.kit.chrome(), headless=self.headless,
                               profile_root=self.profile_root)
        try:
            browser.open(url)
            page = browser.page(url)
            load_s = page.wait_for_load()
            # The page's own description of itself, read BEFORE anything scrolls it. A page is
            # entitled to change its own title as the owner moves through it - the synthetic drill
            # page does exactly that - so "what is this page" has to be asked of the page as loaded.
            facts = page.facts()
            evidence.page = {"title": facts.title, "elements": facts.elements,
                             "height_screens": facts.height_screens, "load_s": round(load_s, 2)}
            endpoint = f"http://127.0.0.1:{browser.port}"

            targets = self.kit.run("list-browser-targets", endpoint)
            target_id = self._target_id(targets, host)
            evidence.lines.append(Line(f"REA attached to the page target at {endpoint}", REA))

            # 1 + 2. Watch while scrolling: REA observes on a thread, V.O.I.D scrolls into it.
            observation: dict = {}

            def observe():
                try:
                    observation["payload"] = self.kit.run(
                        "inspect-web-page", endpoint, target_id,
                        "--observation-ms", str(OBSERVE_MS), "--include-script-sources")
                except Exception as exc:              # recorded, not raised on a worker thread
                    observation["error"] = exc

            worker = threading.Thread(target=observe, name="void-teardown-rea", daemon=True)
            worker.start()
            time.sleep(SCROLL_AFTER_S)
            page.scroll()
            worker.join(timeout=REA_TIMEOUT_S + 30)
            if "error" in observation:
                raise observation["error"]
            inspect = observation.get("payload") or {}
            evidence.lines.append(
                Line(f"watched the page for {OBSERVE_MS / 1000:.0f}s while it was scrolled", REA))

            # 3. Read the code.
            bundle = self.kit.run("analyze-web-bundle", endpoint, target_id)
            evidence.bundle = _normalised(bundle)
            evidence.lines.append(Line("read the page's script bundle statically", REA))

            # 4. Snapshot, from the top.
            page.scroll(20, up=True)
            shot = self.kit.run("capture-web-screenshot", endpoint, target_id)
            evidence.screenshot = self._png(shot)
            evidence.lines.append(Line("captured one viewport snapshot", REA))

            # 5. V.O.I.D's own read of the complete asset log - at the END, because a scroll is
            # what makes a lazily-loaded file load at all.
            evidence.assets = page.resources()
            evidence.lines.append(
                Line(f"read {len(evidence.assets)} loaded files from the browser's own resource "
                     f"timing log, which includes requests made before REA attached", OWN))
            evidence.lines.append(
                Line(f"the page is {facts.height_screens:g} screens tall with {facts.elements} "
                     f"elements", OWN))

            # Fingerprints, over the source REA captured.
            evidence.scripts, source = _script_sources(inspect)
            if not source:
                evidence.limitations.append(
                    "No script source text was available, so the stack below is from file names and "
                    "page strings only.")
            evidence.signals = fingerprints.fingerprint(source)
            evidence.other_banners = fingerprints.other_banners(source, evidence.signals)
            for signal in evidence.signals:
                evidence.lines.append(Line(f"{signal.category}: {signal.described()}", BOTH))

            evidence.evidence_ids = _evidence_ids(targets, inspect, bundle, shot)
            evidence.limitations += _limitations(inspect, bundle, shot)
            evidence.limitations.append(
                "REA attaches to a page that is already running, so it reports what it witnessed "
                "from that moment; the asset log above is the browser's own and covers the rest.")
            evidence.limitations.append(
                "Library names and versions come from licence banners and code strings. Nothing "
                "here can see server-side code.")
            page.close()
        finally:
            browser.close()
            self._running = False
        if browser.cleanup_error:
            evidence.limitations.append(f"Cleanup was incomplete: {browser.cleanup_error}")
        evidence.seconds = round(time.monotonic() - started, 1)
        _log.info("TEARDOWN_CAPTURED seconds=%.1f files=%d signals=%d",
                  evidence.seconds, len(evidence.assets), len(evidence.signals))
        return evidence

    @staticmethod
    def _target_id(payload: dict, host: str) -> str:
        targets = _normalised(payload).get("targets") or []
        for target in targets if isinstance(targets, list) else []:
            if not isinstance(target, dict) or target.get("type") != "page":
                continue
            found = (urlsplit(str(target.get("url") or "")).hostname or "").lower()
            if not host or found == host or host in found or found in host:
                identifier = str(target.get("target_id") or "")
                if identifier:
                    return identifier
        raise TeardownBrowserError("REA listed no page target for that site.")

    @staticmethod
    def _png(payload: dict) -> bytes:
        artifact = _normalised(payload).get("artifact") or {}
        data = artifact.get("data_base64") if isinstance(artifact, dict) else None
        if not isinstance(data, str) or not data:
            return b""
        try:
            return base64.b64decode(data)
        except (ValueError, TypeError):
            return b""


def digest(evidence: Evidence, limit: int = DIGEST_CHARS) -> str:
    """Compress the evidence into plain text for one model call.

    Every line carries its source, because the model is told to keep the attribution straight and it
    cannot do that unless the digest it reads says who saw what.
    """
    totals = evidence.totals()
    parts = [
        f"SITE: {evidence.url}",
        f"QUESTION: {evidence.question or 'how is this site built?'}",
        f"CAPTURED IN: {evidence.seconds}s",
        "",
        "PAGE (read by V.O.I.D's own browser):",
        f"  title: {evidence.page.get('title', '')}",
        f"  {evidence.page.get('elements', 0)} elements, "
        f"{evidence.page.get('height_screens', 0)} screens tall, "
        f"loaded in {evidence.page.get('load_s', 0)}s",
        "",
        f"SCRIPTS ({len(evidence.scripts)}):",
    ]
    for script in evidence.scripts[:TOP_FILES]:
        name = script["url"].rsplit("/", 1)[-1] or script["url"]
        parts.append(f"  {name} - {script['bytes']} bytes"
                     f"{' (source read)' if script['source_included'] else ''}")
    parts += ["", "STACK (each item with how it was found):"]
    for signal in evidence.signals:
        parts.append(f"  {signal.category}: {signal.described()}  evidence: {signal.evidence}")
    if evidence.other_banners:
        parts += ["", "OTHER LICENCE BANNERS FOUND (not identified):"]
        parts += [f"  {banner}" for banner in evidence.other_banners[:6]]
    parts += ["", f"WHAT IT LOADS: {totals['files']} files, {totals['bytes']} bytes total"]
    for kind, row in sorted(totals["by_kind"].items(), key=lambda kv: -kv[1]["bytes"]):
        parts.append(f"  {kind}: {row['count']} files, {row['bytes']} bytes")
    parts += ["", "LARGEST FILES:"]
    for asset in evidence.largest():
        parts.append(f"  {asset['url'].rsplit('/', 1)[-1][:70]} - {asset['bytes']} bytes "
                     f"({asset['kind']})")
    if totals["third_party"]:
        parts += ["", "THIRD-PARTY ORIGINS: " + ", ".join(totals["third_party"][:12])]
    parts += ["", "HOW EACH THING WAS SEEN:"]
    parts += [f"  {line.rendered()}" for line in evidence.lines]
    parts += ["", "WHAT THE EVIDENCE CANNOT SHOW:"]
    parts += [f"  {item}" for item in evidence.limitations]
    text = "\n".join(parts)
    return text[:limit]


#: What the model is told. The rules are the pack's, and the last one is the one that makes a teardown
#: worth showing anyone: a section for what the evidence could not establish.
NOTE_SYSTEM_PROMPT = """You are writing a technical teardown of a website from captured evidence.

Every claim must trace to the evidence given. Say "likely" for an inference, and never invent a
library, a file or a technique that is not in the evidence. Keep the attribution straight: the
evidence says who saw each thing, REA or V.O.I.D's own browser read, and so must you.

Output EXACTLY this shape and nothing else:

LABEL: Teardown - <host>
SPOKEN: two or three sentences to be read aloud: the answer first, then the most surprising detail.
No web addresses and no file names.
SUMMARY: one short paragraph.
---NOTE---
# <host> - teardown

**Verdict:** one line.

## The answer
## The stack
Each item with the evidence it came from.
## How the effect works
Numbered steps.
## What it loads
## Rebuild it
Five to seven steps someone could follow.
## What the evidence can't show
"""


@dataclass(frozen=True)
class Written:
    """The outcome of turning evidence into a note."""

    spoken: str
    summary: str
    note_path: str
    note_id: int | None
    label: str = ""


#: Where a teardown note is written, under the vault's captures folder.
NOTE_DIR = "teardowns"


def parse_note(raw: object) -> tuple[str, str, str, str]:
    """Split the model's reply into (label, spoken, summary, markdown).

    Lenient about missing sections and strict about never inventing one: an absent SPOKEN line yields
    an empty string, which the caller reports rather than papering over.
    """
    text = str(raw or "")
    body = ""
    head = text
    if "---NOTE---" in text:
        head, _, body = text.partition("---NOTE---")

    def section(name: str) -> str:
        match = re.search(rf"^{name}:\s*(.+?)(?=^\s*(?:LABEL|SPOKEN|SUMMARY):|\Z)",
                          head, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        return " ".join(match.group(1).split()) if match else ""

    return section("LABEL"), section("SPOKEN"), section("SUMMARY"), body.strip()


def write_note(evidence: Evidence, reply: object, *, vault) -> Written:
    """Write the teardown into the owner's vault and index it immediately.

    Through ``Vault.capture``, so the note lands under the owner's allowed roots, through the confined
    file layer, and is searchable in the very next question - the same path "write this down" takes.
    """
    label, spoken, summary, markdown = parse_note(reply)
    host = evidence.host or "site"
    stamp = time.strftime("%Y-%m-%d")
    if not markdown:
        raise TeardownBrowserError("The teardown reply carried no note section.")
    evidence_section = ["", "## Evidence", "",
                        f"- Captured in {evidence.seconds}s from {evidence.url}"]
    totals = evidence.totals()
    evidence_section.append(f"- {totals['files']} files, {totals['bytes']} bytes")
    for signal in evidence.signals:
        evidence_section.append(f"- {signal.category}: {signal.described()}")
    for line in evidence.lines:
        evidence_section.append(f"- {line.rendered()}")
    for identifier in evidence.evidence_ids:
        evidence_section.append(f"- REA evidence id: {identifier}")
    full = markdown.rstrip() + "\n" + "\n".join(evidence_section) + "\n"
    out = vault.capture(full, title=f"{NOTE_DIR} {host} {stamp}")
    if not out.ok:
        raise TeardownBrowserError(f"The teardown note was NOT written: {out.detail}")
    return Written(spoken=spoken, summary=summary, note_path=out.path,
                   note_id=out.note.id if out.note else None, label=label or f"Teardown - {host}")


def from_config(config, *, headless: bool | None = None) -> "Teardown | None":
    """Build the teardown layer, or None when it is switched off.

    Deny-by-default like the camera, the browser and the desktop layers: while ``teardown.enabled``
    is false this returns None and every route answers that it is off. Registration grants nothing.
    """
    if not config.get("teardown.enabled", False):
        return None
    root = config.get("teardown.kit_root", None)
    return Teardown(Kit(Path(str(root)) if root else None),
                    headless=bool(config.get("teardown.headless", True)
                                  if headless is None else headless))
