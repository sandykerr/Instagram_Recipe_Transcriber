"""Resolve one Approved, Accepted, or Rejected Review Sheet row and verify safe retry.

This is an explicit manual Google integration test. It creates or moves real Docs,
appends a real spreadsheet row, and deletes one active Review Sheet row. It is not
part of pytest and requires --execute-writes.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from instagram_recipe_transcriber.config import GoogleWorkflowConfig
from instagram_recipe_transcriber.google_adapters import (
    GoogleDocsRecipeWriter,
    GoogleDriveDocumentOrganizer,
    GoogleSheetsRecipeMasterWriter,
    GoogleSheetsRejectedRecipeWriter,
    GoogleSheetsReviewDecisionReader,
    GoogleSheetsReviewRowRemover,
)
from instagram_recipe_transcriber.google_oauth import GoogleOAuthServiceFactory
from instagram_recipe_transcriber.models import (
    QueuedRecipe,
    RecipeCandidate,
    RecipeDocument,
    RecipeDocumentPresentation,
    ReviewDecision,
    ReviewDecisionStatus,
    ReviewResolutionArtifact,
)
from instagram_recipe_transcriber.publication import (
    JsonPublicationStore,
    JsonReviewResolutionStore,
    JsonReviewStore,
)
from instagram_recipe_transcriber.review_promotion import ReviewPromotionWorkflow

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class ReplayDecisionReader:
    """Return the originally selected row on both passes, never the next live row."""

    def __init__(self, decision: ReviewDecision) -> None:
        self._decision = decision
        self.calls = 0

    def read_next(self) -> ReviewDecision:
        self.calls += 1
        return self._decision


class TrackedDocumentWriter:
    def __init__(self, delegate: GoogleDocsRecipeWriter) -> None:
        self._delegate = delegate
        self.calls = 0

    def create(
        self,
        recipe: RecipeCandidate,
        queued_recipe: QueuedRecipe,
        presentation: RecipeDocumentPresentation | None = None,
    ) -> RecipeDocument:
        print(f"Creating final Recipe Doc for {queued_recipe.recipe_id}...")
        self.calls += 1
        return self._delegate.create(recipe, queued_recipe, presentation)


class TrackedOrganizer:
    def __init__(self, delegate: GoogleDriveDocumentOrganizer) -> None:
        self._delegate = delegate
        self.calls = 0

    def move_to_folder(self, document: RecipeDocument) -> RecipeDocument:
        print(f"Moving Doc {document.document_id} to its destination folder...")
        self.calls += 1
        return self._delegate.move_to_folder(document)


class TrackedMasterWriter:
    def __init__(self, delegate: GoogleSheetsRecipeMasterWriter) -> None:
        self._delegate = delegate
        self.calls = 0

    def append(self, queued_recipe: QueuedRecipe, document: RecipeDocument) -> None:
        print(f"Appending {queued_recipe.recipe_id} to Recipe Master Sheet...")
        self.calls += 1
        self._delegate.append(queued_recipe, document)


class TrackedRejectedWriter:
    def __init__(self, delegate: GoogleSheetsRejectedRecipeWriter) -> None:
        self._delegate = delegate
        self.calls = 0

    def append(self, decision: ReviewDecision, document: RecipeDocument) -> None:
        print(f"Appending {decision.recipe_id} to the Rejected Sheet...")
        self.calls += 1
        self._delegate.append(decision, document)


class TrackedRowRemover:
    def __init__(self, delegate: GoogleSheetsReviewRowRemover) -> None:
        self._delegate = delegate
        self.calls = 0

    def remove(self, decision: ReviewDecision) -> None:
        print(f"Removing {decision.recipe_id} from the active Review Sheet...")
        self.calls += 1
        self._delegate.remove(decision)


class TrackedResolutionStore:
    def __init__(self, delegate: JsonReviewResolutionStore) -> None:
        self._delegate = delegate
        self.load_calls = 0
        self.save_calls = 0

    def load(self, recipe_id: str) -> ReviewResolutionArtifact | None:
        self.load_calls += 1
        return self._delegate.load(recipe_id)

    def save(self, resolution: ReviewResolutionArtifact) -> None:
        self.save_calls += 1
        self._delegate.save(resolution)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=REPOSITORY_ROOT / "google_workflow_config.json",
    )
    parser.add_argument(
        "--execute-writes",
        action="store_true",
        help="Required acknowledgement of real Docs, Drive, and Sheets mutations",
    )
    return parser.parse_args()


def load_config(path: Path) -> GoogleWorkflowConfig:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Google workflow config does not exist: {resolved}")
    return GoogleWorkflowConfig.model_validate_json(resolved.read_text(encoding="utf-8"))


def preflight(config: GoogleWorkflowConfig, *, execute_writes: bool) -> None:
    if not execute_writes:
        raise RuntimeError("Pass --execute-writes to acknowledge real external writes")
    if not config.oauth_client_path.is_file():
        raise FileNotFoundError(f"OAuth client JSON does not exist: {config.oauth_client_path}")
    if not config.oauth_token_path.is_file() or config.oauth_token_path.stat().st_size == 0:
        raise FileNotFoundError("A nonempty OAuth token JSON is required")
    if not config.review_delivery_configured():
        raise ValueError("Review Sheet and review Drive folder must be configured")
    if not config.rejection_delivery_configured():
        raise ValueError("Rejected Sheet and rejected Drive folder must be configured")
    if config.drive_folder_id is None:
        raise ValueError("The normal output Drive folder must be configured")


def read_one_decision(config: GoogleWorkflowConfig, sheets: Any) -> ReviewDecision:
    assert config.review_spreadsheet_id is not None
    decision = GoogleSheetsReviewDecisionReader(
        sheets,
        spreadsheet_id=config.review_spreadsheet_id,
        categories=config.category_tabs,
    ).read_next()
    if decision is None:
        raise RuntimeError(
            "No Approved, Accepted, or Rejected row is available in the Review Sheet"
        )
    return decision


def external_counts(
    document_writer: TrackedDocumentWriter,
    accepted_organizer: TrackedOrganizer,
    master_writer: TrackedMasterWriter,
    rejected_organizer: TrackedOrganizer,
    rejected_writer: TrackedRejectedWriter,
    row_remover: TrackedRowRemover,
) -> dict[str, int]:
    return {
        "final_doc_creates": document_writer.calls,
        "accepted_folder_moves": accepted_organizer.calls,
        "master_row_appends": master_writer.calls,
        "rejected_folder_moves": rejected_organizer.calls,
        "rejected_row_appends": rejected_writer.calls,
        "review_row_deletes": row_remover.calls,
    }


def expected_counts(decision: ReviewDecisionStatus) -> dict[str, int]:
    accepted = decision is ReviewDecisionStatus.ACCEPTED
    return {
        "final_doc_creates": int(accepted),
        "accepted_folder_moves": int(accepted),
        "master_row_appends": int(accepted),
        "rejected_folder_moves": int(not accepted),
        "rejected_row_appends": int(not accepted),
        "review_row_deletes": 1,
    }


def resolution_path(config: GoogleWorkflowConfig, recipe_id: str) -> Path:
    return config.working_root / "review-resolution" / recipe_id / "resolution.json"


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    preflight(config, execute_writes=args.execute_writes)
    services = GoogleOAuthServiceFactory(config).create_services()
    decision = read_one_decision(config, services.sheets)
    print(f"Selected {decision.decision.value} review row for {decision.recipe_id}.")

    review_store = JsonReviewStore(config.working_root / "review-publication")
    review = review_store.load(decision.recipe_id)
    if review is None or review.recipe is None:
        raise RuntimeError("Selected Review Sheet row has no local reviewed recipe checkpoint")
    checkpoint_path = resolution_path(config, decision.recipe_id)
    if checkpoint_path.exists():
        raise RuntimeError(
            "This clean smoke requires no pre-existing resolution checkpoint: "
            f"{checkpoint_path}"
        )

    assert config.review_spreadsheet_id is not None
    assert config.rejected_spreadsheet_id is not None
    assert config.rejected_drive_folder_id is not None
    assert config.drive_folder_id is not None
    decision_reader = ReplayDecisionReader(decision)
    resolution_store = TrackedResolutionStore(
        JsonReviewResolutionStore(config.working_root / "review-resolution")
    )
    document_writer = TrackedDocumentWriter(GoogleDocsRecipeWriter(services.docs))
    accepted_organizer = TrackedOrganizer(
        GoogleDriveDocumentOrganizer(services.drive, folder_id=config.drive_folder_id)
    )
    master_writer = TrackedMasterWriter(
        GoogleSheetsRecipeMasterWriter(
            services.sheets,
            spreadsheet_id=config.master_spreadsheet_id,
        )
    )
    rejected_organizer = TrackedOrganizer(
        GoogleDriveDocumentOrganizer(
            services.drive,
            folder_id=config.rejected_drive_folder_id,
        )
    )
    rejected_writer = TrackedRejectedWriter(
        GoogleSheetsRejectedRecipeWriter(
            services.sheets,
            spreadsheet_id=config.rejected_spreadsheet_id,
        )
    )
    row_remover = TrackedRowRemover(
        GoogleSheetsReviewRowRemover(
            services.sheets,
            spreadsheet_id=config.review_spreadsheet_id,
        )
    )
    workflow = ReviewPromotionWorkflow(
        decision_reader=decision_reader,
        review_store=review_store,
        resolution_store=resolution_store,
        review_row_remover=row_remover,
        document_writer=document_writer,
        master_writer=master_writer,
        publication_store=JsonPublicationStore(config.working_root / "publication"),
        accepted_document_organizer=accepted_organizer,
        rejected_document_organizer=rejected_organizer,
        rejected_writer=rejected_writer,
    )

    started = time.perf_counter()
    print("Promoting selected review row...")
    first = workflow.process_next()
    first_elapsed = time.perf_counter() - started
    assert first is not None
    assert first.decision is decision.decision
    assert first.review_row_removed
    assert checkpoint_path.is_file()
    persisted = JsonReviewResolutionStore(
        config.working_root / "review-resolution"
    ).load(decision.recipe_id)
    assert persisted == first
    assert external_counts(
        document_writer,
        accepted_organizer,
        master_writer,
        rejected_organizer,
        rejected_writer,
        row_remover,
    ) == expected_counts(decision.decision)

    if decision.decision is ReviewDecisionStatus.ACCEPTED:
        assert first.master_row_written
        assert not first.rejected_row_written
        assert first.document.document_id != review.document.document_id
        assert first.document.folder_id == config.drive_folder_id
    else:
        assert first.rejected_row_written
        assert not first.master_row_written
        assert first.document.document_id == review.document.document_id
        assert first.document.folder_id == config.rejected_drive_folder_id

    counts_after_first = external_counts(
        document_writer,
        accepted_organizer,
        master_writer,
        rejected_organizer,
        rejected_writer,
        row_remover,
    )
    saves_after_first = resolution_store.save_calls
    checkpoint_after_first = checkpoint_path.read_bytes()
    started = time.perf_counter()
    print("Verifying retry uses the persisted resolution without external writes...")
    second = workflow.process_next()
    retry_elapsed = time.perf_counter() - started
    assert second == first
    assert external_counts(
        document_writer,
        accepted_organizer,
        master_writer,
        rejected_organizer,
        rejected_writer,
        row_remover,
    ) == counts_after_first
    assert resolution_store.save_calls == saves_after_first
    assert checkpoint_path.read_bytes() == checkpoint_after_first

    print(f"Decision: {decision.decision.value}")
    print(f"Source URL: {decision.source_url}")
    print(f"Resulting Doc URL: {first.document.url}")
    print("review_decision_reads: 1")
    for name, count in counts_after_first.items():
        print(f"{name}: {count}")
    print(f"resolution_loads: {resolution_store.load_calls}")
    print(f"resolution_saves: {resolution_store.save_calls}")
    print(f"decision_replays: {decision_reader.calls}")
    print("PASS: persisted resolution checkpoint matches the workflow result")
    print("PASS: retry made no duplicate Doc, row, move, or deletion")
    print(f"First pass: {first_elapsed:.2f}s; retry: {retry_elapsed:.2f}s")
    print(f"Checkpoint: {checkpoint_path}")


if __name__ == "__main__":
    main()
