# Testing and Manual Verification

Run these commands from the repository root after activating
`instagram-recipe-env`. Commands that use Google or OpenAI require the same
ignored configuration and credentials described in the README.

## Automated checks

Run the unit suite:

```bash
PYTHONPATH=src python -m pytest
```

Run linting and strict type checks:

```bash
PYTHONPATH=src python -m ruff check src tests \
  --exclude tests/scripts/component_smoke_tests
PYTHONPATH=src python -m mypy src
```

## Local vertical slices

Run the local video pipeline against the default example video, or pass a video
path as the first argument:

```bash
PYTHONPATH=src python tests/scripts/local_vertical_slice_smoke.py
```

Run the paid caption-first OpenAI smoke test:

```bash
PYTHONPATH=src python tests/scripts/caption_first_openai_smoke.py
```

## Google queue integration checks

Run one known queue item through acquisition, extraction, publication, and
retry verification:

```bash
PYTHONPATH=src python tests/scripts/google_single_item_smoke.py \
  --execute-writes
```

Run a bounded batch of two eligible Queue Sheet rows:

```bash
PYTHONPATH=src python tests/scripts/process_google_queue.py \
  --max-items 2 \
  --execute-writes
```

Verify the previous batch's terminal rows without processing new Pending rows:

```bash
PYTHONPATH=src python tests/scripts/process_google_queue.py --verify-retry
```

These commands can create Docs, move Drive files, update Sheets, and make paid
OpenAI requests.

## Review-promotion integration checks

Resolve exactly one Accepted or Rejected Review Sheet row and verify an
idempotent replay:

```bash
PYTHONPATH=src python tests/scripts/google_review_promotion_smoke.py \
  --execute-writes
```

Resolve a bounded batch of two actionable review rows:

```bash
PYTHONPATH=src python tests/scripts/process_google_reviews.py \
  --max-items 2 \
  --execute-writes
```

These commands make real Docs, Drive, and Sheets changes.

## Review-state synchronization checks

Run the offline planner, snapshot/applier, and command tests:

```bash
PYTHONPATH=src python -m pytest tests/test_delivery_adapters.py \
  tests/test_review_sync.py tests/test_review_sync_command.py
```

These tests use temporary local checkpoints and fake Google API boundaries; they
verify dry-run safety, deterministic counts, conflict/malformed-row refusal,
checkpoint-derived payloads, descending deletions, and changed-URL rejection.

Preview the live configured Input and active Review Sheet tabs without any Sheets
mutations:

```bash
PYTHONPATH=src python tests/scripts/sync_google_review_state.py \
  --config google_workflow_config.json
```

Review the planned counts, affected internal recipe IDs, and every conflict.
Repair ambiguous identities, missing checkpoints, or malformed rows manually
before authorizing writes. Only after inspecting the dry-run, apply a fresh plan:

```bash
PYTHONPATH=src python tests/scripts/sync_google_review_state.py \
  --config google_workflow_config.json --execute-writes
```

Execution refuses any plan containing conflicts. A successful application prints
completed counts; a subsequent dry-run should report zero planned mutations if
the Sheets have not changed. Do not edit the Sheets concurrently: deletions are
URL-revalidated, but a Google API failure can leave a partially applied plan.
Inspect a fresh dry-run before retrying. Neither mode rewrites checkpoints or
makes OpenAI, Docs, or Drive mutations; OAuth may refresh the local token file.

## Component smoke tests

The component scripts live under `tests/scripts/component_smoke_tests/`:

- `instagram_download_smoke.py`
- `frame_extraction_smoke.py`
- `fw_transcription_test.py`
- `ocr_smoke.py`
- `ocr_downscale_comparison_smoke.py`

Inspect a script's required inputs before running it:

```bash
PYTHONPATH=src python tests/scripts/component_smoke_tests/instagram_download_smoke.py --help
PYTHONPATH=src python tests/scripts/component_smoke_tests/frame_extraction_smoke.py --help
PYTHONPATH=src python tests/scripts/component_smoke_tests/ocr_smoke.py --help
```

Generated media and artifacts belong under ignored working or test-data
directories and must not be committed.
