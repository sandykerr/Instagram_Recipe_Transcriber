from __future__ import annotations

from pathlib import Path

from pydantic import HttpUrl

from instagram_recipe_transcriber.models import (
    EvidenceSegment,
    OcrArtifact,
    RecipeOutcome,
    SourceArtifact,
    SourceKind,
    TranscriptArtifact,
)
from instagram_recipe_transcriber.recipe_processing import RecipeValidator, RuleBasedRecipeExtractor


def _source() -> SourceArtifact:
    return SourceArtifact(
        recipe_id="test-recipe",
        source_url=HttpUrl("https://www.instagram.com/reel/DZdXIrXOklf/"),
        media_path=Path("input.mp4"),
        media_sha256="a" * 64,
    )


def _transcript() -> TranscriptArtifact:
    return TranscriptArtifact(
        segments=(
            EvidenceSegment(
                evidence_id="transcript-1",
                source_kind=SourceKind.TRANSCRIPT,
                text="Add the pasta to the sauce.",
            ),
            EvidenceSegment(
                evidence_id="transcript-2",
                source_kind=SourceKind.TRANSCRIPT,
                text="Serve immediately.",
            ),
        ),
        language="en",
        language_probability=0.95,
        model_name="fake",
        compute_type="fake",
    )


def test_extractor_preserves_explicit_evidence_and_validator_marks_ready() -> None:
    ocr = OcrArtifact(
        status="completed",
        segments=(
            EvidenceSegment(
                evidence_id="ocr-title",
                source_kind=SourceKind.OCR,
                text="HONEY CHIPOTLE ALFREDO",
                confidence=0.99,
            ),
            EvidenceSegment(
                evidence_id="ocr-ingredient",
                source_kind=SourceKind.OCR,
                text="1 cup pasta",
                confidence=0.95,
            ),
        ),
    )

    recipe = RuleBasedRecipeExtractor().extract(_source(), _transcript(), ocr)
    validation = RecipeValidator().validate(recipe)

    assert recipe.title == "HONEY CHIPOTLE ALFREDO"
    assert len(recipe.ingredients) == 1
    assert recipe.ingredients[0].evidence[0].evidence_id == "ocr-ingredient"
    assert len(recipe.instructions) == 2
    assert validation.outcome is RecipeOutcome.READY


def test_validator_routes_unsupported_or_incomplete_recipe_to_review() -> None:
    recipe = RuleBasedRecipeExtractor().extract(_source(), _transcript(), None)

    validation = RecipeValidator().validate(recipe)

    assert validation.outcome is RecipeOutcome.REVIEW
    assert {finding.code for finding in validation.findings} >= {
        "missing_title",
        "missing_ingredients",
    }


def test_extractor_splits_compact_units_and_rejects_serving_summary() -> None:
    ocr = OcrArtifact(
        status="completed",
        segments=(
            EvidenceSegment(
                evidence_id="ocr-1",
                source_kind=SourceKind.OCR,
                text="100g cream cheese",
                confidence=0.99,
            ),
            EvidenceSegment(
                evidence_id="ocr-2",
                source_kind=SourceKind.OCR,
                text="20ml fat free milk",
                confidence=0.99,
            ),
            EvidenceSegment(
                evidence_id="ocr-3",
                source_kind=SourceKind.OCR,
                text="8 delicious servings",
                confidence=0.99,
            ),
            EvidenceSegment(
                evidence_id="ocr-4",
                source_kind=SourceKind.OCR,
                text="12oz. Cooked",
                confidence=0.99,
            ),
        ),
    )

    recipe = RuleBasedRecipeExtractor().extract(_source(), _transcript(), ocr)

    parsed_ingredients = [
        (item.quantity_original, item.unit_original, item.name) for item in recipe.ingredients
    ]
    assert parsed_ingredients == [
        ("100", "g", "cream cheese"),
        ("20", "ml", "fat free milk"),
    ]


def test_validator_requires_more_than_one_instruction_for_ready() -> None:
    recipe = RuleBasedRecipeExtractor().extract(
        _source(),
        TranscriptArtifact(
            segments=(
                EvidenceSegment(
                    evidence_id="transcript-1",
                    source_kind=SourceKind.TRANSCRIPT,
                    text="Add pasta.",
                ),
            ),
            model_name="fake",
            compute_type="fake",
        ),
        OcrArtifact(
            status="completed",
            segments=(
                EvidenceSegment(
                    evidence_id="ocr-title",
                    source_kind=SourceKind.OCR,
                    text="HONEY CHIPOTLE ALFREDO",
                    confidence=0.99,
                ),
                EvidenceSegment(
                    evidence_id="ocr-ingredient",
                    source_kind=SourceKind.OCR,
                    text="1 cup pasta",
                    confidence=0.99,
                ),
            ),
        ),
    )

    validation = RecipeValidator().validate(recipe)

    assert validation.outcome is RecipeOutcome.REVIEW
    assert any(finding.code == "insufficient_instructions" for finding in validation.findings)
