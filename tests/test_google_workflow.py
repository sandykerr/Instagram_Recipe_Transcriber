from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import HttpUrl

from instagram_recipe_transcriber.config import GoogleWorkflowConfig
from instagram_recipe_transcriber.google_oauth import (
    GoogleOAuthServiceFactory,
    _run_local_authorization,
)
from instagram_recipe_transcriber.models import (
    AcquiredRecipe,
    EvidenceSegment,
    PipelineResult,
    QueuedRecipe,
    QueueStatus,
    RecipeCandidate,
    RecipeDocument,
    RecipeDocumentPresentation,
    RecipeJob,
    RecipeOutcome,
    ReviewCategory,
    SourceKind,
    ValidationArtifact,
    ValidationFinding,
)
from instagram_recipe_transcriber.publication import JsonPublicationStore, JsonReviewStore
from instagram_recipe_transcriber.workflow import QueuedRecipeWorkflow


def _queued_recipe() -> QueuedRecipe:
    return QueuedRecipe(
        recipe_id="workflow-recipe",
        source_url=HttpUrl("https://www.instagram.com/reel/DVJBGzyk8E5/"),
        category="Desserts",
        queue_row_number=2,
    )


class _QueueReader:
    def read_next(self) -> QueuedRecipe:
        return _queued_recipe()


class _ListQueueReader:
    def __init__(self, recipes: list[QueuedRecipe]) -> None:
        self._recipes = recipes

    def read_next(self) -> QueuedRecipe | None:
        return self._recipes.pop(0) if self._recipes else None


class _QueueStateWriter:
    def __init__(self) -> None:
        self.marks: list[tuple[str, QueueStatus, str | None]] = []

    def mark(
        self, queued_recipe: QueuedRecipe, status: QueueStatus, detail: str | None = None
    ) -> None:
        self.marks.append((queued_recipe.recipe_id, status, detail))


class _Acquirer:
    version = "fake-acquirer-v1"

    def acquire(self, queued_recipe: QueuedRecipe) -> AcquiredRecipe:
        from instagram_recipe_transcriber.models import RecipeJob

        return AcquiredRecipe(
            queued_recipe=queued_recipe,
            job=RecipeJob(recipe_id=queued_recipe.recipe_id, source_url=queued_recipe.source_url),
            metadata_path=Path("source.info.json"),
        )


class _Pipeline:
    def run(self, job: RecipeJob) -> PipelineResult:
        return PipelineResult(
            recipe=RecipeCandidate(title="Published recipe"),
            validation=ValidationArtifact(outcome=RecipeOutcome.READY),
        )


class _ReviewPipeline:
    def run(self, job: RecipeJob) -> PipelineResult:
        return PipelineResult(
            recipe=RecipeCandidate(title="Needs review"),
            validation=ValidationArtifact(outcome=RecipeOutcome.REVIEW),
        )


class _CategorizedReviewPipeline:
    def run(self, job: RecipeJob) -> PipelineResult:
        return PipelineResult(
            recipe=RecipeCandidate(title="Needs review"),
            validation=ValidationArtifact(
                outcome=RecipeOutcome.REVIEW,
                findings=(
                    ValidationFinding(
                        code="missing-critical-step", message="Final cooking step is missing"
                    ),
                    ValidationFinding(
                        code="unquantified_core_ingredient", message="Chicken has no amount"
                    ),
                ),
            ),
            evidence_segments=(
                EvidenceSegment(
                    evidence_id="transcript-1",
                    source_kind=SourceKind.TRANSCRIPT,
                    text="Cook the chicken until done.",
                ),
            ),
        )


class _FailingAcquirer:
    version = "fake-acquirer-v1"

    def acquire(self, queued_recipe: QueuedRecipe) -> AcquiredRecipe:
        raise RuntimeError("download unavailable")


class _DocumentWriter:
    def __init__(self) -> None:
        self.calls = 0

    def create(
        self,
        recipe: RecipeCandidate,
        queued_recipe: QueuedRecipe,
        presentation: RecipeDocumentPresentation | None = None,
    ) -> RecipeDocument:
        self.calls += 1
        return RecipeDocument(
            document_id="document-1",
            title="Published recipe",
            url=HttpUrl("https://docs.google.com/document/d/document-1/edit"),
        )


class _MasterWriter:
    def __init__(self) -> None:
        self.calls = 0

    def append(self, queued_recipe: QueuedRecipe, document: RecipeDocument) -> None:
        self.calls += 1


class _ReviewDocumentWriter:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, result: PipelineResult, queued_recipe: QueuedRecipe) -> RecipeDocument:
        self.calls += 1
        return RecipeDocument(
            document_id="review-document-1",
            title="REVIEW — Needs review",
            url=HttpUrl("https://docs.google.com/document/d/review-document-1/edit"),
        )


class _ReviewWriter:
    def __init__(self) -> None:
        self.calls = 0

    def append(self, queued_recipe: QueuedRecipe, document: RecipeDocument) -> None:
        self.calls += 1


def test_workflow_persists_publication_before_master_write_and_avoids_duplicates(
    tmp_path: Path,
) -> None:
    writer = _DocumentWriter()
    master_writer = _MasterWriter()
    workflow = QueuedRecipeWorkflow(
        queue_reader=_QueueReader(),
        media_acquirer=_Acquirer(),
        pipeline_runner=_Pipeline(),
        document_writer=writer,
        master_writer=master_writer,
        publication_store=JsonPublicationStore(tmp_path),
    )

    first = workflow.process_next()
    second = workflow.process_next()

    assert first.publication is not None
    assert first.publication.master_row_written
    assert second.publication == first.publication
    assert writer.calls == 1
    assert master_writer.calls == 1


def test_batch_processes_each_item_once_and_records_published_review_and_error_states(
    tmp_path: Path,
) -> None:
    recipes = [
        _queued_recipe(),
        _queued_recipe().model_copy(update={"recipe_id": "needs-review"}),
        _queued_recipe().model_copy(update={"recipe_id": "download-error"}),
    ]
    queue_writer = _QueueStateWriter()
    writer = _DocumentWriter()
    master_writer = _MasterWriter()

    class _AcquirerByRecipe(_Acquirer):
        def acquire(self, queued_recipe: QueuedRecipe) -> AcquiredRecipe:
            if queued_recipe.recipe_id == "download-error":
                return _FailingAcquirer().acquire(queued_recipe)
            return super().acquire(queued_recipe)

    class _PipelineByRecipe(_Pipeline):
        def run(self, job: RecipeJob) -> PipelineResult:
            recipe_id = job.recipe_id
            return _ReviewPipeline().run(job) if recipe_id == "needs-review" else super().run(job)

    workflow = QueuedRecipeWorkflow(
        queue_reader=_ListQueueReader(recipes),
        media_acquirer=_AcquirerByRecipe(),
        pipeline_runner=_PipelineByRecipe(),
        document_writer=writer,
        master_writer=master_writer,
        publication_store=JsonPublicationStore(tmp_path),
        queue_state_writer=queue_writer,
    )

    result = workflow.process_all()

    assert result.processed_count == 3
    assert [item.queue_status for item in result.items] == [
        QueueStatus.PUBLISHED,
        QueueStatus.REVIEW,
        QueueStatus.ERROR,
    ]
    assert writer.calls == 1
    assert master_writer.calls == 1
    assert [mark[:2] for mark in queue_writer.marks] == [
        ("workflow-recipe", QueueStatus.PROCESSING),
        ("workflow-recipe", QueueStatus.PUBLISHED),
        ("needs-review", QueueStatus.PROCESSING),
        ("needs-review", QueueStatus.REVIEW),
        ("download-error", QueueStatus.PROCESSING),
        ("download-error", QueueStatus.ERROR),
    ]


def test_batch_requires_durable_queue_state_writer(tmp_path: Path) -> None:
    workflow = QueuedRecipeWorkflow(
        queue_reader=_QueueReader(),
        media_acquirer=_Acquirer(),
        pipeline_runner=_Pipeline(),
        document_writer=_DocumentWriter(),
        master_writer=_MasterWriter(),
        publication_store=JsonPublicationStore(tmp_path),
    )

    with pytest.raises(ValueError, match="queue_state_writer"):
        workflow.process_all()


def test_review_delivery_is_idempotent_and_links_the_queue_to_the_review_doc(
    tmp_path: Path,
) -> None:
    review_document_writer = _ReviewDocumentWriter()
    review_writer = _ReviewWriter()
    queue_writer = _QueueStateWriter()
    workflow = QueuedRecipeWorkflow(
        queue_reader=_QueueReader(),
        media_acquirer=_Acquirer(),
        pipeline_runner=_ReviewPipeline(),
        document_writer=_DocumentWriter(),
        master_writer=_MasterWriter(),
        publication_store=JsonPublicationStore(tmp_path),
        queue_state_writer=queue_writer,
        review_document_writer=review_document_writer,
        review_writer=review_writer,
        review_store=JsonReviewStore(tmp_path),
    )

    first = workflow.process_next()
    second = workflow.process_next()

    assert first.review is not None
    assert first.review.review_row_written
    assert second.review == first.review
    assert review_document_writer.calls == 1
    assert review_writer.calls == 1
    assert queue_writer.marks[-1] == (
        "workflow-recipe",
        QueueStatus.REVIEW,
        "https://docs.google.com/document/d/review-document-1/edit",
    )


def test_review_delivery_persists_multiple_categories_and_transcript(tmp_path: Path) -> None:
    workflow = QueuedRecipeWorkflow(
        queue_reader=_QueueReader(),
        media_acquirer=_Acquirer(),
        pipeline_runner=_CategorizedReviewPipeline(),
        document_writer=_DocumentWriter(),
        master_writer=_MasterWriter(),
        publication_store=JsonPublicationStore(tmp_path),
        queue_state_writer=_QueueStateWriter(),
        review_document_writer=_ReviewDocumentWriter(),
        review_writer=_ReviewWriter(),
        review_store=JsonReviewStore(tmp_path),
    )

    result = workflow.process_next()

    assert result.review is not None
    assert result.review.review_categories == (
        ReviewCategory.INGREDIENTS_AMOUNTS_MISSING,
        ReviewCategory.MISSING_CRITICAL_STEP,
    )
    assert result.review.transcript_text == "Cook the chicken until done."


def test_oauth_factory_builds_three_services_without_authorizing(
    tmp_path: Path,
) -> None:
    config = GoogleWorkflowConfig(
        queue_spreadsheet_id="queue-sheet",
        master_spreadsheet_id="master-sheet",
        category_tabs=("Desserts",),
        oauth_client_path=tmp_path / "client.json",
        oauth_token_path=tmp_path / "token.json",
        working_root=tmp_path / "working",
    )
    calls: list[tuple[str, str, object]] = []

    def build_service(name: str, version: str, credentials: object) -> str:
        calls.append((name, version, credentials))
        return f"{name}-service"

    services = GoogleOAuthServiceFactory(
        config,
        credential_provider=lambda _config: "credentials",
        service_builder=build_service,
    ).create_services()

    assert services.sheets == "sheets-service"
    assert services.docs == "docs-service"
    assert services.drive == "drive-service"
    assert calls == [
        ("sheets", "v4", "credentials"),
        ("docs", "v1", "credentials"),
        ("drive", "v3", "credentials"),
    ]


def test_local_oauth_prints_url_instead_of_requiring_wsl_browser() -> None:
    class Flow:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def run_local_server(self, **kwargs: object) -> str:
            self.kwargs = kwargs
            return "credentials"

    flow = Flow()

    credentials = _run_local_authorization(flow)

    assert credentials == "credentials"
    assert flow.kwargs["port"] == 0
    assert flow.kwargs["open_browser"] is False
    assert "Windows browser" in str(flow.kwargs["authorization_prompt_message"])
