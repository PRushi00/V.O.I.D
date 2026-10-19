"""The vault: the owner's own markdown notes, retrievable, cited, and honest about absence.

The gap this closes. V.O.I.D could already *find* a file by name (``search_files``) and *read* one
(``read_file``), and it could recall short owner facts from encrypted memory. What it could not do is
answer a question **from a body of the owner's own long-form notes** and say which notes the answer
came from - the one capability every edition of the Jarvis prompt packs builds first, and the one
their "not in the vault" law is about (see ``docs/PROMPT_PACK_SOURCES.md`` §3).

Three properties shape the design.

**It is retrieval, not a second memory.** ``void.memory`` is the encrypted, owner-reviewed store of
short facts *about the owner*; it holds at most five recalled items and no file content. This module
holds no secrets, encrypts nothing, decides nothing about the owner, and owns no storage of its own:
it reads markdown files that already exist inside the owner's allowed roots and builds an in-RAM
index over them. Deleting the index loses nothing.

**Absence is an answer.** A retrieval layer that always returns its best six notes teaches the model
to improvise from whatever came back. :class:`Recall` therefore carries a verdict, and
:attr:`Verdict.ABSENT` is a first-class outcome that instructs the caller to say the notes do not
cover it rather than to answer from a note that merely shares a topic.

**Note text is untrusted data.** A note can contain a sentence written to look like an instruction.
Nothing here interprets note content; it is scored, excerpted and handed back as data, and the agent
loop already frames tool output as untrusted before a model ever sees it.

Retrieval reuses :mod:`void.memory.index` - the same stemmed tokenizer and the same BM25 - rather
than introducing a second scoring engine. The packs ask for "keyword overlap, title matches weigh
extra"; BM25 with a title boost is that, computed better, with no new dependency.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

from void.memory.index import BM25Index, tokenize

_log = logging.getLogger(__name__)

#: How much of a note is kept as its excerpt. The packs say "roughly a 700-character excerpt"; it is
#: enough to answer from and small enough that six of them fit in a prompt without crowding it out.
EXCERPT_CHARS = 700

#: How many notes a recall returns at most. The packs' "take the top 6", unchanged: six excerpts is
#: ~4 200 characters, which is a sane ceiling for the context budget of a single answer.
TOP_K = 6

#: How much more a term in the title counts than the same term in the body. A note called "Pricing"
#: is about pricing in a way that a note mentioning the word once is not.
TITLE_WEIGHT = 2.5

#: A note scoring below this fraction of the best note's score is dropped from the recall even when
#: there is room for it. The packs' NOTE_KEEP gate: padding the context with weak notes is how an
#: answer ends up citing something irrelevant.
KEEP_RATIO = 0.40

#: Coverage gates. ``coverage`` is the fraction of the question's own content words that appear in
#: the best-scoring note, which is EVIDENCE FROM RETRIEVAL - deliberately not a probability from a
#: judge model. The Jev pack's version of this check asks a decision model for P(the notes contain
#: the answer) and gates on 0.70/0.35; that service is unreachable from here (see
#: docs/PROMPT_PACK_SOURCES.md §5), and a made-up probability would be worse than an honest ratio.
#: Retune these two numbers only from logged recalls, never from one bad answer.
COVERAGE_FOUND = 0.60
COVERAGE_ABSENT = 0.25

#: Ceiling on how many notes are indexed in one pass, and on how much of one note is read. Both are
#: here so that pointing the vault at an enormous tree degrades into "indexed the first N" rather
#: than into an unbounded read of the owner's disk.
MAX_NOTES = 5_000
MAX_NOTE_BYTES = 200_000

#: Directories never walked when scanning for notes. Deliberately the same shape of list the file
#: layer uses, and for the same reason: a vault under a project root should not index its vendored
#: dependencies.
SKIP_DIRS = frozenset({".git", ".obsidian", ".trash", "node_modules", "__pycache__",
                       ".venv", "venv", ".idea", ".vscode", "site-packages"})

#: Where a captured note is written, relative to the vault root. The packs' ``captures/``.
CAPTURES_DIR = "captures"

#: Longest multi-word run of the question tried as a possible note name when correcting a misheard
#: title, and the shortest phonetic key allowed to rewrite anything. One short syllable matches far
#: too much ("note" would rewrite to any note whose name sounds like it).
MAX_NAME_GRAM = 3
MIN_PHONETIC_KEY = 4

_WIKILINK = re.compile(r"\[\[([^\[\]|#]+)")
_FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
_HEADING = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_WORDS = re.compile(r"[A-Za-z0-9']+")

#: Words that are never treated as a note NAME even when a note is called that. "Notes", "Untitled"
#: and the like match everything and name nothing, and a generic title that is allowed to rewrite a
#: question turns a misheard word into a confident wrong lookup.
_GENERIC_TITLES = frozenset({
    "note", "notes", "untitled", "new", "index", "readme", "inbox", "todo", "todos", "scratch",
    "draft", "drafts", "misc", "temp", "test", "daily", "journal", "home", "map", "moc",
})

#: Phrases that mean "show me the note itself" rather than "answer this question". The packs call
#: this the NAMED case: the note IS the answer, so there is nothing to summarise.
_OPEN_PHRASES = (
    "open my note", "open the note", "open my notes", "open note", "pull up my note",
    "pull up the note", "pull up my notes", "show me my note", "show me the note",
    "show my note", "bring up my note", "find my note", "read my note", "read the note",
)


class Verdict:
    """What retrieval established about whether the vault holds the answer.

    Four outcomes, because the three obvious ones are not enough: "the notes name this directly",
    "the notes say nothing about this", "the notes might" and "you asked for a note by name" lead to
    four different things for a caller to do, and collapsing any pair of them produces either a bluff
    or a needless refusal.
    """

    FOUND = "found"
    #: Nothing in the vault is really about the question. The caller must say so plainly.
    ABSENT = "absent"
    #: Something matched, but not well enough to promise the answer is in there.
    UNSURE = "unsure"
    #: The owner asked for a note by name; the note itself is the answer.
    NAMED = "named"

    ALL = frozenset({FOUND, ABSENT, UNSURE, NAMED})


class VaultUnavailable(Exception):
    """No vault is configured, or its root cannot be read. Never raised for an empty vault."""


@dataclass(frozen=True)
class Note:
    """One markdown file, as the vault sees it.

    ``id`` is the note's index in the sorted note list, which is what the packs require so that an
    answer can cite its sources as plain numbers. It is a handle **within one index generation**: a
    captured note is appended with the next free id so earlier citations keep their meaning, and a
    :meth:`Vault.reload` re-derives every id from the sorted paths. :attr:`Recall.generation` is how
    a caller tells one numbering from another.
    """

    id: int
    title: str
    path: str
    group: str
    excerpt: str
    chars: int
    modified: float
    #: Ids of notes this one is linked to: it names them in a ``[[wikilink]]``, or it writes their
    #: title out, or they do one of those to it. Undirected on purpose - "related" has no direction.
    links: tuple[int, ...] = ()

    def as_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "path": self.path, "group": self.group,
                "excerpt": self.excerpt, "chars": self.chars, "links": list(self.links)}


@dataclass(frozen=True)
class Recall:
    """The result of asking the vault a question.

    ``notes`` is already trimmed to what is worth sending: at most :data:`TOP_K`, nothing below
    :data:`KEEP_RATIO` of the best score, and empty when the verdict is ABSENT. A caller that ignores
    ``verdict`` and uses ``notes`` anyway gets the same behaviour as before this module existed,
    which is why the verdict is first in the signature and in the docstring.
    """

    verdict: str
    notes: tuple[Note, ...]
    question: str
    coverage: float
    generation: int
    #: The question after misheard note names were corrected, when any were. Empty when unchanged.
    corrected_question: str = ""
    scores: dict[int, float] = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return self.verdict in (Verdict.FOUND, Verdict.NAMED)

    @property
    def ids(self) -> list[int]:
        return [n.id for n in self.notes]


@dataclass(frozen=True)
class CaptureResult:
    """What a capture actually did. ``ok`` false always carries a reason: the packs' rule is that a
    second brain which quietly forgets is worse than no second brain."""

    ok: bool
    note: Note | None
    path: str
    detail: str


def _title_from_filename(path: Path) -> str:
    stem = path.stem.replace("_", " ").replace("-", " ").strip()
    return " ".join(stem.split()) or path.name


def phonetic_key(text: object) -> str:
    """A spelling-insensitive key for a note NAME, for matching what a recogniser heard.

    Speech recognition writes "school" for "Skool", "side view" for "Sideview" and "Travis" for
    "Jarvis", and the vault is then blamed for not holding a note it holds. Four foldings, each
    earned by one of those cases: the three the Jev pack names (soft c, d against t, v against f),
    plus dropping a non-initial "h" so that "school" and "Skool" land on the same key. Word
    boundaries are discarded for the same reason, which is what makes "side view" match "Sideview".
    Doubled letters collapse, so "Skool" and "Scool" agree too.

    Nothing else is folded, and vowels in particular are kept: every extra rule widens the set of
    unrelated words that collide, and a wrong correction - a question rewritten to ask about the
    wrong note - is worse than no correction at all.
    """
    s = "".join(_WORDS.findall(str(text or ""))).lower()
    out: list[str] = []
    for i, ch in enumerate(s):
        if ch == "c":
            nxt = s[i + 1] if i + 1 < len(s) else ""
            out.append("s" if nxt in ("e", "i", "y") else "k")
        elif ch == "d":
            out.append("t")
        elif ch == "v":
            out.append("f")
        elif ch == "h" and out:
            continue                                  # "school" -> "skol", which is "Skool"
        else:
            out.append(ch)
    folded = "".join(out)
    squeezed: list[str] = []
    for ch in folded:
        if not squeezed or squeezed[-1] != ch:
            squeezed.append(ch)
    return "".join(squeezed)


def _is_nameable(title: str) -> bool:
    """True when a title is specific enough to be allowed to rewrite a question."""
    words = [w.lower() for w in _WORDS.findall(title)]
    if not words:
        return False
    if all(w in _GENERIC_TITLES for w in words):
        return False
    return len(phonetic_key(title)) >= MIN_PHONETIC_KEY


def _excerpt(body: str) -> str:
    text = _FRONTMATTER.sub("", body)
    text = _HEADING.sub("", text)
    text = " ".join(text.split())
    return text[:EXCERPT_CHARS]


class Vault:
    """An in-RAM index over the markdown notes under one root.

    Construction is cheap and does no I/O: the first question (or an explicit :meth:`reload`) is what
    scans the tree, so an owner who never asks a vault question pays nothing for having configured
    one. Every path read goes through the confined file layer when one is supplied, so the vault can
    never read outside the owner's allowed roots, and a vault root that is itself outside them fails
    loudly at :meth:`reload` instead of silently indexing nothing.
    """

    def __init__(self, root: Path | str, *, file_actions=None, now=time.time,
                 max_notes: int = MAX_NOTES):
        self.root = Path(os.path.expanduser(str(root)))
        self._files = file_actions
        self._now = now
        self._max_notes = max(1, int(max_notes))
        self._notes: list[Note] = []
        self._index: BM25Index | None = None
        self._title_tokens: dict[int, set[str]] = {}
        self._loaded = False
        self._generation = 0

    # --- indexing ------------------------------------------------------

    @property
    def generation(self) -> int:
        """Which numbering the current ids belong to. Bumped by every :meth:`reload`."""
        return self._generation

    @property
    def node_count(self) -> int:
        self._ensure()
        return len(self._notes)

    def notes(self) -> list[Note]:
        self._ensure()
        return list(self._notes)

    def _ensure(self) -> None:
        if not self._loaded:
            self.reload()

    def _confine(self, path: Path) -> Path:
        """Resolve through the file layer's confinement when there is one.

        The vault holds no second opinion about which paths are readable. With no file layer (unit
        tests, a standalone index) the path is used as given.
        """
        if self._files is None:
            return path
        return self._files._confine(str(path))        # noqa: SLF001 - one confinement implementation, deliberately

    def reload(self) -> int:
        """Scan the root and rebuild the index. Returns the number of notes indexed."""
        try:
            root = self._confine(self.root)
        except Exception as exc:                      # PathNotAllowed, OSError
            self._loaded = True
            self._notes, self._index = [], None
            raise VaultUnavailable(
                f"The notes folder {self.root} cannot be read: {exc}") from exc
        if not root.is_dir():
            self._loaded = True
            self._notes, self._index = [], None
            raise VaultUnavailable(f"The notes folder {self.root} does not exist.")

        found: list[tuple[str, Path]] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                if not name.lower().endswith(".md"):
                    continue
                path = Path(dirpath) / name
                try:
                    rel = path.relative_to(root).as_posix()
                except ValueError:
                    continue
                found.append((rel, path))
            if len(found) >= self._max_notes:
                break
        found.sort(key=lambda pair: pair[0])          # ids follow sorted relative paths: deterministic
        found = found[: self._max_notes]

        notes: list[Note] = []
        bodies: list[str] = []
        for note_id, (rel, path) in enumerate(found):
            body = self._read(path)
            if body is None:
                continue
            group = Path(rel).parent.as_posix()
            notes.append(Note(id=len(notes), title=_title_from_filename(path), path=str(path),
                              group="" if group == "." else group, excerpt=_excerpt(body),
                              chars=len(body), modified=self._mtime(path)))
            bodies.append(body)

        self._notes = self._with_links(notes, bodies)
        self._build_index(bodies)
        self._loaded = True
        self._generation += 1
        _log.info("VAULT_INDEXED notes=%d generation=%d", len(self._notes), self._generation)
        return len(self._notes)

    def _read(self, path: Path) -> str | None:
        try:
            return path.read_bytes()[:MAX_NOTE_BYTES].decode("utf-8", errors="replace")
        except OSError as exc:
            _log.warning("VAULT_NOTE_UNREADABLE reason=%s", type(exc).__name__)
            return None

    def _mtime(self, path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _with_links(self, notes: list[Note], bodies: list[str]) -> list[Note]:
        """Link notes that name each other, by ``[[wikilink]]`` or by writing the title out.

        Cost control: a title is only compared against a note whose own tokens contain the title's
        rarest token, so this is a few thousand substring checks rather than every title against
        every body. That matters at a few thousand notes, which is a realistic vault.
        """
        by_title: dict[str, int] = {}
        for note in notes:
            by_title.setdefault(note.title.lower(), note.id)
        token_sets = [set(tokenize(body)) for body in bodies]
        rarest: dict[int, str] = {}
        freq: dict[str, int] = {}
        for tokens in token_sets:
            for tok in tokens:
                freq[tok] = freq.get(tok, 0) + 1
        for note in notes:
            toks = tokenize(note.title)
            if toks:
                rarest[note.id] = min(toks, key=lambda t: freq.get(t, 0))

        edges: dict[int, set[int]] = {note.id: set() for note in notes}
        for note, body, tokens in zip(notes, bodies, token_sets):
            lower = body.lower()
            for target in _WIKILINK.findall(body):
                other = by_title.get(" ".join(target.split()).lower())
                if other is not None and other != note.id:
                    edges[note.id].add(other)
                    edges[other].add(note.id)
            for other in notes:
                if other.id == note.id:
                    continue
                token = rarest.get(other.id)
                if token is None or token not in tokens:
                    continue
                if len(other.title) < 3 or other.title.lower() in _GENERIC_TITLES:
                    continue
                if other.title.lower() in lower:
                    edges[note.id].add(other.id)
                    edges[other.id].add(note.id)
        return [replace(note, links=tuple(sorted(edges[note.id]))) for note in notes]

    def _build_index(self, bodies: list[str]) -> None:
        docs: dict[str, list[str]] = {}
        self._title_tokens = {}
        for note, body in zip(self._notes, bodies):
            title_tokens = tokenize(note.title)
            self._title_tokens[note.id] = set(title_tokens)
            # The WHOLE note is indexed, not its 700-character excerpt: a question answered on a
            # note's third page is still answered by that note, and coverage is measured against
            # the same tokens the score came from.
            docs[str(note.id)] = tokenize(body) + title_tokens
        self._index = BM25Index(docs) if docs else None

    # --- retrieval -----------------------------------------------------

    def correct_names(self, question: str) -> tuple[str, str]:
        """Rewrite note names the recogniser mangled. Returns ``(question, matched_title)``.

        Every one-to-three-word run of the question is reduced to a phonetic key and compared with
        the keys of the vault's own note names. A longer run always beats a shorter one inside it, so
        "side view pricing" matches a note called "Sideview Pricing" rather than one called "View".
        Only a nameable title may rewrite anything (see :func:`_is_nameable`): a vault with a note
        called "Notes" must not turn every sentence into a lookup of it.
        """
        self._ensure()
        words = _WORDS.findall(question or "")
        if not words:
            return question, ""
        keys: dict[str, str] = {}
        for note in self._notes:
            if _is_nameable(note.title):
                keys.setdefault(phonetic_key(note.title), note.title)
        if not keys:
            return question, ""
        for size in range(min(MAX_NAME_GRAM, len(words)), 0, -1):
            for start in range(0, len(words) - size + 1):
                gram = words[start:start + size]
                title = keys.get(phonetic_key(" ".join(gram)))
                if title is None or " ".join(gram).lower() == title.lower():
                    continue
                rewritten = " ".join(words[:start] + [title] + words[start + size:])
                return rewritten, title
        return question, ""

    def _asks_to_open(self, question: str) -> bool:
        lowered = " ".join(str(question or "").lower().split())
        return any(phrase in lowered for phrase in _OPEN_PHRASES)

    def _coverage(self, question_tokens: list[str], note_id: int, body_tokens: set[str]) -> float:
        if not question_tokens:
            return 0.0
        wanted = set(question_tokens)
        present = wanted & (body_tokens | self._title_tokens.get(note_id, set()))
        return len(present) / len(wanted)

    def search(self, question: str, *, limit: int = TOP_K) -> Recall:
        """Score every note against ``question`` and decide whether the vault holds the answer."""
        self._ensure()
        corrected, matched = self.correct_names(question)
        asked = corrected if matched else (question or "")
        tokens = tokenize(asked)
        if self._index is None or not self._notes or not tokens:
            return Recall(Verdict.ABSENT, (), question or "", 0.0, self._generation,
                          corrected_question=corrected if matched else "")

        raw = self._index.score(tokens)
        scores: dict[int, float] = {}
        for key, value in raw.items():
            note_id = int(key)
            boost = TITLE_WEIGHT if (self._title_tokens.get(note_id, set()) & set(tokens)) else 1.0
            scores[note_id] = value * boost
        if not scores:
            return Recall(Verdict.ABSENT, (), question or "", 0.0, self._generation,
                          corrected_question=corrected if matched else "")

        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        best_id, best_score = ranked[0]
        kept = [note_id for note_id, score in ranked[: max(1, int(limit))]
                if score >= best_score * KEEP_RATIO]
        by_id = {note.id: note for note in self._notes}
        coverage = self._coverage(tokens, best_id, set(self._index.tokens(str(best_id))))

        if matched and self._asks_to_open(question):
            verdict = Verdict.NAMED
            kept = [best_id]
        elif self._asks_to_open(question) and coverage >= COVERAGE_ABSENT:
            verdict = Verdict.NAMED
            kept = [best_id]
        elif coverage >= COVERAGE_FOUND:
            verdict = Verdict.FOUND
        elif coverage < COVERAGE_ABSENT:
            verdict = Verdict.ABSENT
            kept = []
        else:
            verdict = Verdict.UNSURE
        return Recall(verdict, tuple(by_id[i] for i in kept), question or "", round(coverage, 3),
                      self._generation, corrected_question=corrected if matched else "",
                      scores={i: round(scores[i], 4) for i in kept})

    def note(self, note_id: int) -> Note | None:
        self._ensure()
        for note in self._notes:
            if note.id == int(note_id):
                return note
        return None

    def read_note(self, note_id: int) -> str | None:
        """The full text of one note, read through the confined file layer."""
        note = self.note(note_id)
        if note is None:
            return None
        try:
            return self._read(self._confine(Path(note.path)))
        except Exception:                             # PathNotAllowed: the tree moved under us
            return None

    # --- growth --------------------------------------------------------

    def capture(self, text: str, *, title: str | None = None) -> CaptureResult:
        """Write one note into ``captures/`` and index it immediately.

        Writing a file is not the same as indexing it. The packs are emphatic about this and they are
        right: a capture that lands on disk but cannot be retrieved until the next restart reads, to
        the owner, exactly like a capture that was lost. So the new note is appended to the live index
        - with the next free id, so ids already cited keep their meaning - and is a candidate in the
        very next question.
        """
        body = " ".join(str(text or "").split())
        if not body:
            return CaptureResult(False, None, "", "There was nothing to write down.")
        self._ensure()
        name = (title or " ".join(body.split()[:8])).strip()
        slug = re.sub(r"[^A-Za-z0-9 _-]", "", name).strip() or "capture"
        stamp = time.strftime("%Y-%m-%d", time.localtime(self._now()))
        target = Path(self.root) / CAPTURES_DIR / f"{slug[:60]}.md"
        n = 2
        while target.exists():
            target = Path(self.root) / CAPTURES_DIR / f"{slug[:60]}-{n}.md"
            n += 1
        content = f"# {slug[:60]}\n\n{stamp}\n\n{body}\n"

        written = self._write(target, content)
        if written is not None:
            return CaptureResult(False, None, str(target), written)

        note = Note(id=(max((x.id for x in self._notes), default=-1) + 1),
                    title=_title_from_filename(target), path=str(target),
                    group=CAPTURES_DIR, excerpt=_excerpt(content), chars=len(content),
                    modified=self._mtime(target))
        self._notes.append(note)
        self._reindex_appended(note, content)
        return CaptureResult(True, note, str(target),
                             f"Noted, and indexed as note {note.id}.")

    def _write(self, target: Path, content: str) -> str | None:
        """Write one capture. Returns None on success, or the reason it failed."""
        if self._files is not None:
            out = self._files.write(str(target), content, overwrite=False)
            return None if out.ok else (out.error or out.summary)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"Could not write {target}: {exc}"
        return None

    def _reindex_appended(self, note: Note, body: str) -> None:
        """Rebuild the BM25 index including one appended note.

        BM25Index is immutable by design (it precomputes document frequencies), so a capture rebuilds
        it from the excerpts already in hand rather than re-reading the tree. At vault scale this is
        milliseconds, and it keeps the append path free of a second indexing implementation.
        """
        docs: dict[str, list[str]] = {}
        for existing in self._notes:
            if existing.id == note.id:
                title_tokens = tokenize(existing.title)
                self._title_tokens[existing.id] = set(title_tokens)
                docs[str(existing.id)] = tokenize(body) + title_tokens
            else:
                docs[str(existing.id)] = (self._index.tokens(str(existing.id))
                                          if self._index is not None else [])
        self._index = BM25Index(docs)


def from_config(config, *, file_actions=None) -> "Vault | None":
    """Build the vault from ``vault.notes_root``, or return None when none is configured.

    Absence of configuration is the switch. A vault reads the owner's own markdown inside roots the
    file layer already allows, so it grants no new reach - but it should not guess where the notes
    are, and a wrong guess (indexing a whole home directory) would be both slow and a surprise.
    """
    if not config.get("vault.enabled", True):
        return None
    root = config.get("vault.notes_root", None)
    if not root:
        return None
    return Vault(Path(str(root)), file_actions=file_actions,
                 max_notes=int(config.get("vault.max_notes", MAX_NOTES)))
