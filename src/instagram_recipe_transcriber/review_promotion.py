"""Manual Review Sheet decision processing, kept separate from recipe extraction."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .interfaces import (
    DocumentOrganizer,
    PublicationStore,
    RecipeDocumentWriter,
    RecipeMasterWriter,
    RejectedRecipeWriter,
    ReviewDecisionReader,
    ReviewResolutionStore,
    ReviewRowRemover,
    ReviewStore,
)
from .models import (
    PublicationArtifact,
    QueuedRecipe,
    RecipeDocumentPresentation,
    RecipeInstructionFormat,
    ReviewArtifact,
    ReviewCategory,
    ReviewDecision,
    ReviewDecisionStatus,
    ReviewResolutionArtifact,
)


class ReviewPromotionBatchResult(BaseModel):
    """Results from one bounded, sequential manual-review promotion run."""

    model_config = ConfigDict(frozen=True)

    resolutions: tuple[ReviewResolutionArtifact, ...] = ()

    @property
    def processed_count(self) -> int:
        return len(self.resolutions)


class ReviewPromotionWorkflow:
    """Promotes accepted reviews or archives rejected reviews one decision at a time."""

    def __init__(
        self,
        *,
        decision_reader: ReviewDecisionReader,
        review_store: ReviewStore,
        resolution_store: ReviewResolutionStore,
        review_row_remover: ReviewRowRemover,
        document_writer: RecipeDocumentWriter,
        master_writer: RecipeMasterWriter,
        publication_store: PublicationStore,
        accepted_document_organizer: DocumentOrganizer,
        rejected_document_organizer: DocumentOrganizer,
        rejected_writer: RejectedRecipeWriter,
    ) -> None:
        self._decision_reader = decision_reader
        self._review_store = review_store
        self._resolution_store = resolution_store
        self._review_row_remover = review_row_remover
        self._document_writer = document_writer
        self._master_writer = master_writer
        self._publication_store = publication_store
        self._accepted_document_organizer = accepted_document_organizer
        self._rejected_document_organizer = rejected_document_organizer
        self._rejected_writer = rejected_writer

    def process_next(self) -> ReviewResolutionArtifact | None:
        decision = self._decision_reader.read_next()
        if decision is None:
            return None
        review = self._review_store.load(decision.recipe_id)
        if review is None or review.recipe is None:
            raise ValueError("Review decision has no persisted candidate recipe to promote")
        queued = QueuedRecipe(
            recipe_id=decision.recipe_id,
            source_url=decision.source_url,
            category=decision.category,
            queue_row_number=decision.review_row_number,
            description=decision.description,
        )
        resolution = self._resolution_store.load(decision.recipe_id)
        if resolution is None:
            if decision.decision is ReviewDecisionStatus.ACCEPTED:
                publication = self._publication_store.load(decision.recipe_id)
                if publication is None:
                    document = self._accepted_document_organizer.move_to_folder(
                        self._document_writer.create(
                            review.recipe,
                            queued,
                            _approved_presentation(review, decision),
                        )
                    )
                    publication = PublicationArtifact(
                        recipe_id=decision.recipe_id,
                        document=document,
                    )
                    self._publication_store.save(publication)
                resolution = ReviewResolutionArtifact(
                    recipe_id=decision.recipe_id,
                    decision=decision.decision,
                    document=publication.document,
                    master_row_written=publication.master_row_written,
                )
            else:
                document = self._rejected_document_organizer.move_to_folder(review.document)
                resolution = ReviewResolutionArtifact(
                    recipe_id=decision.recipe_id,
                    decision=decision.decision,
                    document=document,
                )
            self._resolution_store.save(resolution)
        if (
            resolution.decision is ReviewDecisionStatus.ACCEPTED
            and not resolution.master_row_written
        ):
            self._master_writer.append(queued, resolution.document)
            resolution = resolution.model_copy(update={"master_row_written": True})
            self._resolution_store.save(resolution)
            self._publication_store.save(
                PublicationArtifact(
                    recipe_id=decision.recipe_id,
                    document=resolution.document,
                    master_row_written=True,
                )
            )
        if (
            resolution.decision is ReviewDecisionStatus.REJECTED
            and not resolution.rejected_row_written
        ):
            self._rejected_writer.append(decision, resolution.document)
            resolution = resolution.model_copy(update={"rejected_row_written": True})
            self._resolution_store.save(resolution)
        if not resolution.review_row_removed:
            self._review_row_remover.remove(decision)
            resolution = resolution.model_copy(update={"review_row_removed": True})
            self._resolution_store.save(resolution)
        return resolution

    def process_all(
        self, *, max_items: int | None = None
    ) -> ReviewPromotionBatchResult:
        """Process approved/rejected rows sequentially, stopping when none remain.

        Errors deliberately stop the batch. The unresolved row remains in the
        Review Sheet and its checkpoint preserves any completed work for a safe
        retry; continuing would otherwise repeatedly select that same row.
        """

        if max_items is not None and max_items < 1:
            raise ValueError("max_items must be at least 1")
        resolutions: list[ReviewResolutionArtifact] = []
        while max_items is None or len(resolutions) < max_items:
            resolution = self.process_next()
            if resolution is None:
                break
            resolutions.append(resolution)
        return ReviewPromotionBatchResult(resolutions=tuple(resolutions))


def _approved_presentation(
    review: ReviewArtifact, decision: ReviewDecision
) -> RecipeDocumentPresentation | None:
    """Apply manual review fields and raw-transcript instructions when needed."""

    uses_raw_transcript = ReviewCategory.MISSING_CRITICAL_STEP in review.review_categories
    if not uses_raw_transcript and not decision.servings_text and not decision.nutrition_notes:
        return None
    if uses_raw_transcript and (
        review.transcript_text is None or not review.transcript_text.strip()
    ):
        raise ValueError(
            "Cannot approve a missing-critical-step review without a retained transcript"
        )
    return RecipeDocumentPresentation(
        instruction_format=(
            RecipeInstructionFormat.RAW_TRANSCRIPT
            if uses_raw_transcript
            else RecipeInstructionFormat.NUMBERED_STEPS
        ),
        raw_instruction_text=review.transcript_text if uses_raw_transcript else None,
        servings_text=decision.servings_text,
        nutrition_notes=decision.nutrition_notes,
    )
