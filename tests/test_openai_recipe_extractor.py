from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Never

from pydantic import HttpUrl

from instagram_recipe_transcriber.adapters import LocalFileSourceLoader
from instagram_recipe_transcriber.artifacts import JsonArtifactStore
from instagram_recipe_transcriber.models import (
    AudioArtifact,
    EvidenceSegment,
    FrameExtractionArtifact,
    OcrArtifact,
    RecipeCandidate,
    RecipeJob,
    RecipeOutcome,
    SourceArtifact,
    SourceKind,
    TranscriptArtifact,
)
from instagram_recipe_transcriber.openai_recipe_extractor import (
    _PROMPT_VERSION,
    _SYSTEM_PROMPT,
    OpenAiRecipeExtractor,
    _ProposedCompletenessFinding,
    _ProposedIngredient,
    _ProposedInstruction,
    _ProposedRecipe,
)
from instagram_recipe_transcriber.pipeline import PipelineRunner
from instagram_recipe_transcriber.recipe_processing import RecipeValidator


class FakeResponse:
    def __init__(self, recipe: _ProposedRecipe, usage: object) -> None:
        self.output_parsed = recipe
        self.model = "gpt-5.4-mini-2026-03-17"
        self.usage = usage


class FakeResponsesApi:
    def __init__(self, recipe: _ProposedRecipe, usage: object) -> None:
        self._recipe = recipe
        self._usage = usage
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        return FakeResponse(self._recipe, self._usage)


class FakeClient:
    def __init__(self, recipe: _ProposedRecipe, usage: object | None = None) -> None:
        self.responses = FakeResponsesApi(recipe, usage or _usage())


def _usage() -> object:
    return SimpleNamespace(
        input_tokens=123,
        input_tokens_details=SimpleNamespace(cached_tokens=11, cache_write_tokens=7),
        output_tokens=45,
        output_tokens_details=SimpleNamespace(reasoning_tokens=19),
        total_tokens=168,
    )


def _source() -> SourceArtifact:
    return SourceArtifact(
        recipe_id="test-recipe",
        source_url=HttpUrl("https://www.instagram.com/reel/DVJBGzyk8E5/"),
        media_path=Path("input.mp4"),
        media_sha256="a" * 64,
        caption=EvidenceSegment(
            evidence_id="caption-1",
            source_kind=SourceKind.CAPTION,
            text=(
                "Easy High Protein Honey BBQ Chicken Mac & Cheese\n"
                "Ingredients:\n• 700g Diced Chicken Breast\n"
                "Instructions:\n1. Season diced chicken with salt.\n"
                "2. Cook chicken until fully cooked."
            ),
        ),
    )


def _transcript() -> TranscriptArtifact:
    return TranscriptArtifact(model_name="fake", compute_type="fake")


def test_prompt_ignores_emojis_and_non_recipe_metadata() -> None:
    assert _PROMPT_VERSION == "4"
    assert "Ignore emojis completely" in _SYSTEM_PROMPT
    assert "Their presence or omission is not a conflict" in _SYSTEM_PROMPT
    assert "Low-confidence, malformed" in _SYSTEM_PROMPT


def test_openai_extractor_uses_structured_output_and_preserves_evidence() -> None:
    proposed = _ProposedRecipe(
        title="Easy High Protein Honey BBQ Chicken Mac & Cheese",
        title_evidence_ids=["caption-1"],
        ingredients=[
            _ProposedIngredient(
                original_text="700g Diced Chicken Breast",
                name="Diced Chicken Breast",
                quantity_original="700",
                unit_original="g",
                evidence_ids=["caption-1"],
            )
        ],
        instructions=[
            _ProposedInstruction(
                original_text="Season diced chicken with salt.", evidence_ids=["caption-1"]
            ),
            _ProposedInstruction(
                original_text="Cook chicken until fully cooked.", evidence_ids=["caption-1"]
            ),
        ],
        conflicts=[],
    )
    client = FakeClient(proposed)

    extraction = OpenAiRecipeExtractor(client=client).extract_artifact(
        _source(), _transcript(), None
    )
    recipe = extraction.recipe

    assert client.responses.calls[0]["model"] == "gpt-5.4-mini"
    assert client.responses.calls[0]["text_format"] is _ProposedRecipe
    assert recipe.title == "Easy High Protein Honey BBQ Chicken Mac & Cheese"
    assert recipe.ingredients[0].evidence[0].evidence_id == "caption-1"
    assert recipe.instructions[1].sequence == 2
    assert RecipeValidator().validate(recipe).outcome is RecipeOutcome.READY
    assert extraction.usage is not None
    assert extraction.usage.model == "gpt-5.4-mini-2026-03-17"
    assert extraction.usage.request_count == 1
    assert extraction.usage.input_tokens == 123
    assert extraction.usage.cached_input_tokens == 11
    assert extraction.usage.cache_write_tokens == 7
    assert extraction.usage.output_tokens == 45
    assert extraction.usage.reasoning_tokens == 19
    assert extraction.usage.total_tokens == 168


def test_openai_extractor_rejects_hallucinated_or_uncited_claims() -> None:
    proposed = _ProposedRecipe(
        title="Invented meal",
        title_evidence_ids=["unknown"],
        ingredients=[
            _ProposedIngredient(
                original_text="2 cups moon dust",
                name="moon dust",
                quantity_original="2",
                unit_original="cups",
                evidence_ids=["caption-1"],
            )
        ],
        instructions=[
            _ProposedInstruction(
                original_text="Teleport dinner to the table.", evidence_ids=["unknown"]
            )
        ],
        conflicts=[],
    )

    extractor = OpenAiRecipeExtractor(client=FakeClient(proposed))
    recipe = extractor.extract(_source(), _transcript(), None)

    assert recipe.title is None
    assert recipe.ingredients == ()
    assert recipe.instructions == ()
    assert len(recipe.conflicts) == 3
    assert RecipeValidator().validate(recipe).outcome is RecipeOutcome.REVIEW


def test_openai_title_accepts_creator_caption_with_punctuation_and_emoji() -> None:
    source = _source().model_copy(
        update={
            "caption": EvidenceSegment(
                evidence_id="caption-1",
                source_kind=SourceKind.CAPTION,
                text="Juicy Chicken-Burger! 🍔\nIngredients: 1 bun",
            )
        }
    )
    proposal = _ProposedRecipe(
        title="Juicy Chicken Burger",
        title_evidence_ids=["caption-1"],
        ingredients=[],
        instructions=[],
        conflicts=[],
    )

    recipe = OpenAiRecipeExtractor(client=FakeClient(proposal)).extract(source, _transcript(), None)

    assert recipe.title == "Juicy Chicken Burger"


def test_openai_ignores_makes_15_vs_weak_ocr_maes_10_conflict() -> None:
    source = _source().model_copy(
        update={
            "caption": EvidenceSegment(
                evidence_id="caption-1", source_kind=SourceKind.CAPTION, text="Makes 15"
            )
        }
    )
    transcript = TranscriptArtifact(
        segments=(
            EvidenceSegment(
                evidence_id="transcript-1", source_kind=SourceKind.TRANSCRIPT, text="Makes 15"
            ),
        ),
        model_name="fake",
        compute_type="fake",
    )
    ocr = OcrArtifact(
        status="completed",
        segments=(
            EvidenceSegment(
                evidence_id="ocr-1",
                source_kind=SourceKind.OCR,
                text="MAES 10",
                confidence=0.3,
            ),
        ),
    )
    proposal = _ProposedRecipe(
        title=None,
        title_evidence_ids=[],
        ingredients=[],
        instructions=[],
        conflicts=["Makes 15 conflicts with OCR MAES 10"],
    )

    recipe = OpenAiRecipeExtractor(client=FakeClient(proposal)).extract(source, transcript, ocr)

    assert recipe.conflicts == ()
    assert ocr.segments[0].text == "MAES 10"


def test_openai_completeness_assessment_routes_imprecise_burger_to_review() -> None:
    source = _source().model_copy(
        update={
            "caption": EvidenceSegment(
                evidence_id="caption-1",
                source_kind=SourceKind.CAPTION,
                text="Juicy ground chicken burgers",
            )
        }
    )
    transcript = TranscriptArtifact(
        segments=(
            EvidenceSegment(
                evidence_id="transcript-1",
                source_kind=SourceKind.TRANSCRIPT,
                text="Grate a Granny Smith apple and cook the apple and onion.",
            ),
            EvidenceSegment(
                evidence_id="transcript-2",
                source_kind=SourceKind.TRANSCRIPT,
                text="Form each patty and chill it before you cook.",
            ),
        ),
        model_name="fake",
        compute_type="fake",
    )
    proposal = _ProposedRecipe(
        title="Juicy ground chicken burgers",
        title_evidence_ids=["caption-1"],
        ingredients=[],
        instructions=[],
        conflicts=[],
        completeness_findings=[
            _ProposedCompletenessFinding(
                code="unquantified_core_ingredient",
                message="Ground chicken has no explicit quantity.",
                evidence_ids=["caption-1", "transcript-1"],
            ),
            _ProposedCompletenessFinding(
                code="missing_critical_step",
                message="No supported instruction explains how to cook the burger patties.",
                evidence_ids=["transcript-2"],
            ),
        ],
    )

    recipe = OpenAiRecipeExtractor(client=FakeClient(proposal)).extract(source, transcript, None)
    validation = RecipeValidator().validate(recipe)

    assert [finding.code for finding in recipe.completeness_findings] == [
        "unquantified_core_ingredient",
        "missing_critical_step",
    ]
    assert validation.outcome is RecipeOutcome.REVIEW


class _FailingAudioExtractor:
    version = "failing-audio-v1"

    def extract(self, source: SourceArtifact) -> Never:
        raise AssertionError("caption-ready extraction should not request audio")


class _FailingTranscriber:
    version = "failing-transcriber-v1"

    def transcribe(self, audio: AudioArtifact) -> Never:
        raise AssertionError("caption-ready extraction should not transcribe")


class _FailingOcrGate:
    version = "failing-ocr-gate-v1"

    def decide(
        self,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        recipe: RecipeCandidate,
    ) -> Never:
        raise AssertionError("caption-ready extraction should not evaluate OCR")


class _FailingFrameExtractor:
    version = "failing-frame-extractor-v1"

    def extract(self, source: SourceArtifact) -> Never:
        raise AssertionError("caption-ready extraction should not extract frames")


class _FailingOcrExtractor:
    version = "failing-ocr-extractor-v1"

    def extract(self, frames: FrameExtractionArtifact) -> Never:
        raise AssertionError("caption-ready extraction should not run OCR")


def test_cached_caption_extraction_preserves_usage_without_a_second_request(tmp_path: Path) -> None:
    proposed = _ready_proposal()
    client = FakeClient(proposed)
    media_path = tmp_path / "input.mp4"
    media_path.write_bytes(b"unused caption-only media")
    runner = PipelineRunner(
        artifact_store=JsonArtifactStore(tmp_path / "artifacts"),
        source_loader=LocalFileSourceLoader(),
        audio_extractor=_FailingAudioExtractor(),
        transcriber=_FailingTranscriber(),
        ocr_gate=_FailingOcrGate(),
        frame_extractor=_FailingFrameExtractor(),
        ocr_extractor=_FailingOcrExtractor(),
        recipe_extractor=OpenAiRecipeExtractor(client=client),
        recipe_validator=RecipeValidator(),
    )
    source = _source()
    assert source.caption is not None
    job = RecipeJob(
        recipe_id="cached-openai-caption",
        source_url=HttpUrl("https://www.instagram.com/reel/DVJBGzyk8E5/"),
        media_path=media_path,
        caption_text=source.caption.text,
    )

    first = runner.run(job)
    second = runner.run(job)

    assert first.validation.outcome is RecipeOutcome.READY
    assert second == first
    assert first.recipe_usage is not None
    assert second.recipe_usage == first.recipe_usage
    assert len(client.responses.calls) == 1


def _ready_proposal() -> _ProposedRecipe:
    return _ProposedRecipe(
        title="Easy High Protein Honey BBQ Chicken Mac & Cheese",
        title_evidence_ids=["caption-1"],
        ingredients=[
            _ProposedIngredient(
                original_text="700g Diced Chicken Breast",
                name="Diced Chicken Breast",
                quantity_original="700",
                unit_original="g",
                evidence_ids=["caption-1"],
            )
        ],
        instructions=[
            _ProposedInstruction(
                original_text="Season diced chicken with salt.", evidence_ids=["caption-1"]
            ),
            _ProposedInstruction(
                original_text="Cook chicken until fully cooked.", evidence_ids=["caption-1"]
            ),
        ],
        conflicts=[],
    )
