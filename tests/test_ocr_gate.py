from __future__ import annotations

from pathlib import Path

from pydantic import HttpUrl

from instagram_recipe_transcriber.adapters import DeterministicOcrGate
from instagram_recipe_transcriber.models import (
    EvidenceReference,
    EvidenceSegment,
    Ingredient,
    Instruction,
    OcrPolicy,
    RecipeCandidate,
    SourceArtifact,
    SourceKind,
    TranscriptArtifact,
)


def _source(caption_text: str | None = None) -> SourceArtifact:
    caption = (
        EvidenceSegment(evidence_id="caption-1", source_kind=SourceKind.CAPTION, text=caption_text)
        if caption_text
        else None
    )
    return SourceArtifact(
        recipe_id="test-recipe",
        source_url=HttpUrl("https://www.instagram.com/reel/DZdXIrXOklf/"),
        media_path=Path("input.mp4"),
        media_sha256="a" * 64,
        caption=caption,
    )


def _transcript(probability: float | None = 0.95) -> TranscriptArtifact:
    return TranscriptArtifact(
        segments=(
            EvidenceSegment(
                evidence_id="transcript-1",
                source_kind=SourceKind.TRANSCRIPT,
                text="Cook the pasta.",
            ),
        ),
        language="en",
        language_probability=probability,
        model_name="fake",
        compute_type="fake",
    )


def _recipe() -> RecipeCandidate:
    reference = (EvidenceReference(evidence_id="transcript-1"),)
    return RecipeCandidate(
        title="Pasta",
        ingredients=(
            Ingredient(
                original_text="1 cup pasta",
                name="pasta",
                quantity_original="1",
                unit_original="cup",
                evidence=reference,
                confidence=0.9,
            ),
        ),
        instructions=(
            Instruction(
                original_text="Cook the pasta.", sequence=1, evidence=reference, confidence=0.9
            ),
        ),
    )


def test_gate_requests_ocr_for_missing_recipe_information() -> None:
    decision = DeterministicOcrGate().decide(_source(), _transcript(), RecipeCandidate())

    assert decision.should_run is True
    assert "ingredients" in decision.reason.lower()


def test_gate_requests_ocr_for_explicit_screen_reference() -> None:
    decision = DeterministicOcrGate().decide(
        _source("See ingredients above."), _transcript(), _recipe()
    )

    assert decision.should_run is True


def test_gate_skips_ocr_only_for_complete_high_confidence_recipe() -> None:
    decision = DeterministicOcrGate().decide(_source(), _transcript(), _recipe())

    assert decision.should_run is False
    assert decision.policy is OcrPolicy.WHEN_NEEDED
