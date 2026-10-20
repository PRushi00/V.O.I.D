"""The invoice engine: the model describes the work, Python does the arithmetic.

The phone pack's fourth prompt asks for strict JSON from the model, arithmetic **recomputed in
Python and overriding the model's number**, a numbered ledger, a printable document, and a delivery
that is never claimed unless it happened. The first pass recorded this as DECLINED: its delivery leg
is blocked on Telegram, and an invoice generator is a business feature rather than a capability. The
delivery leg is still blocked. Everything else is built here, because the one rule in it generalises
further than invoices do:

**A language model's arithmetic is never an input.** It is parsed, recomputed, and overridden. Where
the model's figure and the recomputed figure disagree, the recomputed one wins and the disagreement
is **recorded on the invoice** rather than quietly corrected - a model that cannot add up is a fact
about the run, and silently fixing it would hide the one signal that says so.

Money is :class:`decimal.Decimal` throughout and never ``float``. ``0.1 + 0.2`` is the oldest defect
in billing software, and an invoice that is a penny out is worse than one that fails loudly.

Three further things are deliberate:

* **The ledger is append-only and gap-free.** Numbers are issued in sequence, never reused, and
  written through ``void.security.ownership`` at 0600 - an invoice ledger names the owner's clients
  and what they were charged.
* **The document is the existing artifact layer**, not a second generator. ``void.artifacts`` already
  re-opens every file it writes and reports what it actually found, which is the same honesty rule
  this module needs for "the PDF exists".
* **Nothing here sends anything.** :class:`Invoice` carries ``delivered=False`` and there is no code
  path that can set it true, because this module has nothing to send with. The receipt judge
  (``void/orchestration/judge.py``) independently blocks any claim that an invoice was sent.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from void.security import ownership

_log = logging.getLogger(__name__)

#: Money is quantised to this many places, half-up - the rounding a person does on paper, and the
#: one every tax authority expects. Banker's rounding would be defensible and surprising.
CENTS = Decimal("0.01")

#: Guards against a model emitting something absurd. An invoice outside these is refused rather than
#: clamped: a clamped invoice is a wrong invoice that looks right.
MAX_ITEMS = 200
MAX_QUANTITY = Decimal("100000")
MAX_UNIT_PRICE = Decimal("10000000")
MAX_TAX_RATE = Decimal("1")

#: The ledger's filename under the state directory.
LEDGER_NAME = "invoices.jsonl"

#: Invoice numbers look like VOID-2026-0001: a prefix, the year the number was issued, and a
#: zero-padded sequence that restarts each year. Deliberately not random - an invoice number is a
#: reference a client quotes back, and a gap in a sequence is a question an accountant has to answer.
NUMBER_PREFIX = "VOID"
_NUMBER = re.compile(r"^(?P<prefix>[A-Z0-9]+)-(?P<year>\d{4})-(?P<seq>\d{4,})$")


class InvoiceError(ValueError):
    """The model's payload could not be turned into an invoice. Carries why, for the owner."""


@dataclass(frozen=True)
class Discrepancy:
    """A figure the model gave that the recomputation disagreed with.

    Kept on the invoice rather than resolved away. The recomputed value is authoritative; this
    records that the model's was not, which is the only evidence that it got the sums wrong.
    """

    field: str
    model_said: str
    recomputed: str

    def rendered(self) -> str:
        return f"{self.field}: the model said {self.model_said}, recomputed {self.recomputed}"


@dataclass(frozen=True)
class LineItem:
    """One line. ``total`` is a property, never a stored number, so it cannot be stale or supplied."""

    description: str
    quantity: Decimal
    unit_price: Decimal

    @property
    def total(self) -> Decimal:
        return _money(self.quantity * self.unit_price)


@dataclass(frozen=True)
class Invoice:
    """One invoice, with every figure recomputed and the model's disagreements recorded.

    ``delivered`` is false and there is no code path in this module that can make it true. An
    invoice that has been written is not an invoice that has been sent, and the difference is
    exactly the kind of thing the packs' receipts rule exists to stop anyone blurring.
    """

    number: str
    issued: str
    client: str
    items: tuple[LineItem, ...]
    currency: str
    tax_rate: Decimal
    notes: str = ""
    discrepancies: tuple[Discrepancy, ...] = ()
    delivered: bool = False

    @property
    def subtotal(self) -> Decimal:
        return _money(sum((item.total for item in self.items), Decimal(0)))

    @property
    def tax(self) -> Decimal:
        return _money(self.subtotal * self.tax_rate)

    @property
    def total(self) -> Decimal:
        return _money(self.subtotal + self.tax)

    def as_dict(self) -> dict:
        """The ledger row. Strings for the money, because JSON has no decimal."""
        return {"number": self.number, "issued": self.issued, "client": self.client,
                "currency": self.currency, "tax_rate": str(self.tax_rate),
                "subtotal": str(self.subtotal), "tax": str(self.tax), "total": str(self.total),
                "items": [{"description": i.description, "quantity": str(i.quantity),
                           "unit_price": str(i.unit_price), "total": str(i.total)}
                          for i in self.items],
                "discrepancies": [d.rendered() for d in self.discrepancies],
                "delivered": False}


def _money(value) -> Decimal:
    """Quantise to cents, half-up. Rejects anything that is not a finite number."""
    try:
        amount = Decimal(value) if not isinstance(value, Decimal) else value
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvoiceError(f"not an amount: {value!r}") from exc
    if not amount.is_finite():
        raise InvoiceError(f"not a finite amount: {value!r}")
    return amount.quantize(CENTS, rounding=ROUND_HALF_UP)


def _decimal(value, *, field_name: str) -> Decimal:
    """A model's number as a Decimal, from a string or an int - never from a float.

    A float has already lost precision by the time it reaches here, so a JSON number like 19.99
    arrives as the string "19.99" when the model is doing as it is told, and is converted from its
    repr otherwise. ``Decimal(str(float))`` is the least-bad reading of a number that should not
    have been a float, and the strict parser warns about it separately.
    """
    if isinstance(value, bool):                 # bool is an int in Python; it is not a quantity
        raise InvoiceError(f"{field_name}: expected a number, got a boolean")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    text = str(value or "").strip().replace(",", "")
    if not text:
        raise InvoiceError(f"{field_name}: no value")
    try:
        return Decimal(text)
    except (InvalidOperation, DecimalException) as exc:
        raise InvoiceError(f"{field_name}: {value!r} is not a number") from exc


# --- strict parsing -------------------------------------------------------------------------------

INVOICE_SYSTEM_PROMPT = """You turn a description of work into invoice data. Reply with ONE JSON object and nothing else - no prose, no markdown fence, no explanation.

{
  "client": "who is being billed",
  "currency": "GBP",
  "tax_rate": "0.20",
  "notes": "optional, one or two sentences",
  "items": [
    {"description": "what the line is for", "quantity": "2", "unit_price": "450.00"}
  ]
}

Rules:
- Every number is a STRING, so no precision is lost on the way here.
- quantity and unit_price only. Do NOT include a line total, a subtotal, a tax amount or a total:
  they are computed from your numbers and anything you supply for them is checked against the
  computation and discarded if it disagrees.
- tax_rate is a fraction, so 20% is "0.20". Use "0" if there is no tax.
- If the description does not say what to charge, leave items empty rather than inventing a price.
"""


def parse_strict(raw: object) -> dict:
    """Read the model's reply as one JSON object, or refuse.

    Strict on purpose. A lenient parser that repaired a half-written payload would be guessing at
    what someone is being charged, and the right behaviour when an invoice is ambiguous is to stop.
    The one liberty taken is stripping a markdown fence, because models add them reflexively and a
    fence changes no digit.
    """
    text = str(raw or "").strip()
    if not text:
        raise InvoiceError("The reply was empty.")
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvoiceError(f"The reply was not JSON: {exc.msg} at line {exc.lineno}.") from exc
    if not isinstance(payload, dict):
        raise InvoiceError(f"The reply was {type(payload).__name__}, not a JSON object.")
    if not isinstance(payload.get("items"), list):
        raise InvoiceError("The reply carried no 'items' list.")
    if len(payload["items"]) > MAX_ITEMS:
        raise InvoiceError(f"{len(payload['items'])} line items; the limit is {MAX_ITEMS}.")
    return payload


def build(payload: dict, *, number: str, issued: str | None = None,
          currency: str = "GBP", default_tax_rate: str = "0") -> Invoice:
    """Turn a parsed payload into an invoice, recomputing every figure.

    Any subtotal, tax or total the model supplied is compared with the recomputation and recorded as
    a :class:`Discrepancy` if it differs. The recomputed value is the one that reaches the document;
    the model's never is.
    """
    if not isinstance(payload, dict):
        raise InvoiceError("Not an invoice payload.")
    client = " ".join(str(payload.get("client") or "").split())[:200]
    if not client:
        raise InvoiceError("The invoice has no client, so there is nobody to send it to.")

    rows = payload.get("items") or []
    items: list[LineItem] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise InvoiceError(f"Line {index} is not an object.")
        description = " ".join(str(row.get("description") or "").split())[:300]
        if not description:
            raise InvoiceError(f"Line {index} has no description.")
        quantity = _decimal(row.get("quantity"), field_name=f"line {index} quantity")
        unit_price = _decimal(row.get("unit_price"), field_name=f"line {index} unit price")
        if quantity <= 0:
            raise InvoiceError(f"Line {index} has a quantity of {quantity}.")
        if unit_price < 0:
            raise InvoiceError(f"Line {index} has a negative unit price.")
        if quantity > MAX_QUANTITY or unit_price > MAX_UNIT_PRICE:
            raise InvoiceError(f"Line {index} is outside the sane range for an invoice.")
        items.append(LineItem(description=description, quantity=quantity, unit_price=unit_price))
    if not items:
        raise InvoiceError("The invoice has no line items, so there is nothing to charge for.")

    tax_rate = _decimal(payload.get("tax_rate", default_tax_rate), field_name="tax rate")
    if tax_rate < 0 or tax_rate > MAX_TAX_RATE:
        raise InvoiceError(f"A tax rate of {tax_rate} is not a fraction between 0 and 1.")

    invoice = Invoice(
        number=check_number(number),
        issued=issued or time.strftime("%Y-%m-%d"),
        client=client,
        items=tuple(items),
        currency=(str(payload.get("currency") or currency).strip() or currency)[:8].upper(),
        tax_rate=tax_rate,
        notes=" ".join(str(payload.get("notes") or "").split())[:600],
    )
    return Invoice(**{**invoice.__dict__, "discrepancies": _discrepancies(payload, invoice)})


def _discrepancies(payload: dict, invoice: Invoice) -> tuple[Discrepancy, ...]:
    """Every figure the model supplied that the recomputation disagrees with.

    Line totals are checked as well as the summary ones: a model that gets one line wrong and the
    subtotal right has still got the line wrong, and the owner is entitled to know which.
    """
    found: list[Discrepancy] = []

    def compare(name: str, supplied, computed: Decimal) -> None:
        if supplied is None:
            return
        try:
            said = _money(_decimal(supplied, field_name=name))
        except InvoiceError:
            found.append(Discrepancy(name, repr(supplied), str(computed)))
            return
        if said != computed:
            found.append(Discrepancy(name, str(said), str(computed)))

    for index, (row, item) in enumerate(zip(payload.get("items") or [], invoice.items), start=1):
        if isinstance(row, dict):
            compare(f"line {index} total", row.get("total"), item.total)
    compare("subtotal", payload.get("subtotal"), invoice.subtotal)
    compare("tax", payload.get("tax"), invoice.tax)
    compare("total", payload.get("total"), invoice.total)
    return tuple(found)


# --- the numbered ledger --------------------------------------------------------------------------

def check_number(number: object) -> str:
    """Validate an invoice number's shape, or refuse it.

    A number is a reference a client quotes back; one that does not match the sequence is worse than
    no invoice, because it cannot be reconciled.
    """
    text = str(number or "").strip().upper()
    if not _NUMBER.match(text):
        raise InvoiceError(f"{number!r} is not an invoice number like {NUMBER_PREFIX}-2026-0001.")
    return text


@dataclass
class Ledger:
    """Append-only, numbered, owner-only.

    JSON lines rather than a database: an invoice ledger has to be readable by a person and by
    whatever accounting software they already use, and the smallest thing that achieves that is a
    file of one object per line. Written through ``void.security.ownership`` at 0600, because this
    file names the owner's clients and what each was charged.
    """

    path: Path
    prefix: str = NUMBER_PREFIX
    _entries: list[dict] = field(default_factory=list, repr=False)

    @classmethod
    def from_config(cls, config) -> "Ledger":
        """The ledger under the owner's own state directory, which is already created owner-only."""
        state = Path(config.state_dir())
        try:
            prefix = str(config.get("billing.invoice_prefix", NUMBER_PREFIX) or NUMBER_PREFIX)
        except Exception:                       # noqa: BLE001
            prefix = NUMBER_PREFIX
        cleaned = re.sub(r"[^A-Z0-9]", "", prefix.upper()) or NUMBER_PREFIX
        return cls(path=state / LEDGER_NAME, prefix=cleaned)

    # --- reading ----------------------------------------------------------------------------------

    def entries(self) -> tuple[dict, ...]:
        """Every row, oldest first. A malformed line is skipped and logged, never guessed at."""
        rows: list[dict] = []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise InvoiceError(f"The invoice ledger could not be read: {exc}") from exc
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                _log.warning("INVOICE_LEDGER_LINE_UNREADABLE")
                continue
            if isinstance(row, dict) and row.get("number"):
                rows.append(row)
        return tuple(rows)

    def next_number(self, *, year: int | None = None) -> str:
        """The next number in sequence for the year, gap-free and never reused.

        Derived from the ledger itself rather than from a counter kept somewhere else: two sources
        of truth about invoice numbers is how an invoice number gets issued twice.
        """
        this_year = int(year or time.strftime("%Y"))
        highest = 0
        for row in self.entries():
            match = _NUMBER.match(str(row.get("number") or "").upper())
            if match and int(match.group("year")) == this_year:
                highest = max(highest, int(match.group("seq")))
        return f"{self.prefix}-{this_year}-{highest + 1:04d}"

    # --- writing ----------------------------------------------------------------------------------

    def record(self, invoice: Invoice) -> dict:
        """Append one invoice. Refuses to reuse a number that is already in the ledger."""
        existing = {str(row.get("number") or "").upper() for row in self.entries()}
        if invoice.number.upper() in existing:
            raise InvoiceError(f"{invoice.number} is already in the ledger; numbers are never reused.")
        row = invoice.as_dict()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            ownership.restrict(self.path.parent, directory=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            ownership.restrict(self.path)
        except OSError as exc:
            raise InvoiceError(f"The invoice was NOT recorded: {exc}") from exc
        return row


# --- the document ---------------------------------------------------------------------------------

def plan_for(invoice: Invoice, kind: str = "pdf"):
    """The invoice as an :class:`void.artifacts.ArtifactPlan`.

    Reuses the existing artifact layer rather than adding a second generator - and gets its honesty
    rule for free, because ``void.artifacts`` re-opens every file it writes and reports what it
    actually found rather than assuming the write worked.
    """
    from void.artifacts import ArtifactPlan, Section

    money = f"{invoice.currency} "
    lines = [f"{item.description} — {item.quantity} × {money}{item.unit_price} = "
             f"{money}{item.total}" for item in invoice.items]
    totals = [f"Subtotal: {money}{invoice.subtotal}"]
    if invoice.tax_rate:
        totals.append(f"Tax at {invoice.tax_rate * 100:g}%: {money}{invoice.tax}")
    totals.append(f"Total due: {money}{invoice.total}")

    sections = [
        Section(heading=f"Invoice {invoice.number}",
                body=f"Issued {invoice.issued} to {invoice.client}."),
        Section(heading="Work", bullets=tuple(lines)),
        Section(heading="Totals", bullets=tuple(totals)),
    ]
    if invoice.notes:
        sections.append(Section(heading="Notes", body=invoice.notes))
    if invoice.discrepancies:
        # On the document, not just in a log. A model that cannot add up is a fact about this
        # invoice, and the owner should see it before the invoice goes anywhere.
        sections.append(Section(
            heading="Recomputed",
            body="Every figure above was computed from the quantities and unit prices. "
                 "These differed from what was first supplied and were overridden:",
            bullets=tuple(d.rendered() for d in invoice.discrepancies)))
    return ArtifactPlan(title=f"Invoice {invoice.number} — {invoice.client}",
                        kind=kind, sections=tuple(sections))


@dataclass(frozen=True)
class Issued:
    """What issuing an invoice actually produced. ``delivered`` is always false - see the module docstring."""

    invoice: Invoice
    document: bytes
    #: What re-opening the document actually found, in the format's own units - pages for a PDF.
    #: Taken from void.artifacts' inspection rather than assumed, which is the point of inspecting.
    units: int = 0
    unit_name: str = "section"
    delivered: bool = False
    #: Why not. There is no transport wired, and saying so is the honest answer.
    delivery: str = ("Nothing was sent. V.O.I.D has no invoice transport configured, so the "
                     "document exists and the delivery does not.")


def issue(payload: dict, *, ledger: Ledger, kind: str = "pdf", issued: str | None = None,
          currency: str = "GBP", default_tax_rate: str = "0") -> Issued:
    """The whole engine: recompute, number, render, verify, record - in that order.

    The order matters. The document is rendered and **re-opened** before the ledger is written, so a
    number is never consumed by an invoice that could not be produced; and the ledger is written
    before this returns, so an invoice that reached the owner is always one that was recorded.
    """
    from void.artifacts import generate, inspect_artifact

    invoice = build(payload, number=ledger.next_number(), issued=issued, currency=currency,
                    default_tax_rate=default_tax_rate)
    plan = plan_for(invoice, kind=kind)
    document = generate(plan)
    look = inspect_artifact(document, kind, plan)
    if not look.ok:
        raise InvoiceError(f"The invoice document could not be verified: {look.detail}")
    ledger.record(invoice)
    return Issued(invoice=invoice, document=document, units=look.units,
                  unit_name=look.unit_name)
