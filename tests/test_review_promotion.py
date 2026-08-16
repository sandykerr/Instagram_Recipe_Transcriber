from __future__ import annotations

from pydantic import HttpUrl

from instagram_recipe_transcriber.models import (
    EvidenceReference,
    Ingredient,
    Instruction,
    PublicationArtifact,
    QueuedRecipe,
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
from instagram_recipe_transcriber.review_promotion import ReviewPromotionWorkflow


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
    workflow = ReviewPromotionWorkflow(
        decision_reader=_DecisionReader(_decision(ReviewDecisionStatus.ACCEPTED)),
        review_store=review_store,  # type: ignore[arg-type]
        resolution_store=resolution_store,  # type: ignore[arg-type]
        review_row_remover=remover,
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


def test_rejected_review_moves_doc_archives_row_and_removes_active_review_row() -> None:
    review_store = _Store(_review())
    resolution_store = _Store()
    rejected, remover = _Appender(), _Remover()
    rejected_organizer = _Organizer()
    workflow = ReviewPromotionWorkflow(
        decision_reader=_DecisionReader(_decision(ReviewDecisionStatus.REJECTED)),
        review_store=review_store,  # type: ignore[arg-type]
        resolution_store=resolution_store,  # type: ignore[arg-type]
        review_row_remover=remover,
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
        review_store=_RecipeStore(
            {first.recipe_id: first_review, second.recipe_id: second_review}
        ),
        resolution_store=_ArtifactStore(),  # type: ignore[arg-type]
        review_row_remover=remover,
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
