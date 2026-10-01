"""The typed shapes that cross the MCP boundary.

The SDK derives every tool's JSON Schema from these annotations, so this module IS the wire contract - there is no
hand-written JSON Schema anywhere, and no second place for it to drift from.

Two conventions hold throughout:

* **Every response carries ``ok``.** A failure is a normal response with ``ok=False`` and an ``error`` object, not a
  protocol fault. That is deliberate: a RiskGate denial is a real answer the caller must be able to read and branch
  on, and an exception would flatten the whole taxonomy in :mod:`void.mcp.errors` into "something went wrong".
* **Results are bounded.** Every list has a server-enforced ceiling, so no MCP caller can make V.O.I.D serialise an
  unbounded amount of the machine into a response.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: Largest number of applications one call may return. This is ``find_app``'s OWN hard maximum: both read tools go
#: through that existing capability, so its bound is the real one and inventing a larger number here would be a lie.
MAX_APPLICATIONS = 50
#: Default page size for ``list_applications`` - ``find_app``'s own default.
DEFAULT_APPLICATIONS = 25
#: Largest number of candidates ``find_application`` reports for an ambiguous name.
MAX_CANDIDATES = 25


class _Model(BaseModel):
    """Forbids unknown fields, so a malformed or padded argument object is rejected by the schema, not by us."""

    model_config = ConfigDict(extra="forbid")


class ErrorInfo(_Model):
    """Why a call failed. ``code`` is from a fixed set (see :class:`void.mcp.errors.ErrorCode`)."""

    code: str = Field(description="Stable machine-readable failure reason.")
    message: str = Field(description="Short human-readable detail. Sanitised; never carries secrets or traces.")


class ApplicationInfo(_Model):
    """One application V.O.I.D can identify. ``app_id`` is the ENGINE's handle for it."""

    name: str = Field(description="Display name as V.O.I.D discovered it.")
    app_id: str = Field(
        description="Engine-owned identifier. Pass this to launch_application. "
                    "It is NOT a path and cannot be constructed by the caller.")


class ListApplicationsResult(_Model):
    ok: bool
    applications: list[ApplicationInfo] = Field(default_factory=list)
    truncated: bool = Field(
        default=False,
        description="True when the limit was reached and more applications may exist. There is no exact total: the "
                    "underlying capability truncates, and counting past it would need a second ungated read.")
    error: ErrorInfo | None = None


class FindApplicationResult(_Model):
    ok: bool
    resolved: ApplicationInfo | None = Field(
        default=None, description="Set only when EXACTLY one application matched.")
    candidates: list[ApplicationInfo] = Field(
        default_factory=list,
        description="Populated when several matched. V.O.I.D never picks between them; ask the owner.")
    error: ErrorInfo | None = None


class LaunchApplicationResult(_Model):
    ok: bool
    launched: ApplicationInfo | None = None
    detail: str | None = Field(default=None, description="What V.O.I.D did, as it would tell the owner.")
    error: ErrorInfo | None = None


class OpenPathResult(_Model):
    ok: bool
    detail: str | None = None
    error: ErrorInfo | None = None


class SystemInfo(_Model):
    """A deliberately small, non-identifying summary. No hostname, no user name, no environment, no paths."""

    ok: bool
    void_version: str | None = None
    os_family: str | None = Field(default=None, description='e.g. "Windows".')
    os_release: str | None = Field(default=None, description='e.g. "11". Not the full build fingerprint.')
    python_version: str | None = None
    cpu_count: int | None = None
    memory_total_gb: float | None = None
    memory_available_gb: float | None = None
    error: ErrorInfo | None = None


class ProviderStatus(_Model):
    """A provider, at the coarsest useful granularity. Never a key, never an endpoint, never a model id."""

    name: str
    available: bool


class VoidStatus(_Model):
    ok: bool
    void_version: str | None = None
    kill_switch_engaged: bool | None = Field(
        default=None, description="True when V.O.I.D is stopped. Every side-effecting tool refuses while it is.")
    active_tasks: int | None = Field(default=None, description="Tasks not in a terminal state.")
    providers: list[ProviderStatus] = Field(default_factory=list)
    voice_state: str | None = Field(default=None, description="Voice runtime state if it is running, else null.")
    health: list[str] = Field(
        default_factory=list,
        description="One line per health check, from V.O.I.D's own doctor: '<status> <name>'.")
    healthy: bool | None = Field(default=None, description="False when any health check failed.")
    error: ErrorInfo | None = None
