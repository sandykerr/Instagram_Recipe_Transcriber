# Instagram Recipe Transcriber — Repository Overview

## Start here

This is the high-level product and implementation-direction document for the repository. Read it before proposing architecture or making implementation changes.

## Current repository state

- No application code has been implemented yet.
- No Python or Node.js project has been scaffolded yet.
- Instagram scraping has not been implemented and is outside the v0.1 scope.
- `yt-dlp` has been smoke-tested as an optional, best-effort adapter for downloading authorized public Reels without cookies. It is not a guaranteed acquisition mechanism; manually supplied local media remains the required fallback.
- The repository currently contains this overview and a `test.txt` file.
- Inspect `git status` before changing anything and preserve any user-owned changes.

## Development environment

The user successfully installed Codex under WSL2 and intends to continue development there.

Recommended setup:

- Work from a clone stored in the WSL Linux filesystem, such as `~/code/IG_Recipe_Trans`, rather than `/mnt/c/...`.
- Confirm the repository path, active branch, remote, and working-tree status before editing.
- Run Codex from the `Instagram_Recipe_Transcriber` repository directory.
- Prefer Python 3.12, subject to compatibility checks for the selected transcription and OCR libraries.
- Create a Linux-native environment inside WSL. Do not reuse the Windows micromamba environment at `C:\Users\sandy\micromamba\envs\ig_recipe_env`; it contains Windows binaries and is not a WSL environment.
- FFmpeg should be installed in WSL and invoked through a small typed subprocess wrapper.

The user previously created an empty Windows micromamba environment, but it should now be treated only as obsolete context unless the user says otherwise.

## Language recommendation

Use Python for the v0.1 processing worker.

Reasons:

- The user has about four years of Python experience and only a few weeks of Node.js/TypeScript experience.
- Local transcription, OCR, OpenCV, FFmpeg orchestration, and scientific/ML dependencies have a direct Python ecosystem.
- The requested design is synchronous, typed, testable, and local-first.
- TypeScript remains a good option for a later human-review UI, after the extraction pipeline is reliable.

Do not introduce a TypeScript service, web framework, asynchronous worker, cloud queue, or review UI in v0.1 without a concrete need and user agreement.

## Confirmed v0.1 boundary

Build a local, synchronous Python pipeline that:

1. Reads eligible recipe jobs sequentially from a Google Sheets queue. The
   queue has category tabs (for example, Desserts, Snacks, and Main Courses)
   with a Reel URL, optional human-readable description, durable processing
   status, and optional status detail in each row.
2. Acquires source media with an optional `yt-dlp` adapter for authorized public Reels, or matches the Instagram URL to a manually supplied local video file, preferably using the Reel shortcode. Local media is the reliable fallback when downloading is unavailable.
3. Accepts manually pasted caption text when automatic caption acquisition is unavailable.
4. Extracts audio with FFmpeg and transcribes it locally.
5. Samples/deduplicates frames and extracts on-screen text with OCR.
6. Preserves caption, transcript, OCR, and their timestamped segments as separate artifacts.
7. Classifies source completeness.
8. Produces a strict structured recipe with evidence and confidence metadata.
9. Runs deterministic validation.
10. Sends incomplete or conflicting recipes to `REVIEW` rather than treating them as processing errors.
11. Creates a Google Doc containing each `READY` recipe, optionally moves it
    to a configured Drive folder, and appends its title and Doc URL to the
    matching category tab in the Recipe Master Doc spreadsheet.

Explicitly defer:

- browser-session automation
- authenticated, arbitrary, or guaranteed Reel downloading
- nutrition calculation
- cloud deployment
- worker concurrency
- a review web application

The first success case is the example rigatoni Reel identified during project planning, using a manually supplied local video.

## Google queue and delivery workflow

The Google-integrated workflow processes queue items sequentially:

```text
Queue Sheet category tab
  (URL, optional description, status, detail)
    -> yt-dlp media download + best-effort caption metadata
    -> caption-first recipe processing pipeline
    -> READY recipe Google Doc
    -> optional Drive-folder placement
    -> matching Recipe Master Doc category tab
      (recipe title, Google Doc URL)
```

Recipes that validate as `REVIEW` follow a separate delivery path:

```text
Queue Sheet row -> Review Google Doc in the review folder
                -> matching category tab in the Review Sheet
                   (URL, description, Review, Review Doc URL, Servings,
                    Nutrition Notes)
```

The Review Sheet uses `Review`, `Approved` (or the legacy `Accepted`), and
`Rejected` as human-managed decision values. An explicit, separately invoked review-promotion worker
processes accepted or rejected rows sequentially.
Never append a `REVIEW` item to the Recipe Master Doc automatically.

The Review and Rejected Sheets have two optional manual fields after `Review
Doc URL`: `Servings` and `Nutrition Notes`. A reviewer can enter creator-stated
or manually checked values there. Approval renders them in the final Recipe Doc
and rejection preserves them in the Rejected Sheet. Automatic publication
requires creator-stated servings, calories, protein, carbs, and fat; their
absence routes an otherwise complete candidate to `REVIEW` for manual entry.
Structured serving and nutrition extraction remains a future improvement; do
not infer or calculate these values in the MVP.

Each review artifact retains one or more machine-readable review categories;
the category is separate from the human decision. For example,
`INGREDIENTS_MISMATCH` and `INGREDIENTS_AMOUNTS_MISSING` can both apply to one
recipe. `MISSING_CRITICAL_STEP` has a deliberate approval behavior: when a
human accepts that recipe, the final Doc keeps its ingredients but replaces the
incomplete numbered candidate steps with the retained raw transcript. This
makes the evidence available without inventing a complete step list.

`Accepted` creates a clean final Recipe Doc from the locally stored reviewed
candidate, moves it to the normal output folder, appends the Recipe Master Doc
row, and then deletes the active Review Sheet row. `Rejected` moves the
existing Review Doc to the rejected folder, appends a matching row to the
rejected spreadsheet, and then deletes the active Review Sheet row. Persist a
local resolution checkpoint before deletion so retries never duplicate Docs,
Master rows, or rejected rows.

Use the review-promotion batch operation with a small `--max-items` value
first. Unlimited review processing is available only through `--all` together
with an explicit `--confirm-all` acknowledgement and `--execute-writes`.

### Implementation update — 2026-08-16

- Implemented review-category persistence. Categories are non-exclusive and
  include ingredient mismatch, missing ingredient amounts, missing critical
  steps, missing title/ingredients/instructions, and source conflicts.
- Review artifacts now retain transcript evidence separately from the recipe
  candidate. This supports a safe approval path for `MISSING_CRITICAL_STEP`:
  the final Recipe Doc displays the raw transcript under `Instructions` and
  intentionally omits the incomplete numbered candidate steps.
- Added `RecipeDocumentPresentation` so this document-rendering choice remains
  outside the provider-independent `RecipeCandidate` domain model.
- The Review Sheet decision reader recognizes both `Approved` and legacy
  `Accepted` as an approval, plus `Rejected`. Its canonical persisted internal
  decision is `accepted` or `rejected`.
- Added unit coverage for multi-category persistence, transcript-based approved
  rendering, and the `Approved` Review Sheet spelling.

The queue tab name is the authoritative recipe category and must be carried
unchanged to the matching Recipe Master Doc tab. Do not derive category from
the recipe title or model output.

For retry safety, persist local publication state after a Doc is created and
after the Master Sheet row is written. A retry should reuse the recorded Doc
instead of creating another one whenever a publication checkpoint exists.
Use `Pending`, `Processing`, `Published`, `Review`, and `Error` as the visible
queue statuses. A blank status is treated as `Pending` so new rows can be
entered quickly. Only `Pending` and an interrupted `Processing` row are
eligible; `Published`, `Review`, and `Error` rows are skipped until a person
changes their status. Store a helpful result detail alongside the status (the
Google Doc URL for published recipes, validation reason for review, or error
message for failures). Batch runs are strictly synchronous and may be bounded
with a maximum item count for an initial safe run.

If a previously failed acquisition becomes processable after an adapter update
(for example, caption-only carousel support), manually reset its `Error` status
to blank or `Pending`. The worker does not automatically retry terminal `Error`
rows, which prevents unintended repeated paid/external work.

## Future features and optimizations

These are intentional follow-on options, not v0.1 requirements. Re-evaluate
them only after the single-job local pipeline is reliable and benchmarked.

- **Cloud migration:** package the synchronous worker for a small Linux VM so
  batches can run remotely. For the expected low volume (about 20–30 Reels per
  month), favor an x86 VM that is started for batch processing and stopped
  afterward over an always-on worker, serverless architecture, or GPU. Account
  for persistent storage, networking, and API costs; configure cost alerts
  before deployment. Benchmark the selected transcription and OCR dependencies
  on the exact cloud image before committing to a provider or instance size.
- **Concurrency:** consider only after profiling demonstrates a bottleneck and
  job idempotency, artifact locking, and external-service rate limits are
  proven.
- **Review UI:** add a human-review application only if Sheet-based review is
  insufficient.
- **Nutrition:** support a clearly separated calculated-nutrition stage only
  after the recipe-extraction pipeline is reliable.

## Non-negotiable data rules

- Never invent missing quantities, servings, temperatures, timings, or ingredients.
- Never infer servings from plates or bowls shown in a video.
- Preserve exact, approximate, ranged, optional, and `to taste` quantities distinctly.
- Preserve creator-unspecified ingredients with a null quantity rather than
  inventing one. This is acceptable for inherently unmeasured items such as
  cooking spray, lettuce, tomatoes, onions, and seasoning when their original
  text is supported by evidence. An ingredient line that appears to contain an
  unparsed numeric quantity, or a recipe missing ingredients/instructions/title,
  remains a `REVIEW` case.
- Do not silently resolve conflicts between caption, narration, OCR, or description.
- Preserve original values when normalized values are added.
- Every extracted ingredient and instruction must retain evidence references and confidence.
- Keep creator-provided nutrition claims separate from any future calculated nutrition.
- Retain the source URL and creator attribution when available.
- Do not redistribute source media or distinctive creator prose.

## Preferred architecture direction

Use a package-oriented synchronous design with small interfaces around external or replaceable behavior. Likely boundaries include:

- `GoogleSheetsClient`
- `GoogleDriveClient`
- `GoogleDocsClient`
- `MediaAcquirer`, implemented initially by `LocalFileAcquirer` and an optional best-effort `YtDlpAcquirer`
- `AudioExtractor`
- `Transcriber`
- `FrameExtractor`
- `OcrExtractor`
- `SourceClassifier`
- `RecipeExtractor`
- `RecipeValidator`
- `RecipeRenderer`

Use composition and dependency injection where they make tests easier. Avoid an inheritance hierarchy merely for architectural appearance.

## Recipe extraction strategy

Use a hybrid extraction design that defaults to deterministic, evidence-first parsing:

1. Rule-based extraction identifies likely ingredient lines, quantities, units,
   timings, temperatures, headings, and instruction steps from the separately
   preserved caption, transcript, and OCR artifacts.
2. Deterministic validation checks that each structured claim is supported by
   evidence and that no source conflicts remain unresolved.
3. Missing, ambiguous, or conflicting content is routed to `REVIEW`; it must
   not be filled with inferred values.

Run extraction in a cost-aware escalation order: first validate the caption
alone; if it is `READY`, finish without downloading audio or running speech
recognition. Otherwise, add the transcript and validate again. Run OCR only
when that combined result is still not `READY` and the OCR gate requests it.

The structured extraction response must also contain an evidence-grounded
completeness assessment. It identifies blocking problems such as an
unquantified core ingredient or a missing critical cooking step; deterministic
validation routes any such supported finding to `REVIEW`. This assessment is
not permission to invent facts and does not replace evidence checks.

Evaluate this default against representative fixtures before adding an LLM.
If testing shows that deterministic extraction produces unacceptably poor
structure or excessive review cases, add an optional OpenAI API-backed
`RecipeExtractor` adapter as a fallback. Use the API's structured-output
capability with the recipe schema, but treat its response as a proposed
structure, not a source of facts: validation must reject any claim lacking an
evidence reference or introducing a new value. Keep the provider, model,
prompt version, input-artifact hashes, and provider-reported usage metadata in
the extraction artifact. Usage records request and token counts only; calculate
mutable model pricing in a separate reporting layer. API credentials must
remain in untracked configuration.

Each pipeline stage should:

- accept and return typed models;
- persist a versioned artifact;
- be safe to retry;
- avoid repeating completed expensive work when its input/configuration hash is unchanged;
- distinguish operational failure from incomplete recipe content.

Suggested artifact layout:

```text
data/working/{recipe_id}/
├── source.json
├── caption.json
├── transcript.json
├── ocr.json
├── classification.json
├── recipe.json
├── validation.json
└── rendering.json
```

Keep these files out of Git except for sanitized fixtures under `tests/fixtures/`.

## State-machine direction

Keep job status/outcome conceptually distinct from the current processing stage. A reasonable starting flow is:

```text
NEW
  -> WAITING_FOR_MEDIA
  -> EXTRACTING
  -> CLASSIFYING
  -> STRUCTURING
  -> VALIDATING
  -> RENDERING
  -> DONE
```

Branches:

- `VALIDATING -> REVIEW` for missing information, low confidence, or unresolved conflicts.
- Any processing stage may enter `ERROR` for actual operational failures.
- Duplicate or confirmed non-recipe inputs may enter `SKIPPED` when configured.
- Rendering must be idempotent: if a row already has a valid `doc_id`, a retry must not create another document.

Refine the exact state and Sheet schemas before implementation. Prefer a smaller coherent model over preserving every proposed column or status when it is not needed.

## Persistence direction

Start with:

- Google Sheets as the human-facing job queue and summary state;
- versioned JSON artifacts on the local filesystem for intermediate results.

Do not add SQLite initially unless design analysis identifies a concrete consistency, querying, locking, or recovery requirement that the filesystem-plus-Sheet design cannot reasonably satisfy.

## Expected engineering standards

- Python 3.12 if supported by selected dependencies
- type annotations throughout
- Pydantic v2 models
- pytest
- Ruff
- mypy or Pyright; choose one and configure it strictly enough to be useful
- structured logging
- startup configuration validation
- explicit custom exceptions
- retries only around retryable external operations
- bounded exponential backoff with jitter
- no hidden global state
- secrets and personal identifiers outside Git
- small commits with tests

Favor direct synchronous code. Do not add asyncio or parallel processing for the MVP.

## Documentation that must be current before coding integrations

Verify relevant claims against official primary documentation immediately before selecting or implementing integrations, especially:

- supported Python versions and installation requirements for the chosen transcription library;
- supported Python versions, native dependencies, models, and licensing for the chosen OCR library;
- current FFmpeg behavior used by extraction commands;
- Google OAuth installed-app flow and least-privilege scopes;
- Google Sheets, Drive, and Docs request formats and quota/retry guidance;
- the chosen LLM provider's current structured-output API and schema limitations;
- Meta/Instagram API capabilities and terms before any future automated acquisition work;
- USDA FoodData Central API details before the deferred nutrition stage.

Use primary/official sources for technical integration decisions. Do not rely on remembered package or API details.

## Security and Git hygiene

Never commit:

- OAuth client secrets or refresh tokens
- API keys
- browser cookies
- downloaded or screen-recorded source media
- generated working artifacts
- personal Sheet, template, or Drive folder IDs unless intentionally stored in private untracked configuration

Check and strengthen `.gitignore` during initial scaffolding. Provide an `.env.example` or sanitized configuration example containing names and descriptions, never real secret values.

## Final implementation note

The Windows-side conversation performed planning only. There is no partially implemented architecture to preserve. Treat this file as the current high-level product and implementation-direction document. Begin with inspection and architecture discussion, not code generation or dependency installation.
