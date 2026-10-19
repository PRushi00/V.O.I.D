"""Naming a website's stack from the code it actually shipped, and labelling the guesses.

The cheat code the REA pack found, and it holds up: **licence banners survive minification.** A
bundler will rename every variable in a library and still keep the comment block the licence requires,
so a minified file that contains "Three.js Authors" followed by a revision number is evidence of an
exact version, not an inference from a file name. That distinction is the whole point of this module:
:class:`Signal` carries how it was found, and a caller that renders a guess as a fact is lying.

Two rules shape the table:

**Ordered by what a reader actually asks.** The 3-D engine first, then animation, then scroll, then
the visual effects, then compression and build tools - because "what is that site built with" means
the engine, and nobody's first question is which minifier it used.

**A version only when the banner gives one.** A pattern that matches the library's name but not its
version yields a signal with an empty version rather than a plausible-looking number. Every hour lost
to this kind of work is lost to something that failed quietly and sounded right.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: How a signal was established, strongest first. Rendered verbatim by the note writer so that a
#: reader can see which claims are evidence and which are inference.
BANNER = "licence banner"
STRING = "code string"
GUESS = "guess from a name"

#: Evidence classes a caller may rely on without hedging.
FACTUAL = frozenset({BANNER, STRING})


@dataclass(frozen=True)
class Signal:
    """One thing found in the code, with what it means and how it was found."""

    #: What a reader cares about: "3D engine", "animation", "scroll", ...
    category: str
    name: str
    version: str = ""
    how: str = BANNER
    #: The matched text, trimmed. Kept so a note can quote its own evidence.
    evidence: str = ""

    @property
    def factual(self) -> bool:
        return self.how in FACTUAL

    def described(self) -> str:
        label = f"{self.name} {self.version}".strip()
        return f"{label} ({self.how})" if self.factual else f"{label} - {self.how}, not evidence"

    def as_dict(self) -> dict:
        return {"category": self.category, "name": self.name, "version": self.version,
                "how": self.how, "evidence": self.evidence}


#: The table. Each row: category, library name, a pattern whose first group is the version (or None),
#: and how a match should be labelled. Ordered by what matters to someone asking about a site.
#:
#: Add the libraries your own audience asks about; that is where this file's taste lives.
PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    # 3-D engines
    ("3D engine", "Three.js", r"Three\.js\s+Authors[\s\S]{0,400}?\br(\d{3})\b", BANNER),
    ("3D engine", "Three.js", r"\bREVISION\s*[=:]\s*['\"](\d{3})['\"]", STRING),
    ("3D engine", "Babylon.js", r"Babylon\.js\s*v?(\d+\.\d+\.\d+)", BANNER),
    ("3D engine", "PlayCanvas", r"PlayCanvas\s+Engine\s+v?(\d+\.\d+\.\d+)", BANNER),
    ("3D engine", "WebGPU", r"\bnavigator\.gpu\b", STRING),
    # animation
    ("animation", "GSAP", r"GSAP\s+(\d+\.\d+\.\d+)", BANNER),
    ("animation", "GSAP", r"gsap(?:Version|\.version)\s*[=:]\s*['\"](\d+\.\d+\.\d+)", STRING),
    ("animation", "Framer Motion", r"framer-motion[@/](\d+\.\d+\.\d+)", BANNER),
    ("animation", "anime.js", r"animejs\s+v?(\d+\.\d+\.\d+)", BANNER),
    ("animation", "Motion One", r"\bmotion(?:-dom)?[@/](\d+\.\d+\.\d+)", BANNER),
    # scroll
    ("scroll", "Lenis", r"\blenis[@/](\d+\.\d+\.\d+)", BANNER),
    ("scroll", "Locomotive Scroll", r"locomotive-scroll[@/]?v?(\d+\.\d+\.\d+)?", BANNER),
    ("scroll", "ScrollTrigger", r"\bScrollTrigger\b", STRING),
    ("scroll", "Smooth Scrollbar", r"smooth-scrollbar[@/](\d+\.\d+\.\d+)", BANNER),
    # visual effects
    ("effects", "postprocessing", r"postprocessing\s+v?(\d+\.\d+\.\d+)", BANNER),
    ("effects", "depth of field", r"\bnearCoCBuffer\b|\bCircleOfConfusion\b", STRING),
    ("effects", "bloom", r"\bbloom(?:Pass|Intensity|Texture)\b", STRING),
    ("effects", "shaders", r"\bgl_FragColor\b|\bvoid\s+main\s*\(\s*\)\s*\{", STRING),
    # frameworks
    ("framework", "React", r"react(?:-dom)?[@/](\d+\.\d+\.\d+)", BANNER),
    ("framework", "Next.js", r"\b__NEXT_DATA__\b|next[@/](\d+\.\d+\.\d+)", STRING),
    ("framework", "Vue", r"\bVue\s*v?(\d+\.\d+\.\d+)", BANNER),
    ("framework", "Svelte", r"\bsvelte[@/](\d+\.\d+\.\d+)", BANNER),
    ("framework", "Nuxt", r"\b__NUXT__\b", STRING),
    # compression and build
    ("compression", "Draco", r"\bDRACOLoader\b|draco_decoder", STRING),
    ("compression", "KTX2 / Basis", r"\bKTX2Loader\b|basis_transcoder", STRING),
    ("compression", "Meshopt", r"\bMeshoptDecoder\b", STRING),
    ("build", "Vite", r"\bvite[@/](\d+\.\d+\.\d+)|/@vite/client", STRING),
    ("build", "webpack", r"\bwebpackChunk|\b__webpack_require__\b", STRING),
    ("build", "Turbopack", r"\bturbopack\b", STRING),
)

#: Order categories are reported in.
CATEGORY_ORDER = ("3D engine", "animation", "scroll", "effects", "framework", "compression", "build")

#: How much of a match is kept as the quotable evidence. Long enough to recognise, short enough that a
#: note is not a code dump.
EVIDENCE_CHARS = 120

#: Other licence banners found but not in the table, reported separately so that the main list stays
#: the one a reader asked for.
_OTHER_BANNER = re.compile(
    r"(?:@license|Copyright\s+\(c\)|\(c\)\s*\d{4})\s*([^\n\r*]{4,80})", re.IGNORECASE)


def fingerprint(code: object) -> list[Signal]:
    """Signals found in ``code``, in reporting order, one per library at most."""
    text = str(code or "")
    if not text:
        return []
    found: dict[tuple[str, str], Signal] = {}
    for category, name, pattern, how in PATTERNS:
        match = re.search(pattern, text)
        if match is None:
            continue
        version = ""
        for group in match.groups():
            if group:
                version = group
                break
        key = (category, name)
        signal = Signal(category=category, name=name, version=version, how=how,
                        evidence=" ".join(match.group(0).split())[:EVIDENCE_CHARS])
        # A banner beats a code string for the same library: it carries the exact version.
        if key not in found or (found[key].how != BANNER and how == BANNER):
            found[key] = signal
    ordered = sorted(found.values(),
                     key=lambda s: (CATEGORY_ORDER.index(s.category)
                                    if s.category in CATEGORY_ORDER else len(CATEGORY_ORDER),
                                    s.name))
    return ordered


def other_banners(code: object, known: list[Signal] | None = None, limit: int = 10) -> list[str]:
    """Licence banners present in the code that the table does not name.

    Reported apart from :func:`fingerprint` for the reason the pack gives: the value of the main list
    is that it answers the question that was asked, and padding it with every copyright line in a
    bundle destroys that.
    """
    text = str(code or "")
    named = {s.name.lower() for s in (known or [])}
    seen, out = set(), []
    for match in _OTHER_BANNER.finditer(text):
        line = " ".join(match.group(1).split())[:80]
        low = line.lower()
        if not line or low in seen or any(name in low for name in named):
            continue
        seen.add(low)
        out.append(line)
        if len(out) >= limit:
            break
    return out
