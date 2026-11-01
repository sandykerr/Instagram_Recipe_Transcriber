# Google Review State Synchronization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile historical Input/Review Sheet drift and keep future accepted or rejected reviews synchronized automatically.

**Architecture:** Add a pure planning module that compares Input Sheet rows, active Review Sheet rows, and durable local checkpoints, returning typed actions or conflicts without side effects. Google adapters load snapshots and apply those actions; the CLI defaults to dry-run, while review promotion uses a narrow queue-resolution writer after its durable resolution and active-row removal steps.

**Tech Stack:** Python 3.12, Pydantic 2, Google Sheets API v4, pytest 9, Ruff, mypy strict

**Spec:** `docs/superpowers/specs/2026-11-01-google-review-state-sync-design.md`

## Global Constraints

- Do not call OpenAI or process Instagram media during synchronization.
- Google mutations require an explicit `--execute-writes` flag.
- Local review and resolution checkpoints are authoritative; never infer from Drive placement.
- Duplicate URLs, missing checkpoints, category conflicts, and malformed rows must be reported without mutation.
- Review Sheet deletions must validate identity and run bottom-up within each tab.
- Existing private Google IDs and credentials remain untracked.

## Review Focus

- Duplicate source URLs within one or multiple tabs must produce conflicts, never multiple writes (Task 2).
- A review checkpoint marked `review_row_written=True` but absent from the live Review Sheet must still plan one append (Task 2).
- A resolved row already synchronized must produce no action on rerun (Tasks 2 and 4).
- A review row deleted or moved between planning and apply must fail identity validation before deletion (Task 3).
- A failure after Review Sheet deletion but before Input Sheet update must be recoverable from the resolution checkpoint (Tasks 2 and 4).

---

### Task 1: Terminal Queue State and Source-Addressed Queue Writer

**Files:**
- Modify: `src/instagram_recipe_transcriber/models.py`
- Modify: `src/instagram_recipe_transcriber/interfaces.py`
- Modify: `src/instagram_recipe_transcriber/google_adapters.py`
- Modify: `tests/test_delivery_adapters.py`

**Interfaces:**
- Consumes: Google Sheets queue layout `A:D` and existing `QueueStatus` values.
- Produces: `QueueStatus.REJECTED`; protocol `ReviewQueueStateWriter.resolve(decision: ReviewDecision, status: QueueStatus, detail: str) -> None`; adapter `GoogleSheetsReviewQueueStateWriter` implementing it by exact category/source-URL lookup.

- [ ] **Step 1: Write failing adapter tests**

Add tests asserting exact URL lookup updates `C:D` to `published` or `rejected`, missing URLs raise `PipelineOperationalError`, and duplicate matching URLs raise without updating.

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `/home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/pytest tests/test_delivery_adapters.py -q`

Expected: FAIL because `QueueStatus.REJECTED`, `ReviewQueueStateWriter`, and `GoogleSheetsReviewQueueStateWriter` do not exist.

- [ ] **Step 3: Implement the status, protocol, and adapter**

Use the exact source URL within the decision's category tab as the durable identity. Require exactly one match before calling `values().update()` on that row's `C:D` cells.

- [ ] **Step 4: Run focused tests**

Run: `/home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/pytest tests/test_delivery_adapters.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/instagram_recipe_transcriber/models.py src/instagram_recipe_transcriber/interfaces.py src/instagram_recipe_transcriber/google_adapters.py tests/test_delivery_adapters.py
git commit -m "feat: add terminal review queue state writer"
```

### Task 2: Pure Review-State Reconciliation Planner

**Files:**
- Create: `src/instagram_recipe_transcriber/review_sync.py`
- Create: `tests/test_review_sync.py`

**Interfaces:**
- Consumes: typed queue/review row snapshots plus mappings of recipe IDs to `ReviewArtifact` and `ReviewResolutionArtifact` loaded by the caller.
- Produces: `ReviewSyncPlanner.plan(queue_rows: tuple[ReviewQueueRow, ...], active_rows: tuple[ActiveReviewRow, ...]) -> ReviewSyncPlan`, containing typed append, removal, queue-update, and conflict entries.

- [ ] **Step 1: Write failing planner tests**

Cover: unresolved review present/no-op; unresolved review missing/append; accepted/update to `Published`; rejected/update to `Rejected`; resolved active row/removal; already synchronized/no-op; checkpoint-less queue review/conflict; duplicate live URL/conflict; mismatched category/conflict; and `review_row_written=True` with a missing live row/append.

- [ ] **Step 2: Run planner tests and verify failure**

Run: `/home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/pytest tests/test_review_sync.py -q`

Expected: FAIL because `review_sync` does not exist.

- [ ] **Step 3: Implement immutable row, action, conflict, plan models and `ReviewSyncPlanner`**

The planner must be deterministic, perform no I/O, deduplicate by recipe ID/source URL, and suppress every mutation associated with an ambiguous record.

- [ ] **Step 4: Run planner tests**

Run: `/home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/pytest tests/test_review_sync.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/instagram_recipe_transcriber/review_sync.py tests/test_review_sync.py
git commit -m "feat: plan review sheet reconciliation"
```

### Task 3: Read-Only-by-Default Synchronization Command

**Files:**
- Modify: `src/instagram_recipe_transcriber/google_adapters.py`
- Create: `tests/scripts/sync_google_review_state.py`
- Modify: `tests/test_delivery_adapters.py`
- Create: `tests/test_review_sync_command.py`
- Modify: `README.md`
- Modify: `docs/testing.md`

**Interfaces:**
- Consumes: `GoogleWorkflowConfig`, Google Sheets clients, `JsonReviewStore`, `JsonReviewResolutionStore`, and `ReviewSyncPlanner`.
- Produces: CLI `sync_google_review_state.py [--config PATH] [--execute-writes]`; snapshot adapters returning `ReviewQueueRow` and `ActiveReviewRow`; action applier with validated bottom-up deletes.

- [ ] **Step 1: Write failing snapshot/applier and command tests**

Assert all category tabs are read, dry-run performs zero mutations, conflicts make execute mode fail before any write, actions print deterministic counts, deletes are ordered descending per tab, and deletion revalidation rejects a changed URL.

- [ ] **Step 2: Run focused tests and verify failure**

Run: `/home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/pytest tests/test_delivery_adapters.py tests/test_review_sync_command.py -q`

Expected: FAIL because snapshot adapters, applier, and CLI do not exist.

- [ ] **Step 3: Implement snapshot readers and action applier**

Keep Google-specific A1 ranges and mutation mechanics in `google_adapters.py`; use exact URL/category identity checks immediately before deletion.

- [ ] **Step 4: Implement the command and documentation**

Dry-run prints planned append/remove/update/conflict counts and affected internal recipe IDs. `--execute-writes` refuses to run when any conflict exists and prints completed counts after application.

- [ ] **Step 5: Run focused tests and static checks**

Run:

```bash
/home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/pytest tests/test_delivery_adapters.py tests/test_review_sync.py tests/test_review_sync_command.py -q
/home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/ruff check src tests/scripts/sync_google_review_state.py tests/test_review_sync.py tests/test_review_sync_command.py
/home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/mypy src
```

Expected: all commands exit 0.

- [ ] **Step 6: Commit**

```bash
git add src/instagram_recipe_transcriber/google_adapters.py tests/scripts/sync_google_review_state.py tests/test_delivery_adapters.py tests/test_review_sync_command.py README.md docs/testing.md
git commit -m "feat: add review state synchronization command"
```

### Task 4: Synchronize Future Review Promotions

**Files:**
- Modify: `src/instagram_recipe_transcriber/review_promotion.py`
- Modify: `tests/test_review_promotion.py`
- Modify: `tests/scripts/process_google_reviews.py`
- Modify: `tests/scripts/google_review_promotion_smoke.py`

**Interfaces:**
- Consumes: `ReviewQueueStateWriter.resolve(decision, status, detail)` from Task 1.
- Produces: `ReviewPromotionWorkflow(..., queue_state_writer: ReviewQueueStateWriter)` that persists `Published` or `Rejected` after durable resolution and active Review Sheet row removal.

- [ ] **Step 1: Write failing promotion tests**

Assert accepted decisions resolve the Input row to `Published`, rejected decisions resolve it to `Rejected`, the final Doc URL is stored as detail, reruns perform no duplicate queue update, and a failure before queue update is completed safely on retry from the resolution checkpoint.

- [ ] **Step 2: Run promotion tests and verify failure**

Run: `/home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/pytest tests/test_review_promotion.py -q`

Expected: FAIL because the workflow does not accept or call a queue-state writer.

- [ ] **Step 3: Add durable queue-update state and workflow behavior**

Add `queue_row_updated: bool = False` to `ReviewResolutionArtifact`. Save the resolution after the queue update; on retry, skip every previously completed external action and perform only the missing queue update.

- [ ] **Step 4: Wire both manual Google review commands**

Construct `GoogleSheetsReviewQueueStateWriter` with `config.queue_spreadsheet_id` and include queue-update counts in command output and retry assertions.

- [ ] **Step 5: Run promotion and full regression suites**

Run:

```bash
/home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/pytest tests/test_review_promotion.py -q
/home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/pytest -q
/home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/ruff check .
/home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/mypy src
```

Expected: all tests pass and both static checks exit 0.

- [ ] **Step 6: Run the live reconciliation command in dry-run mode only**

Run: `PYTHONPATH=src /home/sandy/.local/share/micromamba/envs/instagram-recipe-env/bin/python tests/scripts/sync_google_review_state.py`

Expected: a deterministic plan summary with zero Google mutations. Do not run `--execute-writes` without a separate explicit user instruction after reviewing this output.

- [ ] **Step 7: Commit**

```bash
git add src/instagram_recipe_transcriber/models.py src/instagram_recipe_transcriber/review_promotion.py tests/test_review_promotion.py tests/scripts/process_google_reviews.py tests/scripts/google_review_promotion_smoke.py
git commit -m "feat: sync promoted reviews to input queue"
```

