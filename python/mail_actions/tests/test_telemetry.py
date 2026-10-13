from unittest.mock import create_autospec

import pytest
from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, PeriodicExportingMetricReader

from mail_actions.telemetry import (
    DURATION_BUCKETS_MS,
    Metrics,
    bound_outcome,
    bound_principal,
    configure,
    otlp_reader,
)


@pytest.fixture
def reader():
    return InMemoryMetricReader()


@pytest.fixture
def recorder(reader):
    return Metrics(MeterProvider(metric_readers=[reader]).get_meter("test"))


@pytest.fixture
def install():
    return create_autospec(metrics.set_meter_provider)


def collected(reader):
    data = reader.get_metrics_data()
    return {
        m.name: m.data
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
    }


def test_tool_call_counts_by_tool_outcome_and_principal(reader, recorder):
    recorder.tool_call(tool="file", outcome="file", principal="orchestrator", duration_ms=12.5)
    recorder.tool_call(tool="file", outcome="file", principal="orchestrator", duration_ms=40)
    recorder.tool_call(tool="file", outcome="refused:protected", principal="x", duration_ms=3)

    data = collected(reader)
    calls = {
        tuple(sorted(p.attributes.items())): p.value for p in data["mail_actions.calls"].data_points
    }
    assert calls == {
        (("outcome", "file"), ("principal", "orchestrator"), ("tool", "file")): 2,
        (("outcome", "refused:protected"), ("principal", "x"), ("tool", "file")): 1,
    }
    (duration,) = data["mail_actions.duration"].data_points
    assert dict(duration.attributes) == {"tool": "file"}
    assert tuple(duration.explicit_bounds) == DURATION_BUCKETS_MS
    assert duration.count == 3
    assert sum(1 for n in duration.bucket_counts if n) == 3  # spread, not one bucket


def test_an_unbounded_principal_is_recorded_as_other(reader, recorder):
    recorder.tool_call(tool="label", outcome="label", principal="Robert'); DROP", duration_ms=1)

    (point,) = collected(reader)["mail_actions.calls"].data_points
    assert point.attributes["principal"] == "other"


@pytest.mark.parametrize(
    ("outcome", "bounded"),
    [
        ("file", "file"),
        ("refused:protected", "refused:protected"),
        ("refused:provider:rateLimitExceeded", "refused:provider:rateLimitExceeded"),
        ("refused:provider:transport", "refused:provider:transport"),
        ("refused:provider:token:invalid_grant", "refused:provider:token"),
        ("refused:provider:403", "refused:provider:http_4xx"),
        ("refused:provider:503", "refused:provider:http_5xx"),
        ("refused:provider:someNewGoogleReason", "refused:provider:other"),
        ("refused:provider:", "refused:provider:other"),
    ],
)
def test_bound_outcome(outcome, bounded):
    assert bound_outcome(outcome) == bounded


def test_a_provider_reason_outside_the_set_is_recorded_as_other(reader, recorder):
    recorder.tool_call(
        tool="file", outcome="refused:provider:attackerChosen", principal="x", duration_ms=1
    )

    (point,) = collected(reader)["mail_actions.calls"].data_points
    assert point.attributes["outcome"] == "refused:provider:other"


@pytest.mark.parametrize(
    ("principal", "bounded"),
    [("orchestrator", "orchestrator"), ("unknown", "unknown"), ("A" * 3, "other"), (None, "other")],
)
def test_bound_principal(principal, bounded):
    assert bound_principal(principal) == bounded


def test_cap_usage_is_a_gauge_by_kind(reader, recorder):
    usage = {"actions": 3, "filter_changes": 1}
    recorder.watch_caps(lambda: usage)

    points = collected(reader)["mail_actions.cap_used"].data_points
    assert {p.attributes["kind"]: p.value for p in points} == usage


def test_the_default_recorder_is_a_no_op_without_a_provider():
    Metrics().tool_call(tool="file", outcome="file", principal="unknown", duration_ms=1)


def test_configure_is_a_no_op_without_an_endpoint(install):
    assert configure({}, install=install) is None
    assert configure({"OTEL_EXPORTER_OTLP_ENDPOINT": ""}, install=install) is None
    install.assert_not_called()


def test_configure_installs_an_exporting_provider(install, reader):
    env = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4317", "OTEL_SERVICE_NAME": "svc"}

    make_reader = create_autospec(otlp_reader, return_value=reader)

    provider = configure(env, reader=make_reader, install=install)

    make_reader.assert_called_once_with("http://collector:4317")

    install.assert_called_once_with(provider)
    assert isinstance(provider, MeterProvider)
    Metrics(provider.get_meter("test")).tool_call(
        tool="file", outcome="file", principal="unknown", duration_ms=1
    )
    (resource_metrics,) = reader.get_metrics_data().resource_metrics
    assert resource_metrics.resource.attributes["service.name"] == "svc"
    provider.shutdown()


def test_otlp_reader_exports_periodically_to_the_given_endpoint():
    reader = otlp_reader("http://collector:4317")

    assert isinstance(reader, PeriodicExportingMetricReader)
    assert reader._exporter._endpoint == "collector:4317"
    reader.shutdown(timeout_millis=1000)
