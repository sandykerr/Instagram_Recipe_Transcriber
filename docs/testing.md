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
