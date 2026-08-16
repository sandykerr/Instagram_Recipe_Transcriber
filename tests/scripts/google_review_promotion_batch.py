"""Process a bounded batch of Approved/Accepted/Rejected Review Sheet rows.

This is a manual integration operation, not a pytest test. It makes real Docs,
Drive, and Sheets changes and requires --execute-writes. It defaults to two
rows. --all additionally requires --confirm-all.
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
)
from instagram_recipe_transcriber.publication import (
    JsonPublicationStore,
    JsonReviewResolutionStore,
    JsonReviewStore,
)
from instagram_recipe_transcriber.review_promotion import ReviewPromotionWorkflow

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class RecordingDecisionReader:
    """Reads live decisions and retains them for read-only retry verification."""

    def __init__(self, delegate: GoogleSheetsReviewDecisionReader) -> None:
        self._delegate = delegate
        self.decisions: list[ReviewDecision] = []

    def read_next(self) -> ReviewDecision | None:
        decision = self._delegate.read_next()
        if decision is not None:
            self.decisions.append(decision)
            print(
                f"[{len(self.decisions)}] Selected {decision.decision.value} review row: "
                f"{decision.recipe_id}"
            )
        return decision


class ReplayDecisionReader:
    """Replays selected decisions without consulting the live Review Sheet."""

    def __init__(self, decisions: tuple[ReviewDecision, ...]) -> None:
        self._decisions = list(decisions)

    def read_next(self) -> ReviewDecision | None:
        return self._decisions.pop(0) if self._decisions else None


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=REPOSITORY_ROOT / "google_workflow_config.json",
    )
    limit = parser.add_mutually_exclusive_group()
    limit.add_argument("--max-items", type=int, default=2)
    limit.add_argument("--all", action="store_true", help="Process every actionable row")
    parser.add_argument(
        "--confirm-all",
        action="store_true",
        help="Required together with --all to acknowledge unlimited processing",
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


def preflight(config: GoogleWorkflowConfig, args: argparse.Namespace) -> None:
    if not args.execute_writes:
        raise RuntimeError("Pass --execute-writes to acknowledge real external writes")
    if args.all and not args.confirm_all:
        raise RuntimeError("--all requires --confirm-all")
    if not args.all and args.max_items < 1:
        raise ValueError("--max-items must be at least 1")
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


def call_counts(
    document_writer: TrackedDocumentWriter,
    accepted_organizer: TrackedOrganizer,
    master_writer: TrackedMasterWriter,
    rejected_organizer: TrackedOrganizer,
    rejected_writer: TrackedRejectedWriter,
    row_remover: TrackedRowRemover,
) -> dict[str, int]:
    return {
        "final_docs": document_writer.calls,
        "accepted_moves": accepted_organizer.calls,
        "master_rows": master_writer.calls,
        "rejected_moves": rejected_organizer.calls,
        "rejected_rows": rejected_writer.calls,
        "review_rows_deleted": row_remover.calls,
    }


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    preflight(config, args)
    assert config.review_spreadsheet_id is not None
    assert config.rejected_spreadsheet_id is not None
    assert config.rejected_drive_folder_id is not None
    assert config.drive_folder_id is not None
    services = GoogleOAuthServiceFactory(config).create_services()

    live_reader = RecordingDecisionReader(
        GoogleSheetsReviewDecisionReader(
            services.sheets,
            spreadsheet_id=config.review_spreadsheet_id,
            categories=config.category_tabs,
        )
    )
    review_store = JsonReviewStore(config.working_root / "review-publication")
    resolution_store = JsonReviewResolutionStore(config.working_root / "review-resolution")
    document_writer = TrackedDocumentWriter(GoogleDocsRecipeWriter(services.docs))
    accepted_organizer = TrackedOrganizer(
        GoogleDriveDocumentOrganizer(services.drive, folder_id=config.drive_folder_id)
    )
    master_writer = TrackedMasterWriter(
        GoogleSheetsRecipeMasterWriter(services.sheets, spreadsheet_id=config.master_spreadsheet_id)
    )
    rejected_organizer = TrackedOrganizer(
        GoogleDriveDocumentOrganizer(services.drive, folder_id=config.rejected_drive_folder_id)
    )
    rejected_writer = TrackedRejectedWriter(
        GoogleSheetsRejectedRecipeWriter(
            services.sheets,
            spreadsheet_id=config.rejected_spreadsheet_id,
        )
    )
    row_remover = TrackedRowRemover(
        GoogleSheetsReviewRowRemover(services.sheets, spreadsheet_id=config.review_spreadsheet_id)
    )

    def build_workflow(reader: Any) -> ReviewPromotionWorkflow:
        return ReviewPromotionWorkflow(
            decision_reader=reader,
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
    limit_label = "all actionable rows" if args.all else f"up to {args.max_items} row(s)"
    print(f"Starting review-promotion batch for {limit_label}...")
    result = build_workflow(live_reader).process_all(
        max_items=None if args.all else args.max_items
    )
    elapsed = time.perf_counter() - started
    counts_after_first = call_counts(
        document_writer,
        accepted_organizer,
        master_writer,
        rejected_organizer,
        rejected_writer,
        row_remover,
    )

    for resolution in result.resolutions:
        print(
            f"{resolution.decision.value}: {resolution.recipe_id} | "
            f"{resolution.document.url}"
        )
    print(f"Processed: {result.processed_count} review row(s) in {elapsed:.2f}s")
    if not result.resolutions:
        print("No Approved, Accepted, or Rejected Review Sheet rows were available.")

    print("Verifying retry uses persisted resolutions without external writes...")
    retry = build_workflow(ReplayDecisionReader(tuple(live_reader.decisions))).process_all(
        max_items=len(live_reader.decisions) or 1
    )
    assert retry.resolutions == result.resolutions
    assert call_counts(
        document_writer,
        accepted_organizer,
        master_writer,
        rejected_organizer,
        rejected_writer,
        row_remover,
    ) == counts_after_first
    print("PASS: in-process retry reused every resolution without extra external writes")
    external_calls = ", ".join(f"{name}={count}" for name, count in counts_after_first.items())
    print("External calls: " + external_calls)


if __name__ == "__main__":
    main()
