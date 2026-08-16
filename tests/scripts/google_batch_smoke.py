"""Run and verify a real bounded Google Sheet batch.

This paid/manual integration test updates queue statuses, may call OpenAI, creates
Google Docs for READY recipes, and appends published recipes to the Master Sheet.
It is deliberately not part of pytest. The default processes one bounded batch of
two items. Use --verify-retry for read-only inspection; never call a second batch
merely to verify retries, because two calls with --max-items 2 can process four rows.
"""

from __future__ import annotations

import argparse
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

from instagram_recipe_transcriber.acquisition import YtDlpAcquirer
from instagram_recipe_transcriber.adapters import (
    DeterministicOcrGate,
    FasterWhisperTranscriber,
    FfmpegAudioExtractor,
    FfmpegFrameExtractor,
    LocalFileSourceLoader,
    PaddleOcrExtractor,
)
from instagram_recipe_transcriber.artifacts import JsonArtifactStore
from instagram_recipe_transcriber.config import GoogleWorkflowConfig
from instagram_recipe_transcriber.google_adapters import (
    GoogleDocsRecipeReviewWriter,
    GoogleDocsRecipeWriter,
    GoogleDriveDocumentOrganizer,
    GoogleSheetsRecipeMasterWriter,
    GoogleSheetsRecipeQueueReader,
    GoogleSheetsRecipeQueueStateWriter,
    GoogleSheetsRecipeReviewWriter,
)
from instagram_recipe_transcriber.google_oauth import GoogleOAuthServiceFactory
from instagram_recipe_transcriber.models import (
    AcquiredRecipe,
    OcrArtifact,
    PublicationArtifact,
    QueuedRecipe,
    QueueStatus,
    RecipeCandidate,
    RecipeDocument,
    RecipeDocumentPresentation,
    RecipeExtractionArtifact,
    SourceArtifact,
    TranscriptArtifact,
)
from instagram_recipe_transcriber.openai_recipe_extractor import OpenAiRecipeExtractor
from instagram_recipe_transcriber.pipeline import PipelineRunner
from instagram_recipe_transcriber.publication import JsonPublicationStore, JsonReviewStore
from instagram_recipe_transcriber.recipe_processing import RecipeValidator
from instagram_recipe_transcriber.workflow import (
    BatchWorkflowResult,
    QueuedRecipeWorkflow,
    QueueWorkflowResult,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class TrackedAcquirer:
    version = "tracked-yt-dlp-acquirer-v1"

    def __init__(self, delegate: YtDlpAcquirer) -> None:
        self._delegate = delegate
        self.calls: list[str] = []

    def acquire(self, queued_recipe: QueuedRecipe) -> AcquiredRecipe:
        self.calls.append(queued_recipe.recipe_id)
        return self._delegate.acquire(queued_recipe)


class TrackedRecipeExtractor:
    def __init__(self, delegate: OpenAiRecipeExtractor) -> None:
        self._delegate = delegate
        self.calls: list[str] = []

    @property
    def version(self) -> str:
        return self._delegate.version

    def extract(
        self,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        ocr: OcrArtifact | None,
    ) -> RecipeCandidate:
        return self.extract_artifact(source, transcript, ocr).recipe

    def extract_artifact(
        self,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        ocr: OcrArtifact | None,
    ) -> RecipeExtractionArtifact:
        self.calls.append(source.recipe_id)
        return self._delegate.extract_artifact(source, transcript, ocr)


class TrackedDocumentWriter:
    def __init__(self, delegate: GoogleDocsRecipeWriter) -> None:
        self._delegate = delegate
        self.calls: list[str] = []

    def create(
        self,
        recipe: RecipeCandidate,
        queued_recipe: QueuedRecipe,
        presentation: RecipeDocumentPresentation | None = None,
    ) -> RecipeDocument:
        self.calls.append(queued_recipe.recipe_id)
        return self._delegate.create(recipe, queued_recipe, presentation)


class TrackedDocumentOrganizer:
    def __init__(self, delegate: GoogleDriveDocumentOrganizer) -> None:
        self._delegate = delegate
        self.calls: list[str] = []

    def move_to_folder(self, document: RecipeDocument) -> RecipeDocument:
        self.calls.append(document.document_id)
        return self._delegate.move_to_folder(document)


class TrackedMasterWriter:
    def __init__(self, delegate: GoogleSheetsRecipeMasterWriter) -> None:
        self._delegate = delegate
        self.calls: list[str] = []

    def append(self, queued_recipe: QueuedRecipe, document: RecipeDocument) -> None:
        self.calls.append(queued_recipe.recipe_id)
        self._delegate.append(queued_recipe, document)


class TrackedQueueStateWriter:
    def __init__(self, delegate: GoogleSheetsRecipeQueueStateWriter) -> None:
        self._delegate = delegate
        self.calls: list[tuple[str, QueueStatus, str | None]] = []

    def mark(
        self,
        queued_recipe: QueuedRecipe,
        status: QueueStatus,
        detail: str | None = None,
    ) -> None:
        self.calls.append((queued_recipe.recipe_id, status, detail))
        self._delegate.mark(queued_recipe, status, detail)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=REPOSITORY_ROOT / "google_workflow_config.json",
    )
    parser.add_argument("--model", default="gpt-5.4-mini")
    limit = parser.add_mutually_exclusive_group()
    limit.add_argument("--max-items", type=int, default=2)
    limit.add_argument(
        "--all",
        action="store_true",
        help="Process every currently eligible row; use only after the bounded smoke passes",
    )
    parser.add_argument(
        "--verify-retry",
        action="store_true",
        help="Read-only verification of statuses recorded by the previous batch smoke",
    )
    parser.add_argument(
        "--execute-writes",
        action="store_true",
        help="Required acknowledgement of paid calls and real Google/queue writes",
    )
    return parser.parse_args()


def load_config(path: Path) -> GoogleWorkflowConfig:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Google workflow config does not exist: {resolved}")
    return GoogleWorkflowConfig.model_validate_json(resolved.read_text(encoding="utf-8"))


def preflight(config: GoogleWorkflowConfig, args: argparse.Namespace) -> None:
    if args.verify_retry and args.all:
        raise ValueError("--verify-retry cannot be combined with --all")
    if not args.verify_retry and not args.execute_writes:
        raise RuntimeError("Pass --execute-writes to acknowledge real external writes")
    if not args.all and args.max_items < 1:
        raise ValueError("--max-items must be at least 1")
    if not args.verify_retry and not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError("OPENAI_API_KEY must be exported in this process")
    if not config.oauth_client_path.is_file():
        raise FileNotFoundError(f"OAuth client JSON does not exist: {config.oauth_client_path}")
    if not config.oauth_token_path.is_file():
        raise FileNotFoundError(
            "OAuth token JSON is missing; complete the single-item OAuth smoke first"
        )
    if config.oauth_token_path.stat().st_size == 0:
        raise RuntimeError(f"OAuth token JSON is empty: {config.oauth_token_path}")


def build_workflow(
    config: GoogleWorkflowConfig, services: Any, model: str
) -> tuple[QueuedRecipeWorkflow, dict[str, Any]]:
    acquisition_root = config.working_root / "acquisition"
    tracked_acquirer = TrackedAcquirer(YtDlpAcquirer(acquisition_root))
    recipe_extractor = TrackedRecipeExtractor(OpenAiRecipeExtractor(model=model))
    pipeline = PipelineRunner(
        artifact_store=JsonArtifactStore(config.working_root / "pipeline-artifacts"),
        source_loader=LocalFileSourceLoader(),
        audio_extractor=FfmpegAudioExtractor(config.working_root),
        transcriber=FasterWhisperTranscriber(),
        ocr_gate=DeterministicOcrGate(),
        frame_extractor=FfmpegFrameExtractor(config.working_root, sampling_fps=0.2),
        ocr_extractor=PaddleOcrExtractor(maximum_image_dimension=960),
        recipe_extractor=recipe_extractor,
        recipe_validator=RecipeValidator(),
    )
    document_writer = TrackedDocumentWriter(GoogleDocsRecipeWriter(services.docs))
    master_writer = TrackedMasterWriter(
        GoogleSheetsRecipeMasterWriter(
            services.sheets,
            spreadsheet_id=config.master_spreadsheet_id,
        )
    )
    queue_state_writer = TrackedQueueStateWriter(
        GoogleSheetsRecipeQueueStateWriter(
            services.sheets,
            spreadsheet_id=config.queue_spreadsheet_id,
        )
    )
    organizer = None
    if config.drive_folder_id is not None:
        organizer = TrackedDocumentOrganizer(
            GoogleDriveDocumentOrganizer(services.drive, folder_id=config.drive_folder_id)
        )
    review_document_organizer = None
    review_document_writer = None
    review_writer = None
    review_store = None
    if config.review_delivery_configured():
        assert config.review_spreadsheet_id is not None
        assert config.review_drive_folder_id is not None
        review_document_writer = GoogleDocsRecipeReviewWriter(services.docs)
        review_writer = GoogleSheetsRecipeReviewWriter(
            services.sheets,
            spreadsheet_id=config.review_spreadsheet_id,
        )
        review_store = JsonReviewStore(config.working_root / "review-publication")
        review_document_organizer = GoogleDriveDocumentOrganizer(
            services.drive,
            folder_id=config.review_drive_folder_id,
        )
    workflow = QueuedRecipeWorkflow(
        queue_reader=GoogleSheetsRecipeQueueReader(
            services.sheets,
            spreadsheet_id=config.queue_spreadsheet_id,
            categories=config.category_tabs,
        ),
        queue_state_writer=queue_state_writer,
        media_acquirer=tracked_acquirer,
        pipeline_runner=pipeline,
        document_writer=document_writer,
        master_writer=master_writer,
        publication_store=JsonPublicationStore(config.working_root / "publication"),
        document_organizer=organizer,
        review_document_writer=review_document_writer,
        review_writer=review_writer,
        review_store=review_store,
        review_document_organizer=review_document_organizer,
    )
    tracked: dict[str, Any] = {
        "acquirer": tracked_acquirer,
        "recipe_extractor": recipe_extractor,
        "document_writer": document_writer,
        "master_writer": master_writer,
        "queue_state_writer": queue_state_writer,
        "organizer": organizer,
    }
    return workflow, tracked


def print_batch(label: str, result: BatchWorkflowResult) -> None:
    print(f"{label}: {result.processed_count} item(s)")
    for item in result.items:
        assert item.queued_recipe is not None
        status = item.queue_status.value if item.queue_status else "unknown"
        parts = [f"URL={item.queued_recipe.source_url}", f"status={status}"]
        if item.publication is not None:
            parts.extend(
                (
                    f"title={item.publication.document.title}",
                    f"doc={item.publication.document.url}",
                )
            )
        elif item.error_message:
            parts.append(f"error={item.error_message}")
        elif item.pipeline_result is not None:
            detail = "; ".join(
                finding.message for finding in item.pipeline_result.validation.findings
            )
            parts.append(f"review={detail or 'Recipe requires human review'}")
        print("  " + " | ".join(parts))


def per_recipe_counts(
    tracked: dict[str, Any], items: tuple[QueueWorkflowResult, ...]
) -> dict[str, dict[str, int]]:
    publications = {
        item.queued_recipe.recipe_id: item.publication
        for item in items
        if item.queued_recipe is not None
    }
    counts: dict[str, dict[str, int]] = {}
    for recipe_id, publication in publications.items():
        counts[recipe_id] = {
            "acquisition": Counter(tracked["acquirer"].calls)[recipe_id],
            "openai": Counter(tracked["recipe_extractor"].calls)[recipe_id],
            "docs": Counter(tracked["document_writer"].calls)[recipe_id],
            "drive": (
                Counter(tracked["organizer"].calls)[publication.document.document_id]
                if publication is not None and tracked["organizer"] is not None
                else 0
            ),
            "master": Counter(tracked["master_writer"].calls)[recipe_id],
        }
    return counts


def _queue_statuses(config: GoogleWorkflowConfig, sheets: Any) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for category in config.category_tabs:
        values = (
            sheets.spreadsheets()
            .values()
            .get(
                spreadsheetId=config.queue_spreadsheet_id,
                range=f"'{category.replace("'", "''")}'!A2:D",
            )
            .execute()
            .get("values", [])
        )
        if not isinstance(values, list):
            continue
        for row in values:
            if isinstance(row, list) and row and isinstance(row[0], str):
                statuses[row[0]] = str(row[2]).strip().lower() if len(row) > 2 else "pending"
    return statuses


def verify_prior_batch(config: GoogleWorkflowConfig, sheets: Any) -> None:
    result_path = config.working_root / "google_batch_smoke_result.json"
    if not result_path.is_file():
        raise FileNotFoundError(f"No prior batch result exists: {result_path}")
    prior = BatchWorkflowResult.model_validate_json(result_path.read_text(encoding="utf-8"))
    statuses = _queue_statuses(config, sheets)
    expected = {QueueStatus.PUBLISHED, QueueStatus.REVIEW, QueueStatus.ERROR}
    observed: set[QueueStatus] = set()
    for item in prior.items:
        assert item.queued_recipe is not None
        actual = statuses.get(str(item.queued_recipe.source_url))
        if actual is None:
            raise AssertionError(f"Queue row is missing for {item.queued_recipe.source_url}")
        status = QueueStatus(actual)
        if status not in expected:
            raise AssertionError(
                f"Queue row is no longer terminal: {item.queued_recipe.source_url} -> {status}"
            )
        observed.add(status)
    next_item = GoogleSheetsRecipeQueueReader(
        sheets,
        spreadsheet_id=config.queue_spreadsheet_id,
        categories=config.category_tabs,
    ).read_next()
    prior_ids = {
        item.queued_recipe.recipe_id for item in prior.items if item.queued_recipe is not None
    }
    assert next_item is None or next_item.recipe_id not in prior_ids
    print(f"READ-ONLY PASS: verified {len(prior.items)} prior row(s) remain terminal")
    print("READ-ONLY PASS: queue reader skips those terminal rows")
    print("Terminal statuses exercised: " + ", ".join(sorted(status.value for status in observed)))


def _print_call_counts(counts: dict[str, dict[str, int]]) -> None:
    for recipe_id, values in counts.items():
        print(
            f"Calls {recipe_id}: acquisition={values['acquisition']}, openai={values['openai']}, "
            f"docs={values['docs']}, drive={values['drive']}, master={values['master']}"
        )


def _terminal_statuses(result: BatchWorkflowResult) -> set[QueueStatus]:
    return {
        item.queue_status
        for item in result.items
        if item.queue_status in {QueueStatus.PUBLISHED, QueueStatus.REVIEW, QueueStatus.ERROR}
    }


def publication_for(item: QueueWorkflowResult) -> PublicationArtifact | None:
    return item.publication


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    preflight(config, args)
    services = GoogleOAuthServiceFactory(config).create_services()
    if args.verify_retry:
        verify_prior_batch(config, services.sheets)
        return
    workflow, tracked = build_workflow(config, services, args.model)
    max_items = None if args.all else args.max_items

    started = time.perf_counter()
    first = workflow.process_all(max_items=max_items)
    first_elapsed = time.perf_counter() - started
    print_batch("First batch", first)
    counts_after_first = per_recipe_counts(tracked, first.items)
    publications = {
        item.queued_recipe.recipe_id: publication_for(item)
        for item in first.items
        if item.queued_recipe is not None
    }

    terminal = {QueueStatus.PUBLISHED, QueueStatus.REVIEW, QueueStatus.ERROR}
    assert all(item.queue_status in terminal for item in first.items)
    assert all(
        publication is None or publication.master_row_written
        for publication in publications.values()
    )

    result_path = config.working_root / "google_batch_smoke_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        first.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    _print_call_counts(counts_after_first)
    observed = _terminal_statuses(first)
    print("Terminal statuses exercised: " + ", ".join(sorted(status.value for status in observed)))
    print(
        "PASS: first batch reached terminal statuses listed above; "
        "no unobserved status is claimed"
    )
    print(
        "Run --verify-retry for read-only terminal-row verification; "
        "it will not process new Pending rows"
    )
    print(f"First batch: {first_elapsed:.2f}s")
    print(f"Result: {result_path}")


if __name__ == "__main__":
    main()
