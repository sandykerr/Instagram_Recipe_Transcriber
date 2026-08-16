from __future__ import annotations

from pydantic import HttpUrl

from instagram_recipe_transcriber.models import (
    EvidenceReference,
    Ingredient,
    Instruction,
    QueuedRecipe,
    RecipeCandidate,
    RecipeDocument,
    RecipeDocumentPresentation,
    RecipeInstructionFormat,
    ReviewArtifact,
    ReviewCategory,
    ReviewDecision,
    ReviewDecisionStatus,
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
        resolution_store=_Store(),  # type: ignore[arg-type]
        review_row_remover=_Remover(),
        document_writer=writer,
        master_writer=_Appender(),
        publication_store=_Store(),  # type: ignore[arg-type]
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
