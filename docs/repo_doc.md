# Instagram Recipe Transcriber — Repository Overview

## Start here

This is the high-level product and implementation-direction document for the repository. Read it before proposing architecture or making implementation changes.

## Current repository state

- No application code has been implemented yet.
- No Python or Node.js project has been scaffolded yet.
- Instagram scraping and automated media downloading have not been implemented and are explicitly outside the v0.1 scope.
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

1. Reads eligible recipe jobs from a Google Sheet.
2. Matches each Instagram URL to a manually supplied local video file, preferably using the Reel shortcode.
3. Accepts manually pasted caption text when automatic caption acquisition is unavailable.
4. Extracts audio with FFmpeg and transcribes it locally.
5. Samples/deduplicates frames and extracts on-screen text with OCR.
6. Preserves caption, transcript, OCR, and their timestamped segments as separate artifacts.
7. Classifies source completeness.
8. Produces a strict structured recipe with evidence and confidence metadata.
9. Runs deterministic validation.
10. Sends incomplete or conflicting recipes to `REVIEW` rather than treating them as processing errors.
11. Copies/renders a Google Docs template and writes the resulting Doc URL and state back to the Sheet.

Explicitly defer:

- browser-session automation
- arbitrary Reel downloading
- nutrition calculation
- cloud deployment
- worker concurrency
- a review web application

The first success case is the example rigatoni Reel identified during project planning, using a manually supplied local video.

## Non-negotiable data rules

- Never invent missing quantities, servings, temperatures, timings, or ingredients.
- Never infer servings from plates or bowls shown in a video.
- Preserve exact, approximate, ranged, optional, and `to taste` quantities distinctly.
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
- `MediaAcquirer`, initially implemented only by `LocalFileAcquirer`
- `AudioExtractor`
- `Transcriber`
- `FrameExtractor`
- `OcrExtractor`
- `SourceClassifier`
- `RecipeExtractor`
- `RecipeValidator`
- `RecipeRenderer`

Use composition and dependency injection where they make tests easier. Avoid an inheritance hierarchy merely for architectural appearance.

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
