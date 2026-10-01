"""The MCP adapter: does every call land on V.O.I.D's existing capability and security path?

This is the file that pins CONVERGENCE. The adapter owns no resolver, no path policy and no authorization; what it
does is translate typed MCP arguments into calls on capabilities V.O.I.D already has, through ``Agent.invoke_tool``
- the same funnel the agent loop uses. Two things must therefore be true of every side-effecting call:

  * it appears in ``Agent._run_call``, so the kill switch, the per-call risk level, ``RiskGate.authorize``, the audit
    line and the telemetry event all happened;
  * the arguments that reach the underlying tool are V.O.I.D's own (a catalog ``app_id``), never the caller's string.

No real application is launched and no real path is opened: the rig injects ``AppActions``' launcher seam and
narrows ``allowed_roots`` to a temporary directory.
"""
import pytest

from tests.test_computer import FakeBackend, _exe
from void.actions.apps import AppActions
from void.actions.computer import AppCatalog, ComputerActions
from void.actions.files import FileActions
from void.actions.registry import ToolRegistry
from void.app import Assistant
from void.config import Config
from void.core.agent import Agent
from void.mcp.adapter import VoidMcpAdapter
from void.mcp.errors import ErrorCode
from void.providers.base import LLMProvider, LLMResponse
from void.providers.registry import ProviderRegistry
from void.security.protected import EngineProtected
from void.security.risk import RiskGate

APPS = [("WhatsApp", "whatsapp.exe"), ("Visual Studio Code", "code.exe"), ("Discord", "discord.exe"),
        ("Aurora One", "a1.exe"), ("Aurora Two", "a2.exe")]


class _Counting(LLMProvider):
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def available(self):
        return True

    def generate(self, messages, tools=None):
        self.calls += 1
        return LLMResponse(text="(the model answered)")


class Rig:
    """A real Assistant on test seams, plus the recorders the assertions need."""

    def __init__(self, adapter, launched, opened, gemini, local, root, calls):
        self.adapter = adapter
        self.launched = launched
        self.opened = opened
        self.gemini = gemini
        self.local = local
        self.root = root
        self.calls = calls          # every (name, arguments) that reached Agent._run_call

    @property
    def providers_used(self):
        return self.gemini.calls + self.local.calls


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Builds the adapter over a fake catalog, an injected launcher and a narrow allowed root.

    ``subprocess`` is NOT patched globally: doing so breaks ``asyncio.windows_utils``, which subclasses
    ``subprocess.Popen`` at import time. The launcher seam ``AppActions`` already exposes is the correct way in.
    """
    def make(apps=APPS, risk_gate=None, confirm_at=None):
        root = tmp_path / "allowed"
        root.mkdir(exist_ok=True)
        cfg = Config({"app": {"state_dir": str(tmp_path / ".void")}, "memory": {"enabled": False},
                      "security": {"allowed_roots": [str(root)]}})
        a = Assistant(config=cfg)
        if risk_gate is not None:
            a.risk_gate = risk_gate
        elif confirm_at is not None:
            a.risk_gate = RiskGate(confirm_at_or_above=confirm_at)

        backend = FakeBackend(apps=[{"name": n, "kind": "exe", "target": _exe(tmp_path, t)} for n, t in apps])
        catalog = AppCatalog(backend)
        launched, opened = [], []
        fa = FileActions([root], engine_protected=EngineProtected.default(state_dir=cfg.state_dir()))
        monkeypatch.setattr("void.actions.apps.os.startfile", lambda t: opened.append(str(t)), raising=False)
        a.tools = ToolRegistry()
        a.tools.register_all(fa.tools())
        a.tools.register_all(AppActions(fa, catalog=catalog,
                                        launcher=lambda kind, target: launched.append(target)).tools())
        a.tools.register_all(ComputerActions(backend, catalog).tools())

        gemini, local = _Counting("gemini"), _Counting("local")
        a.providers = ProviderRegistry({"gemini": gemini, "local": local}, ["gemini", "local"])

        calls = []
        real = Agent._run_call

        def spy(self, name, arguments, owner_decision=None):
            calls.append((name, dict(arguments or {})))
            return real(self, name, arguments, owner_decision)
        monkeypatch.setattr(Agent, "_run_call", spy)

        return Rig(VoidMcpAdapter(assistant=a), launched, opened, gemini, local, root, calls)
    return make


# --- convergence ----------------------------------------------------------------------------------

def test_launching_goes_through_the_existing_funnel_with_the_catalogs_own_identifier(rig):
    """The whole point: MCP does not launch anything, it asks V.O.I.D's launch capability to."""
    r = rig()
    out = r.adapter.launch_application("whatsapp")
    assert out.ok and out.launched is not None and out.launched.name == "WhatsApp"
    assert ("launch_app", {"name": out.launched.app_id}) in r.calls
    assert out.launched.app_id.startswith("app-"), "the caller's string reached the launcher"
    assert len(r.launched) == 1


def test_opening_a_path_goes_through_the_existing_funnel(rig):
    r = rig()
    target = r.root / "notes"
    target.mkdir()
    out = r.adapter.open_path(str(target))
    assert out.ok, out.error
    assert ("open_path", {"target": str(target)}) in r.calls


def test_reads_go_through_the_funnel_too(rig):
    """A read is gated and audited exactly as find_app is when V.O.I.D itself runs it."""
    r = rig()
    r.adapter.find_application("whatsapp")
    r.adapter.list_applications(limit=3)
    assert [n for n, _ in r.calls] == ["find_app", "find_app"]


def test_every_capability_tool_reaches_only_existing_void_tools(rig):
    r = rig()
    r.adapter.list_applications(limit=2)
    r.adapter.find_application("discord")
    r.adapter.launch_application("discord")
    target = r.root / "x"
    target.mkdir()
    r.adapter.open_path(str(target))
    assert {n for n, _ in r.calls} <= {"find_app", "launch_app", "open_path"}


def test_no_model_is_ever_consulted(rig):
    r = rig()
    r.adapter.list_applications()
    r.adapter.find_application("whatsapp")
    r.adapter.launch_application("whatsapp")
    r.adapter.get_system_info()
    r.adapter.get_void_status()
    assert r.providers_used == 0


def test_the_agent_the_adapter_builds_cannot_reach_a_model(rig):
    """Structural, not behavioural: the provider it is given refuses rather than answering."""
    from void.providers.base import ProviderUnavailable
    r = rig()
    agent = r.adapter._agent()
    assert agent.provider.available() is False
    with pytest.raises(ProviderUnavailable):
        agent.provider.generate([{"role": "user", "content": "hi"}])


# --- resolution semantics (V.O.I.D's, not a second resolver) --------------------------------------

def test_an_exact_name_resolves(rig):
    out = rig().adapter.find_application("WhatsApp")
    assert out.ok and out.resolved.name == "WhatsApp"


def test_the_existing_hierarchy_still_applies(rig):
    """"whats app" -> WhatsApp is the spacing tier of the existing resolver, not anything added here."""
    out = rig().adapter.find_application("whats app")
    assert out.ok and out.resolved.name == "WhatsApp"


def test_an_ambiguous_name_is_reported_and_never_chosen(rig):
    r = rig()
    out = r.adapter.find_application("aurora")
    assert not out.ok
    assert out.error.code == ErrorCode.AMBIGUOUS.value
    assert {c.name for c in out.candidates} == {"Aurora One", "Aurora Two"}
    assert out.resolved is None


def test_an_ambiguous_name_launches_nothing(rig):
    r = rig()
    out = r.adapter.launch_application("aurora")
    assert not out.ok and out.error.code == ErrorCode.AMBIGUOUS.value
    assert r.launched == []
    assert not any(n == "launch_app" for n, _ in r.calls)


def test_an_unknown_name_is_not_found_and_launches_nothing(rig):
    r = rig()
    out = r.adapter.launch_application("definitely not installed")
    assert not out.ok and out.error.code == ErrorCode.NOT_FOUND.value
    assert r.launched == []


# --- bounded results ------------------------------------------------------------------------------

def test_results_are_bounded_by_the_underlying_capability(rig):
    from void.mcp import schemas
    many = [(f"App {i:03d}", f"a{i}.exe") for i in range(120)]
    r = rig(apps=many)
    out = r.adapter.list_applications(limit=1000)
    assert out.ok
    assert len(out.applications) <= schemas.MAX_APPLICATIONS
    assert out.truncated is True


def test_the_order_does_not_depend_on_the_limit(rig):
    r = rig(apps=[(f"App {i:03d}", f"a{i}.exe") for i in range(30)])
    first_of = {}
    for limit in (2, 5, 9):
        out = r.adapter.list_applications(limit=limit)
        assert len(out.applications) == limit
        first_of[limit] = [x.name for x in out.applications[:2]]
    assert len({tuple(v) for v in first_of.values()}) == 1


def test_the_same_call_twice_gives_the_same_answer(rig):
    r = rig()
    a = r.adapter.list_applications(limit=4)
    b = r.adapter.list_applications(limit=4)
    assert [x.app_id for x in a.applications] == [x.app_id for x in b.applications]


@pytest.mark.parametrize("limit", [0, -1, "x", None, 2.5, [], {}])
def test_a_bad_limit_is_invalid_input_not_a_crash(rig, limit):
    out = rig().adapter.list_applications(limit=limit)
    if out.ok:
        # 2.5 is coercible to 2; anything coercible is fine as long as it stays bounded.
        assert len(out.applications) <= 2
    else:
        assert out.error.code == ErrorCode.INVALID_INPUT.value


def test_listing_has_no_side_effects(rig):
    r = rig()
    r.adapter.list_applications()
    r.adapter.find_application("whatsapp")
    assert r.launched == [] and r.opened == []


# --- status tools ---------------------------------------------------------------------------------

def test_system_info_reports_the_machine_without_identifying_it(rig):
    out = rig().adapter.get_system_info()
    assert out.ok and out.os_family and out.python_version
    blob = out.model_dump_json()
    import getpass
    import platform
    for secret in (platform.node(), getpass.getuser()):
        if secret:
            assert secret.lower() not in blob.lower(), "system info identifies the machine or the user"


def test_void_status_is_bounded_and_read_only(rig):
    r = rig()
    out = r.adapter.get_void_status()
    assert out.ok
    assert out.kill_switch_engaged is False
    assert isinstance(out.active_tasks, int) and out.active_tasks >= 0
    assert len(out.health) <= 40
    assert r.launched == [] and r.opened == []
    assert not any(n in ("launch_app", "open_path") for n, _ in r.calls)


def test_void_status_does_not_serialise_unbounded_task_history(rig, monkeypatch):
    """A status call must not read the whole task table to count what is active."""
    from void.mcp import adapter as adapter_mod
    r = rig()
    seen = {}

    def fake_list(status=None, limit=50):
        seen["limit"] = limit
        return []
    monkeypatch.setattr(r.adapter.assistant.store, "list", fake_list)
    r.adapter.get_void_status()
    assert seen["limit"] == adapter_mod.MAX_TASKS_SCANNED
    assert adapter_mod.MAX_TASKS_SCANNED <= 1000


def test_void_status_reports_the_kill_switch(rig):
    r = rig()
    r.adapter.assistant.kill_switch.engage("test")
    assert r.adapter.get_void_status().kill_switch_engaged is True


def test_a_provider_whose_probe_raises_is_reported_unavailable_not_a_failure(rig):
    r = rig()

    class Exploding(_Counting):
        def available(self):
            raise RuntimeError("probe blew up")
    r.adapter.assistant.providers = ProviderRegistry({"boom": Exploding("boom")}, ["boom"])
    out = r.adapter.get_void_status()
    assert out.ok
    assert [(p.name, p.available) for p in out.providers] == [("boom", False)]


def test_status_tools_never_reach_a_model(rig):
    r = rig()
    r.adapter.get_system_info()
    r.adapter.get_void_status()
    assert r.providers_used == 0
