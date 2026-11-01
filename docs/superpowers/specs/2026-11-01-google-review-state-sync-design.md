# Google Review State Synchronization

## Intent

Keep the Input Sheet, active Review Sheet, and local review checkpoints consistent.
An Input Sheet row should say `Review` only while human action is still pending.
Accepted reviews become `Published`; rejected reviews become `Rejected`.

## Goals

- Repair historical divergence without duplicating Review Sheet rows.
- Restore unresolved reviews that are missing from the active Review Sheet.
- Update resolved Input Sheet rows from `Review` to `Published` or `Rejected`.
- Remove any active Review Sheet row whose durable resolution checkpoint says it
  has already been resolved.
- Keep future promotion runs synchronized automatically.
- Make reconciliation dry-run by default and require `--execute-writes` for any
  Google mutation.

## Non-goals

- Do not reprocess Instagram media or call OpenAI.
- Do not recreate reviews that lack a local review checkpoint.
- Do not infer resolution from Drive-folder placement alone.
- Do not alter `Pending`, `Processing`, `Error`, or already-correct terminal rows.

## Source of truth and invariants

- A local review checkpoint proves that a review artifact exists and supplies its
  Review Doc URL.
- A local resolution checkpoint is authoritative for `Published` versus
  `Rejected` and supplies the resulting Doc URL.
- The Review Sheet is an active work queue, not a historical ledger.
- An unresolved checkpoint must have exactly one active Review Sheet row.
- A resolved checkpoint must have no active Review Sheet row.
- A resolved Input Sheet row must be `Published` or `Rejected`, with the resulting
  Doc URL in its detail cell.
- Ambiguous states—duplicate URLs, missing checkpoints, conflicting categories,
  or malformed rows—are reported and never mutated automatically.

## Selected approach

Create a reconciliation module behind one small interface that reads all three
state sources, produces a deterministic plan, and optionally applies that plan.
Expose it through `tests/scripts/sync_google_review_state.py` with a default
read-only report and an explicit `--execute-writes` mode.

Extend review promotion with an injected Input Sheet resolution writer. After the
resolution checkpoint is durable and the active Review Sheet row is removed, the
workflow updates the matching Input Sheet row by category and source URL. A crash
between those operations is safe because the reconciliation command can finish
the update from the checkpoint.

The reconciliation plan supports three mutations:

1. append one missing unresolved row to the Review Sheet;
2. remove one stale resolved row from the Review Sheet;
3. update one resolved Input Sheet row to `Published` or `Rejected`.

Apply row deletions from the bottom upward within each tab so row-number shifts do
not target the wrong review. Re-read or validate identifying values before every
destructive row deletion.

## Considered alternatives

- **Reconciliation only:** repairs current data but permits future drift.
- **Promotion synchronization only:** prevents new drift but leaves historical
  discrepancies.
- **Permanent Review Sheet ledger:** conflicts with the existing active-queue
  workflow and would require a larger redesign.

The selected approach repairs current state and prevents recurrence while retaining
the Review Sheet's existing active-queue semantics.

## Verification

- Unit-test planning for unresolved, accepted, rejected, missing, duplicate, and
  conflicting states.
- Unit-test dry-run behavior and mutation ordering.
- Extend promotion tests to assert the Input Sheet terminal status update and
  retry safety.
- Run the command in dry-run mode against the live Sheets before authorizing
  writes; print planned and completed action counts without exposing credentials.

