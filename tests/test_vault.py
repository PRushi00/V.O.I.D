"""The notes vault: indexing, citation ids, honest absence, misheard names, live capture,
confinement, and the tool surface that carries all of it to a model.

The properties worth defending here are the ones the prompt packs were emphatic about and that a
retrieval layer gets wrong by default: it must say the notes do not cover something rather than
answer from a note that merely shares a topic; a captured note must be retrievable immediately, not
after a restart; a capture that fails must say so; and an id that was cited must keep meaning what
it meant.
"""
import os
from pathlib import Path

import pytest

from void.actions.files import FileActions
from void.actions.vault import ABSENT_LINE, NO_VAULT, VaultActions
from void.app import Assistant
from void.config import Config
from void.vault import (COVERAGE_ABSENT, COVERAGE_FOUND, EXCERPT_CHARS, KEEP_RATIO, TOP_K,
                        Vault, VaultUnavailable, Verdict, from_config, phonetic_key)

NOTES = {
    "Projects/Sideview Pricing.md": (
        "# Sideview Pricing\n"
        "The Sideview retainer is 2400 pounds a month for the first six months, then 3200.\n"
        "Discounts are capped at 10 percent and need sign-off. See [[Retainer Terms]].\n"),
    "Projects/Retainer Terms.md": (
        "# Retainer Terms\n"
        "Thirty days notice either way. Invoices go out on the first working day of the month.\n"),
    "Admin/Wake Word Threshold.md": (
        "# Wake Word Threshold\n"
        "The wake word operating point is 0.30 bounded, from owner-voice recall of 22 of 23.\n"),
    "Admin/Notes.md": "# Notes\nA generic page of nothing much.\n",
}


def write_notes(root: Path, notes=None) -> Path:
    for rel, body in (notes or NOTES).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


@pytest.fixture
def vault(tmp_path) -> Vault:
    v = Vault(write_notes(tmp_path / "notes"))
    v.reload()
    return v


# ================================================================== indexing and identity

def test_every_markdown_file_is_indexed_with_its_folder_as_its_group(vault):
    titles = {n.title: n.group for n in vault.notes()}
    assert titles == {"Sideview Pricing": "Projects", "Retainer Terms": "Projects",
                      "Wake Word Threshold": "Admin", "Notes": "Admin"}


def test_ids_are_the_index_in_the_note_list_and_are_stable_across_reloads(vault):
    first = {n.id: n.title for n in vault.notes()}
    assert sorted(first) == list(range(len(first)))          # 0..n-1, no gaps: these are array indexes
    vault.reload()
    assert {n.id: n.title for n in vault.notes()} == first   # same tree -> same numbering


def test_the_numbering_does_not_depend_on_the_order_the_filesystem_hands_files_over(tmp_path):
    """Ids follow sorted relative paths, so two machines agree on what 'note 3' means."""
    a = Vault(write_notes(tmp_path / "a"))
    b = Vault(write_notes(tmp_path / "b", {k: v for k, v in reversed(list(NOTES.items()))}))
    a.reload()
    b.reload()
    assert [n.title for n in a.notes()] == [n.title for n in b.notes()]


def test_an_excerpt_is_bounded_and_carries_no_markdown_scaffolding(tmp_path):
    body = "---\ntags: [x]\n---\n\n## Heading\n\n" + ("word " * 400)
    root = write_notes(tmp_path / "notes", {"Long.md": body})
    v = Vault(root)
    v.reload()
    note = v.notes()[0]
    assert len(note.excerpt) <= EXCERPT_CHARS
    assert "---" not in note.excerpt and "##" not in note.excerpt
    # The WHOLE note's size, not the excerpt's - measured against the FILE rather than the string
    # that was written, because on Windows write_text turns each \n into \r\n and the two differ.
    on_disk = (root / "Long.md").read_text(encoding="utf-8")
    assert note.chars == len(on_disk) >= len(body)


def test_notes_are_linked_by_wikilink_and_by_naming_each_others_titles(vault):
    by_title = {n.title: n for n in vault.notes()}
    pricing, terms = by_title["Sideview Pricing"], by_title["Retainer Terms"]
    assert terms.id in pricing.links and pricing.id in terms.links    # [[Retainer Terms]], both ways
    assert by_title["Notes"].links == ()                              # a generic title links nothing


def test_junk_directories_are_never_walked(tmp_path):
    root = write_notes(tmp_path / "notes")
    for junk in (".obsidian", "node_modules", ".git"):
        (root / junk).mkdir(parents=True, exist_ok=True)
        (root / junk / "cache.md").write_text("# cache\nnoise\n", encoding="utf-8")
    v = Vault(root)
    v.reload()
    assert "cache" not in {n.title for n in v.notes()}


def test_an_empty_folder_is_a_vault_with_no_notes_not_an_error(tmp_path):
    (tmp_path / "empty").mkdir()
    v = Vault(tmp_path / "empty")
    assert v.reload() == 0
    assert v.search("anything").verdict == Verdict.ABSENT


def test_a_missing_folder_fails_loudly(tmp_path):
    with pytest.raises(VaultUnavailable):
        Vault(tmp_path / "nope").reload()


def test_the_indexing_pass_is_capped(tmp_path):
    notes = {f"n{i:03d}.md": f"# n{i}\nbody {i}\n" for i in range(40)}
    v = Vault(write_notes(tmp_path / "notes", notes), max_notes=10)
    assert v.reload() == 10


# ================================================================== retrieval and absence

def test_a_question_the_notes_answer_comes_back_found_and_cites_the_right_note(vault):
    recall = vault.search("what is the sideview retainer per month")
    assert recall.verdict == Verdict.FOUND
    assert vault.note(recall.ids[0]).title == "Sideview Pricing"
    assert recall.coverage >= COVERAGE_FOUND


def test_a_question_the_notes_do_not_cover_is_absent_and_returns_no_excerpts(vault):
    """The whole point. An absent recall hands the model nothing to improvise from."""
    recall = vault.search("what is the weather in Paris tomorrow")
    assert recall.verdict == Verdict.ABSENT
    assert recall.notes == () and recall.ids == []
    assert recall.coverage < COVERAGE_ABSENT


def test_a_title_match_outweighs_the_same_words_buried_in_another_note(tmp_path):
    notes = {"Pricing.md": "# Pricing\nThe number is 2400.\n",
             "Diary.md": "# Diary\nI thought about pricing, pricing, pricing all day.\n"}
    v = Vault(write_notes(tmp_path / "notes", notes))
    v.reload()
    assert v.note(v.search("what is our pricing").ids[0]).title == "Pricing"


def test_weak_matches_are_dropped_rather_than_padding_the_answer(vault):
    recall = vault.search("sideview retainer discount sign-off")
    best = max(recall.scores.values())
    assert all(score >= best * KEEP_RATIO for score in recall.scores.values())
    assert len(recall.notes) <= TOP_K


def test_the_default_cap_holds_and_an_explicit_limit_is_honoured(tmp_path):
    notes = {f"n{i}.md": f"# n{i}\nretainer pricing notice invoice month\n" for i in range(20)}
    v = Vault(write_notes(tmp_path / "notes", notes))
    v.reload()
    assert len(v.search("retainer pricing notice").notes) == TOP_K
    assert len(v.search("retainer pricing notice", limit=3).notes) == 3


def test_asking_for_a_note_by_name_returns_that_note_as_the_answer(vault):
    recall = vault.search("open my notes on sideview pricing")
    assert recall.verdict == Verdict.NAMED
    assert len(recall.notes) == 1 and recall.notes[0].title == "Sideview Pricing"


def test_a_recall_says_which_numbering_its_ids_belong_to(vault):
    before = vault.search("retainer").generation
    vault.reload()
    assert vault.search("retainer").generation == before + 1


# ================================================================== misheard names

def test_the_foldings_are_the_ones_the_mishearings_need():
    assert phonetic_key("Skool") == phonetic_key("school")        # dropped h
    assert phonetic_key("Sideview") == phonetic_key("side view")  # discarded word boundary
    assert phonetic_key("pricing") == phonetic_key("Pricing")     # soft c
    assert phonetic_key("todo") == phonetic_key("toto")           # d against t


def test_vowels_are_not_folded_so_unrelated_names_do_not_collide():
    assert phonetic_key("pricing") != phonetic_key("pracing")
    assert phonetic_key("Jarvis") != phonetic_key("Travis")


def test_a_split_note_name_is_corrected_back_to_the_vaults_own_spelling(vault):
    corrected, matched = vault.correct_names("what is the side view pricing")
    assert matched == "Sideview Pricing" and "Sideview Pricing" in corrected


def test_the_longest_match_wins_over_a_word_inside_it(tmp_path):
    notes = {"View.md": "# View\nabout views\n",
             "Sideview Pricing.md": "# Sideview Pricing\nthe retainer is 2400\n"}
    v = Vault(write_notes(tmp_path / "notes", notes))
    v.reload()
    _, matched = v.correct_names("what is the side view pricing")
    assert matched == "Sideview Pricing"


def test_a_generic_title_may_never_rewrite_a_question(vault):
    """A vault with a note called "Notes" must not turn every sentence into a lookup of it."""
    for question in ("open my note", "make a note of this", "notes"):
        assert vault.correct_names(question)[1] == ""


def test_the_wake_word_is_stripped_before_note_names_are_matched(tmp_path):
    """A note whose title sounds like the assistant's name must not hijack every question."""
    notes = {"Voit Pricing.md": "# Voit Pricing\nA title that sounds like the wake word.\n",
             "Sideview Pricing.md": "# Sideview Pricing\nThe retainer is 2400.\n"}
    v = Vault(write_notes(tmp_path / "notes", notes))
    v.reload()
    for spoken in ("Void, what is the side view pricing",
                   "voit what is the side view pricing",
                   "hey void what is the side view pricing"):
        _corrected, matched = v.correct_names(spoken)
        assert matched == "Sideview Pricing", spoken


def test_the_wake_word_is_only_stripped_from_the_front(tmp_path):
    """In the middle of a sentence it is an ordinary word."""
    v = Vault(write_notes(tmp_path / "notes",
                          {"Sideview Pricing.md": "# Sideview Pricing\nthe retainer\n"}))
    v.reload()
    assert v.correct_names("the void between stars")[0] == "the void between stars"


def test_a_correction_is_reported_rather_than_applied_silently(vault):
    recall = vault.search("open my notes on side view pricing")
    assert recall.corrected_question and "Sideview Pricing" in recall.corrected_question


# ================================================================== growth

def test_a_captured_note_is_on_disk_with_a_title_and_todays_date(vault, tmp_path):
    out = vault.capture("the finish window should be 900 milliseconds")
    assert out.ok and out.note is not None
    body = Path(out.path).read_text(encoding="utf-8")
    assert "900 milliseconds" in body
    assert Path(out.path).parent.name == "captures"
    import time
    assert time.strftime("%Y-%m-%d") in body


def test_a_structured_capture_keeps_its_line_breaks(vault):
    """A spoken capture is one line; a note with headings must not arrive as one paragraph."""
    markdown = "# Teardown\n\n**Verdict:** a Three.js scene.\n\n## The stack\n- Three.js r165\n"
    out = vault.capture(markdown, title="teardowns example.com")
    assert out.ok
    body = Path(out.path).read_text(encoding="utf-8")
    assert "## The stack" in body and "- Three.js r165" in body
    assert body.count("\n") >= 5


def test_a_note_name_keeps_the_dots_in_a_host(vault):
    out = vault.capture("about a site", title="teardowns igloo.inc 2026-10-19")
    assert out.ok and "igloo.inc" in Path(out.path).name
    assert not Path(out.path).name.startswith(".")


def test_a_captured_note_is_retrievable_by_the_very_next_question(vault):
    """Writing a file is not the same as indexing it - no reload, no restart, no re-scan."""
    before = vault.node_count
    out = vault.capture("the finish window should be 900 milliseconds")
    assert vault.node_count == before + 1
    recall = vault.search("what should the finish window be")
    assert out.note.id in recall.ids and recall.verdict == Verdict.FOUND


def test_a_capture_takes_the_next_free_id_so_earlier_citations_still_mean_what_they_meant(vault):
    before = {n.id: n.title for n in vault.notes()}
    out = vault.capture("something new entirely")
    assert out.note.id == max(before) + 1
    assert all(vault.note(i).title == title for i, title in before.items())


def test_capturing_does_not_damage_retrieval_of_the_notes_that_were_already_there(vault):
    assert vault.search("sideview retainer per month").verdict == Verdict.FOUND
    vault.capture("an unrelated passing thought")
    recall = vault.search("what is the sideview retainer per month")
    assert recall.verdict == Verdict.FOUND
    assert vault.note(recall.ids[0]).title == "Sideview Pricing"


def test_two_captures_of_the_same_words_do_not_overwrite_each_other(vault):
    first = vault.capture("the finish window should be 900 milliseconds")
    second = vault.capture("the finish window should be 900 milliseconds")
    assert first.ok and second.ok and first.path != second.path


def test_an_empty_capture_is_refused_with_a_reason(vault):
    out = vault.capture("   ")
    assert not out.ok and out.detail and out.note is None


def test_a_capture_that_cannot_be_written_says_so_instead_of_pretending(vault, monkeypatch):
    """A second brain that quietly forgets is worse than no second brain."""
    monkeypatch.setattr(Path, "write_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only filesystem")))
    before = vault.node_count
    out = vault.capture("this must not be silently lost")
    assert not out.ok and "read-only filesystem" in out.detail
    assert vault.node_count == before                 # and nothing was added to the index either


# ================================================================== confinement

def test_the_vault_reads_through_the_confined_file_layer(tmp_path):
    """A vault root outside the owner's allowed roots indexes nothing and says why."""
    outside = write_notes(tmp_path / "elsewhere")
    files = FileActions(allowed_roots=[tmp_path / "work"])
    (tmp_path / "work").mkdir(exist_ok=True)
    with pytest.raises(VaultUnavailable):
        Vault(outside, file_actions=files).reload()


def test_a_capture_is_written_through_the_file_layer_and_obeys_its_refusals(tmp_path):
    root = write_notes(tmp_path / "work" / "notes")
    files = FileActions(allowed_roots=[tmp_path / "work"])
    v = Vault(root, file_actions=files)
    assert v.capture("inside the allowed root").ok

    confined = FileActions(allowed_roots=[tmp_path / "work"],
                           protected_roots=[tmp_path / "work" / "notes" / "captures"])
    blocked = Vault(root, file_actions=confined)
    out = blocked.capture("into a protected subtree")
    assert not out.ok and "protected" in out.detail.lower()


# ================================================================== the tool surface

def test_with_no_vault_configured_every_tool_answers_honestly_and_names_the_setting():
    actions = VaultActions(vault=lambda: None)
    for tool in actions.tools():
        args = {"search_notes": {"question": "x"}, "read_note": {"note_id": 0},
                "write_note": {"text": "x"}}[tool.name]
        out = tool.run(**args)
        assert not out.ok and out.summary == NO_VAULT and "vault.notes_root" in out.summary


def test_the_absent_verdict_reaches_the_model_as_an_instruction_not_as_a_failure(vault):
    out = VaultActions(vault=lambda: vault).search("what is the weather in Paris tomorrow")
    assert out.ok                                     # nothing failed: the vault answered, and it is "no"
    assert out.data["verdict"] == Verdict.ABSENT and out.data["notes"] == []
    assert "do not answer this from a note on a similar topic" in out.summary


def test_a_found_recall_hands_the_model_citable_numbers(vault):
    out = VaultActions(vault=lambda: vault).search("what is the sideview retainer per month")
    assert out.ok and out.data["verdict"] == Verdict.FOUND
    assert out.data["node_ids"] and "[note " in out.summary


def test_reading_a_note_that_does_not_exist_says_how_many_there_are(vault):
    out = VaultActions(vault=lambda: vault).read(999)
    assert not out.ok and str(vault.node_count) in out.summary


def test_a_failed_capture_reaches_the_model_as_a_failure(vault, monkeypatch):
    monkeypatch.setattr(Path, "write_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    out = VaultActions(vault=lambda: vault).capture("keep this")
    assert not out.ok and "NOT written" in out.summary and "disk full" in out.summary


def test_the_tools_are_read_shaped_except_the_one_that_writes(vault):
    from void.security.risk import RiskLevel
    risks = {t.name: t.risk for t in VaultActions(vault=lambda: vault).tools()}
    assert risks["search_notes"] == RiskLevel.LOW and risks["read_note"] == RiskLevel.LOW
    assert risks["write_note"] == RiskLevel.MEDIUM


# ================================================================== configuration and wiring

def test_absence_of_a_notes_root_is_the_switch(tmp_path):
    assert from_config(Config({"vault": {"enabled": True}})) is None
    assert from_config(Config({"vault": {"enabled": False,
                                         "notes_root": str(tmp_path)}})) is None
    assert from_config(Config({"vault": {"notes_root": str(tmp_path)}})) is not None


def test_the_assistant_registers_the_notes_tools_and_builds_no_index_until_asked(tmp_path):
    root = write_notes(tmp_path / "work" / "notes")
    cfg = Config({"app": {"state_dir": str(tmp_path / "state")},
                  "security": {"allowed_roots": [str(tmp_path / "work")]},
                  "memory": {"enabled": False},
                  "vault": {"notes_root": str(root)}})
    a = Assistant(config=cfg)
    assert {"search_notes", "read_note", "write_note"} <= set(a.tools.names())
    assert a.vault.generation == 0                    # constructing the Assistant walked no tree
    assert a.tools.execute("search_notes", {"question": "sideview retainer"}).ok
    assert a.vault.generation == 1


def test_a_vault_is_not_configured_by_default(tmp_path):
    cfg = Config({"app": {"state_dir": str(tmp_path / "state")},
                  "security": {"allowed_roots": [str(tmp_path / "work")]},
                  "memory": {"enabled": False}})
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    a = Assistant(config=cfg)
    assert a.vault is None
    assert not a.tools.execute("search_notes", {"question": "anything"}).ok


# ================================================================== a question aimed at the notes

@pytest.mark.parametrize("utterance, question", [
    ("what do my notes say about the retainer", "the retainer"),
    ("do my notes mention the kickoff", "the kickoff"),
    ("does my vault cover pricing", "pricing"),
    ("check my notes for the invoice date", "the invoice date"),
    ("search my notes for pricing", "pricing"),
    ("look in my notes for the discount cap", "the discount cap"),
    ("according to my notes, when is the kickoff", "when is the kickoff"),
    ("in my notes, what is the retainer", "what is the retainer"),
    ("from my notes, the discount cap", "the discount cap"),
    ("ask my notes about pricing", "pricing"),
    ("what do I have on pricing in my notes", "pricing"),
    ("Void, what do my notes say about pricing?", "pricing"),
    ("okay, check my notes for the retainer", "the retainer"),
])
def test_an_explicitly_notes_scoped_question_is_recognised(utterance, question):
    from void.vault.ask import scoped_question
    assert scoped_question(utterance) == question


@pytest.mark.parametrize("utterance", [
    # ordinary questions: the notes may or may not be the best source, and that is the agent's call
    "what is the retainer", "when is the kickoff", "what is the weather in Paris",
    # about notes, but not a question for them
    "open my notes folder", "open my notes", "my notes are a mess", "take notes on this",
    "remember that pricing is 2400", "write a note about pricing",
    # scoped but asking nothing
    "what do my notes say", "check my notes", "ask my notes",
    # the notes of a meeting or a document, which are not the owner's vault
    "what do the notes on that document say", "read the notes in the margin",
    "", "   ",
])
def test_an_utterance_that_is_not_a_question_for_the_notes_is_left_alone(utterance):
    from void.vault.ask import scoped_question
    assert scoped_question(utterance) is None


def assistant_with_notes(tmp_path, **over):
    root = write_notes(tmp_path / "work" / "Notes")
    data = {"app": {"state_dir": str(tmp_path / "state")},
            "security": {"allowed_roots": [str(tmp_path / "work")]},
            "memory": {"enabled": False},
            "vault": {"notes_root": str(root)}}
    data.update(over)
    return Assistant(config=Config(data))


def test_a_notes_question_the_notes_cannot_answer_is_answered_without_a_model(tmp_path):
    """The sharp end of "not in the vault": it must not depend on a model choosing to be honest."""
    a = assistant_with_notes(tmp_path)
    out = a.run("what do my notes say about the weather in Paris")
    assert out.steps == 0 and out.engine_authored is True
    assert out.result == a.NOT_IN_NOTES


def test_the_honest_answer_carries_no_untrusted_text(tmp_path):
    """engine_authored means spoken aloud, so not one character may come from a note or a filename."""
    a = assistant_with_notes(tmp_path)
    out = a.run("check my notes for the quantum flux capacitor")
    assert out.result == a.NOT_IN_NOTES
    for note in a.vault.notes():
        assert note.title not in out.result and note.path not in out.result


def test_a_notes_question_the_notes_can_answer_goes_to_the_agent_and_its_tool(tmp_path):
    """No second answering path: the tool result already carries the verified header."""
    a = assistant_with_notes(tmp_path)
    assert a._vault_route("what do my notes say about the sideview retainer") is None


def test_asking_the_notes_with_no_notes_folder_configured_names_the_setting(tmp_path):
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    a = Assistant(config=Config({"app": {"state_dir": str(tmp_path / "state")},
                                 "security": {"allowed_roots": [str(tmp_path / "work")]},
                                 "memory": {"enabled": False}}))
    out = a.run("what do my notes say about the retainer")
    assert out.steps == 0 and out.engine_authored is True
    assert "vault.notes_root" in out.result


def test_opening_the_notes_folder_is_still_a_folder_and_not_a_question(tmp_path):
    """The route sits after every matcher that could claim these words, deliberately."""
    a = assistant_with_notes(tmp_path)
    assert a._vault_route("open my notes folder") is None
    assert a._vault_route("open my notes") is None


def test_a_vault_that_cannot_be_read_falls_through_rather_than_claiming_absence(tmp_path):
    """"That is not in your notes" would be a lie when the truth is "I could not read them"."""
    a = assistant_with_notes(tmp_path)
    import shutil
    shutil.rmtree(tmp_path / "work" / "Notes")
    a.vault._loaded = False
    assert a._vault_route("what do my notes say about the retainer") is None
