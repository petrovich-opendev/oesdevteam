# Migration Plan — OESDevTeam → /home/dev/devteam

This document is the concrete checklist for merging the v2 modules
shipped in `/home/dev/OESDevTeam/` into the production pipeline at
`/home/dev/devteam/`.

## Guiding principles

1. **v1 keeps running until v2 is proven.** The migration is additive:
   every new module lands alongside existing code. Only after a full
   validation pass do we swap call sites.

2. **One module at a time.** Step 1 (model routing) lands first, smoke-
   tested on a real feature, committed. Then Step 2 (reviewers). Then
   Step 3 (gate). Breaking the migration into ~9 commits keeps any
   regression localised to one revert.

3. **No silent behaviour change.** Every gate can be disabled via env
   (`OESDEVTEAM_DISABLE_SENIOR_REVIEW=1`, etc.). The controller's first
   v2 run can flip them on one at a time.

## Phase 0 — Pre-flight checks

- [ ] Confirm `/home/dev/devteam/` has no uncommitted work:
  `git -C /home/dev/devteam status` → clean.
- [ ] Back up v1 state: `cp -r /home/dev/devteam /home/dev/devteam-v1-backup-YYYYMMDD`.
- [ ] Smoke-test OESDevTeam against the real Claude CLI (cost ~$1):
  ```
  cd /home/dev/OESDevTeam
  python3 scripts/smoke_squad.py --roles senior_backend
  ```
  Green output → the bridge works. Proceed.

## Phase 1 — Model routing

**Goal:** get `claude -p` invocations in v1 to go through
`build_claude_cli_command` with explicit `--model`.

- [ ] Copy:
  - `OESDevTeam/src/config.py` → `devteam/src/config.py`
  - `OESDevTeam/config/models.yaml` → `devteam/config/models.yaml`
  - Add `OESDevTeam/src/claude_bridge.py`'s `build_claude_cli_command`
    function into the v1 `claude_bridge.py` as a new public helper
    (keep the existing class intact for now).
- [ ] Update `ClaudeBridge._run_task` in v1 to build its argv via
  `build_claude_cli_command` instead of inline `cmd = [...]`.
- [ ] Run existing v1 tests. Any failure is either a model-name typo
  or an import path issue — fix before proceeding.
- [ ] Commit: `feat(migration-1): route agent calls through explicit model pin`.

**Validation:** run one existing feature through v1. Verify the JSONL
pipeline log contains an explicit `"model": "claude-opus-4-7"` line.

## Phase 2 — Reviewer infrastructure (non-binding)

**Goal:** get the five Senior Reviewers runnable, but NOT yet blocking
commits. Advisory mode lets v1 lessons accumulate against the new
reviewers before they vote on production.

- [ ] Copy `OESDevTeam/src/reviewers/` → `devteam/src/reviewers/`.
- [ ] Copy `OESDevTeam/prompts/reviewers/` → `devteam/prompts/reviewers/`.
- [ ] Add to `devteam/src/feature_controller.py`:
  ```python
  from .reviewers import ClaudeCliReviewerRunner, ReviewInput, run_reviewer_squad

  async def _advisory_senior_review(self, feature, diff, files_changed):
      """Advisory squad — run but never block. Results logged only."""
      if os.environ.get("OESDEVTEAM_SENIOR_REVIEW_DISABLED"):
          return
      runner = ClaudeCliReviewerRunner()
      review_input = ReviewInput(
          feature_id=feature.id,
          feature_goal=feature.description,
          files_changed=files_changed,
          diff=diff,
          domain_context=self._load_domain_context(),
      )
      result = await run_reviewer_squad(review_input, runner)
      self._log_advisory(result)
  ```
- [ ] Call `_advisory_senior_review` from the existing verify step.
- [ ] Commit: `feat(migration-2): run Senior Reviewer squad in advisory mode`.

**Validation:** run 5-10 real features in advisory mode. Compare
squad findings to actual PR outcomes. If squad reliably catches real
issues, proceed. If reviewers produce too many false positives, tune
prompts in place (hot-editable, no deploy needed) before the next
phase.

## Phase 3 — Code Review Gate (blocking)

**Goal:** flip the squad from advisory to binding.

- [ ] Copy `OESDevTeam/src/gates/` → `devteam/src/gates/`.
- [ ] Replace `_advisory_senior_review` with a gate call:
  ```python
  from .gates import run_code_review_gate, GateInput

  async def _senior_review_gate(self, feature, diff, files_changed):
      if os.environ.get("OESDEVTEAM_SENIOR_REVIEW_DISABLED"):
          return  # emergency bypass
      gate_input = GateInput(
          feature_id=feature.id, feature_goal=feature.description,
          files_changed=files_changed, diff=diff,
          domain_context=self._load_domain_context(),
      )
      result = await run_code_review_gate(gate_input, ClaudeCliReviewerRunner())
      if not result.passed:
          feature.status = TaskStatus.NEEDS_REWORK
          feature.rework_reason = result.reason
          feature.rework_details = result.details
          raise GateBlocked(result)
  ```
- [ ] Wire gate call between the existing `verify` step and
  `git commit`. If the gate blocks, the commit is skipped and the
  feature moves to `needs_rework`.
- [ ] Commit: `feat(migration-3): Senior review gate now blocks commits`.

**Validation:** run the same features again. Expect most to pass (they
passed advisory mode in Phase 2). A block rate above ~15% means the
reviewers are too strict — tune prompts before accepting.

## Phase 4 — API Contract Gate

- [ ] Copy `OESDevTeam/config/api_contract.yaml` →
  `devteam/config/api_contract.yaml`. Adjust patterns if v1 project
  layout differs (check `backend_schema.patterns`).
- [ ] Wire `run_api_contract_gate` in `feature_controller.py` **before**
  the Senior Review gate (it is deterministic and cheap — fail fast
  without burning Opus tokens on features with obvious contract drift).
- [ ] Commit: `feat(migration-4): API contract gate in pipeline`.

## Phase 5 — SRE Review Gate

- [ ] Copy `OESDevTeam/prompts/reviewers/senior_sre.md` →
  `devteam/prompts/reviewers/senior_sre.md`.
- [ ] Copy `OESDevTeam/config/sre_review.yaml` →
  `devteam/config/sre_review.yaml`.
- [ ] Add `AgentRole.SENIOR_SRE` to `devteam/src/models.py` and the
  corresponding entry in `devteam/config/models.yaml`.
- [ ] Wire `run_sre_review_gate` after the code-review gate but before
  the deploy step in the GoalController.
- [ ] Commit: `feat(migration-5): SRE gate for deploy-surface features`.

## Phase 6 — Cost tracker + Langfuse

- [ ] Copy `OESDevTeam/src/observability/` → `devteam/src/observability/`.
- [ ] Instrument `ClaudeBridge` to record cost per call via
  `CostTracker.record(...)`.
- [ ] Call `tracker.assert_within_budget(feature_id)` at the top of every
  retry iteration. A `BudgetExceeded` moves the feature to `stuck`.
- [ ] Wire `NullExporter` by default. If `LANGFUSE_PUBLIC_KEY` is in
  env, construct `LangfuseExporter` and pass LLM spans through it.
- [ ] Commit: `feat(migration-6): aggregate cost tracker + optional Langfuse`.

## Phase 7 — Escalation + learning loop

- [ ] Copy `OESDevTeam/src/controller/` → `devteam/src/controller/` (or
  the nearest namespace if that path collides — `devteam/src/learning/`
  is acceptable).
- [ ] Replace the existing v1 stuck-feature handler with:
  ```python
  if should_escalate(feature.retries):
      esc = FeatureEscalation(
          feature_id=feature.id, goal=feature.description,
          attempts=self._attempt_history(feature),
          files_touched=tuple(feature.files_changed or ()),
          cost_usd=self.cost.total_for_feature(feature.id),
      )
      report = generate_escalation_report(esc)
      (TASKS_DIR / "backlog" / f"escalation-{feature.id}.md").write_text(report)
      feature.status = TaskStatus.STUCK
  ```
- [ ] On `done` transition, call `extract_success_pattern` +
  `append_success_pattern` to accumulate v2's `success_patterns.md`.
- [ ] Update agent-prompt building to use `load_memory_blob` instead of
  reading `lessons_learned.md` directly.
- [ ] Commit: `feat(migration-7): escalation reports + positive learning loop`.

## Phase 8 — Resilience rules enforcement

- [ ] Copy `OESDevTeam/docs/RESILIENCE_RULES.md` → `devteam/docs/RESILIENCE_RULES.md`.
- [ ] Ensure reviewer prompts in the v1 tree reference the doc exactly
  as the OESDevTeam versions do. Easier path: copy the OESDevTeam
  `prompts/reviewers/*.md` verbatim.
- [ ] Update `devteam/CLAUDE.md` with the External Data Resilience
  section and two new "Do NOT" bullets.
- [ ] Commit: `feat(migration-8): resilience rules enforced by reviewers`.

## Phase 9 — Validation sweep

- [ ] Re-run one feature from each existing namespace (biocoach,
  insight-portal, devteam itself) end-to-end through the migrated
  pipeline.
- [ ] Verify every pipeline log carries explicit model name, cost
  entry, and gate outcomes.
- [ ] Diff the `pipeline-log/features.log` shape before / after
  migration. Document any schema change in
  `docs/LOG_FORMAT_MIGRATION.md`.
- [ ] Only after two consecutive successful runs: delete the env-var
  bypass flags added in Phase 2 (no more "disable senior review"
  escape hatch). Commit: `chore(migration): remove advisory-mode bypass flags`.

## Rollback procedure

At any phase, if production behaviour regresses:

1. `git -C /home/dev/devteam revert <migration-commit-sha>`.
2. If multiple phases need reverting, revert in reverse order
   (Phase 7 before Phase 6, etc.).
3. The `/home/dev/devteam-v1-backup-YYYYMMDD` copy created in Phase 0
   is the absolute-last-resort restore path.

## Estimated effort

- Phase 0: 30 min
- Phase 1: 1 h
- Phase 2: 2 h + 1-2 days of advisory soak
- Phase 3: 1 h + 1 day of active monitoring
- Phase 4-8: 1 h each + short validation sweeps
- Phase 9: 2 h + one full feature cycle

Total hands-on engineering: ~1-1.5 days. Calendar time (including
soak periods where the pipeline runs in advisory mode before binding):
~1 week.

## What this plan does NOT do

- Replace the existing v1 `FeatureController` state machine. The
  migration wires new modules INTO the existing controller; a full
  re-architecture is a separate piece of work.
- Require a downtime window. Every phase is opt-in via env flag until
  Phase 9 removes the bypass.
- Touch the NATS event schema. Gate results become NEW event types
  (`devteam.gate.*`); nothing existing is renamed.
