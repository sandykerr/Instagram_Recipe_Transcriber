from __future__ import annotations

import json
from pathlib import Path

from pydantic import HttpUrl

from instagram_recipe_transcriber.acquisition import YtDlpAcquirer, recipe_id_for_url
from instagram_recipe_transcriber.google_adapters import (
    GoogleDocsRecipeWriter,
    GoogleDriveDocumentOrganizer,
    GoogleSheetsRecipeMasterWriter,
    GoogleSheetsRecipeQueueReader,
)
from instagram_recipe_transcriber.models import (
    EvidenceReference,
    Ingredient,
    Instruction,
    QueuedRecipe,
    RecipeCandidate,
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

    def get(self, **kwargs: object) -> _Request:
        self.get_calls.append(kwargs)
        range_name = kwargs["range"]
        assert isinstance(range_name, str)
        return _Request({"values": self._values_by_range[range_name]})

    def append(self, **kwargs: object) -> _Request:
        self.append_calls.append(kwargs)
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
        {"'Desserts'!A2:B": [["", ""], ["https://www.instagram.com/reel/DVJBGzyk8E5/", ""]]}
    )

    queued = GoogleSheetsRecipeQueueReader(
        service, spreadsheet_id="queue-sheet", categories=("Desserts",)
    ).read_next()

    assert queued is not None
    assert queued.category == "Desserts"
    assert queued.queue_row_number == 3
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


def test_yt_dlp_acquirer_returns_video_and_caption_metadata(tmp_path: Path) -> None:
    queued = _queued_recipe()

    def runner(command: list[str]) -> None:
        output_template = Path(command[command.index("--output") + 1])
        output_dir = output_template.parent
        (output_dir / "source.mp4").write_bytes(b"video")
        (output_dir / "source.info.json").write_text(
            json.dumps({"description": "Caption from Instagram"}), encoding="utf-8"
        )

    acquired = YtDlpAcquirer(tmp_path, command_runner=runner).acquire(queued)

    assert acquired.job.media_path == tmp_path / queued.recipe_id / "source.mp4"
    assert acquired.job.caption_text == "Caption from Instagram"
