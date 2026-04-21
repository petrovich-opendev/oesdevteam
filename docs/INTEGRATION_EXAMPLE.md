# Integration example — wiring v2 gates into the v1 FeatureController

This document shows the smallest concrete code change required to
wire the OESDevTeam v2 gates into the existing
`/home/dev/devteam/src/feature_controller.py`. It's intended as
reference for Phase 3+ of `MIGRATION_PLAN.md`.

## Shape of the change

The v1 controller already runs `_fast_verify(feature)` between
implementation and commit. The v2 integration inserts the API contract
gate, the Senior Review gate, and (when applicable) the SRE gate
between `_fast_verify` and `_git_commit`:

```
Before (v1):
    implement → _fast_verify → _git_commit

After (v2):
    implement → _fast_verify → api_contract_gate
                             → senior_review_gate
                             → sre_review_gate (if deploy-surface)
                             → _git_commit
```

A blocked gate converts the feature's status to `NEEDS_REWORK` with a
reflection payload that the next attempt consumes. Unblocked gates
are invisible (gate result logged, nothing halted).

## Minimum code delta

### 1. New imports at the top of `feature_controller.py`

```python
from .gates import (
    ApiContractGate,
    ApiContractConfig,
    CodeReviewGate,
    GateInput,
    SreReviewGate,
    SreReviewConfig,
)
from .reviewers import ClaudeCliReviewerRunner
from .observability import CostTracker, FeatureBudget, BudgetExceeded
```

### 2. Initialise collaborators in `FeatureController.__init__`

```python
def __init__(self, namespace_path: Path):
    ...  # existing v1 init
    self.cost = CostTracker()
    self.runner = ClaudeCliReviewerRunner()
    self._api_gate = ApiContractGate(config=ApiContractConfig.load())
    self._code_review_gate = CodeReviewGate(runner=self.runner)
    self._sre_gate = SreReviewGate(
        runner=self.runner,
        config=SreReviewConfig.load(),
    )
```

### 3. New method — `_run_gates`

```python
async def _run_gates(
    self,
    feature: Feature,
    diff: str,
    files_changed: list[str],
) -> GateResult | None:
    """Run the v2 gate chain. Returns the first blocking result, or None
    if every gate passed (or short-circuited as not-applicable).

    Order matters: cheap deterministic gates first (API contract), then
    Opus-backed review gate, then SRE (only on deploy-surface).
    """
    gate_input = GateInput(
        feature_id=feature.id,
        feature_goal=feature.description,
        files_changed=files_changed,
        diff=diff,
        domain_context=self._load_domain_context(),
        verify_commands=feature.verify or [],
    )

    # 1. API Contract Gate (deterministic, fast, no LLM cost)
    api_result = await self._api_gate.check(gate_input)
    self._publish_gate_event(api_result)
    if not api_result.passed:
        return api_result

    # Budget check before spending Opus tokens on reviewers.
    try:
        self.cost.assert_within_budget(feature.id)
    except BudgetExceeded as e:
        feature.status = TaskStatus.STUCK
        feature.error = str(e)
        return None

    # 2. Senior Review Gate (five reviewers in parallel)
    review_result = await self._code_review_gate.check(gate_input)
    self._publish_gate_event(review_result)
    # Record reviewer cost into the tracker (each reviewer's cost comes
    # back from the ClaudeCliReviewerRunner's last_call_cost — see
    # Phase 6 of the migration plan).
    for call_cost in self.runner.drain_costs():
        self.cost.record(
            feature_id=feature.id,
            role=call_cost.role,
            model=call_cost.model,
            cost_usd=call_cost.cost_usd,
        )
    if not review_result.passed:
        return review_result

    # 3. SRE Gate (only when deploy surface is touched — the gate
    # itself short-circuits to PASS on non-deploy diffs).
    sre_result = await self._sre_gate.check(gate_input)
    self._publish_gate_event(sre_result)
    if not sre_result.passed:
        return sre_result

    return None  # every gate passed or was not applicable
```

### 4. Convert a blocking gate into `NEEDS_REWORK`

```python
async def _handle_one_attempt(self, feature: Feature) -> None:
    diff, files_changed = await self._implement(feature)
    if not await self._fast_verify(feature):
        feature.status = TaskStatus.NEEDS_REWORK
        feature.reflection = self._reflection_from_verify(feature)
        return

    blocking = await self._run_gates(feature, diff, files_changed)
    if blocking is not None:
        feature.status = TaskStatus.NEEDS_REWORK
        feature.reflection = self._reflection_from_gate(blocking)
        return

    if not await self._git_commit(feature):
        feature.status = TaskStatus.NEEDS_COMMIT
        return

    feature.status = TaskStatus.DONE
    self._append_success_pattern(feature)  # Step 8
```

### 5. Reflection shape

The `reflection` field from a gate block is what the next attempt
sees. v2 ships it as structured Markdown so the Developer agent
doesn't have to interpret JSON:

```python
def _reflection_from_gate(self, result: GateResult) -> str:
    """Convert a gate block into a prompt the next attempt can read."""
    if result.gate_type == QualityGateType.SENIOR_REVIEW:
        from .gates.code_review_gate import render_code_review_report
        return render_code_review_report(result)
    if result.gate_type == QualityGateType.API_CONTRACT:
        from .gates.api_contract_gate import render_api_contract_report
        return render_api_contract_report(result)
    if result.gate_type == QualityGateType.SRE_REVIEW:
        from .gates.sre_review_gate import render_sre_review_report
        return render_sre_review_report(result)
    # Fallback — the base renderer still produces something readable.
    from .gates.base import format_gate_report
    return format_gate_report(result)
```

## Event publication

Each gate outcome should be published over NATS so the existing
event-trace infrastructure captures it:

```python
def _publish_gate_event(self, result: GateResult) -> None:
    event = Event(
        type=f"gate.{result.gate_type.value}",
        data=result.model_dump(),
        model=None,  # gates themselves do not have a model
    )
    # Existing v1 plumbing — publishes to `devteam.gate.*`
    self.nats.publish_sync(f"devteam.gate.{result.gate_type.value}", event.to_json())
```

Subscribers (dashboards, test harnesses, the admin UI) get gate
outcomes immediately — no extra instrumentation required.

## What this does NOT change

- The existing v1 `retries` / `stuck` / `needs_commit` state machine.
- The existing JSONL pipeline log schema (we add new record types,
  we do not rename old ones).
- Any existing namespace's `features.json` schema. Gates read whatever
  the controller already has on disk.

## Testing the integration

After wiring, run one feature end-to-end with gates enabled:

```bash
cd /home/dev/devteam
OESDEVTEAM_SENIOR_REVIEW_DISABLED= \
    python3 run_features.py namespaces/dev/biocoach
```

Expected outcome: the feature either passes every gate (and commits
as before) or moves to `NEEDS_REWORK` with a Markdown reflection the
next attempt can consume.
