# Instagram Recipe Transcriber

Local-first Python tooling for turning Instagram recipe posts into reviewable,
evidence-backed recipe documents.

The project is designed to be conservative: it preserves source evidence,
avoids inventing recipe details, sends uncertain results to human review, and
uses local JSON checkpoints so retries do not duplicate Google Docs or Sheet
rows.

## Current pipeline

For each queued Instagram URL, the pipeline works synchronously through these
steps:

1. Read the next eligible row from a category tab in the Queue Sheet.
2. Inspect Instagram metadata with `yt-dlp`, collecting the caption and only
   downloading video when media is needed. Caption-only image carousels can
   proceed without a video file.
3. Try to extract a complete recipe from the caption first.
4. If the caption is not sufficient and media exists, extract audio with FFmpeg,
   transcribe with Faster-Whisper, and use OCR on sampled frames when needed.
5. Use structured OpenAI extraction with evidence references, then validate the
   candidate without filling in missing facts.
6. Publish `READY` recipes as Google Docs and append them to the matching tab
   in the Recipe Master Sheet.
7. Send uncertain recipes to a Review Doc and matching Review Sheet tab.

Queue rows have four columns:

```text
URL | Description | Status | Detail
```

The normal terminal statuses are `Published`, `Review`, and `Error`. Blank
status is treated as `Pending`.

## Human review and promotion

Review results keep their candidate recipe, validation findings, evidence
excerpts, raw transcript (when available), and one or more machine-readable
review categories. Examples include `INGREDIENTS_MISMATCH`,
`INGREDIENTS_AMOUNTS_MISSING`, and `MISSING_CRITICAL_STEP`.

In the Review Sheet, set the status to `Approved` (or legacy `Accepted`) or
`Rejected`.

The optional `Servings` and `Nutrition Notes` columns follow `Review Doc URL`:

```text
URL | Description | Status | Review Doc URL | Servings | Nutrition Notes
```

Enter reviewed values manually for now; approval copies them into the final
Recipe Doc and rejection preserves them in the Rejected Sheet. Automatic
serving and macro extraction is deferred. Until then, automatic publication
requires creator-stated servings, calories, protein, carbs, and fat; a missing
value sends the recipe to Review for manual completion.

- An approved recipe gets a clean final Doc in the normal output folder and a
  row in Recipe Master Sheet.
- A rejected review Doc moves to the rejected Drive folder and is recorded in
  the rejected Sheet.
- If an approved item had `MISSING_CRITICAL_STEP`, the final Doc deliberately
  uses the retained raw transcript under **Instructions** instead of the
  incomplete numbered candidate steps.

Local publication and resolution checkpoints make retries safe: the workflow
does not intentionally create duplicate Docs or spreadsheet rows.

## Local setup

This repository currently uses the existing micromamba environment:

```bash
micromamba activate instagram-recipe-env
```

Run Python commands from the repository root with `PYTHONPATH=src`. Google
workflow runs additionally require local, ignored configuration and credential
files such as `google_workflow_config.json`, `google_oauth_client.json`, and
`google_oauth_token.json`. Paid extraction runs require `OPENAI_API_KEY` to be
exported in the shell.

Do not commit API keys, OAuth files, downloaded media, working artifacts, or
personal Google IDs.

## Example local runs

Run the automated unit suite:

```bash
PYTHONPATH=src python -m pytest
```

Run the local video vertical slice (uses the default local example video, or
pass a path as the first argument):

```bash
PYTHONPATH=src python tests/scripts/local_vertical_slice_smoke.py
```

Run the paid caption-first OpenAI smoke test:

```bash
PYTHONPATH=src python tests/scripts/caption_first_openai_smoke.py
```

Run a bounded Google batch of two eligible Queue Sheet rows. This makes real
external writes and may make paid OpenAI requests:

```bash
PYTHONPATH=src python tests/scripts/google_batch_smoke.py --max-items 2 --execute-writes
```

Verify the previous batch's terminal rows without processing additional pending
rows:

```bash
PYTHONPATH=src python tests/scripts/google_batch_smoke.py --verify-retry
```

Process a bounded batch of two manually approved or rejected Review Sheet rows.
This creates or moves real Docs, updates Sheets, and removes each resolved
Review Sheet row:

```bash
PYTHONPATH=src python tests/scripts/google_review_promotion_batch.py --max-items 2 --execute-writes
```

To resolve every actionable Review Sheet row, use the explicit unlimited mode:

```bash
PYTHONPATH=src python tests/scripts/google_review_promotion_batch.py --all --confirm-all --execute-writes
```

Use `--all` on either batch script only when you intend to process every
currently eligible row.

## Future plans

- Run the existing workflow as a scheduled cloud job after the local MVP is
  stable and its small monthly volume justifies hosting.
- Add a lightweight human-review UI only if Sheets cease to be sufficient.
- Improve extraction prompts, validation rules, and evidence presentation using
  real review feedback.
- Add support for more acquisition paths while retaining manual local-media
  fallback.
- Consider nutrition enrichment only after recipe extraction and review are
  reliable.

For the fuller architecture and operating rules, see
[docs/repo_doc.md](docs/repo_doc.md).
