# Instagram Recipe Transcriber

Local-first Python tooling that turns queued Instagram recipe posts into
evidence-backed Google Docs. It preserves source evidence, avoids inventing
missing details, and routes uncertain recipes through a Google Sheets review
workflow.

## Workflow

```text
Queue Sheet
  → Instagram caption/media acquisition
  → caption-first extraction
  → transcription and OCR when needed
  → validation
  → Recipe Doc + Recipe Master Sheet
     or Review Doc + Review Sheet
```

Blank queue statuses are treated as `Pending`. Queue outcomes include
`Published`, `Review`, `Rejected`, and `Error`. Approved review items are published;
rejected items are archived in the configured rejected folder and Sheet.
Local JSON checkpoints make retries idempotent.

## Setup

From the repository root:

```bash
micromamba activate instagram-recipe-env
direnv allow
```

The tracked `.envrc` loads variables from the ignored `.env` file whenever you
enter the repository. Store the OpenAI key in this format:

```dotenv
OPENAI_API_KEY=your-key
```

Google OAuth is loaded from `google_oauth_client.json`; its client secret does
not need to be exported as a shell variable.

Commands use `PYTHONPATH=src`. Google workflows require these ignored local
files:

- `google_workflow_config.json`
- `google_oauth_client.json`
- `google_oauth_token.json`

Never commit API keys, OAuth files, downloaded media, working artifacts, or
private Google resource IDs.

### Regenerate Google OAuth credentials

When the token is missing:

```bash
PYTHONPATH=src python tests/scripts/regenerate_google_oauth_token.py
```

To explicitly replace an expired or revoked token:

```bash
PYTHONPATH=src python tests/scripts/regenerate_google_oauth_token.py --force
```

The utility performs authorization only; it does not run a recipe workflow or
modify recipe Sheets, Docs, or Drive files.

## Production commands

`process_google_queue.py` processes every currently eligible Queue Sheet row:

```bash
PYTHONPATH=src python tests/scripts/process_google_queue.py \
  --all \
  --execute-writes
```

`process_google_reviews.py` processes every approved, accepted, or rejected
Review Sheet row:

```bash
PYTHONPATH=src python tests/scripts/process_google_reviews.py \
  --all \
  --confirm-all \
  --execute-writes
```

Both commands make real external changes. Queue processing may also make paid
OpenAI requests. Check the configured Sheet rows and local checkpoints before
using unlimited mode.

### Synchronize review state

Preview repairs across every configured Input and active Review Sheet category
tab, using local review and resolution checkpoints:

```bash
PYTHONPATH=src python tests/scripts/sync_google_review_state.py
```

The default is read-only: it reports planned append/remove/update/conflict counts
and affected internal recipe IDs. Pass `--config PATH` to use a different local
configuration. To apply the inspected plan, rerun with explicit write permission:

```bash
PYTHONPATH=src python tests/scripts/sync_google_review_state.py --execute-writes
```

Execution reads a fresh snapshot and refuses all writes if any identity or
checkpoint conflict exists; malformed nonblank rows also fail before any write.
Missing unresolved reviews are restored with their existing Review Doc link.
Resolved active reviews are removed, and Input rows become `Published` or
`Rejected` with the resulting Doc link. Deletions run bottom-up within each tab
and recheck the exact source URL immediately before deletion. Already-correct
rows and `Pending`, `Processing`, or `Error` statuses are left unchanged.

The command makes no OpenAI calls and does not create Docs, move Drive files,
or rewrite checkpoints. OAuth may refresh its local token file even during a
dry-run. Avoid concurrent Sheet edits while applying; Google changes are not
transactional. If execution fails partway through, inspect a fresh dry-run before
retrying: reconciliation uses current rows rather than checkpoint write flags.

## Documentation

- [Testing and smoke-test commands](docs/testing.md)
- [Architecture and operating rules](docs/repo_doc.md)
- [OCR design notes](docs/OCR_ideas.md)
