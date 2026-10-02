"""Artifact generation, and - the part that matters - checking what was actually produced.

The blueprint is blunt about this: V.O.I.D "should review generated output rather than assuming that
successful file creation means successful output". A generator that writes a valid-but-empty PowerPoint
*succeeds* at every level a naive implementation can see: no exception, a file on disk, a plausible
extension. The owner gets nothing useful.

So an artifact is not finished when it is written. It is finished when it has been **re-opened and read
back**, and the reader agrees it contains what was asked for:

    plan  →  generate  →  save  →  REOPEN AND INSPECT  →  verify  →  report

:func:`inspect_artifact` is that step. It opens the file with the same library that wrote it and counts
what is actually inside - slides, paragraphs, rows, pages - so "PPT created" is a claim backed by having
read the PPT.

Four formats, each with the canonical library: DOCX (python-docx), PPTX (python-pptx), XLSX (openpyxl),
PDF (reportlab). These are mature, standard and narrow, which is the dependency policy the blueprint asks
for - no framework was added to get a feature.

**Everything is confined.** An artifact is written through V.O.I.D's existing ``FileActions``, so it lands
only inside the owner's allowed roots and never in a protected one. This module never touches a path
itself: it builds bytes and hands them to the file layer, which is what keeps artifact generation from
becoming a way to write anywhere.

**Content is the owner's and the model's, never an instruction.** Text that goes into a document is treated
as data on the way in, and what comes back out of an inspection is treated as data on the way out.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from void.perception import clean_text

_log = logging.getLogger(__name__)

#: Supported formats, mapped to the library that owns each. One canonical library per format.
FORMATS = {
    "docx": "python-docx",
    "pptx": "python-pptx",
    "xlsx": "openpyxl",
    "pdf": "reportlab",
}

#: A generated artifact smaller than this is not plausibly the thing that was asked for. Each format's
#: own container overhead is well above it, so a file under this never contains real content.
MIN_PLAUSIBLE_BYTES = 1024

#: Bounds on what one artifact may contain. Generous for real documents, bounded so a runaway model cannot
#: ask for a million slides.
MAX_SECTIONS = 60
MAX_TEXT = 20_000
MAX_TITLE = 300


class ArtifactError(RuntimeError):
    """Generation or inspection failed. The message is safe to say to the owner."""


@dataclass
class Section:
    """One heading-plus-body unit. The common shape across all four formats.

    A deliberately simple intermediate representation: a slide, a document section, a sheet and a PDF
    block are all "a heading and some text", and flattening them to that is what lets one plan produce any
    format without a per-format planner.
    """

    heading: str = ""
    body: str = ""
    bullets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.heading = clean_text(self.heading, MAX_TITLE)
        self.body = clean_text(self.body, MAX_TEXT)
        self.bullets = tuple(clean_text(bullet, 500) for bullet in (self.bullets or ())
                             if clean_text(bullet, 500))[:30]

    @property
    def empty(self) -> bool:
        return not (self.heading or self.body or self.bullets)


@dataclass
class ArtifactPlan:
    """What to produce, before producing it.

    Separating the plan from the generation is what makes the workflow inspectable and the formats
    interchangeable: the same plan renders as a deck or a report, and a caller can show the plan to the
    owner before anything is written.
    """

    title: str
    kind: str
    sections: tuple[Section, ...] = ()
    #: Where sources came from, for a research artifact. Recorded so a claim can be traced.
    sources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.title = clean_text(self.title, MAX_TITLE) or "Untitled"
        self.kind = clean_text(self.kind, 8).lower()
        if self.kind not in FORMATS:
            raise ArtifactError(
                f"I can create {', '.join(sorted(FORMATS))} files, not '{self.kind}'.")
        self.sections = tuple(section for section in self.sections if not section.empty
                              )[:MAX_SECTIONS]
        self.sources = tuple(clean_text(source, 300) for source in (self.sources or ())
                             if clean_text(source, 300))[:50]

    @property
    def usable(self) -> bool:
        """Is there enough here to make a document worth creating?"""
        return bool(self.sections)


@dataclass
class Inspection:
    """What re-opening the written file actually found."""

    kind: str
    size_bytes: int = 0
    #: Units the format counts in: slides, paragraphs, rows, pages.
    units: int = 0
    unit_name: str = "section"
    text_found: int = 0
    ok: bool = False
    detail: str = ""
    problems: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "size_bytes": self.size_bytes, "units": self.units,
                "unit_name": self.unit_name, "text_found": self.text_found, "ok": self.ok,
                "detail": self.detail, "problems": list(self.problems)}


# --- generation: plan -> bytes --------------------------------------------------------------------
#
# Each builder returns bytes. None of them touches a path: the caller hands the bytes to V.O.I.D's
# confined file layer, which is what keeps artifact generation from being able to write anywhere.

def _build_docx(plan: ArtifactPlan) -> bytes:
    try:
        from docx import Document
    except Exception as exc:                                    # noqa: BLE001
        raise ArtifactError("Word support is not installed (python-docx).") from exc
    document = Document()
    document.add_heading(plan.title, level=0)
    for section in plan.sections:
        if section.heading:
            document.add_heading(section.heading, level=1)
        if section.body:
            document.add_paragraph(section.body)
        for bullet in section.bullets:
            document.add_paragraph(bullet, style="List Bullet")
    if plan.sources:
        document.add_heading("Sources", level=1)
        for source in plan.sources:
            document.add_paragraph(source, style="List Bullet")
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _build_pptx(plan: ArtifactPlan) -> bytes:
    try:
        from pptx import Presentation
        from pptx.util import Inches, Pt
    except Exception as exc:                                    # noqa: BLE001
        raise ArtifactError("PowerPoint support is not installed (python-pptx).") from exc
    presentation = Presentation()
    title_layout = presentation.slide_layouts[0]
    body_layout = presentation.slide_layouts[1]
    opening = presentation.slides.add_slide(title_layout)
    opening.shapes.title.text = plan.title
    if len(opening.placeholders) > 1:
        opening.placeholders[1].text = f"{len(plan.sections)} section(s)"
    for section in plan.sections:
        slide = presentation.slides.add_slide(body_layout)
        slide.shapes.title.text = section.heading or plan.title
        frame = slide.placeholders[1].text_frame
        lines = list(section.bullets) or ([section.body] if section.body else [])
        if lines:
            frame.text = lines[0]
            for line in lines[1:]:
                paragraph = frame.add_paragraph()
                paragraph.text = line
                paragraph.level = 1
    if plan.sources:
        slide = presentation.slides.add_slide(body_layout)
        slide.shapes.title.text = "Sources"
        frame = slide.placeholders[1].text_frame
        frame.text = plan.sources[0]
        for source in plan.sources[1:]:
            frame.add_paragraph().text = source
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _build_xlsx(plan: ArtifactPlan) -> bytes:
    try:
        from openpyxl import Workbook
    except Exception as exc:                                    # noqa: BLE001
        raise ArtifactError("Excel support is not installed (openpyxl).") from exc
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = (plan.title[:28] or "Sheet") .replace("/", "-").replace("\\", "-")
    sheet.append(["Section", "Detail"])
    for section in plan.sections:
        sheet.append([section.heading, section.body])
        for bullet in section.bullets:
            sheet.append(["", bullet])
    if plan.sources:
        sheet.append([])
        sheet.append(["Sources", ""])
        for source in plan.sources:
            sheet.append(["", source])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _build_pdf(plan: ArtifactPlan) -> bytes:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer
    except Exception as exc:                                    # noqa: BLE001
        raise ArtifactError("PDF support is not installed (reportlab).") from exc
    import html
    buffer = io.BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, title=plan.title)
    styles = getSampleStyleSheet()
    flow = [Paragraph(html.escape(plan.title), styles["Title"]), Spacer(1, 12)]
    for section in plan.sections:
        if section.heading:
            flow.append(Paragraph(html.escape(section.heading), styles["Heading2"]))
        if section.body:
            flow.append(Paragraph(html.escape(section.body), styles["BodyText"]))
        if section.bullets:
            flow.append(ListFlowable(
                [ListItem(Paragraph(html.escape(bullet), styles["BodyText"]))
                 for bullet in section.bullets], bulletType="bullet"))
        flow.append(Spacer(1, 8))
    if plan.sources:
        flow.append(Paragraph("Sources", styles["Heading2"]))
        flow.append(ListFlowable(
            [ListItem(Paragraph(html.escape(source), styles["BodyText"]))
             for source in plan.sources], bulletType="bullet"))
    document.build(flow)
    return buffer.getvalue()


_BUILDERS = {"docx": _build_docx, "pptx": _build_pptx, "xlsx": _build_xlsx, "pdf": _build_pdf}


def generate(plan: ArtifactPlan) -> bytes:
    """Render a plan to bytes. Raises :class:`ArtifactError` with something sayable on failure."""
    if not plan.usable:
        raise ArtifactError("There is nothing to put in the document yet.")
    builder = _BUILDERS.get(plan.kind)
    if builder is None:                                         # pragma: no cover - ArtifactPlan guards
        raise ArtifactError(f"I cannot create '{plan.kind}' files.")
    try:
        payload = builder(plan)
    except ArtifactError:
        raise
    except Exception as exc:                                    # noqa: BLE001 - the CLASS only
        _log.info("ARTIFACT_BUILD_FAILED kind=%s error=%s", plan.kind, type(exc).__name__)
        raise ArtifactError(f"The {plan.kind.upper()} could not be built "
                            f"({type(exc).__name__}).") from exc
    if len(payload) < MIN_PLAUSIBLE_BYTES:
        raise ArtifactError(
            f"The {plan.kind.upper()} came out at only {len(payload)} bytes, which cannot be right.")
    return payload


# --- inspection: re-open what was written and read it back ----------------------------------------

def inspect_artifact(payload: bytes, kind: str, plan: ArtifactPlan | None = None) -> Inspection:
    """Re-open generated bytes with the same library and report what is genuinely inside.

    This is the step that makes "PPT created" a claim rather than a hope. It counts real units (slides,
    paragraphs, rows, pages) and the amount of text actually present, and compares both against the plan
    where one is given.

    Never raises: a failed inspection is an :class:`Inspection` with ``ok=False`` and a reason, because
    "I could not verify this" is a useful answer and an exception here would lose the artifact that was
    already written.
    """
    kind = clean_text(kind, 8).lower()
    size = len(payload or b"")
    result = Inspection(kind=kind, size_bytes=size)
    problems: list[str] = []
    if size < MIN_PLAUSIBLE_BYTES:
        result.problems = (f"the file is only {size} bytes",)
        result.detail = "too small to contain anything useful"
        return result
    try:
        if kind == "docx":
            from docx import Document
            document = Document(io.BytesIO(payload))
            paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
            result.units, result.unit_name = len(paragraphs), "paragraph"
            result.text_found = sum(len(text) for text in paragraphs)
        elif kind == "pptx":
            from pptx import Presentation
            presentation = Presentation(io.BytesIO(payload))
            slides = list(presentation.slides)
            result.units, result.unit_name = len(slides), "slide"
            total = 0
            empty = 0
            for slide in slides:
                words = 0
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        words += len(shape.text_frame.text.strip())
                total += words
                if words == 0:
                    empty += 1
            result.text_found = total
            if empty:
                problems.append(f"{empty} slide(s) contain no text")
        elif kind == "xlsx":
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(payload))
            sheet = workbook.active
            rows = [row for row in sheet.iter_rows(values_only=True)
                    if any(cell not in (None, "") for cell in row)]
            result.units, result.unit_name = len(rows), "row"
            result.text_found = sum(len(str(cell)) for row in rows for cell in row if cell)
        elif kind == "pdf":
            # No PDF parser is installed, and adding one to count pages would be a dependency for a
            # single check. The structural markers a valid PDF must contain are enough to tell a real
            # document from a truncated or empty one, and they are honest about what they prove.
            head = payload[:5]
            if head != b"%PDF-":
                problems.append("it does not start with a PDF header")
            if b"%%EOF" not in payload[-2048:]:
                problems.append("it has no end-of-file marker, so it may be truncated")
            pages = payload.count(b"/Type /Page") or payload.count(b"/Type/Page")
            result.units, result.unit_name = max(pages, 0), "page"
            result.text_found = size            # bytes stand in; no parser is installed
        else:
            result.problems = (f"I do not know how to inspect '{kind}' files",)
            result.detail = "no inspector for that format"
            return result
    except Exception as exc:                                    # noqa: BLE001
        result.problems = (f"it could not be reopened ({type(exc).__name__})",)
        result.detail = "the file was written but could not be read back"
        return result

    if result.units <= 0:
        problems.append(f"it contains no {result.unit_name}s")
    if kind != "pdf" and result.text_found < 20:
        problems.append("it contains almost no text")
    if plan is not None and kind == "pptx":
        # One title slide plus one per section is what the builder produces; fewer means content was lost.
        expected = len(plan.sections) + 1
        if result.units < expected:
            problems.append(f"expected about {expected} slides but found {result.units}")
    result.problems = tuple(problems)
    result.ok = not problems
    result.detail = (f"{result.units} {result.unit_name}(s), "
                     f"{result.text_found} characters of content"
                     if result.ok else "; ".join(problems))
    return result
