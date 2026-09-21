"""The narrow memory API (V2.0 P3): everything else in V.O.I.D talks to memory only through this.

remember / propose / accept / reject / correct / forget / pin / list / show / retrieve /
build_context. Memory is DATA: nothing here imports or calls RiskGate, the tool registry or
any authorization path, and nothing returned from here can grant a permission.

Privacy: this module logs and emits telemetry with counts, durations and content-free codes
ONLY - never memory text, queries or keys.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

from void import perf
from void.memory import context as ctx
from void.memory import policy
from void.memory.crypto import KeyringKeyProvider, MemoryUnavailable
from void.memory.index import BM25Index, tokenize
from void.memory.scope import OWNER_CHANNELS
from void.memory.store import MemoryItem, MemoryStore

_log = logging.getLogger(__name__)

DAY = 86400.0
SMALL_STORE = 5             # at most this many recallable items: a lexical miss falls back to the whole store
# Words that only say "recall" ("what do you REMEMBER about me", "what did I TELL you"): no content to match.
_RECALL_META = frozenset(tokenize("remember remind recall recalled know knew tell told say said ask asked mention mentioned "
                                  "store stored save saved note noted memory memories anything something everything"))
AMBIGUITY = 0.85           # a runner-up scoring above this fraction of the best is "not clearly one item"


@dataclass(frozen=True)
class Settings:
    max_items: int = ctx.MAX_ITEMS
    max_tokens: int = ctx.MAX_TOKENS
    voice_auto_accept: bool = False
    cloud_normal: bool = True
    proposal_ttl_days: float = 30.0
    episode_ttl_days: float = 90.0
    max_proposals_per_task: int = 3
    score_floor: float = 0.0

    def __post_init__(self):
        # Hard ceilings: no configuration or caller can recall more than 5 items / ~400 tokens.
        object.__setattr__(self, "max_items", max(0, min(int(self.max_items), ctx.MAX_ITEMS)))
        object.__setattr__(self, "max_tokens", max(0, min(int(self.max_tokens), ctx.MAX_TOKENS)))

    @classmethod
    def from_config(cls, cfg) -> "Settings":
        g = cfg.get
        return cls(max_items=min(int(g("memory.max_items", ctx.MAX_ITEMS)), ctx.MAX_ITEMS),
                   max_tokens=min(int(g("memory.max_context_tokens", ctx.MAX_TOKENS)), ctx.MAX_TOKENS),
                   voice_auto_accept=bool(g("memory.voice_auto_accept", False)),
                   cloud_normal=bool(g("memory.cloud_normal", True)),
                   proposal_ttl_days=float(g("memory.proposal_ttl_days", 30)),
                   episode_ttl_days=float(g("memory.episode_ttl_days", 90)),
                   max_proposals_per_task=int(g("memory.max_proposals_per_task", 3)),
                   score_floor=float(g("memory.score_floor", 0.0)))


@dataclass
class WriteResult:
    status: str                       # active|proposed|quarantined|rejected|duplicate|superseded|not_found|limit
    item: MemoryItem | None = None
    reason: str | None = None         # content-free code
    replaced_id: str | None = None
    similar_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def stored(self) -> bool:
        return self.status in ("active", "proposed", "quarantined", "superseded")


@dataclass
class ForgetOutcome:
    deleted: int
    ambiguous_ids: tuple[str, ...] = ()


class MemoryService:
    def __init__(self, path, *, key_provider=None, now=time.time, settings: Settings | None = None):
        self._now = now
        self.settings = settings or Settings()
        self._store = MemoryStore(path, key_provider or KeyringKeyProvider(), now=now)
        self._cache: tuple[int, BM25Index, dict[str, MemoryItem]] | None = None
        self._last_purge = 0.0

    @classmethod
    def from_config(cls, config, **kw) -> "MemoryService":
        # peek_state_dir never creates anything: the database is created lazily on first write.
        return cls(Path(config.peek_state_dir()) / "memory.sqlite", settings=Settings.from_config(config), **kw)

    @property
    def path(self) -> Path:
        return self._store.path

    # ------------------------------------------------------------------ telemetry
    def _measure(self, op: str, t0: float, n: int) -> None:
        if self._store.exists():            # no telemetry noise on installs that have no memory yet
            perf.emit("memory", op=op, n=n, duration_s=round(time.perf_counter() - t0, 6))

    # ------------------------------------------------------------------ retention
    def _expiry_for(self, status: str, kind: str, *, pinned: bool = False) -> float | None:
        if pinned:
            return None
        if status in ("proposed", "quarantined"):
            return self._now() + self.settings.proposal_ttl_days * DAY
        if kind == "episode":
            return self._now() + self.settings.episode_ttl_days * DAY
        return None

    def purge_expired(self) -> int:
        """Delete items past ``expires_at`` (unreviewed proposals 30 d, un-pinned episodes 90 d)."""
        n = self._store.delete_expired(self._now())
        if n:
            _log.info("MEMORY_PURGED count=%d", n)
        return n

    def _maybe_purge(self) -> None:
        if self._now() - self._last_purge >= 6 * 3600:
            self._last_purge = self._now()
            self.purge_expired()

    # ------------------------------------------------------------------ writes
    def _pending_or_active(self, statuses=("active",)) -> list[MemoryItem]:
        return [i for i in self._store.list(statuses) if i.readable]

    def _find_duplicate(self, text: str, statuses) -> MemoryItem | None:
        for it in self._pending_or_active(statuses):
            if policy.similarity(text, it.text) >= policy.DUPLICATE_SIMILARITY:
                return it
        return None

    def _similar_ids(self, text: str, statuses=("active",)) -> tuple[str, ...]:
        return tuple(i.id for i in self._pending_or_active(statuses) if 0.3 <= policy.similarity(text, i.text) < policy.DUPLICATE_SIMILARITY)

    def _store_decision(self, d: policy.Decision, *, actor: str, task_id=None, pin=False,
                        dedupe_statuses=("active",), supersedes_id=None) -> WriteResult:
        dup = self._find_duplicate(d.text, dedupe_statuses)
        if dup is not None:
            self._store.update(dup.id, actor, "duplicate")     # refresh, no second copy
            return WriteResult("duplicate", self._store.load(dup.id))
        item = self._store.insert(text=d.text, kind=d.kind, origin=d.origin, status=d.status,
                                  sensitivity=d.sensitivity, cloud_ok=d.cloud_ok, actor=actor,
                                  expires_at=self._expiry_for(d.status, d.kind, pinned=pin),
                                  supersedes_id=supersedes_id, source_task_id=task_id)
        return WriteResult(d.status, item, similar_ids=self._similar_ids(d.text) if d.status == "active" else ())

    def remember(self, text: str, *, channel: str = "cli", kind: str | None = None,
                 task_id: str | None = None, pin: bool = False) -> WriteResult:
        """An explicit statement by the owner. ``channel`` comes from V.O.I.D's own code."""
        t0 = time.perf_counter()
        d = policy.decide_owner(text, channel=channel, kind=kind,
                                voice_auto_accept=self.settings.voice_auto_accept,
                                cloud_normal=self.settings.cloud_normal)
        if not d.accepted:
            _log.info("MEMORY_REJECTED reason=%s", d.reason.split(":")[0])
            return WriteResult("rejected", reason=d.reason)
        res = self._store_decision(d, actor=channel, task_id=task_id, pin=pin,
                                   dedupe_statuses=("active",) if d.status == "active" else ("active", "proposed"))
        _log.info("MEMORY_WRITE status=%s", res.status)
        self._measure("write", t0, 1)
        return res

    def propose(self, text: str, *, kind: str | None = None, tainted: bool = True,
                task_id: str | None = None) -> WriteResult:
        """A model-originated suggestion. Structurally cannot return ``active``."""
        t0 = time.perf_counter()
        d = policy.decide_proposal(text, kind=kind, tainted=tainted, cloud_normal=self.settings.cloud_normal)
        if not d.accepted:
            _log.info("MEMORY_PROPOSAL_REJECTED reason=%s", d.reason.split(":")[0])
            return WriteResult("rejected", reason=d.reason)
        assert d.status in ("proposed", "quarantined")
        res = self._store_decision(d, actor="agent", task_id=task_id,
                                   dedupe_statuses=("active", "proposed", "quarantined"))
        _log.info("MEMORY_PROPOSAL status=%s", res.status)
        self._measure("write", t0, 1)
        return res

    def correct(self, item_id: str, new_text: str, *, channel: str = "cli") -> WriteResult:
        """Replace an item with a corrected version; the old one is kept as ``superseded``
        (provenance) until forgotten or purged, and is never retrieved."""
        old = self._store.load(item_id)
        if old is None or old.status == "superseded":
            return WriteResult("not_found")
        d = policy.decide_owner(new_text, channel=channel, kind=old.kind,
                                voice_auto_accept=self.settings.voice_auto_accept,
                                cloud_normal=self.settings.cloud_normal)
        if not d.accepted:
            return WriteResult("rejected", reason=d.reason)
        if d.status != "active":            # a non-owner channel may not replace trusted memory
            return WriteResult("rejected", reason="not_owner_channel")
        item = self._store.insert(text=d.text, kind=d.kind, origin="owner_stated", status="active",
                                  sensitivity=d.sensitivity, cloud_ok=d.cloud_ok, actor=channel,
                                  expires_at=self._expiry_for("active", d.kind), supersedes_id=old.id,
                                  source_task_id=old.source_task_id)
        self._store.update(old.id, channel, "superseded", status="superseded", expires_at=None)
        _log.info("MEMORY_CORRECTED")
        return WriteResult("superseded", item, replaced_id=old.id)

    def correct_by_query(self, new_text: str, *, channel: str = "cli") -> WriteResult:
        """"That's wrong, remember that I prefer Y": supersede the single best-matching active
        item; if it is not clearly one item, store the new statement and say what is similar."""
        if channel not in OWNER_CHANNELS:
            return self.remember(new_text, channel=channel)
        target, ambiguous = self._best_match(new_text)
        if target is None or ambiguous:
            res = self.remember(new_text, channel=channel)
            if ambiguous:
                res.similar_ids = tuple([target.id, *ambiguous])
            return res
        return self.correct(target.id, new_text, channel=channel)

    def accept(self, item_id: str) -> WriteResult:
        """Owner review: proposed/quarantined -> active (``owner_confirmed``)."""
        it = self._store.load(item_id)
        if it is None or it.status not in ("proposed", "quarantined"):
            return WriteResult("not_found")
        if not it.readable:
            return WriteResult("rejected", reason="unreadable")
        d = policy.decide_owner(it.text, channel="cli", kind=it.kind, cloud_normal=self.settings.cloud_normal)
        if not d.accepted:
            return WriteResult("rejected", reason=d.reason)
        new = self._store.update(it.id, "cli", "accept", status="active", origin="owner_confirmed",
                                 sensitivity=d.sensitivity, cloud_ok=d.cloud_ok,
                                 expires_at=self._expiry_for("active", it.kind))
        return WriteResult("active", new)

    def reject(self, item_id: str) -> WriteResult:
        it = self._store.load(item_id)
        if it is None or it.status not in ("proposed", "quarantined"):
            return WriteResult("not_found")
        self._store.delete(item_id, "cli", "reject")
        return WriteResult("rejected", reason="owner_rejected")

    def pin(self, item_id: str) -> bool:
        return self._store.update(item_id, "cli", "pin", expires_at=None) is not None

    # ------------------------------------------------------------------ forgetting
    def forget(self, item_id: str) -> int:
        """Hard-delete the item and every superseded version of it, then VACUUM."""
        n = self._store.delete(item_id, "cli")
        self._cache = None
        _log.info("MEMORY_FORGOT count=%d", n)
        return n

    def forget_all(self) -> int:
        n = self._store.delete_many(("active", "proposed", "quarantined", "superseded"), "cli", "forget_all")
        self._cache = None
        _log.info("MEMORY_FORGOT count=%d", n)
        return n

    def forget_matching(self, query: str) -> ForgetOutcome:
        target, ambiguous = self._best_match(query)
        if target is None:
            return ForgetOutcome(0, tuple(ambiguous))
        if ambiguous:
            return ForgetOutcome(0, tuple([target.id, *ambiguous]))
        return ForgetOutcome(self.forget(target.id))

    # ------------------------------------------------------------------ reads
    def verify_state(self) -> str:
        """'absent' (no database yet) or 'ok'; raises MemoryUnavailable if it cannot be opened safely."""
        return self._store.verify()

    def list(self, *, include_superseded: bool = False, statuses=None) -> list[MemoryItem]:
        self._maybe_purge()
        if statuses is None:
            statuses = ("active", "proposed", "quarantined") + (("superseded",) if include_superseded else ())
        return self._store.list(statuses)

    def pending(self) -> list[MemoryItem]:
        return self._store.list(("proposed", "quarantined"))

    def pending_matches(self, query: str) -> int:
        """How many awaiting-review items share a content word with ``query`` (a count only)."""
        qt = set(tokenize(query))
        generic = all(t in _RECALL_META for t in qt)          # "what do you remember about me?": any pending item counts
        return sum(1 for it in self._store.list(("proposed",)) if it.readable and (generic or qt & set(tokenize(it.text))))

    def show(self, item_id: str) -> tuple[MemoryItem, list[tuple]] | None:
        it = self._store.load(item_id)
        return (it, self._store.events(item_id)) if it else None

    def _active_index(self):
        gen = self._store.generation()
        if self._cache is None or self._cache[0] != gen:
            items = {i.id: i for i in self._store.list(("active",)) if i.readable}
            index = BM25Index({i: tokenize(it.text) for i, it in items.items()})
            self._cache = (gen, index, items)
        return self._cache[1], self._cache[2]

    def _ranked(self, query: str, *, for_cloud: bool = False,
                recent_fallback: bool = False) -> list[tuple[float, MemoryItem]]:
        qt = tokenize(query)
        if (not qt and not recent_fallback) or self._store.verify() == "absent":
            return []
        index, items = self._active_index()
        now = self._now()
        out = []
        for iid, s in (index.score(qt).items() if qt else ()):
            it = items[iid]
            if s <= self.settings.score_floor or (for_cloud and not it.cloud_ok):
                continue
            recency = math.exp(-max(0.0, now - it.updated_at) / (180 * DAY))
            out.append((s * (1.0 + 0.1 * recency + 0.02 * min(it.use_count, 10)), it))
        out.sort(key=lambda p: (-p[0], -p[1].updated_at, p[1].id))
        if not out and recent_fallback:
            return self._recent_candidates(qt, items, for_cloud)
        return out

    def _recent_candidates(self, qt, items, for_cloud) -> list[tuple[float, MemoryItem]]:
        """When nothing matches lexically for a MEMORY question (opt-in, memory-first route only):

        * a generic recall ("what do you remember about me?", "what did I tell you?") has no content word
          to match, so it is answered with the most recent memories;
        * in a tiny store (the MVP owner has a handful of memories) the whole store IS the candidate set and
          fits the budget, so a paraphrase with no shared word ("what project am I building?" vs "...an
          assistant for my laptop") still finds it. The model then judges relevance.
        Ranking is untouched; this is bounded by the same 5-item / 400-token caps."""
        pool = sorted((it for it in items.values() if not (for_cloud and not it.cloud_ok)),
                      key=lambda it: (-it.updated_at, it.id))
        generic = all(t in _RECALL_META for t in qt)
        return [(0.0, it) for it in pool] if (generic or len(pool) <= SMALL_STORE) else []

    def _best_match(self, text: str):
        ranked = self._ranked(text)
        if not ranked:
            return None, []
        query = set(tokenize(text))
        top_score, top = ranked[0]
        top_terms = query & set(tokenize(top.text))
        # Not clearly ONE item if a runner-up scores about as well, or matches exactly the same
        # query terms (e.g. both only share the generic word "prefer"): then do not guess.
        close = [it.id for s, it in ranked[1:]
                 if s > AMBIGUITY * top_score or (query & set(tokenize(it.text))) == top_terms]
        return top, close

    def retrieve(self, query: str, *, for_cloud: bool = False, limit: int | None = None,
                 recent_fallback: bool = False) -> list[MemoryItem]:
        t0 = time.perf_counter()
        self._maybe_purge()
        hits = [it for _s, it in self._ranked(query, for_cloud=for_cloud, recent_fallback=recent_fallback)][
            : min(limit or self.settings.max_items, self.settings.max_items)]
        self._measure("retrieve", t0, len(hits))
        return hits

    def build_context(self, query: str, *, for_cloud: bool = False,
                      recent_fallback: bool = False) -> ctx.MemoryContext | None:
        t0 = time.perf_counter()
        self._maybe_purge()
        hits = [it for _s, it in self._ranked(query, for_cloud=for_cloud, recent_fallback=recent_fallback)]
        block = ctx.render(hits, max_items=self.settings.max_items, max_tokens=self.settings.max_tokens)
        if block is not None:
            self._store.touch_used(list(block.ids))
        self._measure("context", t0, len(block.ids) if block else 0)
        return block
