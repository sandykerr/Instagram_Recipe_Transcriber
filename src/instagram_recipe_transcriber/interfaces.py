"""Small replaceable boundaries around pipeline behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from .models import (
    AcquiredRecipe,
    AudioArtifact,
    FrameExtractionArtifact,
    OcrArtifact,
    OcrDecisionArtifact,
    PipelineResult,
    PublicationArtifact,
    QueuedRecipe,
    RecipeCandidate,
    RecipeDocument,
    RecipeExtractionArtifact,
    RecipeJob,
    SourceArtifact,
    TranscriptArtifact,
    ValidationArtifact,
)


class VersionedComponent(Protocol):
    @property
    def version(self) -> str: ...


class SourceLoader(VersionedComponent, Protocol):
    def load(self, job: RecipeJob) -> SourceArtifact: ...


class AudioExtractor(VersionedComponent, Protocol):
    def extract(self, source: SourceArtifact) -> AudioArtifact: ...


class FrameExtractor(VersionedComponent, Protocol):
    def extract(self, source: SourceArtifact) -> FrameExtractionArtifact: ...


class Transcriber(VersionedComponent, Protocol):
    def transcribe(self, audio: AudioArtifact) -> TranscriptArtifact: ...


class OcrGate(VersionedComponent, Protocol):
    def decide(
        self, source: SourceArtifact, transcript: TranscriptArtifact, recipe: RecipeCandidate
    ) -> OcrDecisionArtifact: ...


class OcrExtractor(VersionedComponent, Protocol):
    def extract(self, frames: FrameExtractionArtifact) -> OcrArtifact: ...


class RecipeExtractor(VersionedComponent, Protocol):
    def extract(
        self,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        ocr: OcrArtifact | None,
    ) -> RecipeCandidate: ...


class RecipeExtractionArtifactProducer(Protocol):
    """Optional metadata extension that leaves ``RecipeExtractor`` provider-neutral."""

    def extract_artifact(
        self,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        ocr: OcrArtifact | None,
    ) -> RecipeExtractionArtifact: ...


class RecipeValidator(VersionedComponent, Protocol):
    def validate(self, recipe: RecipeCandidate) -> ValidationArtifact: ...


class ArtifactStore(Protocol):
    def load(
        self, recipe_id: str, stage: str, input_hash: str, model_type: type[BaseModel]
    ) -> BaseModel | None: ...

    def save(self, recipe_id: str, stage: str, input_hash: str, payload: BaseModel) -> None: ...


class WorkingDirectory(Protocol):
    root: Path


class RecipeQueueReader(Protocol):
    def read_next(self) -> QueuedRecipe | None: ...


class MediaAcquirer(VersionedComponent, Protocol):
    def acquire(self, queued_recipe: QueuedRecipe) -> AcquiredRecipe: ...


class RecipeDocumentWriter(Protocol):
    def create(self, recipe: RecipeCandidate, queued_recipe: QueuedRecipe) -> RecipeDocument: ...


class RecipeMasterWriter(Protocol):
    def append(self, queued_recipe: QueuedRecipe, document: RecipeDocument) -> None: ...


class DocumentOrganizer(Protocol):
    def move_to_folder(self, document: RecipeDocument) -> RecipeDocument: ...


class RecipePipeline(Protocol):
    def run(self, job: RecipeJob) -> PipelineResult: ...


class PublicationStore(Protocol):
    def load(self, recipe_id: str) -> PublicationArtifact | None: ...

    def save(self, publication: PublicationArtifact) -> None: ...
