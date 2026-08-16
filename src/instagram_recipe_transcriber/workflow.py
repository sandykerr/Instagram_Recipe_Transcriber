"""Synchronous one-item orchestration from the queue sheet to published recipe."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .interfaces import (
    DocumentOrganizer,
    MediaAcquirer,
    PublicationStore,
    RecipeDocumentWriter,
    RecipeMasterWriter,
    RecipePipeline,
    RecipeQueueReader,
    RecipeQueueStateWriter,
    RecipeReviewDocumentWriter,
    RecipeReviewWriter,
    ReviewStore,
)
from .models import (
    PipelineResult,
    PublicationArtifact,
    QueuedRecipe,
    QueueStatus,
    RecipeOutcome,
    ReviewArtifact,
    ReviewCategory,
    SourceKind,
)


class QueueWorkflowResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    queued_recipe: QueuedRecipe | None = None
    pipeline_result: PipelineResult | None = None
    publication: PublicationArtifact | None = None
    review: ReviewArtifact | None = None
    queue_status: QueueStatus | None = None
    error_message: str | None = None


class BatchWorkflowResult(BaseModel):
    """Summary of one bounded, sequential queue-processing run."""

    model_config = ConfigDict(frozen=True)

    items: tuple[QueueWorkflowResult, ...] = ()

    @property
    def processed_count(self) -> int:
        return len(self.items)


class QueuedRecipeWorkflow:
    """Processes at most one queue entry and records delivery checkpoints for retries."""

    def __init__(
        self,
        *,
        queue_reader: RecipeQueueReader,
        media_acquirer: MediaAcquirer,
        pipeline_runner: RecipePipeline,
        document_writer: RecipeDocumentWriter,
        master_writer: RecipeMasterWriter,
        publication_store: PublicationStore,
        document_organizer: DocumentOrganizer | None = None,
        queue_state_writer: RecipeQueueStateWriter | None = None,
        review_document_writer: RecipeReviewDocumentWriter | None = None,
        review_writer: RecipeReviewWriter | None = None,
        review_store: ReviewStore | None = None,
        review_document_organizer: DocumentOrganizer | None = None,
    ) -> None:
        self._queue_reader = queue_reader
        self._media_acquirer = media_acquirer
        self._pipeline_runner = pipeline_runner
        self._document_writer = document_writer
        self._master_writer = master_writer
        self._publication_store = publication_store
        self._document_organizer = document_organizer
        self._queue_state_writer = queue_state_writer
        review_components = (review_document_writer, review_writer, review_store)
        if any(value is not None for value in review_components) and not all(
            value is not None for value in review_components
        ):
            raise ValueError("review delivery requires a document writer, sheet writer, and store")
        self._review_document_writer = review_document_writer
        self._review_writer = review_writer
        self._review_store = review_store
        self._review_document_organizer = review_document_organizer

    def process_next(self) -> QueueWorkflowResult:
        queued_recipe = self._queue_reader.read_next()
        if queued_recipe is None:
            return QueueWorkflowResult()
        self._mark(queued_recipe, QueueStatus.PROCESSING)
        try:
            acquired_recipe = self._media_acquirer.acquire(queued_recipe)
            pipeline_result = self._pipeline_runner.run(acquired_recipe.job)
            if pipeline_result.validation.outcome is not RecipeOutcome.READY:
                review = self._deliver_review(queued_recipe, pipeline_result)
                detail = (
                    str(review.document.url)
                    if review is not None
                    else _review_detail(pipeline_result)
                )
                self._mark(queued_recipe, QueueStatus.REVIEW, detail)
                return QueueWorkflowResult(
                    queued_recipe=queued_recipe,
                    pipeline_result=pipeline_result,
                    review=review,
                    queue_status=QueueStatus.REVIEW,
                )

            publication = self._publication_store.load(queued_recipe.recipe_id)
            if publication is None:
                document = self._document_writer.create(pipeline_result.recipe, queued_recipe)
                if self._document_organizer is not None:
                    document = self._document_organizer.move_to_folder(document)
                publication = PublicationArtifact(
                    recipe_id=queued_recipe.recipe_id,
                    document=document,
                )
                self._publication_store.save(publication)
            if not publication.master_row_written:
                self._master_writer.append(queued_recipe, publication.document)
                publication = publication.model_copy(update={"master_row_written": True})
                self._publication_store.save(publication)
            self._mark(queued_recipe, QueueStatus.PUBLISHED, str(publication.document.url))
            return QueueWorkflowResult(
                queued_recipe=queued_recipe,
                pipeline_result=pipeline_result,
                publication=publication,
                queue_status=QueueStatus.PUBLISHED,
            )
        except Exception as error:
            self._mark(queued_recipe, QueueStatus.ERROR, str(error))
            return QueueWorkflowResult(
                queued_recipe=queued_recipe,
                queue_status=QueueStatus.ERROR,
                error_message=str(error),
            )

    def process_all(self, *, max_items: int | None = None) -> BatchWorkflowResult:
        """Process eligible rows synchronously, continuing after individual failures."""
        if max_items is not None and max_items < 1:
            raise ValueError("max_items must be at least 1")
        if self._queue_state_writer is None:
            raise ValueError("process_all requires a queue_state_writer to advance the queue")
        items: list[QueueWorkflowResult] = []
        while max_items is None or len(items) < max_items:
            item = self.process_next()
            if item.queued_recipe is None:
                break
            items.append(item)
        return BatchWorkflowResult(items=tuple(items))

    def _mark(
        self,
        queued_recipe: QueuedRecipe,
        status: QueueStatus,
        detail: str | None = None,
    ) -> None:
        if self._queue_state_writer is not None:
            self._queue_state_writer.mark(queued_recipe, status, detail)

    def _deliver_review(
        self, queued_recipe: QueuedRecipe, pipeline_result: PipelineResult
    ) -> ReviewArtifact | None:
        if self._review_store is None:
            return None
        review = self._review_store.load(queued_recipe.recipe_id)
        if review is None:
            assert self._review_document_writer is not None
            document = self._review_document_writer.create(pipeline_result, queued_recipe)
            if self._review_document_organizer is not None:
                document = self._review_document_organizer.move_to_folder(document)
            review = ReviewArtifact(
                recipe_id=queued_recipe.recipe_id,
                document=document,
                recipe=pipeline_result.recipe,
                review_categories=_review_categories(pipeline_result),
                transcript_text=_transcript_text(pipeline_result),
            )
            self._review_store.save(review)
        if not review.review_row_written:
            assert self._review_writer is not None
            self._review_writer.append(queued_recipe, review.document)
            review = review.model_copy(update={"review_row_written": True})
            self._review_store.save(review)
        return review


def _review_detail(pipeline_result: PipelineResult) -> str:
    findings = pipeline_result.validation.findings
    if not findings:
        return "Recipe requires human review"
    return "; ".join(finding.message for finding in findings)


def _review_categories(pipeline_result: PipelineResult) -> tuple[ReviewCategory, ...]:
    """Translate validation codes into durable, non-exclusive review categories."""

    categories: set[ReviewCategory] = set()
    for finding in pipeline_result.validation.findings:
        code = finding.code.lower().replace("-", "_")
        message = finding.message.lower()
        if code in {"unquantified_core_ingredient", "missing_quantity"}:
            categories.add(ReviewCategory.INGREDIENTS_AMOUNTS_MISSING)
        elif code == "missing_critical_step":
            categories.add(ReviewCategory.MISSING_CRITICAL_STEP)
        elif code in {"missing_title", "unsupported_title"}:
            categories.add(ReviewCategory.MISSING_TITLE)
        elif code == "missing_ingredients":
            categories.add(ReviewCategory.MISSING_INGREDIENTS)
        elif code in {"missing_instructions", "missing_steps"}:
            categories.add(ReviewCategory.MISSING_INSTRUCTIONS)
        elif code == "missing_servings":
            categories.add(ReviewCategory.SERVINGS_MISSING)
        elif code in {"missing_calories", "missing_macros"}:
            categories.add(ReviewCategory.NUTRITION_MISSING)
        elif code in {"source_conflict", "evidence_conflict"}:
            categories.add(
                ReviewCategory.INGREDIENTS_MISMATCH
                if "ingredient" in message
                else ReviewCategory.SOURCE_CONFLICT
            )
        else:
            categories.add(ReviewCategory.RECIPE_INCOMPLETE)
    return tuple(sorted(categories, key=lambda category: category.value))


def _transcript_text(pipeline_result: PipelineResult) -> str | None:
    segments = [
        segment.text.strip()
        for segment in pipeline_result.evidence_segments
        if segment.source_kind is SourceKind.TRANSCRIPT and segment.text.strip()
    ]
    return "\n".join(segments) or None
