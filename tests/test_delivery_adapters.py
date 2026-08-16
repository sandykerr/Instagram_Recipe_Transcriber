from __future__ import annotations

import json
from pathlib import Path

from pydantic import HttpUrl

from instagram_recipe_transcriber.acquisition import YtDlpAcquirer, recipe_id_for_url
from instagram_recipe_transcriber.google_adapters import (
    GoogleDocsRecipeReviewWriter,
    GoogleDocsRecipeWriter,
    GoogleDriveDocumentOrganizer,
    GoogleSheetsRecipeMasterWriter,
    GoogleSheetsRecipeQueueReader,
    GoogleSheetsRecipeQueueStateWriter,
    GoogleSheetsRecipeReviewWriter,
    GoogleSheetsReviewDecisionReader,
)
from instagram_recipe_transcriber.models import (
    EvidenceReference,
    EvidenceSegment,
    Ingredient,
    Instruction,
    QueuedRecipe,
    QueueStatus,
    RecipeCandidate,
    RecipeDocumentPresentation,
    RecipeInstructionFormat,
    RecipeOutcome,
    SourceKind,
    ValidationArtifact,
)


class _Request:
    def __init__(self, response: dict[str, object]) -> None:
        self._response = response

    def execute(self) -> dict[str, object]:
        return self._response


class _Values:
    def __init__(self, values_by_range: dict[str, list[list[str]]]) -> None:
        self._values_by_range = values_by_range
        self.get_calls: list[dict[str, object]] = []
        self.append_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> _Request:
        self.get_calls.append(kwargs)
        range_name = kwargs["range"]
        assert isinstance(range_name, str)
        return _Request({"values": self._values_by_range[range_name]})

    def append(self, **kwargs: object) -> _Request:
        self.append_calls.append(kwargs)
        return _Request({})

    def update(self, **kwargs: object) -> _Request:
        self.update_calls.append(kwargs)
        return _Request({})


class _SheetsService:
    def __init__(self, values_by_range: dict[str, list[list[str]]]) -> None:
        self.values_api = _Values(values_by_range)

    def spreadsheets(self) -> _SheetsService:
        return self

    def values(self) -> _Values:
        return self.values_api


class _Documents:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, object]] = []
        self.batch_calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> _Request:
        self.create_calls.append(kwargs)
        return _Request({"documentId": "doc-123"})

    def batchUpdate(self, **kwargs: object) -> _Request:
        self.batch_calls.append(kwargs)
        return _Request({})


class _DocsService:
    def __init__(self) -> None:
        self.documents_api = _Documents()

    def documents(self) -> _Documents:
        return self.documents_api


class _Files:
    def __init__(self) -> None:
        self.update_calls: list[dict[str, object]] = []

    def get(self, **kwargs: object) -> _Request:
        return _Request({"parents": ["old-folder"]})

    def update(self, **kwargs: object) -> _Request:
        self.update_calls.append(kwargs)
        return _Request({})


class _DriveService:
    def __init__(self) -> None:
        self.files_api = _Files()

    def files(self) -> _Files:
        return self.files_api


def _queued_recipe() -> QueuedRecipe:
    return QueuedRecipe(
        recipe_id="queued-recipe",
        source_url=HttpUrl("https://www.instagram.com/reel/DVJBGzyk8E5/"),
        category="Desserts",
        queue_row_number=2,
        description="Quick weeknight recipe",
    )


def _recipe() -> RecipeCandidate:
    reference = EvidenceReference(evidence_id="caption-1")
    return RecipeCandidate(
        title="Honey Pasta",
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
                original_text="Cook pasta.", sequence=1, evidence=(reference,), confidence=0.9
            ),
        ),
    )


def test_queue_reader_returns_the_first_url_from_category_tabs() -> None:
    service = _SheetsService(
        {
            "'Desserts'!A2:D": [
                ["", ""],
                ["https://www.instagram.com/reel/already-published/", "", "published"],
                ["https://www.instagram.com/reel/DVJBGzyk8E5/", "", ""],
            ]
        }
    )

    queued = GoogleSheetsRecipeQueueReader(
        service, spreadsheet_id="queue-sheet", categories=("Desserts",)
    ).read_next()

    assert queued is not None
    assert queued.category == "Desserts"
    assert queued.queue_row_number == 4
    assert queued.status is QueueStatus.PENDING
    assert queued.recipe_id == recipe_id_for_url(str(queued.source_url))


def test_google_document_drive_and_master_sheet_adapters_write_expected_requests() -> None:
    queued = _queued_recipe()
    docs_service = _DocsService()
    document = GoogleDocsRecipeWriter(docs_service).create(_recipe(), queued)
    drive_service = _DriveService()
    organizer = GoogleDriveDocumentOrganizer(drive_service, folder_id="recipe-folder")
    organized = organizer.move_to_folder(document)
    sheets_service = _SheetsService({})
    GoogleSheetsRecipeMasterWriter(sheets_service, spreadsheet_id="master-sheet").append(
        queued, organized
    )

    text = str(docs_service.documents_api.batch_calls[0])
    assert "Honey Pasta" in text
    assert "Quick weeknight recipe" in text
    assert organized.folder_id == "recipe-folder"
    assert drive_service.files_api.update_calls[0]["addParents"] == "recipe-folder"
    assert sheets_service.values_api.append_calls == [
        {
            "spreadsheetId": "master-sheet",
            "range": "'Desserts'!A:B",
            "valueInputOption": "RAW",
            "insertDataOption": "INSERT_ROWS",
            "body": {"values": [["Honey Pasta", str(organized.url)]]},
        }
    ]


def test_google_document_writer_can_render_an_approved_raw_transcript() -> None:
    docs_service = _DocsService()

    GoogleDocsRecipeWriter(docs_service).create(
        _recipe(),
        _queued_recipe(),
        RecipeDocumentPresentation(
            instruction_format=RecipeInstructionFormat.RAW_TRANSCRIPT,
            raw_instruction_text="First, combine everything. Then cook it through.",
        ),
    )

    text = str(docs_service.documents_api.batch_calls[0])
    assert "First, combine everything. Then cook it through." in text
    assert "1. Cook pasta." not in text


def test_review_decision_reader_treats_approved_as_accepted() -> None:
    service = _SheetsService(
        {
            "'Desserts'!A2:D": [
                [
                    "https://www.instagram.com/reel/DVJBGzyk8E5/",
                    "Quick pasta",
                    "Approved",
                    "https://docs.google.com/document/d/review-doc/edit",
                ]
            ]
        }
    )

    decision = GoogleSheetsReviewDecisionReader(
        service,
        spreadsheet_id="review-sheet",
        categories=("Desserts",),
    ).read_next()

    assert decision is not None
    assert decision.decision.value == "accepted"


def test_yt_dlp_acquirer_returns_video_and_caption_metadata(tmp_path: Path) -> None:
    queued = _queued_recipe()

    def runner(command: list[str]) -> None:
        output_template = Path(command[command.index("--output") + 1])
        output_dir = output_template.parent
        if "--skip-download" in command:
            (output_dir / "source.info.json").write_text(
                json.dumps(
                    {"description": "Caption from Instagram", "formats": [{"vcodec": "avc1"}]}
                ),
                encoding="utf-8",
            )
        else:
            (output_dir / "source.mp4").write_bytes(b"video")

    acquired = YtDlpAcquirer(tmp_path, command_runner=runner).acquire(queued)

    assert acquired.job.media_path == tmp_path / queued.recipe_id / "source.mp4"
    assert acquired.job.caption_text == "Caption from Instagram"


def test_queue_state_writer_updates_status_and_detail_for_the_row() -> None:
    service = _SheetsService({})

    GoogleSheetsRecipeQueueStateWriter(service, spreadsheet_id="queue-sheet").mark(
        _queued_recipe(), QueueStatus.PUBLISHED, "https://docs.google.com/document/d/doc-123/edit"
    )

    assert service.values_api.update_calls == [
        {
            "spreadsheetId": "queue-sheet",
            "range": "'Desserts'!C2:D2",
            "valueInputOption": "RAW",
            "body": {
                "values": [
                    ["published", "https://docs.google.com/document/d/doc-123/edit"]
                ]
            },
        }
    ]


def test_review_document_and_sheet_writer_preserve_review_context() -> None:
    queued = _queued_recipe()
    docs_service = _DocsService()
    from instagram_recipe_transcriber.models import PipelineResult

    document = GoogleDocsRecipeReviewWriter(docs_service).create(
        PipelineResult(
            recipe=_recipe(),
            validation=ValidationArtifact(outcome=RecipeOutcome.REVIEW),
            evidence_segments=(
                EvidenceSegment(
                    evidence_id="caption-1",
                    source_kind=SourceKind.CAPTION,
                    text="Creator caption describing the candidate recipe.",
                ),
            ),
        ),
        queued,
    )
    sheets_service = _SheetsService({})
    GoogleSheetsRecipeReviewWriter(sheets_service, spreadsheet_id="review-sheet").append(
        queued, document
    )

    rendered = str(docs_service.documents_api.batch_calls[0])
    assert "Validation findings" in rendered
    assert "caption (caption-1): Creator caption describing the candidate recipe." in rendered
    assert "OpenAI usage" not in rendered
    assert sheets_service.values_api.append_calls[0]["body"] == {
        "values": [
            [
                str(queued.source_url),
                "Quick weeknight recipe",
                "review",
                str(document.url),
            ]
        ]
    }
