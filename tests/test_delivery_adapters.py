from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import HttpUrl

from instagram_recipe_transcriber import google_adapters
from instagram_recipe_transcriber.acquisition import YtDlpAcquirer, recipe_id_for_url
from instagram_recipe_transcriber.errors import PipelineOperationalError
from instagram_recipe_transcriber.google_adapters import (
    GoogleDocsRecipeReviewWriter,
    GoogleDocsRecipeWriter,
    GoogleDriveDocumentOrganizer,
    GoogleSheetsRecipeMasterWriter,
    GoogleSheetsRecipeQueueReader,
    GoogleSheetsRecipeQueueStateWriter,
    GoogleSheetsRecipeReviewWriter,
    GoogleSheetsRejectedRecipeWriter,
    GoogleSheetsReviewDecisionReader,
    GoogleSheetsReviewQueueStateWriter,
)
from instagram_recipe_transcriber.interfaces import ReviewQueueStateWriter
from instagram_recipe_transcriber.models import (
    EvidenceReference,
    EvidenceSegment,
    Ingredient,
    Instruction,
    QueuedRecipe,
    QueueStatus,
    RecipeCandidate,
    RecipeDocument,
    RecipeDocumentPresentation,
    RecipeInstructionFormat,
    RecipeOutcome,
    ReviewDecision,
    ReviewDecisionStatus,
    ReviewResolutionArtifact,
    SourceKind,
    ValidationArtifact,
)
from instagram_recipe_transcriber.review_sync import (
    ActiveReviewRow,
    ReviewRemovalAction,
    ReviewSyncConflictReason,
    ReviewSyncPlan,
    ReviewSyncPlanner,
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
        self.batch_calls: list[dict[str, object]] = []

    def spreadsheets(self) -> _SheetsService:
        return self

    def values(self) -> _Values:
        return self.values_api

    def get(self, **kwargs: object) -> _Request:
        return _Request(
            {
                "sheets": [
                    {"properties": {"sheetId": 7, "title": "Desserts"}},
                    {"properties": {"sheetId": 8, "title": "Chef's Picks"}},
                ]
            }
        )

    def batchUpdate(self, **kwargs: object) -> _Request:
        self.batch_calls.append(kwargs)
        return _Request({})


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


def _document_requests(service: _DocsService) -> list[dict[str, Any]]:
    batch_call = cast(dict[str, Any], service.documents_api.batch_calls[0])
    body = cast(dict[str, Any], batch_call["body"])
    return cast(list[dict[str, Any]], body["requests"])


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


def test_sync_snapshot_reads_every_tab_and_preserves_row_numbers() -> None:
    service = _SheetsService(
        {
            "'Desserts'!A2:D": [[], ["https://example.com/pasta", "Dinner", "Review", "old"]],
            "'Chef''s Picks'!A2:D": [["https://example.com/cake", "", ""]],
            "'Desserts'!A2:F": [
                [
                    "https://example.com/pasta",
                    "Dinner",
                    "Approved",
                    "https://docs.google.com/document/d/review/edit",
                ]
            ],
            "'Chef''s Picks'!A2:F": [],
        }
    )
    queue, active = google_adapters.GoogleSheetsReviewSyncSnapshotReader(
        service,
        queue_spreadsheet_id="input",
        review_spreadsheet_id="review",
        categories=("Desserts", "Chef's Picks"),
    ).read()

    assert [(row.category, row.queue_row_number, row.status, row.detail) for row in queue] == [
        ("Desserts", 3, QueueStatus.REVIEW, "old"),
        ("Chef's Picks", 2, QueueStatus.PENDING, None),
    ]
    assert queue[0].recipe_id == recipe_id_for_url("https://example.com/pasta")
    assert [(row.category, row.review_row_number) for row in active] == [("Desserts", 2)]
    assert {(call["spreadsheetId"], call["range"]) for call in service.values_api.get_calls} == {
        ("input", "'Desserts'!A2:D"),
        ("input", "'Chef''s Picks'!A2:D"),
        ("review", "'Desserts'!A2:F"),
        ("review", "'Chef''s Picks'!A2:F"),
    }
    assert (
        not service.values_api.append_calls + service.values_api.update_calls + service.batch_calls
    )


def test_sync_snapshot_preserves_conflicting_terminal_detail_exactly() -> None:
    detail = "https://example.com/final-doc "
    service = _SheetsService(
        {
            "'Desserts'!A2:D": [["https://example.com/pasta", "", "Published", detail]],
            "'Desserts'!A2:F": [],
        }
    )
    queue, active = google_adapters.GoogleSheetsReviewSyncSnapshotReader(
        service,
        queue_spreadsheet_id="input",
        review_spreadsheet_id="review",
        categories=("Desserts",),
    ).read()
    resolution = ReviewResolutionArtifact(
        recipe_id=recipe_id_for_url("https://example.com/pasta"),
        decision=ReviewDecisionStatus.ACCEPTED,
        document=RecipeDocument(
            document_id="final", title="Final", url=HttpUrl("https://example.com/final-doc")
        ),
        master_row_written=True,
    )

    plan = ReviewSyncPlanner(reviews={}, resolutions={resolution.recipe_id: resolution}).plan(
        queue, active
    )

    assert queue[0].detail == detail
    assert [conflict.reason for conflict in plan.conflicts] == [
        ReviewSyncConflictReason.QUEUE_STATE_MISMATCH
    ]
    assert plan.appends == plan.removals == plan.queue_updates == ()


@pytest.mark.parametrize(
    ("sheet", "row"),
    [
        ("queue", ["", "orphan description"]),
        ("queue", ["not-a-url", "", "review"]),
        ("queue", ["https://example.com/pasta", "", "mystery"]),
        ("queue", "not-a-row"),
        ("queue", [123]),
        ("review", ["https://example.com/pasta", "", "review"]),
        ("review", ["https://example.com/pasta", "", "mystery", "https://example.com/doc"]),
        ("review", ["https://example.com/pasta", "", "review", "not-a-doc-url"]),
    ],
)
def test_sync_snapshot_rejects_malformed_nonblank_rows(sheet: str, row: object) -> None:
    values = {"'Desserts'!A2:D": [], "'Desserts'!A2:F": []}
    values["'Desserts'!A2:D" if sheet == "queue" else "'Desserts'!A2:F"] = [row]
    service = _SheetsService(cast(Any, values))
    reader = google_adapters.GoogleSheetsReviewSyncSnapshotReader(
        service,
        queue_spreadsheet_id="input",
        review_spreadsheet_id="review",
        categories=("Desserts",),
    )

    with pytest.raises(PipelineOperationalError, match="Desserts.*2"):
        reader.read()
    assert (
        not service.values_api.append_calls + service.values_api.update_calls + service.batch_calls
    )


def _sync_removal(category: str, number: int) -> ReviewRemovalAction:
    return ReviewRemovalAction(
        active_row=ActiveReviewRow(
            recipe_id=f"pasta-{number}",
            source_url=HttpUrl(f"https://example.com/{number}"),
            category=category,
            review_row_number=number,
            review_document_url=HttpUrl("https://example.com/review"),
        ),
        resolution=ReviewResolutionArtifact(
            recipe_id=f"pasta-{number}",
            decision=ReviewDecisionStatus.ACCEPTED,
            document=RecipeDocument(
                document_id="final", title="Pasta", url=HttpUrl("https://example.com/final")
            ),
        ),
    )


def test_sync_applier_deletes_bottom_up_per_exact_tab() -> None:
    service = _SheetsService(
        {
            "'Desserts'!A3:F3": [["https://example.com/3"]],
            "'Desserts'!A5:F5": [["https://example.com/5"]],
            "'Chef''s Picks'!A2:F2": [["https://example.com/2"]],
        }
    )
    applier = google_adapters.GoogleSheetsReviewSyncApplier(
        service,
        queue_spreadsheet_id="input",
        review_spreadsheet_id="review",
    )
    applier.apply(
        ReviewSyncPlan(
            removals=(
                _sync_removal("Desserts", 3),
                _sync_removal("Desserts", 5),
                _sync_removal("Chef's Picks", 2),
            )
        )
    )

    ranges = [
        cast(Any, call)["body"]["requests"][0]["deleteDimension"]["range"]
        for call in service.batch_calls
    ]
    assert ranges == [
        {"sheetId": 8, "dimension": "ROWS", "startIndex": 1, "endIndex": 2},
        {"sheetId": 7, "dimension": "ROWS", "startIndex": 4, "endIndex": 5},
        {"sheetId": 7, "dimension": "ROWS", "startIndex": 2, "endIndex": 3},
    ]
    assert all(call["spreadsheetId"] == "review" for call in service.batch_calls)


@pytest.mark.parametrize("live", [[], [["https://example.com/changed"]]])
def test_sync_deletion_revalidation_refuses_a_changed_or_missing_url(
    live: list[list[str]],
) -> None:
    service = _SheetsService({"'Desserts'!A3:F3": live})
    applier = google_adapters.GoogleSheetsReviewSyncApplier(
        service,
        queue_spreadsheet_id="input",
        review_spreadsheet_id="review",
    )

    with pytest.raises(PipelineOperationalError, match="changed"):
        applier.apply(ReviewSyncPlan(removals=(_sync_removal("Desserts", 3),)))
    assert service.batch_calls == []


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
    requests = _document_requests(docs_service)
    paragraph_styles = [
        request["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"]
        for request in requests
        if "updateParagraphStyle" in request
    ]
    assert paragraph_styles == ["HEADING_1", "HEADING_2", "HEADING_2"]


def test_google_document_writer_renders_manual_servings_and_nutrition_notes() -> None:
    docs_service = _DocsService()

    GoogleDocsRecipeWriter(docs_service).create(
        _recipe(),
        _queued_recipe(),
        RecipeDocumentPresentation(
            servings_text="4",
            nutrition_notes="Per serving — 338 kcal | Protein 22 g",
        ),
    )

    text = str(docs_service.documents_api.batch_calls[0])
    assert "Servings: 4" in text
    assert "Nutrition (manual): Per serving — 338 kcal | Protein 22 g" in text
    requests = _document_requests(docs_service)
    bold_requests = [
        request["updateTextStyle"] for request in requests if "updateTextStyle" in request
    ]
    assert len(bold_requests) == 2
    assert all(request["textStyle"] == {"bold": True} for request in bold_requests)


def test_review_decision_reader_treats_approved_as_accepted() -> None:
    service = _SheetsService(
        {
            "'Desserts'!A2:F": [
                [
                    "https://www.instagram.com/reel/DVJBGzyk8E5/",
                    "Quick pasta",
                    "Approved",
                    "https://docs.google.com/document/d/review-doc/edit",
                    "4",
                    "Per serving — 338 kcal | Protein 22 g",
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
    assert decision.servings_text == "4"
    assert decision.nutrition_notes == "Per serving — 338 kcal | Protein 22 g"


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
            "body": {"values": [["published", "https://docs.google.com/document/d/doc-123/edit"]]},
        }
    ]


def _review_decision() -> ReviewDecision:
    return ReviewDecision(
        recipe_id="queued-recipe",
        source_url=_queued_recipe().source_url,
        category="Chef's Desserts",
        review_row_number=20,
        decision=ReviewDecisionStatus.ACCEPTED,
        review_document_url=HttpUrl("https://docs.google.com/document/d/review/edit"),
    )


@pytest.mark.parametrize("status", ["published", "rejected"])
def test_review_queue_writer_resolves_exact_url_in_category_at_its_current_row(
    status: str,
) -> None:
    service = _SheetsService(
        {
            "'Chef''s Desserts'!A2:D": [
                ["https://www.instagram.com/reel/DVJBGzyk8E5/?extra=1", "", "review"],
                [],
                ["https://www.instagram.com/reel/DVJBGzyk8E5/", "", "review"],
            ]
        }
    )
    writer: ReviewQueueStateWriter = GoogleSheetsReviewQueueStateWriter(
        service, spreadsheet_id="queue-sheet"
    )

    writer.resolve(_review_decision(), QueueStatus(status), "Resolved document URL")

    assert service.values_api.get_calls == [
        {"spreadsheetId": "queue-sheet", "range": "'Chef''s Desserts'!A2:D"}
    ]
    assert service.values_api.update_calls == [
        {
            "spreadsheetId": "queue-sheet",
            "range": "'Chef''s Desserts'!C4:D4",
            "valueInputOption": "RAW",
            "body": {"values": [[status, "Resolved document URL"]]},
        }
    ]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [["https://www.instagram.com/reel/DVJBGzyk8E5/?extra=1"]],
        [
            ["https://www.instagram.com/reel/DVJBGzyk8E5/"],
            ["https://www.instagram.com/reel/DVJBGzyk8E5/"],
        ],
    ],
    ids=["empty-tab", "missing-exact-url", "duplicate-url"],
)
def test_review_queue_writer_requires_exactly_one_match_before_updating(
    rows: list[list[str]],
) -> None:
    service = _SheetsService({"'Chef''s Desserts'!A2:D": rows})
    writer = GoogleSheetsReviewQueueStateWriter(service, spreadsheet_id="queue-sheet")

    with pytest.raises(PipelineOperationalError):
        writer.resolve(_review_decision(), QueueStatus.PUBLISHED, "Resolved document URL")

    assert service.values_api.update_calls == []


@pytest.mark.parametrize("status", [QueueStatus.PUBLISHED, QueueStatus.REJECTED])
@pytest.mark.parametrize(
    "current",
    ["", "   ", "Pending", "Processing", "Error", "unknown", "Published", "Rejected"],
)
def test_review_queue_writer_refuses_protected_or_conflicting_input_status_without_writes(
    status: QueueStatus, current: str
) -> None:
    service = _SheetsService(
        {"'Chef''s Desserts'!A2:D": [[str(_review_decision().source_url), "", current, "old"]]}
    )

    with pytest.raises(PipelineOperationalError):
        GoogleSheetsReviewQueueStateWriter(service, spreadsheet_id="queue-sheet").resolve(
            _review_decision(), status, "Resolved document URL"
        )

    assert service.values_api.update_calls == []


@pytest.mark.parametrize("status", [QueueStatus.PUBLISHED, QueueStatus.REJECTED])
def test_review_queue_writer_already_correct_terminal_state_is_a_noop(status: QueueStatus) -> None:
    service = _SheetsService(
        {
            "'Chef''s Desserts'!A2:D": [
                [str(_review_decision().source_url), "", status.value, "Resolved document URL"]
            ]
        }
    )

    GoogleSheetsReviewQueueStateWriter(service, spreadsheet_id="queue-sheet").resolve(
        _review_decision(), status, "Resolved document URL"
    )

    assert service.values_api.update_calls == []


@pytest.mark.parametrize("current", [None, 17, [], {}])
def test_review_queue_writer_refuses_non_text_status_without_writes(current: object) -> None:
    service = _SheetsService(
        {"'Chef''s Desserts'!A2:D": [[str(_review_decision().source_url), "", cast(Any, current)]]}
    )

    with pytest.raises(PipelineOperationalError):
        GoogleSheetsReviewQueueStateWriter(service, spreadsheet_id="queue-sheet").resolve(
            _review_decision(), QueueStatus.PUBLISHED, "Resolved document URL"
        )

    assert service.values_api.update_calls == []


def test_review_queue_writer_refuses_missing_status_without_writes() -> None:
    service = _SheetsService({"'Chef''s Desserts'!A2:D": [[str(_review_decision().source_url)]]})

    with pytest.raises(PipelineOperationalError):
        GoogleSheetsReviewQueueStateWriter(service, spreadsheet_id="queue-sheet").resolve(
            _review_decision(), QueueStatus.PUBLISHED, "Resolved document URL"
        )

    assert service.values_api.update_calls == []


@pytest.mark.parametrize("status", [QueueStatus.PUBLISHED, QueueStatus.REJECTED])
def test_review_queue_writer_refuses_opposite_terminal_state_with_matching_detail(
    status: QueueStatus,
) -> None:
    current = QueueStatus.REJECTED if status is QueueStatus.PUBLISHED else QueueStatus.PUBLISHED
    service = _SheetsService(
        {
            "'Chef''s Desserts'!A2:D": [
                [str(_review_decision().source_url), "", current.value, "Resolved document URL"]
            ]
        }
    )

    with pytest.raises(PipelineOperationalError):
        GoogleSheetsReviewQueueStateWriter(service, spreadsheet_id="queue-sheet").resolve(
            _review_decision(), status, "Resolved document URL"
        )

    assert service.values_api.update_calls == []


@pytest.mark.parametrize("status", [QueueStatus.PENDING, QueueStatus.REVIEW, QueueStatus.ERROR])
def test_review_queue_writer_refuses_non_terminal_target_without_writes(
    status: QueueStatus,
) -> None:
    service = _SheetsService(
        {"'Chef''s Desserts'!A2:D": [[str(_review_decision().source_url), "", "Review"]]}
    )

    with pytest.raises(PipelineOperationalError):
        GoogleSheetsReviewQueueStateWriter(service, spreadsheet_id="queue-sheet").resolve(
            _review_decision(), status, "Resolved document URL"
        )

    assert service.values_api.update_calls == []


def test_queue_reader_skips_terminal_rejected_rows() -> None:
    service = _SheetsService(
        {
            "'Desserts'!A2:D": [
                ["https://www.instagram.com/reel/rejected/", "", "rejected"],
                ["https://www.instagram.com/reel/pending/", "", "pending"],
            ]
        }
    )

    queued = GoogleSheetsRecipeQueueReader(
        service, spreadsheet_id="queue-sheet", categories=("Desserts",)
    ).read_next()

    assert queued is not None
    assert queued.queue_row_number == 3
    assert str(queued.source_url) == "https://www.instagram.com/reel/pending/"


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
    requests = _document_requests(docs_service)
    heading_styles = [
        request["updateParagraphStyle"]["paragraphStyle"]["namedStyleType"]
        for request in requests
        if "updateParagraphStyle" in request
    ]
    assert heading_styles == [
        "HEADING_1",
        "HEADING_2",
        "HEADING_2",
        "HEADING_2",
        "HEADING_2",
        "HEADING_2",
    ]
    evidence_style = next(
        request["updateTextStyle"] for request in requests if "updateTextStyle" in request
    )
    evidence_range = evidence_style["range"]
    inserted_text = requests[0]["insertText"]["text"]
    assert (
        inserted_text[evidence_range["startIndex"] - 1 : evidence_range["endIndex"] - 1]
        == "caption (caption-1):"
    )
    assert sheets_service.values_api.append_calls[0]["body"] == {
        "values": [
            [
                str(queued.source_url),
                "Quick weeknight recipe",
                "review",
                str(document.url),
                "",
                "",
            ]
        ]
    }


def test_rejected_writer_preserves_manual_servings_and_nutrition_notes() -> None:
    sheets_service = _SheetsService({})
    decision = ReviewDecision(
        recipe_id="queued-recipe",
        source_url=_queued_recipe().source_url,
        category="Desserts",
        review_row_number=2,
        decision=ReviewDecisionStatus.REJECTED,
        review_document_url=HttpUrl("https://docs.google.com/document/d/review/edit"),
        servings_text="4",
        nutrition_notes="Per serving — 338 kcal",
    )

    GoogleSheetsRejectedRecipeWriter(sheets_service, spreadsheet_id="rejected-sheet").append(
        decision,
        RecipeDocument(
            document_id="review-doc",
            title="REVIEW — Quick pasta",
            url=HttpUrl("https://docs.google.com/document/d/review-doc/edit"),
        ),
    )

    assert sheets_service.values_api.append_calls[0]["body"] == {
        "values": [
            [
                str(decision.source_url),
                "",
                "rejected",
                "https://docs.google.com/document/d/review-doc/edit",
                "4",
                "Per serving — 338 kcal",
            ]
        ]
    }
