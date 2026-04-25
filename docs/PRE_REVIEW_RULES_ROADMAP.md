# Pre-review Rules Engine — Deferred-Rules Roadmap

This document is the contract between the pre-review rules engine
(`src/rules/`) and the namespace **configuration** that every
Go-targeting industrial-domain project is required to ship.

The pre-review engine ships a deliberately small set of Go rules
(`R-go-silent-err`, `R-go-sql-concat` plus the language-agnostic
`R-scaffold-only` and `R-goal-file-missing`). The rules below are
**deferred from the engine on purpose** — they would either produce
false positives at the regex layer or duplicate work that
`golangci-lint`, `go vet`, `govulncheck`, and `buf breaking` already do
well. Instead, they appear here as **configuration requirements**: a
namespace that targets Go must satisfy them via its tooling
configuration, and the senior_go reviewer enforces them on top.

## Why deferred, not deleted

A bad pre-review rule is worse than a missing one. False positives erode
reviewer trust and train the LLM senior squad to ignore the engine's
findings — exactly the failure mode the engine exists to prevent. Each
rule below was evaluated against the bar "can a single-pass regex / AST
walk catch this with > 95 % precision against realistic Go code?". The
ones that fail that bar live here, not in the engine.

The senior_go reviewer prompt **must reference this document** so any
violation it spots cites the rule ID below; that gives a stable handle
for fixes and for future migration into the engine if a reliable
detector is built.

---

## Mandatory namespace tooling

Every Go-targeting namespace ships **both** of the following at the repo
root, exactly as the project root expects:

- `.golangci.yml` — pinned lint stack (see "golangci-lint baseline" below).
- `Makefile` (or equivalent task runner) with at minimum:
  - `make lint`     → `golangci-lint run ./...`
  - `make vet`      → `go vet ./...`
  - `make vuln`     → `govulncheck ./...`
  - `make buf-breaking` → `buf breaking --against '.git#branch=main'`
    (only when the repo contains protobuf definitions)
  - `make test`     → `go test -race -timeout 30s ./...`

The DevTeam pipeline's `verify_commands` for a Go feature must invoke
these targets. A namespace that omits them is a configuration bug, not
a feature bug.

### golangci-lint baseline

Required enabled linters (additive — namespaces may enable more, must
not disable any):

```yaml
linters:
  enable:
    - errcheck         # catches unchecked errors — pairs with R-go-silent-err
    - errorlint        # forces fmt.Errorf("...: %w", err) over %v / %s
    - bodyclose        # http.Response.Body must be closed
    - sqlclosecheck    # database/sql Rows / Stmt must be closed
    - rowserrcheck     # rows.Err() must be checked after iteration
    - contextcheck     # ctx must propagate through call chain
    - noctx            # no http.Get / *http.Client without ctx
    - exhaustive       # type-switch over enums must be exhaustive
    - gosec            # security baseline
    - govet
    - staticcheck
    - unused
    - ineffassign
    - gocritic
```

The combination above subsumes most of the deferred rules below, which
is why the engine does not duplicate them.

---

## Deferred rules — by category

Each rule has:
- **R-id**       — stable identifier reusable in reviewer findings.
- **Why deferred** — what would go wrong if the engine tried to detect this.
- **Configured by** — the lint setting / convention that enforces it.
- **Reviewer cite** — short phrase the senior_go reviewer should use
  when surfacing the violation.

### Resource lifecycle

#### R-go-defer-rows-close
- **Definition:** Every `rows, err := db.Query(...)` (and pgx variants)
  must be followed by `defer rows.Close()` in the same scope.
- **Why deferred:** Detecting "missing defer" by regex is brittle —
  the close can legitimately live in a helper, in an explicit
  `rows.Close()` call before return, or be tied to a different
  variable name. AST analysis would need cross-statement reasoning.
- **Configured by:** `sqlclosecheck` in golangci-lint.
- **Reviewer cite:** "rows / stmt resource not closed (sqlclosecheck)".

#### R-go-defer-body-close
- **Definition:** Every `resp, err := httpClient.Do(...)` (and Get /
  Post / etc.) must be followed by `defer resp.Body.Close()`.
- **Why deferred:** Same reasoning as `R-go-defer-rows-close`; the
  `bodyclose` linter handles this via SSA analysis.
- **Configured by:** `bodyclose` in golangci-lint.
- **Reviewer cite:** "http.Response.Body not closed (bodyclose)".

#### R-go-rows-err
- **Definition:** Code that iterates `rows.Next()` must check
  `rows.Err()` after the loop — otherwise a driver-level error during
  iteration is silently dropped.
- **Why deferred:** Detection requires matching loop boundary against
  the variable's lifetime; `rowserrcheck` does this with SSA.
- **Configured by:** `rowserrcheck` in golangci-lint.
- **Reviewer cite:** "rows.Err() unchecked after iteration".

### Networking

#### R-go-no-default-http-client
- **Definition:** `http.DefaultClient`, `http.Get`, `http.Post`,
  `http.Head` are forbidden in production code paths. Every HTTP call
  must use a project-level `httpClient` with explicit timeout, TLS
  config, and `context.Context` propagation.
- **Why deferred:** A grep for `http.DefaultClient` is easy, but a
  realistic industrial-Go code base will legitimately reference these
  symbols in vendor-shim layers and tests; surfacing every occurrence
  would drown the engine. `noctx` covers the missing-context dimension
  and the senior_go reviewer covers the per-call config dimension.
- **Configured by:** `noctx` in golangci-lint, plus a project-level
  `internal/httpx` package providing the configured client.
- **Reviewer cite:** "use internal/httpx.Client, not http.Default*".

#### R-go-ctx-propagation
- **Definition:** Every exported function that does I/O (DB, HTTP,
  NATS, Kafka, file) must accept a `ctx context.Context` as its first
  parameter and pass it through.
- **Why deferred:** Project-wide AST analysis required.
- **Configured by:** `contextcheck` + `noctx` in golangci-lint.
- **Reviewer cite:** "context.Context not propagated through I/O call".

### Error handling

#### R-go-error-wrap-verb
- **Definition:** Errors are wrapped with `%w`, never `%v` or `%s`.
  Bare `fmt.Errorf("read failed: %s", err)` loses the error chain.
- **Why deferred:** The `errorlint` linter handles this exhaustively
  with proper type tracking; a regex over format strings would miss
  cases where the format string is a constant defined elsewhere.
- **Configured by:** `errorlint` in golangci-lint.
- **Reviewer cite:** "fmt.Errorf must use %w to preserve error chain".

#### R-go-no-bare-panic
- **Definition:** `panic(...)` is forbidden in handler / service /
  consumer code paths. Permitted only in `main`, in package init, or
  inside a clearly-scoped `recover()` boundary in a worker pool.
- **Why deferred:** A regex catch on `panic(` would flag legitimate
  uses in low-level packages (e.g. internal invariants on a
  state-machine transition). The senior_go reviewer judges intent.
- **Configured by:** Convention + reviewer enforcement; `gocritic` and
  `gosec` cover related cases (panicking on user input).
- **Reviewer cite:** "panic() forbidden in this layer — return error".

#### R-go-errcheck
- **Definition:** Every error-returning call must have its error
  inspected (assigned and used, or explicitly discarded with a
  comment).
- **Why deferred:** This is exactly what `errcheck` does; duplicating
  it would produce redundant findings.
- **Configured by:** `errcheck` in golangci-lint.
- **Reviewer cite:** "unchecked error from <call> (errcheck)".

### Concurrency

#### R-go-waitgroup-add-before-go
- **Definition:** `sync.WaitGroup.Add(n)` must be called **before** the
  `go func() { defer wg.Done(); ... }()` it tracks. Adding inside the
  goroutine is a textbook race condition.
- **Why deferred:** Order of operations across statement boundaries
  requires SSA / control-flow analysis; static linters
  (`govet -copylocks`, `staticcheck`) catch many but not all
  variants. The senior_go reviewer enforces the pattern.
- **Configured by:** `govet`, `staticcheck`; convention enforcement.
- **Reviewer cite:** "wg.Add must precede `go`, not run inside".

#### R-go-channel-close-ownership
- **Definition:** Only the **producer** closes a channel. A consumer
  closing a shared channel is a data-race / panic primitive.
- **Why deferred:** Producer / consumer roles are not visible at the
  syntax layer; reviewer judgment required.
- **Configured by:** Convention; `staticcheck` flags some misuse.
- **Reviewer cite:** "channel closed by non-producer".

### Protobuf / gRPC

#### R-go-proto-no-breaking
- **Definition:** Changes to `*.proto` files must not introduce
  breaking changes against `main` (renumbered fields, removed fields
  without `reserved`, changed types).
- **Why deferred:** Buf-breaking compares against a git revision, not
  a single diff hunk; the engine has no access to the comparison
  baseline.
- **Configured by:** `buf breaking --against '.git#branch=main'` as
  part of the `make buf-breaking` target.
- **Reviewer cite:** "proto change is breaking — see buf-breaking".

#### R-go-proto-service-manifest
- **Definition:** Every gRPC service registered in `cmd/<svc>/main.go`
  must appear in the namespace's `SERVICE_MANIFEST.yaml` with its
  proto file path, port, health-check path, and dependency list. The
  manifest schema and the cross-service two-phase pattern are defined
  in `docs/SERVICE_MANIFEST_SPEC.md`.
- **Why deferred:** Cross-file consistency (Go source vs YAML
  manifest) is exactly the kind of project-wide check that belongs in
  a dedicated lint pass — it will move into the engine once the
  spec stabilises and the first manifest validator (Python, planned in
  `src/manifests/`) lands.
- **Configured by:** Future static check (tracked in pipeline planning);
  reviewer enforcement until then.
- **Reviewer cite:** "service not registered in SERVICE_MANIFEST.yaml"
  (validation rules 1–9 in the spec).

### Security

#### R-go-vuln
- **Definition:** No dependency in `go.sum` may have a known CVE listed
  by `govulncheck`.
- **Why deferred:** Vulnerability data updates daily; embedding a
  vuln list in the engine would rot. `govulncheck` queries the live
  Go vuln database.
- **Configured by:** `govulncheck ./...` as part of `make vuln`,
  enforced by the SECURITY_SCAN gate (Step 11 of v2 pipeline).
- **Reviewer cite:** "govulncheck flagged <module>@<version>".

#### R-go-gosec-baseline
- **Definition:** Standard Go security smells — weak crypto, hardcoded
  credentials, command injection via `exec.Command`, insecure TLS
  config — are blocked.
- **Why deferred:** `gosec` covers this with rules tuned for Go.
- **Configured by:** `gosec` enabled in golangci-lint; `make lint`
  fails on any G-* finding.
- **Reviewer cite:** "gosec G<NN>: <description>".

### Observability

#### R-go-otel-spans-on-io
- **Definition:** Every outbound DB / HTTP / NATS / Kafka call must
  start an OpenTelemetry span with the operation name, propagate
  `context.Context`, and record errors via `span.RecordError`.
- **Why deferred:** Detecting "is this an I/O call?" requires type
  resolution; reviewer enforcement is more reliable than regex.
- **Configured by:** Convention + `internal/otelx` helpers + reviewer.
- **Reviewer cite:** "I/O call without OpenTelemetry span".

#### R-go-slog-structured
- **Definition:** Logging uses `log/slog` with key-value pairs, never
  `fmt.Sprintf` into a single string. Required keys for I/O failures:
  `op`, `err`, `attempt`.
- **Why deferred:** Recognising "this is a log call" requires tracking
  imports; `forbidigo` could ban `fmt.Print*`, but slog adoption is
  better enforced via the `internal/logx` wrapper plus reviewer.
- **Configured by:** Convention + `forbidigo` (optional) + reviewer.
- **Reviewer cite:** "log call must be structured (slog), not fmt-formatted".

### Idempotency

#### R-go-idempotency-key
- **Definition:** Every NATS / Kafka consumer that mutates state must
  read an idempotency key from the message and dedupe via a
  persistent store before applying the mutation. State without a
  dedupe primitive is replay-broken.
- **Why deferred:** Recognising "this consumer mutates state" requires
  semantic understanding of the service's domain; reviewer (senior_go
  + senior_data + senior_domain_logic) is the right layer.
- **Configured by:** Convention + reviewer enforcement + integration
  test `test_replay_dedupe`.
- **Reviewer cite:** "consumer lacks idempotency key check".

---

## Migration path

A rule moves from this document into `src/rules/rules_go.py` when:

1. A reliable detector exists (golangci-lint custom rule, `go-ruleguard`
   pattern, or AST analysis with > 95 % precision).
2. The detector has been validated against three real namespaces.
3. The senior_go reviewer prompt is updated to defer to the engine for
   that rule ID rather than re-detecting it.

When that happens, delete the rule's section from this document and add
the rule entry to `DEFAULT_RULES` in `src/rules/engine.py`. The rule ID
stays the same so existing reviewer findings keep their stable handle.

## Cross-references

- `src/rules/engine.py` — the engine and rule registry.
- `src/rules/rules_go.py` — the active Go rules (silent-err, sql-concat).
- `prompts/reviewers/senior_go.md` — reviewer prompt; must cite this
  document for every deferred-rule violation it surfaces.
- `docs/RESILIENCE_RULES.md` — R-1 … R-12 cross-language rules
  (Python, Go, TS) that the senior squad enforces.
