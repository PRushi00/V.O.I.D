"""Tools over the notes vault: ask it, read one note, write one down.

Three tools, and the shape of each is set by one rule from the prompt packs: an assistant that
answers from a note which merely shares a topic is worse than one that says it does not know. So
``vault_search`` reports the vault's own verdict in the summary the model reads, and on
:attr:`~void.vault.Verdict.ABSENT` it returns **no** excerpts at all - there is nothing to improvise
from, which is the point.

Nothing here is a second file layer. Every read and the capture write go through the same
``FileActions`` the owner's allowed roots, protected roots and engine-protected paths are enforced
in, so a vault pointed at a directory the owner has not authorised fails the same way any other file
access does. See ``void/vault/__init__.py`` for why absence of configuration - not a boolean - is the
switch.
"""
from __future__ import annotations

from void.actions.base import Tool, ToolResult
from void.security.risk import RiskLevel
from void.vault import TOP_K, Verdict, VaultUnavailable

#: What the model is told when no vault is configured. One sentence, and it names the setting, because
#: "I have no notes" sends a model looking for a workaround and this sends the owner to the fix.
NO_VAULT = ("No notes folder is configured, so there is nothing to search. "
            "Set vault.notes_root in config/local_config.yaml to the owner's notes directory.")

#: The sentence the owner should hear when the notes do not cover the question. The packs' "not in the
#: vault": the honest answer, said plainly, with no attempt to pad it out of a related note.
ABSENT_LINE = "That is not in the notes."


class VaultActions:
    """Tool surface over one :class:`~void.vault.Vault`.

    ``vault`` is a callable rather than an instance so that configuring a notes folder costs nothing
    until a question actually asks about it: the tree is walked on first use, not at construction.
    """

    def __init__(self, vault=None):
        self._vault = vault if callable(vault) else (lambda: vault)

    def _get(self):
        try:
            return self._vault()
        except Exception:                             # a misconfigured vault must not break the registry
            return None

    # --- tools ---------------------------------------------------------

    def search(self, question: str, limit: int = TOP_K) -> ToolResult:
        vault = self._get()
        if vault is None:
            return ToolResult.failure(NO_VAULT, error="no vault configured")
        try:
            recall = vault.search(question, limit=limit)
        except VaultUnavailable as exc:
            return ToolResult.failure(str(exc), error=str(exc))

        payload = {"verdict": recall.verdict, "coverage": recall.coverage,
                   "node_ids": recall.ids, "generation": recall.generation,
                   "notes": [n.as_dict() for n in recall.notes]}
        if recall.corrected_question:
            payload["corrected_question"] = recall.corrected_question

        if recall.verdict == Verdict.ABSENT:
            # Deliberately ok=True: nothing failed. The vault answered, and the answer is "no".
            return ToolResult.success(
                f"The notes do not cover this ({vault.node_count} notes searched). "
                f"Say so plainly - do not answer this from a note on a similar topic.",
                data=payload)
        if recall.verdict == Verdict.NAMED:
            note = recall.notes[0]
            return ToolResult.success(
                f"The owner asked for a note by name. Note {note.id} is '{note.title}' "
                f"({note.path}). Its text follows; the note itself is the answer.\n\n{note.excerpt}",
                data=payload)

        header = ("These notes were checked and do contain the answer; answer from them."
                  if recall.verdict == Verdict.FOUND else
                  "These notes are the closest matches, but the answer was not confirmed to be in "
                  "them. Answer only from what they actually say.")
        lines = [f"[note {n.id}] {n.title} ({n.group or 'root'})\n{n.excerpt}"
                 for n in recall.notes]
        return ToolResult.success(header + "\n\n" + "\n\n".join(lines), data=payload)

    def read(self, note_id: int) -> ToolResult:
        vault = self._get()
        if vault is None:
            return ToolResult.failure(NO_VAULT, error="no vault configured")
        try:
            note = vault.note(int(note_id))
        except (VaultUnavailable, TypeError, ValueError) as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if note is None:
            return ToolResult.failure(
                f"There is no note {note_id}. The vault holds {vault.node_count} notes, "
                f"numbered from 0.")
        text = vault.read_note(note.id)
        if text is None:
            return ToolResult.failure(
                f"Note {note.id} ('{note.title}') could not be read; it may have moved.")
        return ToolResult.success(f"Note {note.id} - {note.title} ({note.path}):\n{text}",
                                  data={"id": note.id, "title": note.title,
                                        "path": note.path, "text": text})

    def capture(self, text: str, title: str | None = None) -> ToolResult:
        vault = self._get()
        if vault is None:
            return ToolResult.failure(NO_VAULT, error="no vault configured")
        try:
            out = vault.capture(text, title=title)
        except VaultUnavailable as exc:
            return ToolResult.failure(str(exc), error=str(exc))
        if not out.ok:
            # Loudly. A capture that fails silently is the failure the packs single out as the worst
            # one available to a second brain, and the owner has to hear about it.
            return ToolResult.failure(f"The note was NOT written: {out.detail}", error=out.detail)
        return ToolResult.success(f"{out.detail} ({out.path})",
                                  data={"id": out.note.id if out.note else None,
                                        "path": out.path})

    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="search_notes",
                description=(
                    "Search the owner's own markdown notes and get the passages that answer a "
                    "question, each with a note number you can cite. Use this for any question "
                    "about the owner's own writing, projects, decisions or plans. The result "
                    "carries a verdict: when it says the notes do not cover the question, say so "
                    "plainly instead of answering from a note on a similar topic."),
                parameters={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string",
                                     "description": "The owner's question, in their own words."},
                        "limit": {"type": "integer",
                                  "description": f"How many notes at most (default {TOP_K})."},
                    },
                    "required": ["question"],
                },
                handler=self.search,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="read_note",
                description=("Read one whole note by the number a notes search returned. Use it "
                             "when an excerpt is not enough, or when the owner asked to see a note."),
                parameters={
                    "type": "object",
                    "properties": {"note_id": {"type": "integer",
                                               "description": "The note number from search_notes."}},
                    "required": ["note_id"],
                },
                handler=self.read,
                risk=RiskLevel.LOW,
            ),
            Tool(
                name="write_note",
                description=("Write a new markdown note into the owner's notes folder (in its "
                             "captures subfolder) and index it immediately, so it can be found by "
                             "the very next search. Use it when the owner asks for something to be "
                             "written down or kept as a note."),
                parameters={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string", "description": "What the note should say."},
                        "title": {"type": "string",
                                  "description": "Optional title; otherwise taken from the text."},
                    },
                    "required": ["text"],
                },
                handler=self.capture,
                risk=RiskLevel.MEDIUM,
            ),
        ]
