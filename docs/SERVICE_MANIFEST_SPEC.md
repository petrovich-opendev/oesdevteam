# SERVICE_MANIFEST.yaml — Specification

> **Audience:** Developer / Architect / DevOps agents working on a Go
> service in a multi-service namespace (any polyglot industrial-domain
> workspace that ships several Go services in one repository or a tightly
> federated set of repositories).
>
> **Authority:** This spec is the source of truth for the file format
> referenced by `AGENT_BRIEF.md`, `prompts/reviewers/senior_go.md`, and
> `docs/PRE_REVIEW_RULES_ROADMAP.md` (rule
> `R-go-proto-service-manifest`).

---

## Why this file exists

A namespace may contain **5–10 Go services** sharing a single repository
(or a tightly federated set of repos). Each service exposes one or more
gRPC servers and consumes RPCs / events from peers. Without a single
source of truth that declares "what runs where, on which port, with
which proto contract", three failure modes are inevitable:

1. **Silent port collisions** — two services pick `:50051` independently
   and the second one fails its readiness probe in production. Caught
   only after deploy.
2. **Phantom dependencies** — service B is updated to call a new RPC on
   service A before service A's PR has merged. The diff looks fine in
   isolation; the breakage shows up at integration time.
3. **Reviewer blind spots** — the senior_go reviewer cannot ask "is this
   new gRPC service registered?" without an authoritative list to check
   against. The manifest is that list.

The manifest is read by:
- The DevTeam Architect agent when planning a feature (to decide which
  services it touches).
- The Developer agent when adding a new gRPC server (to claim a port,
  declare deps).
- The senior_go and senior_sre reviewers (to verify registration,
  health-check paths, dep declarations).
- The DevOps deployment scaffolding (compose / k8s manifests reference
  it for port + image mapping).

The manifest is **NOT** read at service runtime. A service's own config
loader sees env vars, not this file. The manifest is build-time / review-
time / deploy-time metadata.

---

## File location

One per repository, at the repository root:

```
<repo-root>/SERVICE_MANIFEST.yaml
```

For multi-repo industrial-domain topologies each Go-service repo owns
its own manifest; a separate top-level "platform" repo aggregates them
via a manifest-of-manifests (`SERVICE_REGISTRY.yaml`, out of scope here).

---

## Schema (canonical example)

```yaml
# SERVICE_MANIFEST.yaml
# Source of truth for every gRPC service this repository ships.
# See docs/SERVICE_MANIFEST_SPEC.md for the spec this conforms to.

manifest_version: 1
namespace: example-fleet
repo: example-fleet-dispatch     # short repo identifier; matches GitHub repo name

services:
  - name: fleet-dispatch
    # cmd path that registers this gRPC server.
    cmd: cmd/fleet-dispatch/main.go

    # Proto definitions this server implements. Glob allowed; resolved
    # relative to repo root. Reviewers cross-check with `grpc.ServiceDesc`
    # registrations in main.go.
    proto:
      - api/fleet/v1/dispatch.proto

    # Network ports this service binds. Each entry is reserved across
    # the entire namespace — port collision across services is a
    # configuration defect.
    ports:
      grpc: 50051
      http: 8081      # /livez /readyz /healthz + Prometheus /metrics

    # Health-check endpoints (HTTP). All three are mandatory per
    # AGENT_BRIEF.md §3.8. The reviewer rejects a manifest entry that
    # omits any of them.
    health:
      livez:   /livez
      readyz:  /readyz
      healthz: /healthz

    # Other services this one calls. Each entry must resolve to another
    # `services[].name` in this manifest OR in a manifest of a peer
    # repo listed under `external_deps` below.
    deps:
      - service: equipment-state
        rpcs:
          - GetEquipmentState
          - StreamEquipmentEvents
        # Pinned proto-revision hash for breadcrumb tracking — see
        # "Cross-service breadcrumbs" below.
        proto_rev: a3f1c2b

      - service: payload-store
        rpcs:
          - WritePayload
        proto_rev: 7b29d04

    # Brokers this service publishes to / subscribes from.
    # Subjects must match the producer's manifest entry; mismatched
    # subjects are caught by senior_data review.
    nats:
      publishes:
        - subject: fleet.dispatch.commands.v1
      subscribes:
        - subject: equipment.state.events.v1
          durable: fleet-dispatch-state-consumer

    # Owner squad — used by the controller to route review notifications.
    owner: dispatch-squad

  - name: equipment-state
    cmd: cmd/equipment-state/main.go
    proto:
      - api/equipment/v1/state.proto
    ports:
      grpc: 50052
      http: 8082
    health:
      livez:   /livez
      readyz:  /readyz
      healthz: /healthz
    deps: []
    nats:
      publishes:
        - subject: equipment.state.events.v1
    owner: state-squad

# Repos whose manifests this one depends on. Each entry must be a real
# repo with its own SERVICE_MANIFEST.yaml accessible at HEAD of the
# given ref. The `proto_rev` values in services[].deps must resolve
# against the manifest of the named repo.
external_deps:
  - repo: example-fleet-telemetry
    ref: main
  - repo: example-fleet-comms
    ref: main
```

---

## Validation rules (reviewer-enforced)

A reviewer rejects the manifest if any of the following fails. These
double as the future automated lint pass referenced in
`PRE_REVIEW_RULES_ROADMAP.md` § R-go-proto-service-manifest.

1. **Schema integrity.** `manifest_version` must equal `1`. Unknown
   top-level keys = BLOCKER (typo defence).
2. **Name uniqueness.** Every `services[].name` is unique within the
   manifest. The same name may not appear in two different repo
   manifests in the same namespace either (cross-repo check at the
   `SERVICE_REGISTRY.yaml` aggregator level).
3. **Port uniqueness.** No two `services[]` entries share a `ports.grpc`
   or `ports.http` value. Ports outside `1024–65535` rejected.
4. **Health endpoints required.** All three of `livez`, `readyz`,
   `healthz` must be present and start with `/`. A service without all
   three cannot be deployed.
5. **Proto path exists.** Every glob in `services[].proto` must resolve
   to at least one `.proto` file at the repo root.
6. **Dep resolution.** Each `services[].deps[].service` must resolve to
   another `services[].name` in this manifest OR to a service declared
   by a repo listed in `external_deps`. Dangling deps = BLOCKER.
7. **RPC list non-empty.** A `deps[]` entry with empty `rpcs` is
   meaningless — flag as a configuration defect.
8. **`cmd` exists.** The `cmd` path must point to a `main.go` that
   exists in the working tree. Reviewers verify this by reading the
   diff plus the existing tree.
9. **gRPC registration matches.** For each service, the `main.go` at
   `cmd` must contain a `grpc.RegisterXxxServer` call referencing each
   proto under `services[].proto`. Manifest entries that have no
   matching registration in the source = BLOCKER.

Rules (1)–(7) can be machine-checked from the YAML alone. Rules
(8)–(9) require diffing against the repo state, which is why the
automated check is deferred (see `PRE_REVIEW_RULES_ROADMAP.md`).

---

## Cross-service breadcrumbs

When a feature changes a `.proto` file, the change ripples to every
consumer. The manifest helps the reviewer detect this; the
**breadcrumb** mechanism makes it actionable.

### Breadcrumb file format

When the Developer agent commits a proto change, it writes a breadcrumb
alongside the feature output:

```
proto_changes/<feature_id>.yaml
```

Schema:

```yaml
feature_id: 2026-Q2-fleet-dispatch-rebid
proto_change_kind: minor    # one of: minor | breaking
producer:
  repo: example-fleet-dispatch
  service: fleet-dispatch
  proto: api/fleet/v1/dispatch.proto
  before_rev: a3f1c2b
  after_rev:  c8d4e10
diff_summary: |
  Added optional field `priority_band` to `BidRequest`.
  No removed or renamed fields. No removed RPCs.

# Consumer services that import the changed proto, computed by walking
# every other manifest in the namespace and matching `deps[].service`
# against the producer.
affected_consumers:
  - repo: example-fleet-comms
    service: bid-arbiter
    needs_action: false   # additive change, consumer still compiles
  - repo: example-fleet-telemetry
    service: equipment-bidder
    needs_action: false
```

For a `breaking` change the breadcrumb sets `needs_action: true` on
each consumer and the controller MUST refuse to mark the producer
feature `done` until a follow-on feature exists for each affected
consumer (see "Two-phase cross-service feature pattern" below).

### Why a separate file (not git-log mining)

Breadcrumbs are *deterministic input* to the reviewer and the
controller. Inferring "did this PR change a proto?" from `git diff`
works at PR time but loses fidelity once the PR is squashed and merged.
Persisting the breadcrumb in the working tree:
- Survives squash-merge.
- Lets the next feature read it without re-running buf against history.
- Gives the senior_data reviewer something to cite by file path.

The breadcrumb file is consumed by the cross-service follow-up feature
and then deleted by that feature's PR (as the obligation has been
discharged). Stale breadcrumbs in `proto_changes/` indicate unfinished
cross-service work.

---

## Two-phase cross-service feature pattern

### The problem

A single feature like "fleet-dispatch service starts honouring
`priority_band` from `BidRequest`" actually spans two services:

- **Producer** (fleet-dispatch): adds the field to the proto, implements
  the server-side honouring, deploys.
- **Consumer** (bid-arbiter, in the comms repo): reads the new field
  from incoming bids, populates it from its own logic.

A single PR cannot atomically deploy both — the services live in
different repos, ship on different cadences. A naive "do it all at
once" pattern produces either a deploy gap (consumers send empty
fields against an old producer that errors on unknown fields) or a
compile gap (consumer's import refers to a field the producer's
released proto doesn't yet have).

### The pattern

The DevTeam controller decomposes any cross-service proto change into
**two ordered features**, with the breadcrumb as the contract between
them:

#### Phase 1 — Producer feature

1. Producer's PO creates `feature_<n>` in the producer repo.
2. Developer modifies the proto **additively** (new fields are
   `optional`, new RPCs are added but old RPCs preserved).
3. Producer code reads the new field with a default for absent values
   (the deploy gap is bridged by graceful defaulting).
4. `make buf-breaking` must pass — this proves the change is
   non-breaking.
5. Developer writes `proto_changes/<feature_id>.yaml` (see breadcrumb
   format above).
6. Reviewers approve; producer feature merges and deploys.
7. The breadcrumb file is committed alongside the producer code; the
   producer feature is `done`.

A truly breaking change (field removed, RPC renamed) requires a
**three-phase** variant: phase 0 deprecates the old surface, phase 1 (in
the producer) lands the new surface, phase 2 in each consumer migrates.
Buf-breaking will refuse phase 1 if the deprecation step is skipped —
this is the safety net.

#### Phase 2 — Consumer feature(s)

1. Consumer's PO creates `feature_<n>` in the consumer repo, **citing
   the breadcrumb file** in its requirements section. The controller
   refuses to schedule a phase-2 feature whose breadcrumb does not
   exist in the producer repo's `proto_changes/`.
2. Developer bumps the proto-rev in the consumer's
   `SERVICE_MANIFEST.yaml` `deps[].proto_rev` to match the producer's
   `after_rev`.
3. Developer regenerates protobuf bindings, implements the consumer
   change, re-runs all gates.
4. Reviewers approve; consumer feature merges. The PR also **deletes**
   the breadcrumb (`rm proto_changes/<feature_id>.yaml`) — this
   discharges the obligation.

The controller's invariant is:

> No `proto_changes/*.yaml` file with `needs_action: true` for any
> consumer may exist on `main` for longer than the configured
> `cross_service_grace_period` (default: 7 days). Stale breadcrumbs
> page the namespace owner via the SRE oncall channel.

This grace period is the only thing standing between "we shipped the
producer change" and "consumers silently fell behind for a quarter".

### Why this matters for review

The senior_go reviewer for a producer feature checks:
- Is the proto change additive? (`make buf-breaking` will catch it,
  but the reviewer sanity-checks too.)
- Is the breadcrumb file present and well-formed?
- Does the breadcrumb's `affected_consumers` list match what walking
  the manifest registry would produce?

The senior_go reviewer for a consumer feature checks:
- Does the cited breadcrumb exist?
- Is the proto-rev bump in the manifest correct?
- Does the PR delete the breadcrumb on success?

These checks are not yet automated; they live in the reviewer prompts
under `prompts/reviewers/senior_go.md` § "Contract stability" and the
breadcrumb file is the durable artefact that keeps them honest.

---

## Open items (intentionally out of scope for v1)

The following will be added once a concrete feature requires them — per
AGENT_BRIEF.md § 0, we do not build infrastructure ahead of demand.

- **`SERVICE_REGISTRY.yaml`** — namespace-wide aggregator across repos.
  Required when ≥ 3 repos ship in the same namespace.
- **Automated manifest validator (Python).** The schema rules in
  "Validation rules" above can be checked by a small `pyyaml + voluptuous`
  module under `src/manifests/`. Will land when the first manifest is
  authored end-to-end.
- **Manifest-driven port allocation.** A CLI command
  `devteam manifest claim-port <repo>` that picks the next free port
  across the registry. Useful at ~10 services; below that, manual
  selection is fine.
- **buf-breaking integration with breadcrumb generation.** The
  Developer agent currently writes the breadcrumb manually. Wiring
  `buf breaking` JSON output into auto-generated `proto_changes/*.yaml`
  is a future ergonomics improvement, not a correctness fix.

---

## Cross-references

- `prompts/reviewers/senior_go.md` § "Configuration this prompt assumes"
  and § "Contract stability" — reviewer enforcement.
- `docs/PRE_REVIEW_RULES_ROADMAP.md` § R-go-proto-service-manifest,
  § R-go-proto-no-breaking — deferred-rule references.
- `namespaces/<env>/<domain>/AGENT_BRIEF.md` § 4 (Repository layout)
  — where the file lives.
- `docs/RESILIENCE_RULES.md` R-1, R-2 — message-shape contract that
  protobuf changes must preserve at the broker boundary.
