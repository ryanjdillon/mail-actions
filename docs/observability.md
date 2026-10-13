# Observability

mail-actions writes two records of every tool call, both from one event in
`MailboxActions` (`_record` in `actions.py`), so they cannot disagree:

- the **audit line**: one JSON line on the `mail_actions.audit` logger (see
  [threat-model.md](threat-model.md));
- **metrics**, exported over OTLP by `telemetry.py`.

## Enabling export

Metrics are exported only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set (e.g.
`http://otel-collector:4317`). Without it nothing is exported and the
instruments are no-ops. Export uses OTLP/gRPC with the standard `OTEL_*`
environment (`OTEL_EXPORTER_OTLP_*`, `OTEL_METRIC_EXPORT_INTERVAL`), and
`OTEL_SERVICE_NAME` sets `service.name` (default `mail-actions`). `serve`
flushes the last batch on exit.

## Metrics

| OTel name | Prometheus name | Type | Labels |
| --- | --- | --- | --- |
| `mail_actions.calls` | `mail_actions_calls_total` | counter | `tool`, `outcome`, `principal` |
| `mail_actions.duration` (ms) | `mail_actions_duration_milliseconds_*` | histogram | `tool` |
| `mail_actions.cap_used` | `mail_actions_cap_used` | gauge | `kind` |

The Prometheus names assume the usual OTLP translation (dots to underscores,
`_total` on counters, the unit appended to the histogram); check the exact
suffixes against what your collector exposes.

Labels:

- `tool`: `file`, `unfile`, `label`, `sync_filters`, `capabilities`, and
  `ensure_protected` (run at startup, one call per mailbox).
- `outcome`: the audit line's outcome. For `file`, `unfile` and `label` a
  success is the action taken (`file`, `unfile`, `label`, or `label_only` when
  `file` tagged without archiving); for the other tools it is `ok`. A refusal is
  `refused:<reason>`, with the reasons in [interface.md](interface.md), e.g.
  `refused:protected`, `refused:cap_exceeded`, `refused:internal`, and
  `refused:provider:<code>`. The label keeps a provider code only from a
  closed set (`PROVIDER_REASONS` in `telemetry.py`, e.g. `rateLimitExceeded`,
  `quotaExceeded`, `transport`, `token`); a bare HTTP status becomes
  `http_4xx`/`http_5xx` and anything else `other`. The audit line keeps the
  provider's own code.
- `principal`: the gateway client id from `x-client-id`, or `unknown` (no
  header), `ambiguous` (sent twice) or `other` (not shaped like a client id).
  The recorder bounds it again, so a label value is always a short client id
  or one of those three. `ensure_protected` records `startup`.
- `kind` (cap gauge): `actions` counts message actions in the rolling 24 h
  window against `actions_per_day`; `filter_changes` counts approved filter
  changes in the rolling 7-day window against `filter_changes_per_week`. Read
  on each export, so it falls as events age out.

Latency is recorded in milliseconds with explicit bucket bounds (1, 2, 5, 10,
25, 50, 100, 250, 500 ms, then 1, 2.5, 5, 10, 30 s), so calls that take a
fraction of a second spread across buckets instead of landing in the first.

## Example queries

`file` calls in the last hour above 50, per principal. Not `increase()`: the
`outcome="file"` series is created at its first call, and `increase()` over a
series with one sample is 0, so a first burst would go unreported. Compare with
an hour ago instead, counting a series that did not exist then from 0:

```promql
sum by (principal) (
  mail_actions_calls_total{tool="file", outcome=~"file|label_only"}
  - (
    mail_actions_calls_total{tool="file", outcome=~"file|label_only"} offset 1h
    or mail_actions_calls_total{tool="file", outcome=~"file|label_only"} * 0
  )
) > 50
```

A restart resets the counter, which makes the difference negative for the hour
after it; that can miss a burst, but never fires falsely.

Any protected-mail refusal (an agent tried to file family, tax or legal mail),
with the same first-sample-safe pattern:

```promql
sum by (principal) (
  mail_actions_calls_total{outcome="refused:protected"}
  - (
    mail_actions_calls_total{outcome="refused:protected"} offset 1h
    or mail_actions_calls_total{outcome="refused:protected"} * 0
  )
) > 0
```

Other useful views:

```promql
# Refusals by reason over the last day.
sum by (tool, outcome) (increase(mail_actions_calls_total{outcome=~"refused:.*"}[1d]))

# p95 latency per tool.
histogram_quantile(0.95,
  sum by (tool, le) (rate(mail_actions_duration_milliseconds_bucket[15m])))

# Share of the daily action cap in use (200 is the default cap).
mail_actions_cap_used{kind="actions"} / 200
```
