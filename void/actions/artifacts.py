"""Artifact capabilities: create a document, then read it back before saying it worked.

The tool-level half of :mod:`void.artifacts`. Two things make it more than a file writer:

**It writes through V.O.I.D's confined file layer.** This module never opens a path. It builds bytes and
hands them to ``FileActions``, which applies the owner's allowed roots, the protected roots, traversal
protection and the existing write-risk escalation. So "create a PowerPoint" cannot become a way to write
into Windows, into V.O.I.D's own state, or anywhere the owner has not approved - and overwriting an
existing file still asks, because that is ``_write_risk``'s job and it is untouched.

**It inspects what it produced.** After writing, the file is re-opened and counted, and the answer the
owner hears reflects what is genuinely inside. A deck with empty slides is reported as a problem, not as
"PPT created". That is the blueprint's requirement that V.O.I.D "review generated output rather than
assuming that successful file creation means successful output".

Risk is MEDIUM for a new file and escalates to HIGH when it would replace one, which is the same policy
``write_file`` already applies - reused rather than re-decided.

The notification is deliberately short ("PPT created."), with the detail in ``data`` for anyone who asks.
"""
from __future__ import annotations

import logging

from void.actions.base import Tool, ToolResult
from void.artifacts import (FORMATS, ArtifactError, ArtifactPlan, Section, generate,
                            inspect_artifact)
from void.perception import clean_text
from void.security.risk import RiskLevel

_log = logging.getLogger(__name__)

#: How many sections one call may request.
MAX_SECTIONS = 40


def _sections_from(raw) -> tuple[Section, ...]:
    """Build sections from a model-supplied list, defensively.

    A model will send any shape - a list of strings, a list of dicts with different keys, a single string.
    Everything is coerced rather than trusted, and anything unusable is dropped instead of raising, because
    a partially-usable plan is better than no document.
    """
    if raw is None:
        return ()
    if isinstance(raw, (str, bytes)):
        text = clean_text(raw, 20_000)
        return (Section(heading="", body=text),) if text else ()
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    built: list[Section] = []
    for entry in list(raw)[:MAX_SECTIONS]:
        if isinstance(entry, str):
            text = clean_text(entry, 20_000)
            if text:
                built.append(Section(heading=text[:120], body="" if len(text) <= 120 else text))
            continue
        if not isinstance(entry, dict):
            continue
        bullets = entry.get("bullets") or entry.get("points") or ()
        if isinstance(bullets, (str, bytes)):
            bullets = [bullets]
        if not isinstance(bullets, (list, tuple)):
            bullets = ()
        section = Section(heading=entry.get("heading") or entry.get("title") or "",
                          body=entry.get("body") or entry.get("text") or "",
                          bullets=tuple(str(bullet) for bullet in bullets))
        if not section.empty:
            built.append(section)
    return tuple(built)


class ArtifactActions:
    """Document creation, over V.O.I.D's confined file layer."""

    def __init__(self, file_actions=None, recent=None):
        self._files = file_actions
        #: Where to record a document once it exists, so the owner can immediately say "open this chart"
        #: without the model having had the foresight to note it. Resolved lazily because the recent store
        #: is built after the tools are.
        self._recent = recent

    def _note(self, path: str, kind: str, title: str) -> None:
        """Record a created document as referenceable. Never fatal: a failure here costs a later
        convenience, not the document."""
        store = self._recent
        if callable(store):
            try:
                store = store()
            except Exception:                                  # noqa: BLE001
                return
        if store is None:
            return
        try:
            # The file's own name is the label, because that is what the owner will say ("the revenue
            # chart"), and it is the one name here that the owner chose rather than the model.
            label = str(path).replace("\\", "/").rsplit("/", 1)[-1] or title
            store.note(kind="document", label=label, target=str(path),
                       source="artifacts", produced=True, detail=f"{kind} document")
        except Exception:                                      # noqa: BLE001
            _log.info("ARTIFACT_NOTE_FAILED")

    def create_document(self, path: str, kind: str, title: str,
                        sections=None, sources=None) -> ToolResult:
        """Create a document, then reopen it and report what is actually inside.

        ``path`` goes through ``FileActions.write``, so confinement, protected roots and the
        new-vs-overwrite risk escalation all apply unchanged.
        """
        if self._files is None:
            return ToolResult.failure("File access is not configured, so I cannot save a document.")
        target = clean_text(path, 400)
        if not target:
            return ToolResult.failure("Name the file to create.")
        wanted = clean_text(kind, 8).lower()
        if wanted not in FORMATS:
            return ToolResult.failure(
                f"I can create {', '.join(sorted(FORMATS))} files, not '{kind}'.")
        try:
            plan = ArtifactPlan(title=title or "Untitled", kind=wanted,
                                sections=_sections_from(sections),
                                sources=tuple(clean_text(source, 300)
                                              for source in (sources or ())))
        except ArtifactError as bad:
            return ToolResult.failure(str(bad))
        if not plan.usable:
            return ToolResult.failure(
                "I need at least one section of content before I can create the document.")
        try:
            payload = generate(plan)
        except ArtifactError as bad:
            return ToolResult.failure(str(bad))

        # Written through the confined layer. write_bytes keeps binary intact; the path is validated there.
        written = self._write(target, payload)
        if not written.ok:
            return written

        inspection = inspect_artifact(payload, wanted, plan)
        label = wanted.upper()
        data = {"path": written.data, "kind": wanted, "title": plan.title,
                "inspection": inspection.as_dict(), "sources": list(plan.sources)}
        if not inspection.ok:
            # Honest: the file exists, but reading it back found problems. Not reported as success.
            return ToolResult.failure(
                f"I created the {label} but it does not look right: {inspection.detail}.",
                error=inspection.detail)
        self._note(written.data, wanted, plan.title)
        return ToolResult.success(f"{label} created.", data=data)

    def _write(self, path: str, payload: bytes) -> ToolResult:
        """Hand bytes to the confined file layer. Binary-safe."""
        writer = getattr(self._files, "write_bytes", None)
        if not callable(writer):
            # Deliberately no text-mode fallback. A docx/pptx/xlsx is a ZIP; writing it through a text
            # writer corrupts it on Windows (\n -> \r\n). Refusing beats producing a broken file.
            return ToolResult.failure("This build cannot save binary documents.")
        try:
            return writer(path, payload)
        except Exception as exc:                                # noqa: BLE001
            _log.info("ARTIFACT_WRITE_FAILED kind=%s", type(exc).__name__)
            return ToolResult.failure(f"The document could not be saved ({type(exc).__name__}).")

    def inspect_document(self, path: str) -> ToolResult:
        """Reopen an existing document and report what is inside it.

        Useful on its own ("is that PDF actually readable?") and the mechanism behind the verification step
        of the artifact workflow.
        """
        if self._files is None:
            return ToolResult.failure("File access is not configured.")
        target = clean_text(path, 400)
        kind = target.rsplit(".", 1)[-1].lower() if "." in target else ""
        if kind not in FORMATS:
            return ToolResult.failure(
                f"I can inspect {', '.join(sorted(FORMATS))} files, not '{kind or 'that'}'.")
        reader = getattr(self._files, "read_bytes", None)
        if not callable(reader):
            return ToolResult.failure("This build cannot read files back for inspection.")
        got = reader(target)
        if not got.ok or not isinstance(got.data, (bytes, bytearray)):
            return got if not got.ok else ToolResult.failure("That file could not be read.")
        inspection = inspect_artifact(bytes(got.data), kind)
        verb = "looks right" if inspection.ok else "does not look right"
        return ToolResult.success(f"That {kind.upper()} {verb}: {inspection.detail}.",
                                  data=inspection.as_dict())

    def _write_risk(self, arguments: dict) -> RiskLevel:
        """Reuse the file layer's own new-vs-overwrite policy rather than inventing a second one."""
        risk_fn = getattr(self._files, "_write_risk", None)
        if not callable(risk_fn):
            return RiskLevel.HIGH
        try:
            return risk_fn({"path": arguments.get("path")})
        except Exception:                                      # noqa: BLE001
            return RiskLevel.HIGH

    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="create_document",
                description=(
                    "Create a Word (docx), PowerPoint (pptx), Excel (xlsx) or PDF file from a title and "
                    "a list of sections, save it inside the allowed folders, then reopen it to check it "
                    "actually contains the content. Reports a problem rather than success if it does not."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "Where to save it, inside an allowed folder."},
                        "kind": {"type": "string", "enum": sorted(FORMATS),
                                 "description": "The file format."},
                        "title": {"type": "string", "description": "Document title."},
                        "sections": {"type": "array",
                                     "description": ("Sections, each {heading, body, bullets}. "
                                                     "At least one is required."),
                                     "items": {"type": "object"}},
                        "sources": {"type": "array", "description": "Source URLs to cite.",
                                    "items": {"type": "string"}},
                    },
                    "required": ["path", "kind", "title", "sections"],
                },
                handler=self.create_document,
                risk=RiskLevel.MEDIUM,
                risk_fn=self._write_risk,
            ),
            Tool(
                name="inspect_document",
                description=(
                    "Reopen a docx, pptx, xlsx or PDF file inside the allowed folders and report what it "
                    "actually contains - slides, paragraphs, rows or pages - and whether it looks usable."
                ),
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "The file to inspect."}},
                    "required": ["path"],
                },
                handler=self.inspect_document,
                risk=RiskLevel.LOW,
            ),
        ]
