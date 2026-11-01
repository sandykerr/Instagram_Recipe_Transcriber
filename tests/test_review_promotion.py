from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import HttpUrl

from instagram_recipe_transcriber.errors import PipelineOperationalError
from instagram_recipe_transcriber.google_adapters import GoogleSheetsReviewSyncApplier
from instagram_recipe_transcriber.models import (
    EvidenceReference,
    Ingredient,
    Instruction,
    PublicationArtifact,
    QueuedRecipe,
    QueueStatus,
    RecipeCandidate,
    RecipeDocument,
    RecipeDocumentPresentation,
    RecipeInstructionFormat,
    ReviewArtifact,
    ReviewCategory,
    ReviewDecision,
    ReviewDecisionStatus,
    ReviewResolutionArtifact,
)
from instagram_recipe_transcriber.publication import JsonReviewResolutionStore
from instagram_recipe_transcriber.review_promotion import ReviewPromotionWorkflow
from instagram_recipe_transcriber.review_sync import (
    ActiveReviewRow,
    ReviewQueueRow,
    ReviewSyncPlan,
    ReviewSyncPlanner,
)


def _document(identifier: str, title: str) -> RecipeDocument:
    return RecipeDocument(
        document_id=identifier,
        title=title,
        url=HttpUrl(f"https://docs.google.com/document/d/{identifier}/edit"),
    )


class _DecisionReader:
    def __init__(self, decision: ReviewDecision) -> None:
        self.decision = decision

    def read_next(self) -> ReviewDecision:
        return self.decision


class _ListDecisionReader:
    def __init__(self, decisions: list[ReviewDecision]) -> None:
        self._decisions = decisions

    def read_next(self) -> ReviewDecision | None:
        return self._decisions.pop(0) if self._decisions else None


class _Store:
    def __init__(self, value: object | None = None) -> None:
        self.value = value

    def load(self, recipe_id: str) -> object | None:
        return self.value

    def save(self, value: object) -> None:
        self.value = value


class _Writer:
    def __init__(self) -> None:
        self.calls = 0
        self.presentations: list[RecipeDocumentPresentation | None] = []

    def create(
        self,
        recipe: RecipeCandidate,
        queued: QueuedRecipe,
        presentation: RecipeDocumentPresentation | None = None,
    ) -> RecipeDocument:
        self.calls += 1
        self.presentations.append(presentation)
        return _document("final", recipe.title or "Untitled")


class _Organizer:
    def __init__(self) -> None:
        self.calls = 0

    def move_to_folder(self, document: RecipeDocument) -> RecipeDocument:
        self.calls += 1
        return document


class _Appender:
    def __init__(self) -> None:
        self.calls = 0

    def append(self, *args: object) -> None:
        self.calls += 1


class _Remover:
    def __init__(self) -> None:
        self.calls = 0

    def remove(self, decision: ReviewDecision) -> None:
        self.calls += 1


class _QueueStateWriter:
    def __init__(self) -> None:
        self.updates: list[tuple[ReviewDecision, QueueStatus, str]] = []

    def resolve(self, decision: ReviewDecision, status: QueueStatus, detail: str) -> None:
        self.updates.append((decision, status, detail))


def _decision(status: ReviewDecisionStatus) -> ReviewDecision:
    return ReviewDecision(
        recipe_id="review-recipe",
        source_url=HttpUrl("https://www.instagram.com/reel/DVJBGzyk8E5/"),
        category="Main Courses",
        review_row_number=2,
        description="Test recipe",
        decision=status,
        review_document_url=HttpUrl("https://docs.google.com/document/d/review/edit"),
    )


def _review() -> ReviewArtifact:
    ref = EvidenceReference(evidence_id="caption-1")
    recipe = RecipeCandidate(
        title="Approved recipe",
        ingredients=(
            Ingredient(
                original_text="1 cup pasta",
                name="pasta",
                evidence=(ref,),
                confidence=0.9,
            ),
        ),
        instructions=(
            Instruction(
                original_text="Cook pasta.",
                sequence=1,
                evidence=(ref,),
                confidence=0.9,
            ),
        ),
    )
    return ReviewArtifact(
        recipe_id="review-recipe",
        document=_document("review", "REVIEW"),
        recipe=recipe,
    )


class _RecipeStore:
    def __init__(self, reviews: dict[str, ReviewArtifact]) -> None:
        self.reviews = reviews

    def load(self, recipe_id: str) -> ReviewArtifact | None:
        return self.reviews.get(recipe_id)

    def save(self, review: ReviewArtifact) -> None:
        self.reviews[review.recipe_id] = review


class _ArtifactStore:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def load(self, recipe_id: str) -> object | None:
        return self.values.get(recipe_id)

    def save(self, value: PublicationArtifact | ReviewResolutionArtifact) -> None:
        self.values[value.recipe_id] = value


def test_accepted_review_creates_one_final_document_master_row_and_removes_review_row() -> None:
    review_store = _Store(_review())
    resolution_store = _Store()
    writer, master, rejected, remover = _Writer(), _Appender(), _Appender(), _Remover()
    queue = _QueueStateWriter()
    workflow = ReviewPromotionWorkflow(
        decision_reader=_DecisionReader(_decision(ReviewDecisionStatus.ACCEPTED)),
        review_store=review_store,  # type: ignore[arg-type]
        resolution_store=resolution_store,  # type: ignore[arg-type]
        review_row_remover=remover,
        queue_state_writer=queue,
        document_writer=writer,
        master_writer=master,
        publication_store=_Store(),  # type: ignore[arg-type]
        accepted_document_organizer=_Organizer(),
        rejected_document_organizer=_Organizer(),
        rejected_writer=rejected,
    )

    first = workflow.process_next()
    second = workflow.process_next()

    assert first is not None and first.review_row_removed and first.master_row_written
    assert second == first
    assert writer.calls == master.calls == remover.calls == 1
    assert rejected.calls == 0
    assert first.queue_row_updated
    assert queue.updates == [
        (
            _decision(ReviewDecisionStatus.ACCEPTED),
            QueueStatus.PUBLISHED,
            "https://docs.google.com/document/d/final/edit",
        )
    ]


def test_rejected_review_moves_doc_archives_row_and_removes_active_review_row() -> None:
    review_store = _Store(_review())
    resolution_store = _Store()
    rejected, remover = _Appender(), _Remover()
    rejected_organizer = _Organizer()
    queue = _QueueStateWriter()
    workflow = ReviewPromotionWorkflow(
        decision_reader=_DecisionReader(_decision(ReviewDecisionStatus.REJECTED)),
        review_store=review_store,  # type: ignore[arg-type]
        resolution_store=resolution_store,  # type: ignore[arg-type]
        review_row_remover=remover,
        queue_state_writer=queue,
        document_writer=_Writer(),
        master_writer=_Appender(),
        publication_store=_Store(),  # type: ignore[arg-type]
        accepted_document_organizer=_Organizer(),
        rejected_document_organizer=rejected_organizer,
        rejected_writer=rejected,
    )

    result = workflow.process_next()

    assert result is not None and result.rejected_row_written and result.review_row_removed
    assert rejected_organizer.calls == rejected.calls == remover.calls == 1
    assert result.queue_row_updated
    assert workflow.process_next() == result
    assert queue.updates == [
        (
            _decision(ReviewDecisionStatus.REJECTED),
            QueueStatus.REJECTED,
            "https://docs.google.com/document/d/review/edit",
        )
    ]


def test_accepted_missing_critical_step_uses_raw_transcript_not_candidate_steps() -> None:
    review = _review().model_copy(
        update={
            "review_categories": (ReviewCategory.MISSING_CRITICAL_STEP,),
            "transcript_text": "Mix the ingredients, then cook until done.",
        }
    )
    writer = _Writer()
    workflow = ReviewPromotionWorkflow(
        decision_reader=_DecisionReader(_decision(ReviewDecisionStatus.ACCEPTED)),
        review_store=_Store(review),  # type: ignore[arg-type]
        resolution_store=_ArtifactStore(),  # type: ignore[arg-type]
        review_row_remover=_Remover(),
        queue_state_writer=_QueueStateWriter(),
        document_writer=writer,
        master_writer=_Appender(),
        publication_store=_ArtifactStore(),  # type: ignore[arg-type]
        accepted_document_organizer=_Organizer(),
        rejected_document_organizer=_Organizer(),
        rejected_writer=_Appender(),
    )

    workflow.process_next()

    assert writer.presentations == [
        RecipeDocumentPresentation(
            instruction_format=RecipeInstructionFormat.RAW_TRANSCRIPT,
            raw_instruction_text="Mix the ingredients, then cook until done.",
        )
    ]


def test_accepted_review_carries_manual_servings_and_nutrition_notes() -> None:
    writer = _Writer()
    decision = _decision(ReviewDecisionStatus.ACCEPTED).model_copy(
        update={
            "servings_text": "4",
            "nutrition_notes": "Per serving — 338 kcal | Protein 22 g",
        }
    )
    workflow = ReviewPromotionWorkflow(
        decision_reader=_DecisionReader(decision),
        review_store=_Store(_review()),  # type: ignore[arg-type]
        resolution_store=_ArtifactStore(),  # type: ignore[arg-type]
        review_row_remover=_Remover(),
        queue_state_writer=_QueueStateWriter(),
        document_writer=writer,
        master_writer=_Appender(),
        publication_store=_ArtifactStore(),  # type: ignore[arg-type]
        accepted_document_organizer=_Organizer(),
        rejected_document_organizer=_Organizer(),
        rejected_writer=_Appender(),
    )

    workflow.process_next()

    assert writer.presentations == [
        RecipeDocumentPresentation(
            servings_text="4",
            nutrition_notes="Per serving — 338 kcal | Protein 22 g",
        )
    ]


def test_process_all_promotes_a_bounded_number_of_review_decisions() -> None:
    first = _decision(ReviewDecisionStatus.ACCEPTED)
    second = first.model_copy(update={"recipe_id": "second-review", "review_row_number": 3})
    first_review = _review()
    second_review = first_review.model_copy(update={"recipe_id": second.recipe_id})
    writer, master, rejected, remover = _Writer(), _Appender(), _Appender(), _Remover()
    workflow = ReviewPromotionWorkflow(
        decision_reader=_ListDecisionReader([first, second]),
        review_store=_RecipeStore({first.recipe_id: first_review, second.recipe_id: second_review}),
        resolution_store=_ArtifactStore(),  # type: ignore[arg-type]
        review_row_remover=remover,
        queue_state_writer=_QueueStateWriter(),
        document_writer=writer,
        master_writer=master,
        publication_store=_ArtifactStore(),  # type: ignore[arg-type]
        accepted_document_organizer=_Organizer(),
        rejected_document_organizer=_Organizer(),
        rejected_writer=rejected,
    )

    result = workflow.process_all(max_items=2)

    assert result.processed_count == 2
    assert [resolution.recipe_id for resolution in result.resolutions] == [
        first.recipe_id,
        second.recipe_id,
    ]
    assert writer.calls == master.calls == remover.calls == 2
    assert rejected.calls == 0


@pytest.mark.parametrize(
    ("decision_status", "queue_status", "document_url"),
    [
        (
            ReviewDecisionStatus.ACCEPTED,
            QueueStatus.PUBLISHED,
            "https://docs.google.com/document/d/final/edit",
        ),
        (
            ReviewDecisionStatus.REJECTED,
            QueueStatus.REJECTED,
            "https://docs.google.com/document/d/review/edit",
        ),
    ],
)
def test_queue_failure_retries_only_missing_update_from_durable_resolution(
    tmp_path: Path,
    decision_status: ReviewDecisionStatus,
    queue_status: QueueStatus,
    document_url: str,
) -> None:
    decision = _decision(decision_status)
    resolution_root = tmp_path / "resolutions"
    resolution_store = JsonReviewResolutionStore(resolution_root)
    writer, master, rejected, remover = _Writer(), _Appender(), _Appender(), _Remover()
    accepted_organizer, rejected_organizer = _Organizer(), _Organizer()

    class FailingOnceQueueWriter(_QueueStateWriter):
        def resolve(self, decision: ReviewDecision, status: QueueStatus, detail: str) -> None:
            checkpoint = JsonReviewResolutionStore(resolution_root).load(decision.recipe_id)
            assert checkpoint is not None
            assert checkpoint.review_row_removed
            assert checkpoint.master_row_written or checkpoint.rejected_row_written
            assert not checkpoint.queue_row_updated
            assert remover.calls == 1
            super().resolve(decision, status, detail)
            if len(self.updates) == 1:
                raise RuntimeError("Input queue unavailable")

    queue = FailingOnceQueueWriter()

    def build_workflow() -> ReviewPromotionWorkflow:
        return ReviewPromotionWorkflow(
            decision_reader=_DecisionReader(decision),
            review_store=_Store(_review()),  # type: ignore[arg-type]
            resolution_store=JsonReviewResolutionStore(resolution_root),
            review_row_remover=remover,
            queue_state_writer=queue,
            document_writer=writer,
            master_writer=master,
            publication_store=_ArtifactStore(),  # type: ignore[arg-type]
            accepted_document_organizer=accepted_organizer,
            rejected_document_organizer=rejected_organizer,
            rejected_writer=rejected,
        )

    with pytest.raises(RuntimeError, match="Input queue unavailable"):
        build_workflow().process_next()

    checkpoint = resolution_store.load(decision.recipe_id)
    assert checkpoint is not None and not checkpoint.queue_row_updated
    counts = (
        writer.calls,
        master.calls,
        rejected.calls,
        remover.calls,
        accepted_organizer.calls,
        rejected_organizer.calls,
    )
    result = build_workflow().process_next()

    assert result is not None and result.queue_row_updated
    assert resolution_store.load(decision.recipe_id) == result
    assert queue.updates == [(decision, queue_status, document_url)] * 2
    assert (
        writer.calls,
        master.calls,
        rejected.calls,
        remover.calls,
        accepted_organizer.calls,
        rejected_organizer.calls,
    ) == counts
    assert build_workflow().process_next() == result
    assert len(queue.updates) == 2


def test_failed_review_row_removal_does_not_update_queue(tmp_path: Path) -> None:
    class FailingRemover(_Remover):
        def remove(self, decision: ReviewDecision) -> None:
            raise RuntimeError("Review row removal failed")

    queue = _QueueStateWriter()
    resolution_store = JsonReviewResolutionStore(tmp_path)
    workflow = ReviewPromotionWorkflow(
        decision_reader=_DecisionReader(_decision(ReviewDecisionStatus.ACCEPTED)),
        review_store=_Store(_review()),  # type: ignore[arg-type]
        resolution_store=resolution_store,
        review_row_remover=FailingRemover(),
        queue_state_writer=queue,
        document_writer=_Writer(),
        master_writer=_Appender(),
        publication_store=_ArtifactStore(),  # type: ignore[arg-type]
        accepted_document_organizer=_Organizer(),
        rejected_document_organizer=_Organizer(),
        rejected_writer=_Appender(),
    )

    with pytest.raises(RuntimeError, match="Review row removal failed"):
        workflow.process_next()

    checkpoint = resolution_store.load("review-recipe")
    assert checkpoint is not None and checkpoint.master_row_written
    assert not checkpoint.review_row_removed
    assert not checkpoint.queue_row_updated
    assert queue.updates == []


@pytest.mark.parametrize("decision_status", list(ReviewDecisionStatus))
def test_destination_failure_blocks_sync_until_promotion_retry_completes(
    tmp_path: Path, decision_status: ReviewDecisionStatus
) -> None:
    decision = _decision(decision_status)
    resolution_root = tmp_path / "resolutions"

    class LiveReviewQueue:
        def __init__(self) -> None:
            self.queue = ReviewQueueRow(
                recipe_id=decision.recipe_id,
                source_url=decision.source_url,
                category=decision.category,
                queue_row_number=2,
                status=QueueStatus.REVIEW,
            )
            self.active = (
                ActiveReviewRow(
                    recipe_id=decision.recipe_id,
                    source_url=decision.source_url,
                    category=decision.category,
                    review_row_number=decision.review_row_number,
                    review_document_url=decision.review_document_url,
                ),
            )

        def read_next(self) -> ReviewDecision | None:
            return decision if self.active else None

        def remove(self, resolved: ReviewDecision) -> None:
            assert resolved == decision
            self.active = ()

        def resolve(self, resolved: ReviewDecision, status: QueueStatus, detail: str) -> None:
            assert resolved == decision
            self.queue = self.queue.model_copy(update={"status": status, "detail": detail})

    class FailingOnceDestination:
        def __init__(self) -> None:
            self.attempts = 0
            self.documents: list[RecipeDocument] = []

        def append(self, source: QueuedRecipe | ReviewDecision, document: RecipeDocument) -> None:
            assert source.recipe_id == decision.recipe_id
            self.attempts += 1
            if self.attempts == 1:
                raise PipelineOperationalError("Destination append failed")
            self.documents.append(document)

    live = LiveReviewQueue()
    destination = FailingOnceDestination()
    writer = _Writer()
    publication_store = _ArtifactStore()

    def build_workflow() -> ReviewPromotionWorkflow:
        return ReviewPromotionWorkflow(
            decision_reader=live,
            review_store=_RecipeStore({decision.recipe_id: _review()}),
            resolution_store=JsonReviewResolutionStore(resolution_root),
            review_row_remover=live,
            queue_state_writer=live,
            document_writer=writer,
            master_writer=destination
            if decision_status is ReviewDecisionStatus.ACCEPTED
            else _Appender(),
            publication_store=publication_store,  # type: ignore[arg-type]
            accepted_document_organizer=_Organizer(),
            rejected_document_organizer=_Organizer(),
            rejected_writer=destination
            if decision_status is ReviewDecisionStatus.REJECTED
            else _Appender(),
        )

    with pytest.raises(PipelineOperationalError, match="Destination append failed"):
        build_workflow().process_next()

    checkpoint = JsonReviewResolutionStore(resolution_root).load(decision.recipe_id)
    assert checkpoint is not None
    assert not checkpoint.master_row_written and not checkpoint.rejected_row_written
    assert not checkpoint.review_row_removed and not checkpoint.queue_row_updated
    before = (live.queue, live.active)
    blocked = ReviewSyncPlanner(reviews={}, resolutions={decision.recipe_id: checkpoint}).plan(
        (live.queue,), live.active
    )
    assert [conflict.reason.value for conflict in blocked.conflicts] == ["incomplete_resolution"]
    assert blocked.appends == blocked.removals == blocked.queue_updates == ()
    with pytest.raises(PipelineOperationalError, match="conflicts"):
        GoogleSheetsReviewSyncApplier(
            object(), queue_spreadsheet_id="input", review_spreadsheet_id="review"
        ).apply(blocked)
    assert (live.queue, live.active) == before
    assert live.read_next() == decision
    assert destination.documents == []

    result = build_workflow().process_next()

    assert result is not None and result.review_row_removed and result.queue_row_updated
    assert result.master_row_written is (decision_status is ReviewDecisionStatus.ACCEPTED)
    assert result.rejected_row_written is (decision_status is ReviewDecisionStatus.REJECTED)
    assert destination.attempts == 2
    assert destination.documents == [result.document]
    assert writer.calls == (1 if decision_status is ReviewDecisionStatus.ACCEPTED else 0)
    assert live.read_next() is None
    assert live.queue.status is (
        QueueStatus.PUBLISHED
        if decision_status is ReviewDecisionStatus.ACCEPTED
        else QueueStatus.REJECTED
    )
    assert live.queue.detail == str(result.document.url)
    checkpoint = JsonReviewResolutionStore(resolution_root).load(decision.recipe_id)
    assert checkpoint == result
    assert (
        ReviewSyncPlanner(reviews={}, resolutions={decision.recipe_id: checkpoint}).plan(
            (live.queue,), live.active
        )
        == ReviewSyncPlan()
    )
    assert build_workflow().process_next() is None
