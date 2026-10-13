"""Metrics: per-call counts, latency and cap usage, exported over OTLP.

Export is a no-op unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set: :func:`configure`
then installs a meter provider with an OTLP/gRPC reader. Until it does, the
instruments resolve against the API's no-op provider, so tests and local runs
export nothing. :class:`Metrics` is the recorder ``MailboxActions`` writes to,
from the same place it writes the audit line.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterable, Mapping

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Meter, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

log = logging.getLogger(__name__)

ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"
SERVICE_NAME = "mail-actions"

#: Latency bucket bounds in ms. Calls are mostly provider round trips, so the
#: bounds are dense below a second; the SDK default would put most in one bucket.
DURATION_BUCKETS_MS = (1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)

#: The principal is a metric attribute, so it must stay low-cardinality. The
#: server already bounds it; anything else that reaches here is ``other``.
OTHER_PRINCIPAL = "other"
_PRINCIPAL = re.compile(r"[a-z0-9_-]{1,32}")


#: Provider error reasons kept as-is in the ``outcome`` label. A provider passes
#: through its API's reason string (Gmail's ``error.reason`` may be any short
#: token), so the metric keeps only these; the audit line keeps the full reason.
PROVIDER_REASONS = frozenset(
    {
        "transport",
        "unknown_tag",
        "token",
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "quotaExceeded",
        "notFound",
        "forbidden",
        "insufficientPermissions",
        "invalidArgument",
        "badRequest",
        "backendError",
        "failedPrecondition",
    }
)
_PROVIDER = "refused:provider:"
_STATUS = re.compile(r"[1-5][0-9][0-9]")


def bound_outcome(outcome: str) -> str:
    """*outcome* with a provider reason mapped to a closed set: a known reason,
    ``token`` for any OAuth error, ``http_4xx``/``http_5xx`` for a bare status
    code, else ``other``. Every other outcome is a fixed string and passes."""
    if not outcome.startswith(_PROVIDER):
        return outcome
    reason = outcome.removeprefix(_PROVIDER).partition(":")[0]
    if reason in PROVIDER_REASONS:
        return _PROVIDER + reason
    if _STATUS.fullmatch(reason):
        return f"{_PROVIDER}http_{reason[0]}xx"
    return _PROVIDER + "other"


def bound_principal(principal: str) -> str:
    """*principal* if it is shaped like a client id, else ``other``."""
    if isinstance(principal, str) and _PRINCIPAL.fullmatch(principal):
        return principal
    return OTHER_PRINCIPAL


class Metrics:
    """The service's instruments, created on *meter* (default: the global one)."""

    def __init__(self, meter: Meter | None = None) -> None:
        self._meter = meter or metrics.get_meter("mail_actions")
        self._calls = self._meter.create_counter(
            "mail_actions.calls",
            unit="{call}",
            description="Tool calls, by tool, outcome and principal.",
        )
        self._duration = self._meter.create_histogram(
            "mail_actions.duration",
            unit="ms",
            description="Tool call latency, by tool.",
            explicit_bucket_boundaries_advisory=list(DURATION_BUCKETS_MS),
        )

    def tool_call(self, *, tool: str, outcome: str, principal: str, duration_ms: float) -> None:
        """Record one tool call: its outcome and its latency."""
        self._calls.add(
            1,
            {
                "tool": tool,
                "outcome": bound_outcome(outcome),
                "principal": bound_principal(principal),
            },
        )
        self._duration.record(duration_ms, {"tool": tool})

    def watch_caps(self, usage: Callable[[], Mapping[str, int]]) -> None:
        """Report *usage* (cap kind to events in its current window) as the
        ``mail_actions.cap_used`` gauge, read on each export.

        Each call registers the gauge again on the meter, and the SDK keeps only
        the first registration's callback for a name, so only one caller's usage
        is reported. The service runs one ``MailboxActions`` per process, which
        is the only caller.
        """

        def observe(_options: CallbackOptions) -> Iterable[Observation]:
            return [Observation(n, {"kind": kind}) for kind, n in usage().items()]

        self._meter.create_observable_gauge(
            "mail_actions.cap_used",
            callbacks=[observe],
            unit="{event}",
            description="Events counted against each cap in its rolling window, by kind.",
        )


def otlp_reader(endpoint: str) -> MetricReader:
    """A reader that exports over OTLP/gRPC to *endpoint*. Other exporter
    settings (TLS, headers) still come from the standard OTEL_* env."""
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

    return PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint))


def configure(
    env: Mapping[str, str] = os.environ,
    *,
    reader: Callable[[str], MetricReader] = otlp_reader,
    install: Callable[[MeterProvider], None] = metrics.set_meter_provider,
) -> MeterProvider | None:
    """Install an exporting meter provider when *env* names an OTLP endpoint,
    exporting to that endpoint.

    Returns the provider, which the caller shuts down to flush on exit, or None
    when no endpoint is set and nothing is exported.
    """
    endpoint = env.get(ENDPOINT_ENV)
    if not endpoint:
        return None
    service = env.get("OTEL_SERVICE_NAME") or SERVICE_NAME
    provider = MeterProvider(
        resource=Resource.create({"service.name": service}), metric_readers=[reader(endpoint)]
    )
    install(provider)
    log.info("exporting metrics over OTLP as %s", service)
    return provider
