"""The invoice engine: the model describes the work, Python does the arithmetic.

The pack's demand is specific - "arithmetic recomputed in Python and overriding the model's number" -
and it is the one rule here that generalises beyond invoices, so most of this file is about it. The
rest defends the three things that make an invoice ledger different from a log: numbers are never
reused, money is never a float, and a document that exists is not a delivery that happened.

The delivery leg is still blocked (no transport). That is asserted too: there must be no code path
that can claim otherwise.
"""
from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from void.billing import (CENTS, INVOICE_SYSTEM_PROMPT, LEDGER_NAME, MAX_ITEMS, NUMBER_PREFIX,
                          Discrepancy, Invoice, InvoiceError, Issued, Ledger, LineItem,
                          build, check_number, issue, parse_strict, plan_for)

GOOD = {
    "client": "Sideview Ltd",
    "currency": "GBP",
    "tax_rate": "0.20",
    "items": [
        {"description": "Retainer, March", "quantity": "1", "unit_price": "2400.00"},
        {"description": "Extra day", "quantity": "2", "unit_price": "450.00"},
    ],
}


def ledger(tmp_path) -> Ledger:
    return Ledger(path=tmp_path / ".void" / LEDGER_NAME)


def invoice(**over) -> Invoice:
    payload = {**GOOD, **over}
    return build(payload, number="VOID-2026-0001", issued="2026-03-01")


# --- the rule that matters ------------------------------------------------------------------------

def test_the_arithmetic_is_recomputed_not_read():
    inv = invoice()
    assert inv.subtotal == Decimal("3300.00")
    assert inv.tax == Decimal("660.00")
    assert inv.total == Decimal("3960.00")


def test_a_models_wrong_total_is_overridden_and_recorded():
    """Both halves. Overriding without recording would hide the only evidence that the model cannot
    add up, which is a fact about the run that the owner is entitled to."""
    inv = invoice(subtotal="1.00", tax="999.99", total="4299.99")
    assert inv.total == Decimal("3960.00"), "the model's total reached the invoice"
    said = {d.field for d in inv.discrepancies}
    assert said == {"subtotal", "tax", "total"}
    assert any("999.99" in d.rendered() and "660.00" in d.rendered() for d in inv.discrepancies)


def test_a_models_correct_figures_raise_no_discrepancy():
    """The record must mean something. If a right answer also produced a discrepancy, the list would
    be noise and nobody would read it."""
    inv = invoice(subtotal="3300.00", tax="660.00", total="3960.00")
    assert inv.discrepancies == ()


def test_a_wrong_line_total_is_caught_even_when_the_subtotal_is_right():
    """A model that gets one line wrong and the sum right has still got the line wrong, and the
    owner is entitled to know which line."""
    payload = {**GOOD, "items": [
        {"description": "Retainer, March", "quantity": "1", "unit_price": "2400.00",
         "total": "2400.00"},
        {"description": "Extra day", "quantity": "2", "unit_price": "450.00", "total": "45.00"},
    ], "subtotal": "3300.00"}
    inv = build(payload, number="VOID-2026-0001")
    assert [d.field for d in inv.discrepancies] == ["line 2 total"]


def test_a_line_total_is_a_property_so_it_cannot_be_supplied_at_all():
    """Not merely ignored - there is nowhere to put it. A stored total could go stale."""
    item = LineItem(description="x", quantity=Decimal("3"), unit_price=Decimal("1.11"))
    assert item.total == Decimal("3.33")
    with pytest.raises(Exception):
        item.total = Decimal("9999")            # type: ignore[misc]


def test_a_figure_that_is_not_even_a_number_is_a_discrepancy_not_a_crash():
    """A model that answers "about four thousand" must not take the invoice down with it."""
    inv = invoice(total="about four thousand")
    assert inv.total == Decimal("3960.00")
    assert any(d.field == "total" for d in inv.discrepancies)


# --- money is never a float ----------------------------------------------------------------------

def test_every_amount_is_a_decimal():
    inv = invoice()
    for value in (inv.subtotal, inv.tax, inv.total, inv.tax_rate, *[i.total for i in inv.items]):
        assert isinstance(value, Decimal), f"{value!r} is a {type(value).__name__}"


def test_the_oldest_defect_in_billing_software_does_not_happen_here():
    """0.1 + 0.2 in float is 0.30000000000000004. An invoice a penny out is worse than one that
    fails loudly, so the whole module is Decimal."""
    payload = {**GOOD, "items": [
        {"description": "a", "quantity": "1", "unit_price": "0.10"},
        {"description": "b", "quantity": "1", "unit_price": "0.20"},
    ], "tax_rate": "0"}
    assert build(payload, number="VOID-2026-0001").subtotal == Decimal("0.30")


def test_rounding_is_half_up_the_way_a_person_does_it_on_paper():
    """Banker's rounding would be defensible and surprising; every tax authority expects half-up."""
    payload = {**GOOD, "items": [{"description": "a", "quantity": "1", "unit_price": "0.125"}],
               "tax_rate": "0"}
    assert build(payload, number="VOID-2026-0001").subtotal == Decimal("0.13")
    payload["items"] = [{"description": "a", "quantity": "1", "unit_price": "0.135"}]
    assert build(payload, number="VOID-2026-0001").subtotal == Decimal("0.14")


def test_a_json_float_is_read_through_its_repr_rather_than_binary():
    """A model that ignores "every number is a string" has already lost precision. repr() is the
    least-bad reading of a number that should not have been a float."""
    payload = {**GOOD, "items": [{"description": "a", "quantity": 1, "unit_price": 19.99}],
               "tax_rate": "0"}
    assert build(payload, number="VOID-2026-0001").subtotal == Decimal("19.99")


def test_amounts_are_quantised_to_the_documented_precision():
    assert CENTS == Decimal("0.01")
    payload = {**GOOD, "items": [{"description": "a", "quantity": "3", "unit_price": "1.111"}],
               "tax_rate": "0"}
    assert build(payload, number="VOID-2026-0001").subtotal == Decimal("3.33")


def test_a_thousands_separator_from_a_model_is_read_rather_than_refused():
    payload = {**GOOD, "items": [{"description": "a", "quantity": "1", "unit_price": "2,400.00"}],
               "tax_rate": "0"}
    assert build(payload, number="VOID-2026-0001").subtotal == Decimal("2400.00")


# --- strict parsing -------------------------------------------------------------------------------

def test_a_well_formed_payload_parses():
    assert parse_strict(json.dumps(GOOD))["client"] == "Sideview Ltd"


def test_a_markdown_fence_is_the_only_liberty_taken():
    """Models add them reflexively, and a fence changes no digit."""
    assert parse_strict("```json\n" + json.dumps(GOOD) + "\n```")["client"] == "Sideview Ltd"
    assert parse_strict("```\n" + json.dumps(GOOD) + "\n```")["client"] == "Sideview Ltd"


@pytest.mark.parametrize("raw, because", [
    ("", "empty"),
    ("   ", "whitespace"),
    ("not json at all", "prose"),
    ('{"client": "x"', "truncated"),
    ('[{"client": "x"}]', "a list, not an object"),
    ('"just a string"', "a string"),
    ('{"client": "x"}', "no items list"),
    ('{"client": "x", "items": "two days"}', "items is not a list"),
])
def test_anything_ambiguous_is_refused_rather_than_repaired(raw, because):
    """A lenient parser would be guessing at what someone is being charged. The right behaviour when
    an invoice is ambiguous is to stop."""
    with pytest.raises(InvoiceError):
        parse_strict(raw)


def test_an_absurd_number_of_lines_is_refused():
    payload = {**GOOD, "items": [{"description": "x", "quantity": "1", "unit_price": "1"}] * (MAX_ITEMS + 1)}
    with pytest.raises(InvoiceError):
        parse_strict(json.dumps(payload))


def test_the_prompt_tells_the_model_its_arithmetic_will_be_checked():
    """A prompt that asked for totals and then silently discarded them would be inviting the model
    to waste effort and the reader to be confused."""
    assert "recomputed" in INVOICE_SYSTEM_PROMPT or "computed" in INVOICE_SYSTEM_PROMPT
    assert "STRING" in INVOICE_SYSTEM_PROMPT
    assert "Do NOT include a line total" in INVOICE_SYSTEM_PROMPT


# --- what build refuses ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload, because", [
    ({"items": [{"description": "x", "quantity": "1", "unit_price": "1"}]}, "no client"),
    ({"client": "x", "items": []}, "no lines"),
    ({"client": "x", "items": [{"quantity": "1", "unit_price": "1"}]}, "no description"),
    ({"client": "x", "items": [{"description": "x", "quantity": "0", "unit_price": "1"}]}, "zero quantity"),
    ({"client": "x", "items": [{"description": "x", "quantity": "-1", "unit_price": "1"}]}, "negative quantity"),
    ({"client": "x", "items": [{"description": "x", "quantity": "1", "unit_price": "-5"}]}, "negative price"),
    ({"client": "x", "items": [{"description": "x", "quantity": "1", "unit_price": "1"}],
      "tax_rate": "20"}, "a rate of 20 rather than 0.20"),
    ({"client": "x", "items": [{"description": "x", "quantity": "1", "unit_price": "1"}],
      "tax_rate": "-0.1"}, "a negative rate"),
    ({"client": "x", "items": ["two days"]}, "a line that is not an object"),
    ({"client": "x", "items": [{"description": "x", "quantity": "1e400", "unit_price": "1"}]}, "infinity"),
])
def test_an_invoice_that_cannot_be_right_is_refused(payload, because):
    """Refused, not clamped. A clamped invoice is a wrong invoice that looks right."""
    with pytest.raises(InvoiceError):
        build(payload, number="VOID-2026-0001")


def test_a_boolean_is_not_a_quantity():
    """bool is an int in Python, so "quantity": true would otherwise bill for one of something."""
    payload = {"client": "x", "items": [{"description": "x", "quantity": True, "unit_price": "1"}]}
    with pytest.raises(InvoiceError):
        build(payload, number="VOID-2026-0001")


def test_a_zero_tax_rate_is_fine_and_produces_no_tax_line():
    inv = build({**GOOD, "tax_rate": "0"}, number="VOID-2026-0001")
    assert inv.tax == Decimal("0.00") and inv.total == inv.subtotal
    headings = [s.heading for s in plan_for(inv).sections]
    totals = next(s for s in plan_for(inv).sections if s.heading == "Totals")
    assert not any("Tax" in b for b in totals.bullets)
    assert "Totals" in headings


# --- the numbered ledger --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "1", "VOID-26-0001", "VOID-2026-1", "voidy", "2026-0001",
                                 "VOID_2026_0001", None])
def test_a_number_that_is_not_in_the_sequence_is_refused(bad):
    """An invoice number is a reference a client quotes back. One that does not match the sequence
    cannot be reconciled, which is worse than no invoice."""
    with pytest.raises(InvoiceError):
        check_number(bad)


def test_a_well_formed_number_is_accepted_and_normalised():
    assert check_number(" void-2026-0001 ") == "VOID-2026-0001"
    assert check_number("VOID-2026-00012") == "VOID-2026-00012"


def test_the_first_number_of_a_year_starts_at_one(tmp_path):
    assert ledger(tmp_path).next_number(year=2026) == f"{NUMBER_PREFIX}-2026-0001"


def test_numbers_are_issued_in_sequence_and_gap_free(tmp_path):
    led = ledger(tmp_path)
    for expected in range(1, 6):
        number = led.next_number(year=2026)
        assert number == f"{NUMBER_PREFIX}-2026-{expected:04d}"
        led.record(build(GOOD, number=number, issued="2026-01-01"))
    assert [row["number"] for row in led.entries()] == [
        f"{NUMBER_PREFIX}-2026-{n:04d}" for n in range(1, 6)]


def test_the_sequence_restarts_each_year_without_colliding(tmp_path):
    led = ledger(tmp_path)
    led.record(build(GOOD, number=f"{NUMBER_PREFIX}-2026-0007", issued="2026-12-31"))
    assert led.next_number(year=2026) == f"{NUMBER_PREFIX}-2026-0008"
    assert led.next_number(year=2027) == f"{NUMBER_PREFIX}-2027-0001"


def test_a_number_is_never_reused(tmp_path):
    led = ledger(tmp_path)
    inv = build(GOOD, number=f"{NUMBER_PREFIX}-2026-0001", issued="2026-01-01")
    led.record(inv)
    with pytest.raises(InvoiceError, match="never reused"):
        led.record(inv)


def test_the_next_number_is_derived_from_the_ledger_itself(tmp_path):
    """Not from a counter kept somewhere else. Two sources of truth about invoice numbers is how one
    gets issued twice."""
    led = ledger(tmp_path)
    led.path.parent.mkdir(parents=True, exist_ok=True)
    led.path.write_text(json.dumps({"number": f"{NUMBER_PREFIX}-2026-0042"}) + "\n", encoding="utf-8")
    assert led.next_number(year=2026) == f"{NUMBER_PREFIX}-2026-0043"


def test_a_missing_ledger_reads_as_empty_rather_than_failing(tmp_path):
    assert ledger(tmp_path).entries() == ()


def test_a_corrupt_line_is_skipped_rather_than_guessed_at(tmp_path):
    led = ledger(tmp_path)
    led.path.parent.mkdir(parents=True, exist_ok=True)
    led.path.write_text(
        json.dumps({"number": f"{NUMBER_PREFIX}-2026-0001"}) + "\n"
        + "{ this line is not json\n"
        + "\n"
        + json.dumps({"no number": True}) + "\n"
        + json.dumps({"number": f"{NUMBER_PREFIX}-2026-0002"}) + "\n", encoding="utf-8")
    assert [r["number"] for r in led.entries()] == [f"{NUMBER_PREFIX}-2026-0001",
                                                    f"{NUMBER_PREFIX}-2026-0002"]
    assert led.next_number(year=2026) == f"{NUMBER_PREFIX}-2026-0003"


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes do not describe NTFS access")
def test_the_ledger_is_owner_only(tmp_path):
    """It names the owner's clients and what each was charged - the same reason the task store is
    0600, and found by the same preflight probe."""
    led = ledger(tmp_path)
    led.record(build(GOOD, number=f"{NUMBER_PREFIX}-2026-0001", issued="2026-01-01"))
    assert oct(led.path.stat().st_mode & 0o777) == "0o600"
    assert oct(led.path.parent.stat().st_mode & 0o777) == "0o700"


def test_the_ledger_row_carries_strings_for_the_money(tmp_path):
    """JSON has no decimal type. Writing floats would reintroduce the thing Decimal is here to
    prevent, one layer further out."""
    led = ledger(tmp_path)
    row = led.record(build(GOOD, number=f"{NUMBER_PREFIX}-2026-0001", issued="2026-01-01"))
    for key in ("subtotal", "tax", "total", "tax_rate"):
        assert isinstance(row[key], str), f"{key} is a {type(row[key]).__name__}"
    assert row["total"] == "3960.00"
    assert all(isinstance(i["unit_price"], str) for i in row["items"])


# --- the document ---------------------------------------------------------------------------------

def test_the_plan_reuses_the_existing_artifact_layer():
    from void.artifacts import ArtifactPlan

    assert isinstance(plan_for(invoice()), ArtifactPlan)


def test_the_document_shows_the_recomputed_figures():
    """The Totals section carries the recomputed total and NOT the model's. The model's figure
    appears exactly once on the document, in the Recomputed section that exists to name it.
    """
    plan = plan_for(invoice(total="4299.99"))
    totals = next(s for s in plan.sections if s.heading == "Totals")
    joined = " ".join(totals.bullets)
    assert "3960.00" in joined, "the recomputed total is missing from Totals"
    assert "4299.99" not in joined, "the model's total reached the Totals section"

    whole = " ".join(s.heading + " " + s.body + " " + " ".join(s.bullets) for s in plan.sections)
    assert whole.count("4299.99") == 1, "the model's figure should appear once, as a discrepancy"


def test_a_discrepancy_appears_on_the_document_itself():
    """Not only in a log. A model that cannot add up is a fact about this invoice, and the owner
    should see it before the invoice goes anywhere."""
    plan = plan_for(invoice(total="4299.99"))
    assert any(s.heading == "Recomputed" for s in plan.sections)
    recomputed = next(s for s in plan.sections if s.heading == "Recomputed")
    assert any("4299.99" in b and "3960.00" in b for b in recomputed.bullets)


def test_a_clean_invoice_has_no_recomputed_section():
    assert not any(s.heading == "Recomputed" for s in plan_for(invoice()).sections)


def test_issuing_produces_a_real_document_and_records_it(tmp_path):
    led = ledger(tmp_path)
    out = issue(GOOD, ledger=led, kind="pdf", issued="2026-03-01")
    assert isinstance(out, Issued)
    assert out.document[:5] == b"%PDF-" and out.units >= 1
    assert out.invoice.number == f"{NUMBER_PREFIX}-2026-0001"
    assert [r["number"] for r in led.entries()] == [f"{NUMBER_PREFIX}-2026-0001"]


def test_a_number_is_not_consumed_by_an_invoice_that_could_not_be_produced(tmp_path):
    """The order matters: render and verify BEFORE writing the ledger. Otherwise a failed render
    burns a number and leaves a gap an accountant has to explain."""
    led = ledger(tmp_path)
    with pytest.raises(InvoiceError):
        issue({"client": "x", "items": []}, ledger=led)
    assert led.entries() == ()
    assert led.next_number(year=2026).endswith("0001")


def test_an_unverifiable_document_is_not_recorded(tmp_path, monkeypatch):
    """void.artifacts re-opens what it wrote. If that says the file is not what was asked for, no
    ledger row may appear - a recorded invoice with no usable document is worse than neither."""
    from void import billing

    class Bad:
        ok = False
        detail = "the pdf had no text"
        units = 0
        unit_name = "page"

    import void.artifacts as artifacts

    monkeypatch.setattr(artifacts, "inspect_artifact", lambda *a, **k: Bad())
    led = ledger(tmp_path)
    with pytest.raises(InvoiceError, match="could not be verified"):
        issue(GOOD, ledger=led, kind="pdf")
    assert led.entries() == ()
    assert billing is not None


# --- delivery is not claimed ----------------------------------------------------------------------

def test_an_invoice_is_never_marked_delivered():
    assert invoice().delivered is False
    assert invoice().as_dict()["delivered"] is False


def test_issuing_says_plainly_that_nothing_was_sent(tmp_path):
    out = issue(GOOD, ledger=ledger(tmp_path), kind="pdf")
    assert out.delivered is False
    assert "Nothing was sent" in out.delivery


def test_no_code_path_in_the_module_can_send_anything():
    """The delivery leg is genuinely blocked - no transport is configured and none is written here.
    Asserted by syntax tree rather than by searching the text, because the module's prose explains
    at length why it sends nothing and a grep cannot tell an explanation from a call.
    """
    import ast
    import inspect

    from void import billing

    tree = ast.parse(inspect.getsource(billing))
    used, imported = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    for forbidden in ("send", "sendmail", "post", "urlopen", "sendto", "publish", "deliver"):
        assert forbidden not in used, f"the invoice engine USES {forbidden!r}"
    for forbidden in ("smtplib", "requests", "httpx", "urllib", "socket", "telegram", "email"):
        assert forbidden not in imported, f"the invoice engine IMPORTS {forbidden!r}"


def test_the_receipt_judge_independently_blocks_a_claim_that_one_was_sent():
    """Belt and braces, and the braces are not in this module: even if something told the owner an
    invoice had been sent, the judge refuses the sentence for want of a receipt."""
    from void.orchestration import judge

    ruling = judge.screen("I have sent the invoice to Sideview Ltd.", [])
    assert ruling.blocked is True


# --- the command line ----------------------------------------------------------------------------

def test_the_invoice_subcommand_parses():
    from void.cli import build_parser

    args = build_parser().parse_args(["invoice", "two days for Acme"])
    assert args.command == "invoice" and args.kind == "pdf" and args.out == ""


def test_the_subcommand_is_in_mains_known_set():
    """Checked by source, because the set is a local inside main()."""
    import inspect

    from void.cli import main

    known = inspect.getsource(main).split("known = {")[1].split("}")[0]
    assert '"invoice"' in known


def test_the_command_refuses_honestly_with_no_brain(capsys):
    """It needs a model to read the work description. With none, it must say so and write nothing -
    not invent an invoice."""
    from void.cli import cmd_invoice

    code = cmd_invoice("two days for Acme at 450", "pdf", "", as_json=False)
    out = capsys.readouterr().out
    assert code == 2
    assert "No brain is available" in out and "Nothing was written" in out


def test_the_document_is_written_through_the_confined_file_layer():
    """Never a bare open(). The invoice path has to obey allowed_roots like everything else."""
    import ast
    import inspect

    from void.cli import _save_document

    tree = ast.parse(inspect.getsource(_save_document).lstrip())
    calls = {node.func.id for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "open" not in calls
    src = inspect.getsource(_save_document)
    assert "write_bytes" in src


def test_a_refused_write_is_reported_rather_than_claimed(capsys):
    """If the file layer refuses the path, the command must not print a path that does not exist."""
    from void.cli import _save_document

    class Refused:
        ok = False
        summary = "outside the allowed roots"
        data = None

    class Files:
        def write_bytes(self, *_a, **_k):
            return Refused()

    class Asst:
        files = Files()

    assert _save_document(Asst(), "x.pdf", b"%PDF-") == ""
    assert "could not be saved" in capsys.readouterr().out


def test_a_build_without_a_binary_writer_refuses_rather_than_corrupting(capsys):
    """A DOCX or XLSX is a ZIP; a text-mode write corrupts it on Windows. Refusing beats producing a
    broken file - the same reasoning as void/actions/artifacts.py."""
    from void.cli import _save_document

    class Asst:
        files = object()

    assert _save_document(Asst(), "x.pdf", b"%PDF-") == ""
    assert "cannot save binary documents" in capsys.readouterr().out


def test_the_assistant_exposes_its_file_layer():
    """Reached from the CLI. It used to be available only as artifacts._files, and reaching into a
    private attribute from another module is how a refactor breaks something silently."""
    from void.actions.files import FileActions
    from void.app import Assistant

    assert isinstance(Assistant().files, FileActions)


# --- the shape of the public surface --------------------------------------------------------------

def test_a_discrepancy_reads_as_a_sentence():
    line = Discrepancy("total", "4299.99", "3960.00").rendered()
    assert "the model said 4299.99" in line and "recomputed 3960.00" in line


def test_from_config_puts_the_ledger_in_the_owners_state_directory():
    from void.config import Config

    led = Ledger.from_config(Config.load())
    assert led.path.name == LEDGER_NAME
    assert Path(led.path).parent == Path(Config.load().state_dir())
    assert led.prefix == NUMBER_PREFIX


def test_a_configured_prefix_is_sanitised_rather_than_trusted():
    """It ends up inside a filename-adjacent identifier and in every invoice number, so a prefix
    with a hyphen in it would break the number's own grammar."""
    from void.config import Config

    led = Ledger.from_config(Config({"billing": {"invoice_prefix": "ac-me inc.!"}}))
    assert led.prefix == "ACMEINC"
    assert check_number(led.next_number(year=2026))
