"""Response timing: the work the engine must NOT do.

Measured in the response-timing audit (docs/RESPONSE_TIMING_AUDIT_2026-09-28.md). Routing was already correct -
local commands reach no provider - so what this pins is the avoidable work that measurement found:

  * concurrent callers each ran a FULL ~1.6 s application discovery (four threads, four discoveries), because the
    startup prewarm runs alongside the first command by design;
  * a lookup arriving mid-rebuild could see a complete-looking catalog with an empty index, and since a miss is now
    answered "I can't find it on this machine" that is a WRONG answer, not a slow one;
  * an aliased launch scanned PATH twice for the same candidate (19.6 ms per scan here).
"""
import threading
import time

import pytest

from void.actions.computer import AppCatalog, WindowsBackend


class _SlowBackend(WindowsBackend):
    """Discovery that takes a measurable moment, like the real AppsFolder enumeration."""

    def __init__(self, delay=0.15, apps=None, changing_fingerprint=False):
        self.delay = delay
        self._apps = apps or [{"name": "WhatsApp", "kind": "exe", "target": __file__}]
        self.discoveries = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self.checks = 0
        self._changing = changing_fingerprint
        self._lock = threading.Lock()

    def discover_apps(self):
        with self._lock:
            self.discoveries += 1
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            time.sleep(self.delay)
            return [dict(a) for a in self._apps]
        finally:
            with self._lock:
                self.concurrent -= 1

    def discovery_fingerprint(self):
        self.checks += 1
        return f"fp-{self.checks}" if self._changing else "fp"


# --- one discovery, however many callers ---------------------------------------------------------

def test_callers_arriving_together_share_one_discovery():
    """Four threads each ran a full ~1.6 s discovery of the same machine before this was coordinated."""
    be = _SlowBackend()
    cat = AppCatalog(be)
    threads = [threading.Thread(target=cat.entries) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert be.discoveries == 1, f"discovery ran {be.discoveries} times"
    assert be.max_concurrent == 1
    assert cat.builds == 1
    assert len(cat.entries()) == 1


def test_a_waiting_caller_gets_the_finished_catalog_not_an_empty_one():
    be = _SlowBackend()
    cat = AppCatalog(be)
    seen = []
    threads = [threading.Thread(target=lambda: seen.append(len(cat.entries()))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == [1, 1, 1, 1], seen


def test_the_startup_prewarm_and_a_first_command_do_not_both_discover():
    """Exactly the production collision: _prewarm_apps calls entries() on a thread while a command may arrive."""
    be = _SlowBackend(delay=0.25)
    cat = AppCatalog(be)
    prewarm = threading.Thread(target=cat.entries, daemon=True)
    prewarm.start()
    time.sleep(0.05)                                  # the command lands mid-build
    assert cat.resolve_name("whatsapp").entry is not None
    prewarm.join(timeout=5)
    assert be.discoveries == 1


# --- a rebuild is never observable half-done ------------------------------------------------------

def test_a_lookup_during_a_rebuild_never_misses_an_installed_application():
    """The consequence that matters: a transient index gap is spoken as "I can't find it on this machine"."""
    apps = [{"name": f"Application {i}", "kind": "exe", "target": f"{__file__}#{i}"} for i in range(60)]
    apps.append({"name": "WhatsApp", "kind": "exe", "target": __file__})
    be = _SlowBackend(delay=0.01, apps=apps, changing_fingerprint=True)
    cat = AppCatalog(be, ttl_s=0.001)                 # every lookup re-checks, and the signal always "changed"
    cat.entries()

    wrong = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            m = cat.resolve_name("whatsapp")
            if m.entry is None or m.reason:
                wrong.append(m.reason or "missing")
            time.sleep(0.001)

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
    for t in readers:
        t.start()
    time.sleep(1.0)
    stop.set()
    for t in readers:
        t.join(timeout=2)

    assert be.discoveries > 1, "the test did not actually rebuild; it proves nothing"
    assert wrong == [], f"an installed application was reported as {set(wrong)} during a rebuild"


def test_the_index_is_published_only_once_it_is_complete():
    """A non-None _entries must mean the indexes behind it are ready, because that is what readers rely on."""
    be = _SlowBackend()
    cat = AppCatalog(be)
    cat.entries()
    assert cat._entries is not None
    for e in cat._entries:
        assert cat.resolve_name(e.name).entry is not None


def test_invalidate_is_safe_while_others_are_reading():
    be = _SlowBackend(delay=0.01)
    cat = AppCatalog(be)
    cat.entries()
    errors = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                cat.resolve_name("whatsapp")
            except Exception as exc:                  # noqa: BLE001
                errors.append(exc)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    for _ in range(20):
        cat.invalidate()
        cat.entries()
    stop.set()
    t.join(timeout=2)
    assert errors == []


def test_entries_is_published_after_the_indexes_it_stands_for():
    """The ordering IS the invariant.

    A reader treats a non-None ``_entries`` as "the indexes behind this are ready" - that is what makes lock-free
    reads safe, and the fast path depends on staying lock-free (0.02 ms). The window in which the old ordering
    exposed an empty index was the index-population loop, roughly 0.2 ms, so a timing test catches it about once
    in 346 rebuilds. Pinning the order is deterministic; pinning the timing is not.
    """
    import inspect
    src = inspect.getsource(AppCatalog._build)
    published = src.index("self._entries = entries")
    for index_attr in ("self._by_norm, self._by_squash, self._by_publisher = by_norm",
                       "self._by_sound, self._words = by_sound, words_index",
                       "self._by_id = by_id"):
        assert index_attr in src, index_attr
        assert src.index(index_attr) < published, (
            f"{index_attr!r} is assigned AFTER self._entries; a reader can then see a complete-looking "
            f"catalog whose index is empty, and answer 'I can't find it on this machine' for an "
            f"application that is installed")
    # and the indexes must be built into locals first, never mutated in place on the live object
    assert "by_norm: dict" in src and "by_norm.setdefault" in src


def test_a_ttl_refresh_is_not_performed_twice_by_concurrent_callers():
    """The first build is not the only one that can collide: so can a refresh once the TTL has expired."""
    be = _SlowBackend(delay=0.15, changing_fingerprint=True)
    cat = AppCatalog(be, ttl_s=0.05)
    cat.entries()                                     # first build
    assert be.discoveries == 1
    time.sleep(0.10)                                  # the TTL has now expired for everybody

    threads = [threading.Thread(target=cat.entries) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert be.discoveries == 2, f"the refresh ran {be.discoveries - 1} times, not once"
    assert be.max_concurrent == 1


# --- a launch does not scan PATH twice for the same candidate -------------------------------------

def test_an_aliased_launch_scans_path_once_per_candidate(tmp_path, monkeypatch):
    """shutil.which walks every PATH directory - 19.6 ms for "code" on this machine - and it was called twice."""
    import void.actions.apps as apps_mod
    from void.actions.apps import AppActions
    from void.actions.files import FileActions

    looked_up = []
    exe = tmp_path / "code.exe"
    exe.write_text("stub")
    monkeypatch.setattr(apps_mod.shutil, "which",
                        lambda c: (looked_up.append(c), str(exe) if c == "code" else None)[1])
    monkeypatch.setattr(apps_mod.subprocess, "Popen", lambda argv, *a, **k: None)

    aa = AppActions(FileActions([tmp_path]))
    assert aa.launch_app("vscode").ok
    assert looked_up == ["code"], looked_up        # one scan, for the one candidate that matched


def test_a_failing_alias_still_tries_every_candidate_once(tmp_path, monkeypatch):
    import void.actions.apps as apps_mod
    from void.actions.apps import AppActions, _APP_ALIASES
    from void.actions.files import FileActions

    looked_up = []
    monkeypatch.setattr(apps_mod.shutil, "which", lambda c: looked_up.append(c) and None)
    aa = AppActions(FileActions([tmp_path]))
    r = aa.launch_app("vscode")
    assert not r.ok and "no executable" in r.summary
    assert looked_up == list(_APP_ALIASES["vscode"]), looked_up


# --- and the routing the audit confirmed was already right ---------------------------------------

def test_a_local_command_reaches_no_provider_at_all(tmp_path):
    """The audit's central question: 48 local executions made 0 model and 0 Ollama calls."""
    from void.app import Assistant
    from void.config import Config
    from void.providers.base import LLMProvider, LLMResponse
    from void.providers.registry import ProviderRegistry
    from tests.test_computer import FakeBackend, _exe
    from void.actions.apps import AppActions
    from void.actions.files import FileActions
    from void.actions.registry import ToolRegistry
    from void.core.fast_path import FastPath
    from void.security.protected import EngineProtected

    class _Counting(LLMProvider):
        def __init__(self, name):
            self.name = name
            self.calls = 0

        def available(self):
            return True

        def generate(self, messages, tools=None):
            self.calls += 1
            return LLMResponse(text="(answer)")

    home = tmp_path / "home"
    (home / "ws").mkdir(parents=True)
    cfg = Config({"app": {"state_dir": str(tmp_path / ".void")}, "memory": {"enabled": False},
                  "security": {"allowed_roots": [str(home / "ws")]}})
    a = Assistant(config=cfg)
    be = FakeBackend(apps=[{"name": "Opera GX Browser", "kind": "lnk", "target": _exe(tmp_path, "o.lnk")}])
    catalog = AppCatalog(be)
    launched = []
    fa = FileActions([home / "ws"], engine_protected=EngineProtected.default(state_dir=cfg.state_dir()))
    a.tools = ToolRegistry()
    a.tools.register_all(AppActions(fa, catalog=catalog,
                                    launcher=lambda k, t: launched.append((k, t))).tools())
    a._fast = FastPath(catalog)
    gemini, ollama = _Counting("gemini"), _Counting("local")
    a.providers = ProviderRegistry({"gemini": gemini, "local": ollama}, ["gemini", "local"])

    for _ in range(8):
        assert a.run("open opera gx").status == "completed"
    assert len(launched) == 8
    assert gemini.calls == 0 and ollama.calls == 0
