"""Synchronous orchestration of the local vertical slice."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from .artifacts import sha256_file, stable_hash
from .interfaces import (
    ArtifactStore,
    AudioExtractor,
    FrameExtractor,
    OcrExtractor,
    OcrGate,
    RecipeExtractor,
    RecipeValidator,
    SourceLoader,
    Transcriber,
)
from .models import (
    AudioArtifact,
    EvidenceSegment,
    FrameExtractionArtifact,
    OcrArtifact,
    OcrDecisionArtifact,
    PipelineResult,
    RecipeCandidate,
    RecipeExtractionArtifact,
    RecipeJob,
    RecipeOutcome,
    SourceArtifact,
    StageName,
    TranscriptArtifact,
    ValidationArtifact,
    ValidationFinding,
)

ArtifactModel = TypeVar("ArtifactModel", bound=BaseModel)


class PipelineRunner:
    """Coordinates stages and safely reuses matching persisted artifacts."""

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        source_loader: SourceLoader,
        audio_extractor: AudioExtractor,
        transcriber: Transcriber,
        ocr_gate: OcrGate,
        frame_extractor: FrameExtractor,
        ocr_extractor: OcrExtractor,
        recipe_extractor: RecipeExtractor,
        recipe_validator: RecipeValidator,
    ) -> None:
        self._artifact_store = artifact_store
        self._source_loader = source_loader
        self._audio_extractor = audio_extractor
        self._transcriber = transcriber
        self._ocr_gate = ocr_gate
        self._frame_extractor = frame_extractor
        self._ocr_extractor = ocr_extractor
        self._recipe_extractor = recipe_extractor
        self._recipe_validator = recipe_validator

    def run(self, job: RecipeJob) -> PipelineResult:
        media_sha256 = sha256_file(job.media_path) if job.media_path is not None else None
        source = self._run_stage(
            job,
            StageName.SOURCE,
            SourceArtifact,
            {
                "job": job.model_dump(mode="json"),
                "media_sha256": media_sha256,
                "component": self._source_loader.version,
            },
            lambda: self._source_loader.load(job),
        )
        if source.caption is not None:
            caption_transcript = TranscriptArtifact(
                model_name="caption-only",
                compute_type="not-run",
            )
            caption_extraction = self._extract_recipe(
                job,
                StageName.CAPTION_RECIPE,
                source,
                caption_transcript,
                None,
            )
            caption_validation = self._validate_recipe(
                job, StageName.CAPTION_VALIDATION, caption_extraction.recipe
            )
            if caption_validation.outcome is RecipeOutcome.READY:
                return PipelineResult(
                    recipe=caption_extraction.recipe,
                    validation=caption_validation,
                    recipe_usage=caption_extraction.usage,
                    evidence_segments=_evidence_segments(source, caption_transcript, None),
                )
            if source.media_path is None:
                unavailable = ValidationArtifact(
                    outcome=RecipeOutcome.REVIEW,
                    findings=caption_validation.findings
                    + (
                        self._fallback_unavailable_finding(),
                    ),
                )
                return PipelineResult(
                    recipe=caption_extraction.recipe,
                    validation=unavailable,
                    recipe_usage=caption_extraction.usage,
                    evidence_segments=_evidence_segments(source, caption_transcript, None),
                )
        audio = self._run_stage(
            job,
            StageName.AUDIO,
            AudioArtifact,
            {"source": source.model_dump(mode="json"), "component": self._audio_extractor.version},
            lambda: self._audio_extractor.extract(source),
        )
        transcript = self._run_stage(
            job,
            StageName.TRANSCRIPT,
            TranscriptArtifact,
            {"audio": audio.model_dump(mode="json"), "component": self._transcriber.version},
            lambda: self._transcriber.transcribe(audio),
        )
        extraction = self._extract_recipe(
            job, StageName.TRANSCRIPT_RECIPE, source, transcript, None
        )
        recipe = extraction.recipe
        validation = self._validate_recipe(job, StageName.TRANSCRIPT_VALIDATION, recipe)
        if validation.outcome is RecipeOutcome.READY:
            return PipelineResult(
                recipe=recipe,
                validation=validation,
                recipe_usage=extraction.usage,
                evidence_segments=_evidence_segments(source, transcript, None),
            )
        ocr: OcrArtifact | None = None
        decision = self._run_stage(
            job,
            StageName.OCR_DECISION,
            OcrDecisionArtifact,
            {
                "source": source.model_dump(mode="json"),
                "transcript": transcript.model_dump(mode="json"),
                "recipe": recipe.model_dump(mode="json"),
                "component": self._ocr_gate.version,
            },
            lambda: self._ocr_gate.decide(source, transcript, recipe),
        )
        if decision.should_run:
            frames = self._run_stage(
                job,
                StageName.FRAMES,
                FrameExtractionArtifact,
                {
                    "source": source.model_dump(mode="json"),
                    "component": self._frame_extractor.version,
                },
                lambda: self._frame_extractor.extract(source),
            )
            ocr = self._run_stage(
                job,
                StageName.OCR,
                OcrArtifact,
                {
                    "frames": frames.model_dump(mode="json"),
                    "component": self._ocr_extractor.version,
                },
                lambda: self._ocr_extractor.extract(frames),
            )
            extraction = self._extract_recipe(job, StageName.RECIPE, source, transcript, ocr)
            recipe = extraction.recipe
            validation = self._validate_recipe(job, StageName.VALIDATION, recipe)
        return PipelineResult(
            recipe=recipe,
            validation=validation,
            recipe_usage=extraction.usage,
            evidence_segments=_evidence_segments(source, transcript, ocr),
        )

    @staticmethod
    def _fallback_unavailable_finding() -> ValidationFinding:
        return ValidationFinding(
            code="media_fallback_unavailable",
            message=(
                "Caption extraction was not READY and no media was available "
                "for transcription or OCR."
            ),
        )

    def _extract_recipe(
        self,
        job: RecipeJob,
        stage: StageName,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        ocr: OcrArtifact | None,
    ) -> RecipeExtractionArtifact:
        return self._run_stage(
            job,
            stage,
            RecipeExtractionArtifact,
            {
                "source": source.model_dump(mode="json"),
                "transcript": transcript.model_dump(mode="json"),
                "ocr": ocr.model_dump(mode="json") if ocr else None,
                "component": self._recipe_extractor.version,
                "artifact_schema_version": 2,
            },
            lambda: self._produce_recipe_artifact(source, transcript, ocr),
        )

    def _produce_recipe_artifact(
        self,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        ocr: OcrArtifact | None,
    ) -> RecipeExtractionArtifact:
        producer = getattr(self._recipe_extractor, "extract_artifact", None)
        if callable(producer):
            result = producer(source, transcript, ocr)
            if isinstance(result, RecipeExtractionArtifact):
                return result
            raise TypeError("Recipe extraction metadata producer returned an invalid artifact")
        return RecipeExtractionArtifact(
            recipe=self._recipe_extractor.extract(source, transcript, ocr)
        )

    def _validate_recipe(
        self, job: RecipeJob, stage: StageName, recipe: RecipeCandidate
    ) -> ValidationArtifact:
        return self._run_stage(
            job,
            stage,
            ValidationArtifact,
            {"recipe": recipe.model_dump(mode="json"), "component": self._recipe_validator.version},
            lambda: self._recipe_validator.validate(recipe),
        )

    def _run_stage(
        self,
        job: RecipeJob,
        stage: StageName,
        model_type: type[ArtifactModel],
        inputs: object,
        producer: Callable[[], ArtifactModel],
    ) -> ArtifactModel:
        input_hash = stable_hash(inputs)
        cached = self._artifact_store.load(job.recipe_id, stage.value, input_hash, model_type)
        if cached is not None:
            return model_type.model_validate(cached)
        payload = producer()
        self._artifact_store.save(job.recipe_id, stage.value, input_hash, payload)
        return payload


def _evidence_segments(
    source: SourceArtifact,
    transcript: TranscriptArtifact,
    ocr: OcrArtifact | None,
) -> tuple[EvidenceSegment, ...]:
    segments: list[EvidenceSegment] = []
    if source.caption is not None:
        segments.append(source.caption)
    segments.extend(transcript.segments)
    if ocr is not None:
        segments.extend(ocr.segments)
    return tuple(segments)
