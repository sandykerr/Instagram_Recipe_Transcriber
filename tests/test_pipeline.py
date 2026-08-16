from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import HttpUrl

from instagram_recipe_transcriber.artifacts import JsonArtifactStore
from instagram_recipe_transcriber.models import (
    AudioArtifact,
    EvidenceReference,
    EvidenceSegment,
    FrameExtractionArtifact,
    Ingredient,
    Instruction,
    OcrArtifact,
    OcrDecisionArtifact,
    OcrPolicy,
    RecipeCandidate,
    RecipeJob,
    RecipeOutcome,
    SourceArtifact,
    SourceKind,
    TranscriptArtifact,
    ValidationArtifact,
    ValidationFinding,
    VideoProbe,
)
from instagram_recipe_transcriber.pipeline import PipelineRunner


class FakeSourceLoader:
    version = "fake-source-v1"

    def __init__(self) -> None:
        self.calls = 0

    def load(self, job: RecipeJob) -> SourceArtifact:
        self.calls += 1
        assert job.media_path is not None
        caption = (
            EvidenceSegment(
                evidence_id="caption-1",
                source_kind=SourceKind.CAPTION,
                text=job.caption_text,
            )
            if job.caption_text
            else None
        )
        return SourceArtifact(
            recipe_id=job.recipe_id,
            source_url=job.source_url,
            media_path=job.media_path,
            media_sha256=hashlib.sha256(job.media_path.read_bytes()).hexdigest(),
            caption=caption,
        )


class FakeTranscriber:
    version = "fake-transcriber-v1"

    def __init__(self) -> None:
        self.calls = 0

    def transcribe(self, audio: AudioArtifact) -> TranscriptArtifact:
        self.calls += 1
        return TranscriptArtifact(
            segments=(
                EvidenceSegment(
                    evidence_id="transcript-1",
                    source_kind=SourceKind.TRANSCRIPT,
                    text="Boil pasta.",
                    start_seconds=0,
                    end_seconds=2,
                ),
            ),
            model_name="fake",
            compute_type="fake",
        )


class FakeOcrGate:
    version = "fake-ocr-gate-v1"

    def decide(
        self, source: SourceArtifact, transcript: TranscriptArtifact, recipe: RecipeCandidate
    ) -> OcrDecisionArtifact:
        return OcrDecisionArtifact(should_run=False, policy=OcrPolicy.WHEN_NEEDED, reason="test")


class FakeOcrExtractor:
    version = "fake-ocr-v1"

    def extract(self, frames: FrameExtractionArtifact) -> OcrArtifact:
        raise AssertionError("OCR should not run")


class FakeAudioExtractor:
    version = "fake-audio-v1"

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, source: SourceArtifact) -> AudioArtifact:
        self.calls += 1
        assert source.media_path is not None
        assert source.media_sha256 is not None
        return AudioArtifact(
            audio_path=source.media_path,
            audio_sha256=source.media_sha256,
            sample_rate_hz=16_000,
            channels=1,
        )


class FakeFrameExtractor:
    version = "fake-frames-v1"

    def extract(self, source: SourceArtifact) -> FrameExtractionArtifact:
        return FrameExtractionArtifact(
            source=VideoProbe(duration_seconds=1, width=1, height=1),
            sampling_fps=1,
        )


class FakeRecipeExtractor:
    version = "fake-extractor-v1"

    def __init__(self) -> None:
        self.calls = 0

    def extract(
        self, source: SourceArtifact, transcript: TranscriptArtifact, ocr: OcrArtifact | None
    ) -> RecipeCandidate:
        self.calls += 1
        return RecipeCandidate(title="Test recipe")


class FakeValidator:
    version = "fake-validator-v1"

    def validate(self, recipe: RecipeCandidate) -> ValidationArtifact:
        return ValidationArtifact(
            outcome=RecipeOutcome.REVIEW,
            findings=(
                ValidationFinding(code="missing_ingredients", message="No ingredients extracted."),
            ),
        )


def test_pipeline_persists_and_reuses_matching_artifacts(tmp_path: Path) -> None:
    media_path = tmp_path / "input.mp4"
    media_path.write_bytes(b"not a real video")
    source_loader = FakeSourceLoader()
    transcriber = FakeTranscriber()
    recipe_extractor = FakeRecipeExtractor()
    runner = PipelineRunner(
        artifact_store=JsonArtifactStore(tmp_path / "artifacts"),
        source_loader=source_loader,
        audio_extractor=FakeAudioExtractor(),
        transcriber=transcriber,
        ocr_gate=FakeOcrGate(),
        frame_extractor=FakeFrameExtractor(),
        ocr_extractor=FakeOcrExtractor(),
        recipe_extractor=recipe_extractor,
        recipe_validator=FakeValidator(),
    )
    job = RecipeJob(
        recipe_id="rigatoni-test",
        source_url=HttpUrl("https://www.instagram.com/reel/DZdXIrXOklf/"),
        media_path=media_path,
        caption_text="Dinner tonight",
    )

    first_result = runner.run(job)
    second_result = runner.run(job)

    assert first_result.validation.outcome is RecipeOutcome.REVIEW
    assert second_result == first_result
    assert source_loader.calls == 1
    assert transcriber.calls == 1
    assert recipe_extractor.calls == 2
    assert (tmp_path / "artifacts" / job.recipe_id / "transcript.json").is_file()


class CaptionReadyExtractor:
    version = "caption-ready-extractor-v1"

    def __init__(self) -> None:
        self.calls = 0

    def extract(
        self, source: SourceArtifact, transcript: TranscriptArtifact, ocr: OcrArtifact | None
    ) -> RecipeCandidate:
        self.calls += 1
        assert transcript.model_name == "caption-only"
        assert source.caption is not None
        reference = EvidenceReference(evidence_id=source.caption.evidence_id)
        return RecipeCandidate(
            title="Caption recipe",
            ingredients=(
                Ingredient(
                    original_text="1 cup pasta",
                    name="pasta",
                    quantity_original="1",
                    unit_original="cup",
                    evidence=(reference,),
                    confidence=0.9,
                ),
            ),
            instructions=(
                Instruction(
                    original_text="Cook pasta.",
                    sequence=1,
                    evidence=(reference,),
                    confidence=0.9,
                ),
                Instruction(
                    original_text="Serve pasta.",
                    sequence=2,
                    evidence=(reference,),
                    confidence=0.9,
                ),
            ),
        )


class CaptionReadyValidator:
    version = "caption-ready-validator-v1"

    def validate(self, recipe: RecipeCandidate) -> ValidationArtifact:
        return ValidationArtifact(outcome=RecipeOutcome.READY)


class CaptionOnlySourceLoader:
    version = "caption-only-source-v1"

    def load(self, job: RecipeJob) -> SourceArtifact:
        assert job.media_path is None
        assert job.caption_text is not None
        return SourceArtifact(
            recipe_id=job.recipe_id,
            source_url=job.source_url,
            caption=EvidenceSegment(
                evidence_id="caption-1", source_kind=SourceKind.CAPTION, text=job.caption_text
            ),
        )


def test_pipeline_stops_before_audio_when_caption_recipe_is_ready(tmp_path: Path) -> None:
    media_path = tmp_path / "input.mp4"
    media_path.write_bytes(b"not a real video")
    audio_extractor = FakeAudioExtractor()
    transcriber = FakeTranscriber()
    extractor = CaptionReadyExtractor()
    runner = PipelineRunner(
        artifact_store=JsonArtifactStore(tmp_path / "artifacts"),
        source_loader=FakeSourceLoader(),
        audio_extractor=audio_extractor,
        transcriber=transcriber,
        ocr_gate=FakeOcrGate(),
        frame_extractor=FakeFrameExtractor(),
        ocr_extractor=FakeOcrExtractor(),
        recipe_extractor=extractor,
        recipe_validator=CaptionReadyValidator(),
    )

    result = runner.run(
        RecipeJob(
            recipe_id="caption-ready-test",
            source_url=HttpUrl("https://www.instagram.com/reel/DVJBGzyk8E5/"),
            media_path=media_path,
            caption_text="1 cup pasta\nCook pasta.\nServe pasta.",
        )
    )

    assert result.validation.outcome is RecipeOutcome.READY
    assert extractor.calls == 1
    assert audio_extractor.calls == 0
    assert transcriber.calls == 0


def test_caption_only_job_returns_review_when_caption_is_not_ready(tmp_path: Path) -> None:
    audio_extractor = FakeAudioExtractor()
    runner = PipelineRunner(
        artifact_store=JsonArtifactStore(tmp_path / "artifacts"),
        source_loader=CaptionOnlySourceLoader(),
        audio_extractor=audio_extractor,
        transcriber=FakeTranscriber(),
        ocr_gate=FakeOcrGate(),
        frame_extractor=FakeFrameExtractor(),
        ocr_extractor=FakeOcrExtractor(),
        recipe_extractor=FakeRecipeExtractor(),
        recipe_validator=FakeValidator(),
    )

    result = runner.run(
        RecipeJob(
            recipe_id="caption-only-incomplete",
            source_url=HttpUrl("https://www.instagram.com/p/carousel/"),
            caption_text="A partial recipe",
        )
    )

    assert result.validation.outcome is RecipeOutcome.REVIEW
    assert any(
        finding.code == "media_fallback_unavailable" for finding in result.validation.findings
    )
    assert audio_extractor.calls == 0


class TranscriptReadyExtractor:
    version = "transcript-ready-extractor-v1"

    def __init__(self) -> None:
        self.calls = 0

    def extract(
        self, source: SourceArtifact, transcript: TranscriptArtifact, ocr: OcrArtifact | None
    ) -> RecipeCandidate:
        self.calls += 1
        if transcript.model_name == "caption-only":
            return RecipeCandidate(title="Incomplete caption")
        reference = EvidenceReference(evidence_id="transcript-1")
        return RecipeCandidate(
            title="Transcript recipe",
            ingredients=(
                Ingredient(
                    original_text="1 cup pasta",
                    name="pasta",
                    quantity_original="1",
                    unit_original="cup",
                    evidence=(reference,),
                    confidence=0.7,
                ),
            ),
            instructions=(
                Instruction(
                    original_text="Cook pasta.",
                    sequence=1,
                    evidence=(reference,),
                    confidence=0.7,
                ),
                Instruction(
                    original_text="Serve pasta.",
                    sequence=2,
                    evidence=(reference,),
                    confidence=0.7,
                ),
            ),
        )


class ReadyWhenCompleteValidator:
    version = "ready-when-complete-validator-v1"

    def validate(self, recipe: RecipeCandidate) -> ValidationArtifact:
        outcome = RecipeOutcome.READY if recipe.ingredients else RecipeOutcome.REVIEW
        return ValidationArtifact(outcome=outcome)


class FailIfOcrGateRuns:
    version = "fail-ocr-gate-v1"

    def decide(
        self, source: SourceArtifact, transcript: TranscriptArtifact, recipe: RecipeCandidate
    ) -> OcrDecisionArtifact:
        raise AssertionError("OCR gate should not run for a READY transcript recipe")


def test_pipeline_skips_ocr_when_transcript_recipe_is_ready(tmp_path: Path) -> None:
    media_path = tmp_path / "input.mp4"
    media_path.write_bytes(b"not a real video")
    audio_extractor = FakeAudioExtractor()
    transcriber = FakeTranscriber()
    extractor = TranscriptReadyExtractor()
    runner = PipelineRunner(
        artifact_store=JsonArtifactStore(tmp_path / "artifacts"),
        source_loader=FakeSourceLoader(),
        audio_extractor=audio_extractor,
        transcriber=transcriber,
        ocr_gate=FailIfOcrGateRuns(),
        frame_extractor=FakeFrameExtractor(),
        ocr_extractor=FakeOcrExtractor(),
        recipe_extractor=extractor,
        recipe_validator=ReadyWhenCompleteValidator(),
    )

    result = runner.run(
        RecipeJob(
            recipe_id="transcript-ready-test",
            source_url=HttpUrl("https://www.instagram.com/reel/DVJBGzyk8E5/"),
            media_path=media_path,
            caption_text="Dinner tonight",
        )
    )

    assert result.validation.outcome is RecipeOutcome.READY
    assert audio_extractor.calls == 1
    assert transcriber.calls == 1
    assert extractor.calls == 2
