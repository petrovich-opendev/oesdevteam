# External Data Resilience Rules

> Canonical rule set for code that consumes messages from external systems:
> message brokers (Kafka / NATS / MQTT / RabbitMQ), SCADA, FMS, OPC-UA,
> ModBus, any vendor telemetry. **Referenced by Senior Backend, Senior
> Data, and Senior SRE reviewer prompts.** Violations are BLOCKER or
> MAJOR findings depending on the severity of the failure mode.

## Why this file exists

Industrial data sources are hostile by nature. A consumer that assumes
well-formed input breaks in production within days:

- A SCADA engineer renames a tag; every downstream consumer crashes.
- A firmware upgrade reorders JSON keys; the schema "changes" without
  a version bump.
- A broker reconnect delivers a half-written message; the parser
  explodes.
- A network blip creates 30 s of backpressure; the consumer OOMs.
- A vendor starts emitting timestamps in a different timezone; all
  downstream analytics shift by 3 h silently.

Our rule: **no single malformed message, renamed tag, or transient
connection blip may take the service down.** The worst allowed
outcome is "drop the message, increment a counter, surface via health
check, keep consuming". Anything stronger — pagers at 03:00 for a
Kafka hiccup — is a bug, not a feature.

---

## Rule R-1: Validate at the boundary, degrade gracefully

**Every incoming message must pass through a Pydantic (or equivalent)
schema at the consumer entry point.** No downstream business logic
sees a dict or an untyped object.

```python
# BAD — parsing error becomes an unhandled exception up the stack
async def on_message(raw: bytes) -> None:
    data = json.loads(raw)              # ValueError crashes consumer
    chat_id = data["telegram_chat_id"]  # KeyError crashes consumer
    await handle(chat_id)

# GOOD — single catch point, drop + counter + continue
async def on_message(raw: bytes) -> None:
    try:
        msg = IncomingEvent.model_validate_json(raw)
    except (json.JSONDecodeError, ValidationError) as e:
        DROPPED_MESSAGES.labels(reason="invalid_schema").inc()
        logger.warning("dropped malformed message: %s", _truncate(e))
        return
    await handle(msg)
```

Severity if missing: **BLOCKER**. A consumer that propagates
`ValidationError` to the event loop will crash a worker at the first
firmware upgrade.

---

## Rule R-2: Drop, count, log — never crash, never silently swallow

For every drop path, all three must be present:

1. **Drop** — the consumer moves on; it does not retry forever, does
   not block, does not DLQ into itself infinitely.
2. **Count** — a Prometheus / StatsD / whatever counter increments,
   labelled by reason (`invalid_schema`, `unknown_tag`, `duplicate`,
   `backpressure`, `timestamp_drift`, etc.).
3. **Log** — one `WARNING` level line with truncated payload excerpt
   (no full payloads — PII risk). First-seen per reason at `ERROR`.

Silent drop = **BLOCKER**. Drop without a counter = **MAJOR**. Drop
without log = **MAJOR**. All three together = **PASS**.

---

## Rule R-3: Health check reflects drop rate

A health endpoint that returns `200 OK` while 95% of messages are
being dropped is lying to the operator. The health check MUST include
the recent drop rate as a signal:

```python
@app.get("/health")
async def health() -> HealthResponse:
    recent = DROPPED_MESSAGES.recent_rate(window_seconds=60)
    received = RECEIVED_MESSAGES.recent_rate(window_seconds=60)
    drop_fraction = recent / received if received else 0.0
    status = "ok"
    if drop_fraction > 0.1:
        status = "degraded"
    if drop_fraction > 0.5:
        status = "down"
    return HealthResponse(
        status=status,
        drop_fraction=drop_fraction,
        last_successful_receive=LAST_SUCCESS.timestamp(),
    )
```

Thresholds (10% / 50%) are starting points — projects tune them in
`config/health.yaml`. What is non-negotiable:

- The signal MUST be in the health response (status field OR a
  degraded HTTP code).
- The `last_successful_receive` timestamp MUST be exposed — on-call
  needs to know whether the broker is alive at all.

Severity if missing: **MAJOR** (consumer is live but invisible to
on-call); **BLOCKER** if the service is declared production-ready.

---

## Rule R-4: Schema evolution is versioned, renames are explicit

SCADA / FMS tag renames are not a storm-force event — they happen on
every control-system upgrade. Code defends against them structurally.

### Two viable patterns:

**Pattern A — Message envelope with explicit version:**

```python
class Envelope(BaseModel):
    schema_version: Literal["1.0", "1.1", "2.0"]
    payload: dict[str, Any]  # validated below per version
```

The consumer dispatches on `schema_version` and parses with the
matching Pydantic model. Unknown versions → drop with reason
`unknown_schema_version`.

**Pattern B — Field-mapping config (preferred for SCADA/FMS):**

```yaml
# config/tag_mappings.yaml
# Maps external vendor tag names to stable internal field names.
# When vendor renames "P_Temp_1" to "Probe.Temp.1" we edit THIS file;
# no code changes.
internal_to_external:
  probe_temp_c:
    primary: "P_Temp_1"              # current vendor name
    aliases: ["Probe.Temp.1", "PT1"]  # accepted historical names
    required: true
    unit: "celsius"
```

Loader reads each incoming message, looks up each field via the
mapping, handles aliases, and rejects missing required tags with
reason `missing_required_tag:probe_temp_c`.

Severity if tag-name is hardcoded in Python: **BLOCKER** for any
SCADA/FMS integration. The next firmware rollout will take the
consumer down.

---

## Rule R-5: Unknown / extra fields tolerated by default

For evolving external schemas, Pydantic's default (`extra="ignore"`)
is the right choice on the consumer side. A new vendor field must not
crash a consumer that doesn't use it yet.

```python
class TelemetryEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")   # forward-compatible
    ts: datetime
    device_id: str
    value: float
```

Severity if `extra="forbid"` is set on external-message models:
**MAJOR** unless there is a written justification (for example, the
message is from a system under our control and contract violations
must fail loud).

---

## Rule R-6: Connection faults use bounded retry + circuit breaker

Every broker / SCADA client must:

1. Bound reconnect attempts (exponential backoff, cap ≤ 30 s).
2. Expose a circuit-breaker state: `closed` → `open` after N
   consecutive failures; `half_open` probe; back to `closed` on
   success.
3. Report circuit state on the health endpoint.

Never-ending retry loops without visibility = **BLOCKER**.

```python
class BrokerClient:
    """Connection state is visible to the health endpoint."""

    state: Literal["closed", "half_open", "open"] = "closed"
    last_error: str | None = None
    last_success_ts: float = 0.0
```

---

## Rule R-7: Backpressure handled with bounded queues

Every consumer has a bounded ingest queue. On overflow:

- Drop oldest (or newest — project chooses) with reason
  `queue_overflow`.
- Increment drop counter.
- Expose queue depth as a gauge metric.

Unbounded `asyncio.Queue()` = **MAJOR**. Unbounded + no drop metric =
**BLOCKER** (will OOM a worker under traffic spike).

---

## Rule R-8: Dead Letter Queue policy is explicit

For messages that look valid enough to be worth keeping but cannot be
processed right now (downstream DB down, feature-flag off), a DLQ is
appropriate. Rules:

1. DLQ policy MUST be written in the architecture doc — size limit,
   retention, replay procedure.
2. DLQ depth is a Prometheus metric.
3. A DLQ that grows unboundedly and is never drained = **MAJOR**.
4. DLQ never receives unparseable JSON — that is a drop, not a DLQ
   entry. Unparseable messages are dead on arrival.

---

## Rule R-9: Timestamp discipline

External clocks lie. Every message carries either its own timestamp
(from the source) or arrives with a broker timestamp. Rules:

1. **Store both.** `source_ts` and `received_ts` are separate columns.
   Skew analysis becomes trivial later.
2. **Timezone:** all stored timestamps are UTC. A naive
   `datetime.now()` anywhere near data ingestion = **MAJOR**.
3. **Drift guard:** a message whose `source_ts` is > N minutes in the
   future (default N=10) is dropped with reason `timestamp_drift`.
   Clock skew from a misconfigured device should not poison time-
   windowed aggregations.
4. **No reliance on `source_ts` for ordering** unless the source is
   known-good (gps-disciplined clock, atomic time) — use
   `received_ts`.

---

## Rule R-10: Observable counters are mandatory

Every consumer exposes at minimum:

```
# Counters
messages_received_total{source="..."}
messages_dropped_total{source="...", reason="..."}
messages_accepted_total{source="..."}
messages_dlq_total{source="..."}

# Gauges
ingest_queue_depth{source="..."}
broker_connection_state{source="..."}  # 0=closed,1=half,2=open
last_successful_receive_ts{source="..."}

# Histograms
message_processing_seconds{source="...", outcome="..."}
```

A consumer without these counters is operationally blind. An on-call
engineer cannot distinguish "broker is down" from "messages are being
silently dropped" without them. Missing metrics = **MAJOR**, absent
`messages_dropped_total` specifically = **BLOCKER**.

---

## Rule R-11: No business logic in the drop path

A dropped message must NOT trigger side effects downstream. In
particular:

- Do not write a partial row to the analytics DB "just in case".
- Do not emit a downstream event with best-guess fields.
- Do not call an external API with a placeholder payload.

The rationale: we chose to drop specifically because we do not have a
valid business interpretation. Inventing one poisons dashboards.

Violations = **BLOCKER**.

---

## Rule R-12: Test the failure path

Every consumer must have tests for:

- Malformed JSON → drop + counter increments + consumer alive.
- Missing required field → drop with `missing_required_tag:*`.
- Renamed-tag via alias → accepted.
- Circuit breaker opens on N consecutive failures.
- Queue overflow drops oldest + counter.
- Health check goes `degraded` at 10% drop rate, `down` at 50%.

A consumer without failure-path tests = **MAJOR**. Without any
failure tests at all = **BLOCKER**.

---

## Summary table (for reviewer quick reference)

| Rule | Concern                        | Missing → severity |
|------|--------------------------------|--------------------|
| R-1  | Pydantic-boundary validation   | BLOCKER            |
| R-2  | Drop + count + log             | BLOCKER if silent  |
| R-3  | Health reflects drop rate      | MAJOR              |
| R-4  | Versioned schema / tag mapping | BLOCKER if hardcoded for SCADA/FMS |
| R-5  | `extra="ignore"` on external   | MAJOR              |
| R-6  | Bounded retry + circuit breaker| BLOCKER            |
| R-7  | Bounded ingest queue           | MAJOR / BLOCKER    |
| R-8  | Explicit DLQ policy            | MAJOR              |
| R-9  | UTC + drift guard              | MAJOR              |
| R-10 | Observable counters            | MAJOR / BLOCKER    |
| R-11 | No business logic on drop      | BLOCKER            |
| R-12 | Failure-path tests             | BLOCKER if absent  |

Reviewer prompts reference this file directly. Updating a rule means
editing one place, not five.
