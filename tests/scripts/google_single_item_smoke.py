"""Run one real queue-to-Google-Doc workflow and verify retry safety.

This is an explicit manual integration test. It performs a paid OpenAI request when
the pipeline cache is cold and writes a Google Doc plus one Master Sheet row when the
publication cache is cold. It is deliberately not part of the pytest suite.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any

from pydantic import HttpUrl

from instagram_recipe_transcriber.acquisition import YtDlpAcquirer
from instagram_recipe_transcriber.adapters import (
    FasterWhisperTranscriber,
    FfmpegAudioExtractor,
    FfmpegFrameExtractor,
    LocalFileSourceLoader,
    PaddleOcrExtractor,
)
from instagram_recipe_transcriber.artifacts import JsonArtifactStore
from instagram_recipe_transcriber.config import GoogleWorkflowConfig
from instagram_recipe_transcriber.google_adapters import (
    GoogleDocsRecipeWriter,
    GoogleDriveDocumentOrganizer,
    GoogleSheetsRecipeMasterWriter,
    GoogleSheetsRecipeQueueReader,
)
from instagram_recipe_transcriber.google_oauth import GoogleOAuthServiceFactory
from instagram_recipe_transcriber.models import (
    AudioArtifact,
    FrameExtractionArtifact,
    OcrArtifact,
    OcrDecisionArtifact,
    PipelineResult,
    QueuedRecipe,
    RecipeCandidate,
    RecipeDocument,
    RecipeExtractionArtifact,
    RecipeOutcome,
    SourceArtifact,
    TranscriptArtifact,
)
from instagram_recipe_transcriber.openai_recipe_extractor import OpenAiRecipeExtractor
from instagram_recipe_transcriber.pipeline import PipelineRunner
from instagram_recipe_transcriber.publication import JsonPublicationStore
from instagram_recipe_transcriber.recipe_processing import RecipeValidator
from instagram_recipe_transcriber.workflow import QueuedRecipeWorkflow

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_CATEGORY = "Main Courses"
EXPECTED_URL = HttpUrl(
    "https://www.instagram.com/reel/DVJBGzyk8E5/?igsh=MTdicnEzaTM3aGE5ag=="
)
EXPECTED_DESCRIPTION = "Chicken + mac"


class ExpectedQueueReader:
    """Reject an unexpected first queue row before any downstream write."""

    def __init__(self, delegate: GoogleSheetsRecipeQueueReader) -> None:
        self._delegate = delegate
        self.calls = 0

    def read_next(self) -> QueuedRecipe:
        self.calls += 1
        queued = self._delegate.read_next()
        if queued is None:
            raise AssertionError("Google queue did not contain an eligible row")
        assert queued.category == EXPECTED_CATEGORY
        assert queued.source_url == EXPECTED_URL
        assert queued.description == EXPECTED_DESCRIPTION
        return queued


class CountingRecipeExtractor:
    def __init__(self, delegate: OpenAiRecipeExtractor) -> None:
        self._delegate = delegate
        self.calls = 0

    @property
    def version(self) -> str:
        return self._delegate.version

    def extract(
        self,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        ocr: OcrArtifact | None,
    ) -> RecipeCandidate:
        return self.extract_artifact(source, transcript, ocr).recipe

    def extract_artifact(
        self,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        ocr: OcrArtifact | None,
    ) -> RecipeExtractionArtifact:
        self.calls += 1
        return self._delegate.extract_artifact(source, transcript, ocr)


class ForbiddenAudioExtractor(FfmpegAudioExtractor):
    def __init__(self, working_root: Path) -> None:
        super().__init__(working_root)
        self.calls = 0

    def extract(self, source: SourceArtifact) -> AudioArtifact:
        self.calls += 1
        raise AssertionError("Full-caption Google smoke invoked audio extraction")


class ForbiddenFasterWhisper(FasterWhisperTranscriber):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def transcribe(self, audio: AudioArtifact) -> TranscriptArtifact:
        self.calls += 1
        raise AssertionError("Full-caption Google smoke invoked Faster-Whisper")


class ForbiddenFrameExtractor(FfmpegFrameExtractor):
    def __init__(self, working_root: Path) -> None:
        super().__init__(working_root)
        self.calls = 0

    def extract(self, source: SourceArtifact) -> FrameExtractionArtifact:
        self.calls += 1
        raise AssertionError("Full-caption Google smoke invoked frame extraction")


class ForbiddenOcrExtractor(PaddleOcrExtractor):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def extract(self, frames: FrameExtractionArtifact) -> OcrArtifact:
        self.calls += 1
        raise AssertionError("Full-caption Google smoke invoked OCR")


class ForbiddenOcrGate:
    version = "forbidden-google-caption-first-ocr-gate-v1"

    def __init__(self) -> None:
        self.calls = 0

    def decide(
        self,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        recipe: RecipeCandidate,
    ) -> OcrDecisionArtifact:
        self.calls += 1
        raise AssertionError("Full-caption Google smoke invoked the OCR gate")


class CountingDocumentWriter:
    def __init__(self, delegate: GoogleDocsRecipeWriter) -> None:
        self._delegate = delegate
        self.calls = 0

    def create(self, recipe: RecipeCandidate, queued_recipe: QueuedRecipe) -> RecipeDocument:
        self.calls += 1
        return self._delegate.create(recipe, queued_recipe)


class CountingDocumentOrganizer:
    def __init__(self, delegate: GoogleDriveDocumentOrganizer) -> None:
        self._delegate = delegate
        self.calls = 0

    def move_to_folder(self, document: RecipeDocument) -> RecipeDocument:
        self.calls += 1
        return self._delegate.move_to_folder(document)


class CountingMasterWriter:
    def __init__(self, delegate: GoogleSheetsRecipeMasterWriter) -> None:
        self._delegate = delegate
        self.calls = 0

    def append(self, queued_recipe: QueuedRecipe, document: RecipeDocument) -> None:
        self.calls += 1
        self._delegate.append(queued_recipe, document)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=REPOSITORY_ROOT / "google_workflow_config.json",
    )
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument(
        "--execute-writes",
        action="store_true",
        help="Required acknowledgement that this run can create a Doc and append a Sheet row",
    )
    return parser.parse_args()


def load_config(path: Path) -> GoogleWorkflowConfig:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Google workflow config does not exist: {resolved}")
    return GoogleWorkflowConfig.model_validate_json(resolved.read_text(encoding="utf-8"))


def preflight(config: GoogleWorkflowConfig, *, execute_writes: bool) -> None:
    if not execute_writes:
        raise RuntimeError("Pass --execute-writes to acknowledge real Google writes")
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError("OPENAI_API_KEY must be exported in this process")
    if not config.oauth_client_path.is_file():
        raise FileNotFoundError(f"OAuth client JSON does not exist: {config.oauth_client_path}")
    if config.oauth_token_path.exists() and config.oauth_token_path.stat().st_size == 0:
        raise RuntimeError(
            f"OAuth token file is empty; delete it before authorization: {config.oauth_token_path}"
        )
    assert config.category_tabs == ("Desserts", "Snacks", "Main Courses", "Other")
    assert config.drive_folder_id is not None


def build_pipeline(
    config: GoogleWorkflowConfig, model: str
) -> tuple[PipelineRunner, dict[str, Any]]:
    audio = ForbiddenAudioExtractor(config.working_root)
    transcriber = ForbiddenFasterWhisper()
    frames = ForbiddenFrameExtractor(config.working_root)
    ocr = ForbiddenOcrExtractor()
    ocr_gate = ForbiddenOcrGate()
    recipe_extractor = CountingRecipeExtractor(OpenAiRecipeExtractor(model=model))
    pipeline = PipelineRunner(
        artifact_store=JsonArtifactStore(config.working_root / "pipeline-artifacts"),
        source_loader=LocalFileSourceLoader(),
        audio_extractor=audio,
        transcriber=transcriber,
        ocr_gate=ocr_gate,
        frame_extractor=frames,
        ocr_extractor=ocr,
        recipe_extractor=recipe_extractor,
        recipe_validator=RecipeValidator(),
    )
    counters: dict[str, Any] = {
        "audio": audio,
        "transcriber": transcriber,
        "frames": frames,
        "ocr": ocr,
        "ocr_gate": ocr_gate,
        "recipe_extractor": recipe_extractor,
    }
    return pipeline, counters


def assert_caption_fast_path(counters: dict[str, Any]) -> None:
    assert counters["audio"].calls == 0
    assert counters["transcriber"].calls == 0
    assert counters["frames"].calls == 0
    assert counters["ocr"].calls == 0
    assert counters["ocr_gate"].calls == 0


def assert_ready(result: PipelineResult | None) -> PipelineResult:
    assert result is not None
    assert result.validation.outcome is RecipeOutcome.READY
    assert result.recipe.title
    assert result.recipe.ingredients
    assert result.recipe.instructions
    assert result.recipe_usage is not None
    assert result.recipe_usage.provider == "openai"
    return result


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    preflight(config, execute_writes=args.execute_writes)
    services = GoogleOAuthServiceFactory(config).create_services()

    queue_reader = ExpectedQueueReader(
        GoogleSheetsRecipeQueueReader(
            services.sheets,
            spreadsheet_id=config.queue_spreadsheet_id,
            categories=config.category_tabs,
        )
    )
    pipeline, counters = build_pipeline(config, args.model)
    document_writer = CountingDocumentWriter(GoogleDocsRecipeWriter(services.docs))
    assert config.drive_folder_id is not None
    organizer = CountingDocumentOrganizer(
        GoogleDriveDocumentOrganizer(services.drive, folder_id=config.drive_folder_id)
    )
    master_writer = CountingMasterWriter(
        GoogleSheetsRecipeMasterWriter(
            services.sheets,
            spreadsheet_id=config.master_spreadsheet_id,
        )
    )
    workflow = QueuedRecipeWorkflow(
        queue_reader=queue_reader,
        media_acquirer=YtDlpAcquirer(config.working_root / "acquisition"),
        pipeline_runner=pipeline,
        document_writer=document_writer,
        master_writer=master_writer,
        publication_store=JsonPublicationStore(config.working_root / "publication"),
        document_organizer=organizer,
    )

    started = time.perf_counter()
    first = workflow.process_next()
    first_elapsed = time.perf_counter() - started
    first_pipeline = assert_ready(first.pipeline_result)
    assert first.publication is not None
    assert first.publication.master_row_written
    assert first.publication.document.folder_id == config.drive_folder_id
    assert_caption_fast_path(counters)

    calls_after_first = (
        counters["recipe_extractor"].calls,
        document_writer.calls,
        organizer.calls,
        master_writer.calls,
    )
    started = time.perf_counter()
    second = workflow.process_next()
    retry_elapsed = time.perf_counter() - started
    second_pipeline = assert_ready(second.pipeline_result)
    assert second.publication == first.publication
    assert second_pipeline == first_pipeline
    assert (
        counters["recipe_extractor"].calls,
        document_writer.calls,
        organizer.calls,
        master_writer.calls,
    ) == calls_after_first
    assert_caption_fast_path(counters)

    result_path = config.working_root / "google_smoke_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(first.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print("PASS: expected Main Courses queue row was selected")
    print("PASS: pipeline reached READY using OpenAI caption extraction")
    print("PASS: Faster-Whisper, frame extraction, and OCR were not invoked")
    print("PASS: Google Doc was created or recovered from its publication checkpoint")
    print("PASS: Doc is recorded in the configured Drive folder")
    print("PASS: Master Sheet publication is checkpointed")
    print("PASS: retry made no additional OpenAI, Docs, Drive, or Master Sheet write call")
    print(f"First run: {first_elapsed:.2f}s; retry: {retry_elapsed:.2f}s")
    print(f"OpenAI usage: {first_pipeline.recipe_usage.model_dump_json()}")
    print(f"Document URL: {first.publication.document.url}")
    print(f"Result: {result_path}")


if __name__ == "__main__":
    main()
