"""Typed domain models shared by pipeline stages and persisted artifacts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class StageName(StrEnum):
    SOURCE = "source"
    CAPTION_RECIPE = "caption_recipe"
    CAPTION_VALIDATION = "caption_validation"
    AUDIO = "audio"
    TRANSCRIPT = "transcript"
    TRANSCRIPT_RECIPE = "transcript_recipe"
    TRANSCRIPT_VALIDATION = "transcript_validation"
    OCR_DECISION = "ocr_decision"
    FRAMES = "frames"
    OCR = "ocr"
    RECIPE = "recipe"
    VALIDATION = "validation"


class SourceKind(StrEnum):
    CAPTION = "caption"
    TRANSCRIPT = "transcript"
    OCR = "ocr"


class RecipeOutcome(StrEnum):
    READY = "ready"
    REVIEW = "review"


class QueueStatus(StrEnum):
    """Human-visible processing state for an item in the Google Sheets queue."""

    PENDING = "pending"
    PROCESSING = "processing"
    PUBLISHED = "published"
    REVIEW = "review"
    ERROR = "error"


class ReviewDecisionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ReviewCategory(StrEnum):
    """Machine-readable reasons a recipe needs human review."""

    INGREDIENTS_MISMATCH = "ingredients_mismatch"
    INGREDIENTS_AMOUNTS_MISSING = "ingredients_amounts_missing"
    MISSING_CRITICAL_STEP = "missing_critical_step"
    MISSING_TITLE = "missing_title"
    MISSING_INGREDIENTS = "missing_ingredients"
    MISSING_INSTRUCTIONS = "missing_instructions"
    SERVINGS_MISSING = "servings_missing"
    NUTRITION_MISSING = "nutrition_missing"
    SOURCE_CONFLICT = "source_conflict"
    RECIPE_INCOMPLETE = "recipe_incomplete"


class RecipeInstructionFormat(StrEnum):
    """How instructions are presented in a human-approved final document."""

    NUMBERED_STEPS = "numbered_steps"
    RAW_TRANSCRIPT = "raw_transcript"


class RecipeDocumentPresentation(BaseModel):
    """Rendering choice kept outside the provider-independent recipe candidate."""

    model_config = ConfigDict(frozen=True)

    instruction_format: RecipeInstructionFormat = RecipeInstructionFormat.NUMBERED_STEPS
    raw_instruction_text: str | None = None
    servings_text: str | None = None
    nutrition_notes: str | None = None

    @model_validator(mode="after")
    def require_raw_text_for_transcript_format(self) -> RecipeDocumentPresentation:
        if self.instruction_format is RecipeInstructionFormat.RAW_TRANSCRIPT:
            if self.raw_instruction_text is None or not self.raw_instruction_text.strip():
                raise ValueError("raw_instruction_text is required for raw_transcript format")
        return self


class OcrPolicy(StrEnum):
    NEVER = "never"
    WHEN_NEEDED = "when_needed"
    ALWAYS = "always"


class EvidenceSegment(BaseModel):
    """A precise span of source content supporting a structured recipe claim."""

    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1)
    source_kind: SourceKind
    text: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    frame_timestamp_seconds: float | None = Field(default=None, ge=0)
    frame_path: Path | None = None
    bounding_polygon: tuple[tuple[int, int], ...] | None = None


class RecipeJob(BaseModel):
    """Local input for one vertical-slice run."""

    model_config = ConfigDict(frozen=True)

    recipe_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
    source_url: HttpUrl
    media_path: Path | None = None
    caption_text: str | None = None

    @field_validator("caption_text")
    @classmethod
    def strip_empty_caption(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class QueuedRecipe(BaseModel):
    """One pending URL read from a category tab in the queue spreadsheet."""

    model_config = ConfigDict(frozen=True)

    recipe_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
    source_url: HttpUrl
    category: str = Field(min_length=1)
    queue_row_number: int = Field(ge=2)
    description: str | None = None
    status: QueueStatus = QueueStatus.PENDING


class AcquiredRecipe(BaseModel):
    """Local media and best-effort caption metadata for a queued recipe."""

    model_config = ConfigDict(frozen=True)

    queued_recipe: QueuedRecipe
    job: RecipeJob
    metadata_path: Path


class RecipeDocument(BaseModel):
    """A Google Doc created for a validated recipe."""

    model_config = ConfigDict(frozen=True)

    document_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: HttpUrl
    folder_id: str | None = None


class PublicationArtifact(BaseModel):
    """Recovery state for idempotent Google Docs and Master Sheet delivery."""

    model_config = ConfigDict(frozen=True)

    recipe_id: str = Field(min_length=1)
    document: RecipeDocument
    master_row_written: bool = False


class ReviewArtifact(BaseModel):
    """Recovery state for one review document and its Review Sheet row."""

    model_config = ConfigDict(frozen=True)

    recipe_id: str = Field(min_length=1)
    document: RecipeDocument
    recipe: RecipeCandidate | None = None
    review_categories: tuple[ReviewCategory, ...] = ()
    transcript_text: str | None = None
    review_row_written: bool = False


class ReviewDecision(BaseModel):
    """One manual accept/reject action read from a Review Sheet category tab."""

    model_config = ConfigDict(frozen=True)

    recipe_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
    source_url: HttpUrl
    category: str = Field(min_length=1)
    review_row_number: int = Field(ge=2)
    description: str | None = None
    decision: ReviewDecisionStatus
    review_document_url: HttpUrl
    servings_text: str | None = None
    nutrition_notes: str | None = None


class ReviewResolutionArtifact(BaseModel):
    """Recovery state for an accepted or rejected Review Sheet decision."""

    model_config = ConfigDict(frozen=True)

    recipe_id: str = Field(min_length=1)
    decision: ReviewDecisionStatus
    document: RecipeDocument
    master_row_written: bool = False
    rejected_row_written: bool = False
    review_row_removed: bool = False


class SourceArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    recipe_id: str
    source_url: HttpUrl
    media_path: Path | None = None
    media_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    caption: EvidenceSegment | None = None


class AudioArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    audio_path: Path
    audio_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sample_rate_hz: int = Field(gt=0)
    channels: int = Field(gt=0)


class CropRegion(BaseModel):
    model_config = ConfigDict(frozen=True)

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class VideoProbe(BaseModel):
    model_config = ConfigDict(frozen=True)

    duration_seconds: float = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class ExtractedFrame(BaseModel):
    model_config = ConfigDict(frozen=True)

    frame_path: Path
    timestamp_seconds: float = Field(ge=0)


class FrameExtractionArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: VideoProbe
    sampling_fps: float = Field(gt=0)
    crop: CropRegion | None = None
    frames: tuple[ExtractedFrame, ...] = ()


class TranscriptArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    segments: tuple[EvidenceSegment, ...] = ()
    language: str | None = None
    language_probability: float | None = Field(default=None, ge=0, le=1)
    model_name: str = Field(min_length=1)
    compute_type: str = Field(min_length=1)


class OcrDecisionArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    should_run: bool
    policy: OcrPolicy
    reason: str = Field(min_length=1)


class OcrArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    segments: tuple[EvidenceSegment, ...] = ()
    candidate_frame_count: int = Field(default=0, ge=0)
    selected_frame_count: int = Field(default=0, ge=0)
    engine: str | None = None
    engine_version: str | None = None


class EvidenceReference(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1)


class Ingredient(BaseModel):
    """An extracted ingredient; values are optional to avoid inventing facts."""

    model_config = ConfigDict(frozen=True)

    original_text: str = Field(min_length=1)
    name: str = Field(min_length=1)
    quantity_original: str | None = None
    unit_original: str | None = None
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class Instruction(BaseModel):
    model_config = ConfigDict(frozen=True)

    original_text: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class CompletenessFinding(BaseModel):
    """An evidence-grounded reason a candidate is unsafe to auto-publish."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    evidence: tuple[EvidenceReference, ...] = Field(min_length=1)


class RecipeCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str | None = None
    ingredients: tuple[Ingredient, ...] = ()
    instructions: tuple[Instruction, ...] = ()
    conflicts: tuple[str, ...] = ()
    completeness_findings: tuple[CompletenessFinding, ...] = ()


class ApiUsage(BaseModel):
    """Provider-reported request usage; deliberately contains no price calculation."""

    model_config = ConfigDict(frozen=True)

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    request_count: int = Field(default=1, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class RecipeExtractionArtifact(BaseModel):
    """Persisted extraction result, separate from the provider-independent recipe model."""

    model_config = ConfigDict(frozen=True)

    recipe: RecipeCandidate
    usage: ApiUsage | None = None


class ValidationFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ValidationArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: RecipeOutcome
    findings: tuple[ValidationFinding, ...] = ()


class ArtifactEnvelope(BaseModel):
    """Versioned JSON wrapper used for all persisted stage artifacts."""

    model_config = ConfigDict(frozen=True)

    schema_version: int = Field(default=1, ge=1)
    stage: StageName
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, object]


class PipelineResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    recipe: RecipeCandidate
    validation: ValidationArtifact
    recipe_usage: ApiUsage | None = None
    evidence_segments: tuple[EvidenceSegment, ...] = ()
