"""Turning V.O.I.D's spans into something a collector can receive.

:mod:`void.orchestration.trace` already emits spans through the OpenTelemetry **API**, with an attribute
allowlist and never-raises semantics. With no SDK configured those spans are no-ops. This module is the
other half: it configures the official SDK and an OTLP exporter, at which point the instrumentation that
was already there starts arriving somewhere. Nothing in ``trace.py`` changed - that is the point of having
written it against the API.

Four rules shape this module, and all four are about telemetry never mattering more than the work:

**Disabled by default, and disabled safely.** ``observability.enabled`` is false in the shipped config.
While it is false nothing is imported, nothing is configured, and no socket is opened.

**Failure is degradation, never an error.** If the endpoint is unreachable, the SDK is missing, or the
exporter throws on construction, :func:`configure` returns a status saying so and V.O.I.D carries on. A
collector being down must not fail a task. The export itself is asynchronous and batched, so a collector
that goes away mid-session cannot block a tool call either.

**Nothing sensitive leaves.** Attributes are allowlisted at creation in ``trace.py``; this module applies
the same allowlist again on the way out (:class:`AllowlistExporter`), because the cost of a second check is
nothing and the cost of a leaked screenshot path is permanent. Headers - the one place a credential could
appear - are read from configuration or the environment, never logged, and never put in a span.

**Official packages only.** ``opentelemetry-sdk`` and ``opentelemetry-exporter-otlp-proto-http``, pinned to
1.45.0 to match the ``opentelemetry-api`` 1.45.0 already present. No fork, no vendored SDK, and no
V.O.I.D-native reimplementation of a mature library.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from void.orchestration.trace import ATTRIBUTES

_log = logging.getLogger(__name__)

#: The pinned OpenTelemetry version. Matches the API already installed; the SDK and exporter must agree
#: with the API or span creation and export disagree about the data model.
OTEL_VERSION = "1.45.0"

#: Default OTLP/HTTP endpoint for a locally running collector.
DEFAULT_ENDPOINT = "http://127.0.0.1:4318/v1/traces"

#: Transport protocols this module can configure. HTTP/protobuf is the default because it needs only
#: ``urllib3`` (already present) and traverses proxies that gRPC does not.
PROTOCOLS = ("http/protobuf", "grpc")

#: Header names never written to a log line, at any level, under any configuration.
_SECRET_HEADERS = ("authorization", "api-key", "x-api-key", "token", "cookie", "proxy-authorization")


class TelemetryError(RuntimeError):
    """Telemetry could not be configured. Never raised at a caller; carried in a status instead."""


@dataclass(frozen=True)
class TelemetryPolicy:
    """What the owner's configuration asks of telemetry."""

    enabled: bool = False
    endpoint: str = DEFAULT_ENDPOINT
    protocol: str = "http/protobuf"
    #: Export headers, for a collector that authenticates. Values are secret and are never logged.
    headers: dict = field(default_factory=dict)
    timeout_s: float = 10.0
    #: Fraction of traces recorded, 0.0-1.0. 1.0 while developing; lower in a long-running deployment.
    sample_ratio: float = 1.0
    service_name: str = "void"
    #: Print spans to the log instead of exporting them. For checking instrumentation with no collector.
    console: bool = False

    @classmethod
    def from_config(cls, config) -> "TelemetryPolicy":
        def get(key, default):
            try:
                return config.get(key, default)
            except Exception:                                   # noqa: BLE001
                return default

        def number(key, default, low, high):
            try:
                return min(max(type(default)(get(key, default)), low), high)
            except (TypeError, ValueError):
                return default

        protocol = str(get("observability.protocol", "http/protobuf")).strip().lower()
        if protocol not in PROTOCOLS:
            protocol = "http/protobuf"

        # Headers may be configured inline, or named for lookup in the environment so a token never has to
        # be written into a config file that might be committed.
        headers = {}
        configured = get("observability.headers", {}) or {}
        if isinstance(configured, dict):
            for key, value in configured.items():
                if isinstance(key, str) and isinstance(value, str) and key.strip():
                    headers[key.strip()] = value
        env_name = get("observability.headers_env", "") or ""
        if isinstance(env_name, str) and env_name.strip():
            raw = os.environ.get(env_name.strip(), "")
            for pair in raw.split(","):
                name, _, value = pair.partition("=")
                if name.strip() and value:
                    headers[name.strip()] = value.strip()

        endpoint = str(get("observability.endpoint", DEFAULT_ENDPOINT) or DEFAULT_ENDPOINT).strip()
        return cls(enabled=bool(get("observability.enabled", False)),
                   endpoint=endpoint,
                   protocol=protocol,
                   headers=headers,
                   timeout_s=float(number("observability.timeout_s", 10.0, 1.0, 120.0)),
                   sample_ratio=float(number("observability.sample_ratio", 1.0, 0.0, 1.0)),
                   service_name=str(get("observability.service_name", "void") or "void")[:64],
                   console=bool(get("observability.console", False)))

    def safe_endpoint(self) -> str:
        """The endpoint with any credentials stripped, for logging.

        An endpoint can carry ``user:password@`` just as a URL can, so even this is not logged verbatim.
        """
        try:
            from urllib.parse import urlparse
            parsed = urlparse(self.endpoint)
            return f"{parsed.scheme}://{parsed.hostname or '?'}:{parsed.port or '?'}{parsed.path}"
        except Exception:                                       # noqa: BLE001
            return "<unparseable>"


@dataclass
class TelemetryStatus:
    """What happened when telemetry was configured. Reportable, so degradation is visible."""

    enabled: bool = False
    exporting: bool = False
    protocol: str = ""
    endpoint: str = ""
    degraded: bool = False
    reason: str = ""

    def as_dict(self) -> dict:
        return {"enabled": self.enabled, "exporting": self.exporting, "protocol": self.protocol,
                "endpoint": self.endpoint, "degraded": self.degraded, "reason": self.reason}

    def describe(self) -> str:
        if not self.enabled:
            return "Telemetry is off."
        if self.degraded:
            return f"Telemetry is degraded: {self.reason}."
        if self.exporting:
            return f"Telemetry is exporting to {self.endpoint} over {self.protocol}."
        return "Telemetry is recording locally."


def allowed_attributes(attributes) -> dict:
    """The allowlist, applied on the way out.

    Deliberately a second application of the rule already enforced in ``trace.py``. Defence in depth: any
    instrumentation added later - V.O.I.D's own or a library's - passes through here, so a span attribute
    that was never meant to travel cannot reach a collector just because someone forgot.
    """
    out = {}
    for key, value in dict(attributes or {}).items():
        allowed = ATTRIBUTES.get(key)
        if allowed is None:
            continue
        if isinstance(value, bool) and bool not in allowed:
            continue
        if not isinstance(value, allowed):
            continue
        out[key] = value
    return out


def _build_exporter(policy: TelemetryPolicy):
    """The official OTLP exporter for the configured protocol. Raises TelemetryError on any problem."""
    if policy.protocol == "grpc":
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        except ImportError as exc:
            raise TelemetryError(
                "the gRPC OTLP exporter is not installed; "
                "install opentelemetry-exporter-otlp-proto-grpc or use http/protobuf") from exc
        return OTLPSpanExporter(endpoint=policy.endpoint, timeout=int(policy.timeout_s),
                                headers=policy.headers or None)
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError as exc:
        raise TelemetryError(
            "the OTLP HTTP exporter is not installed; "
            f"install opentelemetry-exporter-otlp-proto-http=={OTEL_VERSION}") from exc
    return OTLPSpanExporter(endpoint=policy.endpoint, timeout=int(policy.timeout_s),
                            headers=policy.headers or None)


#: The provider configured by :func:`configure`, kept so :func:`shutdown` can flush it.
_provider = None


def configure(policy: TelemetryPolicy | None = None, exporter=None) -> TelemetryStatus:
    """Configure the OpenTelemetry SDK so V.O.I.D's existing spans are exported.

    ``exporter`` overrides the configured transport, which is how the tests drive a real export without a
    network. Returns a status rather than raising: every failure path here is a degradation.
    """
    global _provider
    policy = policy or TelemetryPolicy()
    status = TelemetryStatus(enabled=policy.enabled, protocol=policy.protocol,
                             endpoint=policy.safe_endpoint())
    if not policy.enabled:
        return status

    try:
        from opentelemetry import trace as trace_api
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ALWAYS_ON, TraceIdRatioBased
    except ImportError as exc:
        status.degraded = True
        status.reason = f"the OpenTelemetry SDK is not installed ({exc.name})"
        _log.info("TELEMETRY_SDK_MISSING module=%s", exc.name)
        return status

    try:
        sampler = ALWAYS_ON if policy.sample_ratio >= 1.0 else TraceIdRatioBased(policy.sample_ratio)
        resource = Resource.create({"service.name": policy.service_name,
                                    "telemetry.sdk.language": "python"})
        provider = TracerProvider(resource=resource, sampler=sampler)
    except Exception as exc:                                    # noqa: BLE001
        status.degraded = True
        status.reason = f"the tracer provider could not be built ({type(exc).__name__})"
        return status

    chosen = exporter
    if chosen is None:
        try:
            chosen = _build_exporter(policy)
        except TelemetryError as bad:
            status.degraded = True
            status.reason = str(bad)
            _log.info("TELEMETRY_EXPORTER_UNAVAILABLE protocol=%s", policy.protocol)
            return status
        except Exception as exc:                                # noqa: BLE001
            status.degraded = True
            status.reason = f"the exporter could not be built ({type(exc).__name__})"
            return status

    try:
        # Wrapped so the allowlist applies to everything leaving the process, and so an exporter that
        # throws cannot propagate into the span that is ending.
        provider.add_span_processor(BatchSpanProcessor(AllowlistExporter(chosen)))
        if policy.console:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
            provider.add_span_processor(SimpleSpanProcessor(AllowlistExporter(ConsoleSpanExporter())))
        trace_api.set_tracer_provider(provider)
    except Exception as exc:                                    # noqa: BLE001
        status.degraded = True
        status.reason = f"the provider could not be installed ({type(exc).__name__})"
        return status

    # OpenTelemetry's global tracer provider can be set exactly ONCE per process: a second
    # ``set_tracer_provider`` logs "Overriding of current TracerProvider is not allowed" and keeps the
    # first one. It does not raise, so without this check a second call would report ``exporting=True``
    # while its exporter received nothing - a status that lies. Verified rather than assumed.
    if trace_api.get_tracer_provider() is not provider:
        status.degraded = True
        status.reason = ("telemetry was already configured in this process, so this configuration was "
                         "ignored; OpenTelemetry allows only one tracer provider per process")
        _log.info("TELEMETRY_ALREADY_CONFIGURED")
        return status

    _provider = provider
    status.exporting = True
    # Endpoint is logged in its credential-stripped form; headers are never logged at all.
    _log.info("TELEMETRY_CONFIGURED protocol=%s endpoint=%s headers=%d",
              policy.protocol, policy.safe_endpoint(), len(policy.headers))
    return status


def current_provider():
    """The tracer provider :func:`configure` installed, or None.

    Exposed so a caller can obtain a tracer from the provider it just configured, rather than from the
    process-global one. That matters for tests: because the global can be set only once per process, a
    test that configures telemetry after something else already has must go through its own provider to
    exercise the export path at all.
    """
    return _provider


def shutdown(timeout_s: float = 5.0) -> bool:
    """Flush and stop the provider configured here. Never raises."""
    global _provider
    provider = _provider
    _provider = None
    if provider is None:
        return False
    try:
        provider.force_flush(int(timeout_s * 1000))
    except Exception:                                           # noqa: BLE001
        pass
    try:
        provider.shutdown()
    except Exception:                                           # noqa: BLE001
        return False
    return True


class AllowlistExporter:
    """Wraps a span exporter: applies V.O.I.D's attribute allowlist and swallows exporter failures.

    Two jobs. It is the last gate before spans leave the process, so an attribute that is not in
    :data:`void.orchestration.trace.ATTRIBUTES` is dropped here even if some future instrumentation set it.
    And it absorbs exporter exceptions, so a collector that is down, slow, or returning errors degrades
    telemetry rather than surfacing anywhere near the work being measured.
    """

    def __init__(self, inner):
        self._inner = inner
        #: Counted so degradation is observable without logging the failures one by one.
        self.failures = 0
        self.exported = 0
        self.dropped_attributes = 0

    def export(self, spans):
        try:
            # A span that could not be filtered comes back as None and is dropped here rather than handed
            # on: losing a span is a telemetry gap, exporting an unfiltered one is a disclosure.
            cleaned = [kept for kept in (self._filter(span) for span in spans) if kept is not None]
            self.exported += len(cleaned)
            if not cleaned:
                from opentelemetry.sdk.trace.export import SpanExportResult
                return SpanExportResult.SUCCESS
            return self._inner.export(cleaned)
        except Exception as exc:                                # noqa: BLE001 - never reaches the caller
            self.failures += 1
            _log.info("TELEMETRY_EXPORT_FAILED kind=%s total=%d", type(exc).__name__, self.failures)
            try:
                from opentelemetry.sdk.trace.export import SpanExportResult
                return SpanExportResult.FAILURE
            except Exception:                                   # noqa: BLE001
                return None

    def _filter(self, span):
        """Return the span with only allowlisted attributes.

        A ``ReadableSpan``'s attributes are not writable through the public interface, so a filtered copy
        is built when anything needs dropping. When nothing needs dropping - the normal case, because
        ``trace.py`` already allowlisted - the original is returned untouched and this costs one comparison.
        """
        try:
            original = dict(getattr(span, "attributes", None) or {})
        except Exception:                                       # noqa: BLE001
            return span
        kept = allowed_attributes(original)
        if len(kept) == len(original):
            return span
        self.dropped_attributes += len(original) - len(kept)
        try:
            from opentelemetry.sdk.trace import ReadableSpan
            return ReadableSpan(
                name=span.name, context=span.get_span_context(), parent=span.parent,
                resource=span.resource, attributes=kept, events=span.events, links=span.links,
                status=span.status, kind=span.kind,
                start_time=span.start_time, end_time=span.end_time,
                instrumentation_scope=getattr(span, "instrumentation_scope", None))
        except Exception:                                       # noqa: BLE001
            # Could not rebuild it: drop the span entirely rather than export unfiltered attributes.
            # Losing a span is a telemetry gap; exporting one is a disclosure.
            self.dropped_attributes += len(original)
            return None

    def shutdown(self):
        try:
            return self._inner.shutdown()
        except Exception:                                       # noqa: BLE001
            return None

    def force_flush(self, timeout_millis: int = 30_000):
        try:
            return self._inner.force_flush(timeout_millis)
        except Exception:                                       # noqa: BLE001
            return False
