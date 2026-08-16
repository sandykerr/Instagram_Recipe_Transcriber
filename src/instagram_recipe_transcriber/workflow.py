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
)
from .models import PipelineResult, PublicationArtifact, QueuedRecipe


class QueueWorkflowResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    queued_recipe: QueuedRecipe | None = None
    pipeline_result: PipelineResult | None = None
    publication: PublicationArtifact | None = None


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
    ) -> None:
        self._queue_reader = queue_reader
        self._media_acquirer = media_acquirer
        self._pipeline_runner = pipeline_runner
        self._document_writer = document_writer
        self._master_writer = master_writer
        self._publication_store = publication_store
        self._document_organizer = document_organizer

    def process_next(self) -> QueueWorkflowResult:
        queued_recipe = self._queue_reader.read_next()
        if queued_recipe is None:
            return QueueWorkflowResult()
        acquired_recipe = self._media_acquirer.acquire(queued_recipe)
        pipeline_result = self._pipeline_runner.run(acquired_recipe.job)
        if pipeline_result.validation.outcome.value != "ready":
            return QueueWorkflowResult(
                queued_recipe=queued_recipe,
                pipeline_result=pipeline_result,
            )

        publication = self._publication_store.load(queued_recipe.recipe_id)
        if publication is None:
            document = self._document_writer.create(pipeline_result.recipe, queued_recipe)
            if self._document_organizer is not None:
                document = self._document_organizer.move_to_folder(document)
            publication = PublicationArtifact(recipe_id=queued_recipe.recipe_id, document=document)
            self._publication_store.save(publication)
        if not publication.master_row_written:
            self._master_writer.append(queued_recipe, publication.document)
            publication = publication.model_copy(update={"master_row_written": True})
            self._publication_store.save(publication)
        return QueueWorkflowResult(
            queued_recipe=queued_recipe,
            pipeline_result=pipeline_result,
            publication=publication,
        )
