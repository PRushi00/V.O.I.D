"""The bridge from MCP tool calls to V.O.I.D's existing capability layer.

This module is the whole adapter. It owns no capability, no resolver, no path policy and no authorization: every
side-effecting call is handed to ``Agent.invoke_tool``, which is the same funnel the agent loop uses, so the kill
switch, the per-call risk level, ``RiskGate.authorize``, memory-scope tainting, the audit line and the telemetry
event all happen exactly as they do for a model-proposed call.

    MCP tool call
        -> pydantic schema validation            (void/mcp/schemas.py, by the SDK)
        -> normalisation + argument rules here
        -> Agent.invoke_tool
             -> kill switch
             -> Tool.effective_risk
             -> RiskGate.authorize
             -> ToolRegistry.execute  -> the existing AppActions / FileActions backend
             -> audit log + perf event
        -> structured result

Three properties this module exists to guarantee:

**No model is ever consulted.** The agent it builds is given ``_NoProvider``, the same refuses-loudly provider the
deterministic fast path uses, so a bug cannot silently reach Gemini or Ollama to answer ``launch_application``.

**The caller cannot name a target V.O.I.D did not resolve.** ``launch_application`` accepts a human name, resolves it
through the existing ``AppCatalog``, and passes the catalog's own ``app_id`` onward. A path, a command line or an
arbitrary executable supplied by the caller cannot become the thing that runs, because the value handed to
``launch_app`` is never the caller's string.

**A security decision is reported, never softened.** A ``RiskGate`` denial becomes ``denied``; a protected location
becomes ``protected``; the kill switch becomes ``stopped``. None of them can become ``ok``.
"""
from __future__ import annotations

import logging
import platform
import re
import sys

from void.actions.computer import ComputerBackendError
from void.app import Assistant, _NoProvider
from void.core.agent import Agent
from void.core.kill_switch import StopRequested
from void.core.task import Status
from void.mcp import schemas
from void.mcp.errors import ErrorCode, McpError, VoidMcpError, error, sanitise
from void.mcp.schemas import (ApplicationInfo, ErrorInfo, FindApplicationResult, LaunchApplicationResult,
                              ListApplicationsResult, OpenPathResult, ProviderStatus, SystemInfo, VoidStatus)

_log = logging.getLogger("void.mcp")

#: Longest argument string accepted from an MCP caller, before any resolution is attempted.
MAX_ARG = 400
#: Ceiling on how much task history ``get_void_status`` reads to count active tasks.
MAX_TASKS_SCANNED = 200

#: Characters that can never appear in an application NAME. A name is what a person says; anything here means the
#: caller is trying to express a path, a command, a redirection or a glob. Refused before resolution, so the
#: catalog is never even asked. (``launch_app`` would refuse an unknown string anyway - this is the outer layer.)
_NAME_FORBIDDEN = re.compile(r"[\\/:;|&<>$`%*?\[\]{}\"'\n\r\t\x00]")

#: Schemes ``open_path`` will consider at all. The existing capability opens http/https via the browser; anything
#: else (file:, data:, javascript:, UNC, custom app schemes) is refused here rather than handed to the OS.
_ALLOWED_SCHEMES = ("http://", "https://")
_SCHEME_LIKE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")


def _terminal(status: str) -> bool:
    return status in (Status.COMPLETED, Status.FAILED, Status.CANCELLED)


class VoidMcpAdapter:
    """Exposes a fixed set of V.O.I.D capabilities. Holds one ``Assistant``; creates a no-model agent per call.

    A fresh ``Agent`` per call mirrors what the deterministic fast path does, and keeps each call's memory scope
    separate rather than accumulating taint across unrelated MCP requests. ``Agent.__init__`` is cheap - it wires
    references and allocates a run scope.
    """

    def __init__(self, assistant: Assistant | None = None):
        self._assistant = assistant if assistant is not None else Assistant()

    # --- the funnel ------------------------------------------------------------------------------
    @property
    def assistant(self) -> Assistant:
        return self._assistant

    def _agent(self) -> Agent:
        """An agent that can execute tools and CANNOT reach a model."""
        a = self._assistant
        return Agent(provider=_NoProvider(), tools=a.tools, risk_gate=a.risk_gate,
                     kill_switch=a.kill_switch, store=a.store,
                     on_event=lambda _msg: None,      # the audit log and perf event happen inside _run_call
                     defer_confirmation=True)         # an MCP call never stands in for the owner's confirmation

    def _invoke(self, tool: str, arguments: dict) -> tuple[bool, str, object, McpError | None]:
        """Run one existing capability through the existing funnel. Returns (ok, summary, data, error).

        This is the ONLY way this module executes anything. Every V.O.I.D security control - the kill switch, the
        per-call risk level, ``RiskGate.authorize``, memory-scope tainting, the audit line, the telemetry event -
        happens inside ``invoke_tool``, unchanged and in that order.
        """
        try:
            out = self._agent().invoke_tool(tool, arguments)
        except StopRequested as stop:
            return False, "", None, error(ErrorCode.STOPPED, f"V.O.I.D is stopped: {stop}")
        except ComputerBackendError as exc:
            return False, "", None, error(ErrorCode.UNAVAILABLE, exc)
        except Exception as exc:                      # noqa: BLE001 - nothing internal crosses this boundary
            _log.exception("MCP_TOOL_FAULT tool=%s", tool)
            return False, "", None, error(ErrorCode.INTERNAL, f"{type(exc).__name__} while running {tool}")
        if out.ok:
            return True, out.summary, out.data, None
        if out.kind == "unauthorized":
            # RiskGate said no, or the owner did. Reported as a denial, never as a failure to try.
            return False, out.summary, None, error(ErrorCode.DENIED,
                                                   "The owner's security policy refused this action.")
        if out.kind == "unknown":
            return False, out.summary, None, error(ErrorCode.INTERNAL, f"Capability '{tool}' is not registered.")
        return False, out.summary, None, self._classify_failure(out.summary)

    @staticmethod
    def _classify_failure(summary: str) -> McpError:
        """Map an existing capability's own failure text onto the stable code set.

        Two refusals get a message of our own rather than the capability's, because the capability is talking to the
        OWNER and this is talking to an untrusted caller: being told a location is out of scope is legitimate, being
        told where the owner's boundaries lie is not.
        """
        low = (summary or "").lower()
        if "outside the allowed roots" in low or "no allowed roots are configured" in low:
            # The owner's scope policy refused this. A DENIAL, not a failed attempt - a client that read
            # 'execution_failed' here would reasonably retry. The configured roots are deliberately not echoed.
            return error(ErrorCode.DENIED,
                         "That location is outside the scope the owner has approved for V.O.I.D.")
        if "protected" in low:
            return error(ErrorCode.PROTECTED,
                         "That location is protected by V.O.I.D and cannot be opened.")
        if "no longer available" in low or "not found" in low or "does not exist" in low:
            return error(ErrorCode.NOT_FOUND, summary)
        if "unknown application" in low or "no executable" in low:
            return error(ErrorCode.NOT_FOUND, summary)
        if "unusable application id" in low or "malformed" in low:
            return error(ErrorCode.INVALID_INPUT, summary)
        return error(ErrorCode.EXECUTION_FAILED, summary)

    # --- argument rules --------------------------------------------------------------------------
    @staticmethod
    def _app_name(raw: object) -> str:
        """A human application name, or refuse. Never a path, a command, or a glob."""
        if not isinstance(raw, str):
            raise VoidMcpError(ErrorCode.INVALID_INPUT, "name must be a string.")
        name = raw.strip()
        if not name:
            raise VoidMcpError(ErrorCode.INVALID_INPUT, "name must not be empty.")
        if len(name) > MAX_ARG:
            raise VoidMcpError(ErrorCode.INVALID_INPUT, f"name must be at most {MAX_ARG} characters.")
        if _NAME_FORBIDDEN.search(name):
            raise VoidMcpError(
                ErrorCode.INVALID_INPUT,
                "An application name may not contain path separators, quotes, shell metacharacters or wildcards. "
                "Give the name as a person would say it.")
        return name

    @staticmethod
    def _path_arg(raw: object) -> str:
        """A path or an http(s) URL, or refuse. Confinement itself stays with the file layer."""
        if not isinstance(raw, str):
            raise VoidMcpError(ErrorCode.INVALID_INPUT, "path must be a string.")
        target = raw.strip().strip('"')
        if not target:
            raise VoidMcpError(ErrorCode.INVALID_INPUT, "path must not be empty.")
        if len(target) > MAX_ARG:
            raise VoidMcpError(ErrorCode.INVALID_INPUT, f"path must be at most {MAX_ARG} characters.")
        if "\x00" in target or "\n" in target or "\r" in target:
            raise VoidMcpError(ErrorCode.INVALID_INPUT, "path must be a single line.")
        if target.startswith(("\\\\", "//")):
            raise VoidMcpError(ErrorCode.INVALID_INPUT, "network locations are not opened through MCP.")
        if _SCHEME_LIKE.match(target) and not target.lower().startswith(_ALLOWED_SCHEMES):
            # file:, data:, javascript:, vbscript:, ms-settings:, shell: ... all refused by name rather than by
            # hoping the OS handler is harmless. A Windows drive letter ("C:\\x") is not a scheme: it is 1 char.
            if not re.match(r"^[A-Za-z]:[\\/]", target):
                raise VoidMcpError(ErrorCode.INVALID_INPUT,
                                   "only http and https URLs, or a local path inside an allowed root, are opened.")
        return target

    # --- tools ----------------------------------------------------------------------------------
    @staticmethod
    def _applications(data: object, limit: int) -> list[ApplicationInfo]:
        """``find_app``'s own ``[{name, app_id}]`` payload, as typed results. Anything malformed is skipped."""
        if not isinstance(data, list):
            return []
        out: list[ApplicationInfo] = []
        for row in data[:limit]:
            if not isinstance(row, dict):
                continue
            name, app_id = row.get("name"), row.get("app_id")
            if isinstance(name, str) and isinstance(app_id, str):
                out.append(ApplicationInfo(name=name, app_id=app_id))
        return out

    def list_applications(self, limit: int = schemas.DEFAULT_APPLICATIONS) -> ListApplicationsResult:
        """Applications V.O.I.D's own discovery knows about. Read-only, bounded, deterministic order.

        Goes through the existing ``find_app`` capability with a wildcard rather than reading the catalog directly,
        so that a read is gated and audited exactly as it is when V.O.I.D itself runs ``find_app`` - including under
        a policy that requires confirmation at LOW.
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return ListApplicationsResult(ok=False, error=_info(error(
                ErrorCode.INVALID_INPUT, "limit must be an integer.")))
        if limit < 1:
            return ListApplicationsResult(ok=False, error=_info(error(
                ErrorCode.INVALID_INPUT, "limit must be at least 1.")))
        limit = min(limit, schemas.MAX_APPLICATIONS)
        # Always ask for the capability's full allowance, then sort and slice. Asking for exactly `limit` would make
        # WHICH applications come back depend on the limit, so two calls with different limits would disagree about
        # the first few names. This is a bounded SAMPLE in a stable order, not a complete inventory: find_app caps
        # at MAX_APPLICATIONS and `truncated` says when more exist. Use find_application to resolve a known name.
        ok, _summary, data, err = self._invoke(
            "find_app", {"query": "*", "max_results": schemas.MAX_APPLICATIONS})
        if not ok:
            return ListApplicationsResult(ok=False, error=_info(err))
        apps = self._applications(data, schemas.MAX_APPLICATIONS)
        apps.sort(key=lambda x: (x.name.casefold(), x.app_id))
        truncated = len(apps) > limit or len(apps) >= schemas.MAX_APPLICATIONS
        return ListApplicationsResult(ok=True, applications=apps[:limit], truncated=truncated)

    def find_application(self, name: str) -> FindApplicationResult:
        """Resolve a spoken/typed application name with V.O.I.D's existing resolution. Never launches anything.

        ``find_app`` is the resolver: exact/glob first, then the same deterministic hierarchy the voice fast path
        uses (spacing, whole-word prefix, vendor word, phonetic). Ambiguity stays ambiguity - this never picks.
        """
        try:
            query = self._app_name(name)
        except VoidMcpError as exc:
            return FindApplicationResult(ok=False, error=_info(exc.mcp))
        ok, _summary, data, err = self._invoke("find_app", {"query": query,
                                                            "max_results": schemas.MAX_CANDIDATES})
        if not ok:
            return FindApplicationResult(ok=False, error=_info(err))
        matches = self._applications(data, schemas.MAX_CANDIDATES)
        if len(matches) == 1:
            return FindApplicationResult(ok=True, resolved=matches[0])
        if len(matches) > 1:
            return FindApplicationResult(
                ok=False, candidates=matches,
                error=_info(error(ErrorCode.AMBIGUOUS,
                                  "Several installed applications match that name; V.O.I.D will not choose between "
                                  "them. Ask the owner which one.")))
        return FindApplicationResult(ok=False, error=_info(error(
            ErrorCode.NOT_FOUND, f"No installed application resolves to '{sanitise(query)}'.")))

    def launch_application(self, name: str) -> LaunchApplicationResult:
        """Launch an application. The caller names it; V.O.I.D decides what that name IS and launches that."""
        found = self.find_application(name)
        if not found.ok or found.resolved is None:
            return LaunchApplicationResult(ok=False, error=found.error)
        target = found.resolved
        # The argument handed on is the CATALOG's app_id, never the caller's string.
        ok, summary, _data, err = self._invoke("launch_app", {"name": target.app_id})
        if not ok:
            return LaunchApplicationResult(ok=False, launched=None, error=_info(err))
        return LaunchApplicationResult(ok=True, launched=target, detail=sanitise(summary))

    def open_path(self, path: str) -> OpenPathResult:
        """Open a file, folder or http(s) URL through the existing filesystem capability, which confines it."""
        try:
            target = self._path_arg(path)
        except VoidMcpError as exc:
            return OpenPathResult(ok=False, error=_info(exc.mcp))
        ok, summary, _data, err = self._invoke("open_path", {"target": target})
        if not ok:
            return OpenPathResult(ok=False, error=_info(err))
        return OpenPathResult(ok=True, detail=sanitise(summary))

    def get_system_info(self) -> SystemInfo:
        """A minimal machine summary. Deliberately omits hostname, user, environment and any path."""
        info = SystemInfo(ok=True)
        try:
            from void import __version__ as void_version
            info.void_version = void_version
        except Exception:                             # noqa: BLE001
            pass
        try:
            info.os_family = platform.system() or None
            info.os_release = platform.release() or None
            info.python_version = platform.python_version()
        except Exception:                             # noqa: BLE001
            pass
        try:
            import os
            info.cpu_count = os.cpu_count()
        except Exception:                             # noqa: BLE001
            pass
        try:
            import psutil
            vm = psutil.virtual_memory()
            info.memory_total_gb = round(vm.total / 2 ** 30, 1)
            info.memory_available_gb = round(vm.available / 2 ** 30, 1)
        except Exception:                             # noqa: BLE001 - psutil is optional to this answer
            pass
        return info

    def get_void_status(self) -> VoidStatus:
        """Runtime health, from V.O.I.D's own doctor and registries. Coarse by design."""
        status = VoidStatus(ok=True)
        a = self._assistant
        try:
            from void import __version__ as void_version
            status.void_version = void_version
        except Exception:                             # noqa: BLE001
            pass
        try:
            status.kill_switch_engaged = bool(a.kill_switch.engaged)
        except Exception:                             # noqa: BLE001
            pass
        try:
            # Bounded on purpose: a status call must not serialise an unbounded task history to count it.
            tasks = a.store.list(limit=MAX_TASKS_SCANNED)
            status.active_tasks = sum(1 for t in tasks if not _terminal(getattr(t, "status", "")))
        except Exception:                             # noqa: BLE001
            pass
        try:
            # ``available()`` is read-only and bounded: Gemini checks the SDK and credential with no network call,
            # and the local provider does a 2 s loopback GET that never pulls a model.
            names = list(a.providers.names()) if hasattr(a.providers, "names") else []
            for pname in names:
                provider = a.providers.get(pname) if hasattr(a.providers, "get") else None
                available = False
                try:
                    available = bool(provider.available()) if provider is not None else False
                except Exception:                     # noqa: BLE001 - a probe failure IS "not available"
                    available = False
                status.providers.append(ProviderStatus(name=str(pname)[:48], available=available))
        except Exception:                             # noqa: BLE001
            pass
        try:
            from void.perf import doctor, health
            state_dir = a.config.state_dir()
            checks = doctor.run_doctor(state_dir, probe_gateway=False)
            status.health = [f"{c.status} {c.name}"[:120] for c in checks]
            status.healthy = doctor.exit_code(checks) == 0
            payload = health.read_health(state_dir) or {}
            voice = payload.get("voice_state") or payload.get("state")
            if isinstance(voice, str):
                status.voice_state = voice[:32]
        except Exception:                             # noqa: BLE001
            pass
        return status

def _info(err: McpError | None) -> ErrorInfo | None:
    return None if err is None else ErrorInfo(**err.as_dict())
