"""``python -m void memory ...``: the owner's window onto, and control of, persistent memory.

Everything here is typed by the owner at the command line, so writes made here are the
owner's own (``active``). Exit codes: 0 ok, 1 not found / rejected, 2 memory unavailable.
"""
from __future__ import annotations

import sys
import time

from void.memory.crypto import MemoryUnavailable
from void.memory.service import MemoryService

FORGET_LIMITS = ("Forgotten items are deleted from V.O.I.D's memory database (rows removed, file vacuumed with "
                 "secure_delete). Not covered: SSD wear-levelling, Windows shadow copies/backups made earlier, "
                 "text already sent to a cloud model, or copies in process memory/page file.")


def _fmt_time(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def _line(it) -> str:
    text = it.text if it.readable else "[unreadable: failed integrity check]"
    flag = " [sensitive]" if it.sensitivity == "sensitive" else ""
    return f"{it.id}  {it.status:<11} {it.kind:<10} {it.origin:<15}{flag} {text}"


def _reject_text(reason: str | None) -> str:
    from void.memory.intent import _reject_message
    return _reject_message(reason or "")


def run(args, config) -> int:
    service = MemoryService.from_config(config)
    try:
        return _dispatch(service, args)
    except MemoryUnavailable as exc:
        print(f"Memory is unavailable [{exc.code}]: {exc}")
        return 2


def _dispatch(svc: MemoryService, a) -> int:
    act = a.memory_action
    if act == "status":
        state = svc.verify_state()
        n = len(svc.list()) if state == "ok" else 0
        print(f"memory database: {svc.path}\nstate: {state}\nitems (excluding superseded): {n}")
        return 0
    if act == "list":
        items = svc.list(include_superseded=a.all, statuses=("proposed", "quarantined") if a.pending else None)
        if not items:
            print("No memories.")
            return 0
        for it in items:
            print(_line(it))
        return 0
    if act == "show":
        got = svc.show(a.id)
        if got is None:
            print(f"No such memory: {a.id}")
            return 1
        it, events = got
        print(_line(it))
        print(f"why I have this: origin={it.origin}, created {_fmt_time(it.created_at)}, updated {_fmt_time(it.updated_at)}, "
              f"used {it.use_count} time(s), cloud_ok={bool(it.cloud_ok)}, source_task={it.source_task_id or '-'}")
        if it.supersedes_id:
            print(f"replaces: {it.supersedes_id}")
        if it.expires_at:
            print(f"expires: {_fmt_time(it.expires_at)} (use `memory pin {it.id}` to keep)")
        for ts, _iid, action, actor in events:
            print(f"  {_fmt_time(ts)}  {action}  by {actor}")
        return 0
    if act == "search":
        hits = svc.retrieve(a.query)
        if not hits:
            print("No relevant memories.")
            return 0
        for it in hits:
            print(_line(it))
        return 0
    if act == "remember":
        res = svc.remember(a.text, channel="cli", kind=a.kind, pin=a.pin)
        if res.status == "rejected":
            print(f"Not stored: {_reject_text(res.reason)}.")
            return 1
        print("Already remembered." if res.status == "duplicate" else f"Remembered as {res.item.id}.")
        if res.similar_ids:
            print("Similar memories exist: " + ", ".join(res.similar_ids) + " (use `memory correct <id> \"...\"` to replace one).")
        return 0
    if act == "correct":
        res = svc.correct(a.id, a.text)
        if res.status in ("not_found",):
            print(f"No such (current) memory: {a.id}")
            return 1
        if res.status == "rejected":
            print(f"Not stored: {_reject_text(res.reason)}.")
            return 1
        print(f"Corrected: {res.item.id} replaces {res.replaced_id} (the old version is kept as 'superseded' "
              f"until you forget it).")
        return 0
    if act == "forget":
        if a.all:
            if not a.yes:
                print("This deletes ALL memory. Re-run with --yes to confirm.")
                return 1
            print(f"Forgot {svc.forget_all()} item(s).")
        else:
            if not a.id:
                print("Give an id, or --all --yes.")
                return 1
            n = svc.forget(a.id)
            if not n:
                print(f"No such memory: {a.id}")
                return 1
            print(f"Forgot {n} item(s) (the item and its superseded versions).")
        print(FORGET_LIMITS)
        return 0
    if act in ("accept", "reject"):
        res = svc.accept(a.id) if act == "accept" else svc.reject(a.id)
        if res.status == "not_found":
            print(f"No pending memory: {a.id}")
            return 1
        if res.status == "rejected" and act == "accept":
            print(f"Not accepted: {_reject_text(res.reason)}.")
            return 1
        print(f"{'Accepted' if act == 'accept' else 'Rejected and deleted'}: {a.id}")
        return 0
    if act == "review":
        pending = svc.pending()
        if not pending:
            print("Nothing to review.")
            return 0
        for it in pending:
            warn = "  ** QUARANTINED: it came from a run that read untrusted content, or reads like a permission claim **" \
                if it.status == "quarantined" else ""
            print(_line(it) + warn)
        if not sys.stdin.isatty():
            print("\nUse `memory accept <id>` or `memory reject <id>`.")
            return 0
        for it in pending:
            ans = input(f"Accept {it.id}? [y/N/r=reject] ").strip().lower()
            if ans == "y":
                print(svc.accept(it.id).status)
            elif ans == "r":
                svc.reject(it.id)
        return 0
    if act == "pin":
        ok = svc.pin(a.id)
        print("Pinned (it will not expire)." if ok else f"No such memory: {a.id}")
        return 0 if ok else 1
    if act == "purge":
        print(f"Purged {svc.purge_expired()} expired item(s).")
        return 0
    print("Usage: python -m void memory status|list|show|search|remember|correct|forget|review|accept|reject|pin|purge")
    return 1


def add_parser(sub) -> None:
    p = sub.add_parser("memory", help="Persistent memory: list/show/remember/correct/forget/review")
    m = p.add_subparsers(dest="memory_action")
    m.add_parser("status", help="Where memory lives and whether it can be opened")
    p_list = m.add_parser("list", help="List memories")
    p_list.add_argument("--all", action="store_true", help="Include superseded (replaced) versions")
    p_list.add_argument("--pending", action="store_true", help="Only proposals awaiting review / quarantined")
    p_show = m.add_parser("show", help="Show one memory and why V.O.I.D has it")
    p_show.add_argument("id")
    p_search = m.add_parser("search", help="Preview what would be recalled for a query")
    p_search.add_argument("query")
    p_rem = m.add_parser("remember", help="Store a memory (yours; active immediately)")
    p_rem.add_argument("text")
    p_rem.add_argument("--kind", choices=["preference", "fact", "episode"], default=None)
    p_rem.add_argument("--pin", action="store_true", help="Never expire (matters for episodes)")
    p_cor = m.add_parser("correct", help="Replace a memory with a corrected version")
    p_cor.add_argument("id")
    p_cor.add_argument("text")
    p_for = m.add_parser("forget", help="Delete a memory (and its superseded versions), or --all --yes")
    p_for.add_argument("id", nargs="?")
    p_for.add_argument("--all", action="store_true")
    p_for.add_argument("--yes", action="store_true")
    m.add_parser("review", help="Review proposals / quarantined items")
    for name, hlp in (("accept", "Accept a proposal (becomes active, owner_confirmed)"),
                      ("reject", "Reject and delete a proposal"), ("pin", "Keep a memory from expiring")):
        sp = m.add_parser(name, help=hlp)
        sp.add_argument("id")
    m.add_parser("purge", help="Delete expired items now")
