"""Injectable Google Sheets, Docs, and Drive adapters for recipe delivery."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import HttpUrl

from .acquisition import recipe_id_for_url
from .errors import PipelineOperationalError
from .models import (
    EvidenceSegment,
    PipelineResult,
    QueuedRecipe,
    QueueStatus,
    RecipeCandidate,
    RecipeDocument,
    RecipeDocumentPresentation,
    RecipeInstructionFormat,
    ReviewDecision,
    ReviewDecisionStatus,
)
from .review_sync import ActiveReviewRow, ReviewQueueRow, ReviewSyncPlan


class GoogleSheetsReviewSyncSnapshotReader:
    """Read every configured Input/Review tab without discarding malformed rows."""

    def __init__(
        self,
        service: Any,
        *,
        queue_spreadsheet_id: str,
        review_spreadsheet_id: str,
        categories: Iterable[str],
    ) -> None:
        self._service = service
        self._queue_id = queue_spreadsheet_id
        self._review_id = review_spreadsheet_id
        self._categories = tuple(categories)
        if not self._queue_id or not self._review_id or not self._categories:
            raise ValueError("Input/Review spreadsheet IDs and categories are required")

    def read(self) -> tuple[tuple[ReviewQueueRow, ...], tuple[ActiveReviewRow, ...]]:
        queue: list[ReviewQueueRow] = []
        active: list[ActiveReviewRow] = []
        for category in self._categories:
            queue_values = _sync_values(self._service, self._queue_id, _queue_range(category))
            for number, row in enumerate(queue_values, start=2):
                if not any(cell.strip() for cell in row):
                    continue
                try:
                    queue.append(
                        ReviewQueueRow(
                            recipe_id=recipe_id_for_url(row[0].strip()),
                            source_url=HttpUrl(row[0].strip()),
                            category=category,
                            queue_row_number=number,
                            status=_queue_status(row[2] if len(row) > 2 else ""),
                            description=_optional_cell(row, 1),
                            detail=_optional_cell(row, 3),
                        )
                    )
                except (ValueError, PipelineOperationalError) as error:
                    raise PipelineOperationalError(
                        f"Malformed Input row in {category!r} at row {number}"
                    ) from error
            review_values = _sync_values(self._service, self._review_id, _review_range(category))
            for number, row in enumerate(review_values, start=2):
                if not any(cell.strip() for cell in row):
                    continue
                try:
                    if len(row) < 4 or row[2].strip().lower() not in {
                        "review",
                        "accepted",
                        "approved",
                        "rejected",
                    }:
                        raise ValueError("Missing or unknown review status/document")
                    active.append(
                        ActiveReviewRow(
                            recipe_id=recipe_id_for_url(row[0].strip()),
                            source_url=HttpUrl(row[0].strip()),
                            category=category,
                            review_row_number=number,
                            description=_optional_cell(row, 1),
                            review_document_url=HttpUrl(row[3].strip()),
                        )
                    )
                except ValueError as error:
                    raise PipelineOperationalError(
                        f"Malformed Review row in {category!r} at row {number}"
                    ) from error
        return tuple(queue), tuple(active)


class GoogleSheetsReviewSyncApplier:
    """Apply a conflict-free plan; revalidate and delete bottom-up within tabs.

    Local checkpoint flags are deliberately untouched: the next live snapshot
    remains authoritative, including after a partially completed provider call.
    """

    def __init__(
        self,
        service: Any,
        *,
        queue_spreadsheet_id: str,
        review_spreadsheet_id: str,
    ) -> None:
        self._service = service
        self._queue_id = queue_spreadsheet_id
        self._review_id = review_spreadsheet_id
        if not self._queue_id or not self._review_id:
            raise ValueError("Input/Review spreadsheet IDs are required")

    def apply(self, plan: ReviewSyncPlan) -> None:
        if plan.conflicts:
            raise PipelineOperationalError("Cannot execute a review sync plan with conflicts")
        # Delete before appending so appends cannot affect the original locations.
        for action in sorted(
            plan.removals,
            key=lambda action: (action.active_row.category, -action.active_row.review_row_number),
        ):
            self._remove(action.active_row)
        writer = GoogleSheetsRecipeReviewWriter(self._service, spreadsheet_id=self._review_id)
        for append in plan.appends:
            writer.append(_sync_queued_recipe(append.queue_row), append.review.document)
        state_writer = GoogleSheetsRecipeQueueStateWriter(
            self._service,
            spreadsheet_id=self._queue_id,
        )
        for update in plan.queue_updates:
            state_writer.mark(_sync_queued_recipe(update.queue_row), update.status, update.detail)

    def _remove(self, row: ActiveReviewRow) -> None:
        try:
            metadata = (
                self._service.spreadsheets()
                .get(
                    spreadsheetId=self._review_id,
                    fields="sheets(properties(sheetId,title))",
                )
                .execute()
            )
            matches = [
                sheet["properties"]["sheetId"]
                for sheet in metadata["sheets"]
                if sheet["properties"]["title"] == row.category
            ]
            if len(matches) != 1:
                raise ValueError("Review category must match exactly one tab")
        except Exception as error:
            raise PipelineOperationalError(
                f"Cannot resolve Review tab {row.category!r} for deletion"
            ) from error
        number = row.review_row_number
        live = _sync_values(
            self._service,
            self._review_id,
            f"{_a1_tab(row.category)}!A{number}:F{number}",
            start_row=number,
        )
        if len(live) != 1 or not live[0] or live[0][0] != str(row.source_url):
            raise PipelineOperationalError(
                f"Review row changed in {row.category!r} at row {number}; refusing deletion"
            )
        try:
            self._service.spreadsheets().batchUpdate(
                spreadsheetId=self._review_id,
                body={
                    "requests": [
                        {
                            "deleteDimension": {
                                "range": {
                                    "sheetId": matches[0],
                                    "dimension": "ROWS",
                                    "startIndex": number - 1,
                                    "endIndex": number,
                                }
                            }
                        }
                    ]
                },
            ).execute()
        except Exception as error:
            raise PipelineOperationalError("Cannot remove resolved Review Sheet row") from error


def _sync_queued_recipe(row: ReviewQueueRow) -> QueuedRecipe:
    return QueuedRecipe.model_validate(row.model_dump(exclude={"detail"}))


def _sync_values(
    service: Any,
    spreadsheet_id: str,
    range_name: str,
    *,
    start_row: int = 2,
) -> list[list[str]]:
    """Strict Sheets boundary: never filter out malformed rows or shift locations."""
    try:
        response = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=range_name,
            )
            .execute()
        )
        values = response.get("values", [])
    except Exception as error:
        raise PipelineOperationalError(f"Cannot read sync snapshot {range_name}") from error
    if not isinstance(values, list):
        raise PipelineOperationalError(f"Invalid sync values in {range_name}")
    rows: list[list[str]] = []
    for number, row in enumerate(values, start=start_row):
        if not isinstance(row, list) or any(not isinstance(cell, str) for cell in row):
            raise PipelineOperationalError(f"Malformed sync row in {range_name} at row {number}")
        rows.append(row)
    return rows


class GoogleSheetsRecipeQueueReader:
    """Reads the next eligible URL from configured category tabs.

    Queue columns are URL, optional description, status, and optional detail.
    Blank status values are treated as pending to support rows entered by hand.
    Processing rows are also eligible so an interrupted local run can resume.
    """

    def __init__(self, service: Any, *, spreadsheet_id: str, categories: Iterable[str]) -> None:
        self._service = service
        self._spreadsheet_id = spreadsheet_id
        self._categories = tuple(categories)
        if not self._spreadsheet_id or not self._categories:
            raise ValueError("spreadsheet_id and at least one category are required")

    def read_next(self) -> QueuedRecipe | None:
        for category in self._categories:
            values = self._get_values(_queue_range(category))
            for row_number, row in enumerate(values, start=2):
                url = row[0].strip() if row else ""
                if not url:
                    continue
                description = row[1].strip() if len(row) > 1 else ""
                status = _queue_status(row[2] if len(row) > 2 else "")
                if status not in (QueueStatus.PENDING, QueueStatus.PROCESSING):
                    continue
                return QueuedRecipe(
                    recipe_id=recipe_id_for_url(url),
                    source_url=HttpUrl(url),
                    category=category,
                    queue_row_number=row_number,
                    description=description or None,
                    status=status,
                )
        return None

    def _get_values(self, range_name: str) -> list[list[str]]:
        try:
            response = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self._spreadsheet_id, range=range_name)
                .execute()
            )
        except Exception as error:
            raise PipelineOperationalError("Cannot read the recipe queue spreadsheet") from error
        values = response.get("values", [])
        if not isinstance(values, list):
            raise PipelineOperationalError("Recipe queue returned invalid values")
        return [row for row in values if isinstance(row, list)]


class GoogleSheetsRecipeQueueStateWriter:
    """Updates the status and optional detail cells for a queue row."""

    def __init__(self, service: Any, *, spreadsheet_id: str) -> None:
        if not spreadsheet_id:
            raise ValueError("spreadsheet_id is required")
        self._service = service
        self._spreadsheet_id = spreadsheet_id

    def mark(
        self,
        queued_recipe: QueuedRecipe,
        status: QueueStatus,
        detail: str | None = None,
    ) -> None:
        range_name = _queue_state_range(queued_recipe.category, queued_recipe.queue_row_number)
        try:
            (
                self._service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=self._spreadsheet_id,
                    range=range_name,
                    valueInputOption="RAW",
                    body={"values": [[status.value, detail or ""]]},
                )
                .execute()
            )
        except Exception as error:
            raise PipelineOperationalError("Cannot update the recipe queue status") from error


class GoogleSheetsReviewQueueStateWriter:
    """Resolves the unique queue row matching a review's category and exact URL."""

    def __init__(self, service: Any, *, spreadsheet_id: str) -> None:
        if not spreadsheet_id:
            raise ValueError("spreadsheet_id is required")
        self._service = service
        self._spreadsheet_id = spreadsheet_id

    def resolve(self, decision: ReviewDecision, status: QueueStatus, detail: str) -> None:
        try:
            response = (
                self._service.spreadsheets()
                .values()
                .get(spreadsheetId=self._spreadsheet_id, range=_queue_range(decision.category))
                .execute()
            )
        except Exception as error:
            raise PipelineOperationalError("Cannot read the recipe queue spreadsheet") from error
        values = response.get("values", [])
        if not isinstance(values, list) or any(not isinstance(row, list) for row in values):
            raise PipelineOperationalError("Recipe queue returned invalid values")
        matches = [
            row_number
            for row_number, row in enumerate(values, start=2)
            if row and row[0] == str(decision.source_url)
        ]
        if len(matches) != 1:
            raise PipelineOperationalError(
                f"Expected exactly one queue row for {decision.source_url} "
                f"in {decision.category!r}; found {len(matches)}"
            )
        try:
            (
                self._service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=self._spreadsheet_id,
                    range=_queue_state_range(decision.category, matches[0]),
                    valueInputOption="RAW",
                    body={"values": [[status.value, detail]]},
                )
                .execute()
            )
        except Exception as error:
            raise PipelineOperationalError("Cannot update the recipe queue status") from error


class GoogleSheetsRecipeMasterWriter:
    """Appends a recipe title and Google Doc URL to the matching master tab."""

    def __init__(self, service: Any, *, spreadsheet_id: str) -> None:
        if not spreadsheet_id:
            raise ValueError("spreadsheet_id is required")
        self._service = service
        self._spreadsheet_id = spreadsheet_id

    def append(self, queued_recipe: QueuedRecipe, document: RecipeDocument) -> None:
        try:
            (
                self._service.spreadsheets()
                .values()
                .append(
                    spreadsheetId=self._spreadsheet_id,
                    range=_master_range(queued_recipe.category),
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={"values": [[document.title, str(document.url)]]},
                )
                .execute()
            )
        except Exception as error:
            raise PipelineOperationalError(
                "Cannot append to the recipe master spreadsheet"
            ) from error


class GoogleSheetsRecipeReviewWriter:
    """Appends a review Doc link to the matching category tab in the Review Sheet."""

    def __init__(self, service: Any, *, spreadsheet_id: str) -> None:
        if not spreadsheet_id:
            raise ValueError("spreadsheet_id is required")
        self._service = service
        self._spreadsheet_id = spreadsheet_id

    def append(self, queued_recipe: QueuedRecipe, document: RecipeDocument) -> None:
        try:
            (
                self._service.spreadsheets()
                .values()
                .append(
                    spreadsheetId=self._spreadsheet_id,
                    range=_review_range(queued_recipe.category),
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={
                        "values": [
                            [
                                str(queued_recipe.source_url),
                                queued_recipe.description or "",
                                QueueStatus.REVIEW.value,
                                str(document.url),
                                "",
                                "",
                            ]
                        ]
                    },
                )
                .execute()
            )
        except Exception as error:
            raise PipelineOperationalError(
                "Cannot append to the recipe review spreadsheet"
            ) from error


class GoogleSheetsReviewDecisionReader:
    """Reads manually accepted or rejected rows from Review Sheet category tabs."""

    def __init__(self, service: Any, *, spreadsheet_id: str, categories: Iterable[str]) -> None:
        self._service = service
        self._spreadsheet_id = spreadsheet_id
        self._categories = tuple(categories)

    def read_next(self) -> ReviewDecision | None:
        reader = GoogleSheetsRecipeQueueReader(
            self._service,
            spreadsheet_id=self._spreadsheet_id,
            categories=self._categories,
        )
        for category in self._categories:
            for row_number, row in enumerate(reader._get_values(_review_range(category)), start=2):
                if len(row) < 4:
                    continue
                decision = _review_decision(row[2])
                if decision is None:
                    continue
                url, detail = row[0].strip(), row[3].strip()
                if not url or not detail:
                    raise PipelineOperationalError(
                        "Accepted/rejected review row is missing URL or Review Doc URL"
                    )
                return ReviewDecision(
                    recipe_id=recipe_id_for_url(url),
                    source_url=HttpUrl(url),
                    category=category,
                    review_row_number=row_number,
                    description=row[1].strip() or None,
                    decision=decision,
                    review_document_url=HttpUrl(detail),
                    servings_text=_optional_cell(row, 4),
                    nutrition_notes=_optional_cell(row, 5),
                )
        return None


class GoogleSheetsReviewRowRemover:
    """Deletes one resolved Review Sheet row after its local checkpoint is saved."""

    def __init__(self, service: Any, *, spreadsheet_id: str) -> None:
        self._service = service
        self._spreadsheet_id = spreadsheet_id

    def remove(self, decision: ReviewDecision) -> None:
        try:
            metadata = self._service.spreadsheets().get(
                spreadsheetId=self._spreadsheet_id,
                fields="sheets(properties(sheetId,title))",
            ).execute()
            sheet_id = next(
                sheet["properties"]["sheetId"]
                for sheet in metadata["sheets"]
                if sheet["properties"]["title"] == decision.category
            )
            (
                self._service.spreadsheets()
                .batchUpdate(
                    spreadsheetId=self._spreadsheet_id,
                    body={
                        "requests": [
                            {
                                "deleteDimension": {
                                    "range": {
                                        "sheetId": sheet_id,
                                        "dimension": "ROWS",
                                        "startIndex": decision.review_row_number - 1,
                                        "endIndex": decision.review_row_number,
                                    }
                                }
                            }
                        ]
                    },
                )
                .execute()
            )
        except Exception as error:
            raise PipelineOperationalError("Cannot remove resolved Review Sheet row") from error


class GoogleSheetsRejectedRecipeWriter:
    """Archives a rejected review in the matching category tab."""

    def __init__(self, service: Any, *, spreadsheet_id: str) -> None:
        self._service = service
        self._spreadsheet_id = spreadsheet_id

    def append(self, decision: ReviewDecision, document: RecipeDocument) -> None:
        try:
            (
                self._service.spreadsheets()
                .values()
                .append(
                    spreadsheetId=self._spreadsheet_id,
                    range=_review_range(decision.category),
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={
                        "values": [
                            [
                                str(decision.source_url),
                                decision.description or "",
                                ReviewDecisionStatus.REJECTED.value,
                                str(document.url),
                                decision.servings_text or "",
                                decision.nutrition_notes or "",
                            ]
                        ]
                    },
                )
                .execute()
            )
        except Exception as error:
            raise PipelineOperationalError(
                "Cannot append to rejected recipe spreadsheet"
            ) from error


class GoogleDocsRecipeWriter:
    """Creates a plain, evidence-preserving Google Doc for a validated recipe."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def create(
        self,
        recipe: RecipeCandidate,
        queued_recipe: QueuedRecipe,
        presentation: RecipeDocumentPresentation | None = None,
    ) -> RecipeDocument:
        if recipe.title is None:
            raise ValueError("A recipe title is required before creating a Google Doc")
        try:
            created = self._service.documents().create(body={"title": recipe.title}).execute()
            document_id = created["documentId"]
            rendered = _render(recipe, queued_recipe, presentation)
            self._service.documents().batchUpdate(
                documentId=document_id, body={"requests": rendered.requests()}
            ).execute()
        except Exception as error:
            raise PipelineOperationalError("Cannot create the recipe Google Doc") from error
        return RecipeDocument(
            document_id=document_id,
            title=recipe.title,
            url=HttpUrl(f"https://docs.google.com/document/d/{document_id}/edit"),
        )


class GoogleDocsRecipeReviewWriter:
    """Creates a review document containing the candidate and validation findings."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def create(self, result: PipelineResult, queued_recipe: QueuedRecipe) -> RecipeDocument:
        title = f"REVIEW — {result.recipe.title or queued_recipe.recipe_id}"
        try:
            created = self._service.documents().create(body={"title": title}).execute()
            document_id = created["documentId"]
            rendered = _render_review(result, queued_recipe)
            self._service.documents().batchUpdate(
                documentId=document_id, body={"requests": rendered.requests()}
            ).execute()
        except Exception as error:
            raise PipelineOperationalError("Cannot create the recipe review Google Doc") from error
        return RecipeDocument(
            document_id=document_id,
            title=title,
            url=HttpUrl(f"https://docs.google.com/document/d/{document_id}/edit"),
        )


class GoogleDriveDocumentOrganizer:
    """Moves a Google Doc into one configured Drive folder."""

    def __init__(self, service: Any, *, folder_id: str) -> None:
        if not folder_id:
            raise ValueError("folder_id is required")
        self._service = service
        self._folder_id = folder_id

    def move_to_folder(self, document: RecipeDocument) -> RecipeDocument:
        try:
            current = (
                self._service.files().get(fileId=document.document_id, fields="parents").execute()
            )
            parents = current.get("parents", [])
            self._service.files().update(
                fileId=document.document_id,
                addParents=self._folder_id,
                removeParents=",".join(parents),
                fields="id,parents",
            ).execute()
        except Exception as error:
            raise PipelineOperationalError(
                "Cannot move recipe Google Doc into Drive folder"
            ) from error
        return document.model_copy(update={"folder_id": self._folder_id})


def _queue_range(category: str) -> str:
    return f"{_a1_tab(category)}!A2:D"


def _review_range(category: str) -> str:
    return f"{_a1_tab(category)}!A2:F"


def _queue_state_range(category: str, row_number: int) -> str:
    return f"{_a1_tab(category)}!C{row_number}:D{row_number}"


def _master_range(category: str) -> str:
    return f"{_a1_tab(category)}!A:B"


def _a1_tab(category: str) -> str:
    return "'" + category.replace("'", "''") + "'"


def _queue_status(value: str) -> QueueStatus:
    normalized = value.strip().lower()
    if not normalized:
        return QueueStatus.PENDING
    try:
        return QueueStatus(normalized)
    except ValueError as error:
        raise PipelineOperationalError(f"Unknown queue status: {value!r}") from error


def _review_decision(value: str) -> ReviewDecisionStatus | None:
    """Read the Review Sheet's human-friendly decision words.

    The sheet historically used ``Accepted``; ``Approved`` is an equally clear
    human action and maps to the same durable internal decision.
    """

    normalized = value.strip().lower()
    if normalized in {"accepted", "approved"}:
        return ReviewDecisionStatus.ACCEPTED
    if normalized == ReviewDecisionStatus.REJECTED.value:
        return ReviewDecisionStatus.REJECTED
    return None


def _optional_cell(row: list[str], index: int) -> str | None:
    if len(row) <= index:
        return None
    return row[index].strip() or None


@dataclass(frozen=True)
class _ParagraphStyleRange:
    start_offset: int
    end_offset: int
    named_style_type: str


@dataclass(frozen=True)
class _TextStyleRange:
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class _RenderedDocument:
    text: str
    paragraph_styles: tuple[_ParagraphStyleRange, ...]
    bold_ranges: tuple[_TextStyleRange, ...]

    def requests(self) -> list[dict[str, object]]:
        requests: list[dict[str, object]] = [
            {"insertText": {"endOfSegmentLocation": {}, "text": self.text}}
        ]
        requests.extend(
            {
                "updateParagraphStyle": {
                    "range": self._docs_range(style.start_offset, style.end_offset),
                    "paragraphStyle": {"namedStyleType": style.named_style_type},
                    "fields": "namedStyleType",
                }
            }
            for style in self.paragraph_styles
        )
        requests.extend(
            {
                "updateTextStyle": {
                    "range": self._docs_range(style.start_offset, style.end_offset),
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            }
            for style in self.bold_ranges
        )
        return requests

    def _docs_range(self, start_offset: int, end_offset: int) -> dict[str, int]:
        return {
            "startIndex": 1 + _utf16_length(self.text[:start_offset]),
            "endIndex": 1 + _utf16_length(self.text[:end_offset]),
        }


class _DocumentRenderBuilder:
    def __init__(self) -> None:
        self._lines: list[str] = []
        self._paragraph_styles: list[_ParagraphStyleRange] = []
        self._bold_ranges: list[_TextStyleRange] = []
        self._offset = 0

    def line(
        self,
        text: str = "",
        *,
        named_style_type: str | None = None,
        bold_prefix_length: int = 0,
        bold_prefix_start_offset: int = 0,
    ) -> None:
        start_offset = self._offset
        self._lines.append(text + "\n")
        self._offset += len(text) + 1
        if named_style_type is not None:
            self._paragraph_styles.append(
                _ParagraphStyleRange(start_offset, self._offset, named_style_type)
            )
        if bold_prefix_length:
            self._bold_ranges.append(
                _TextStyleRange(
                    start_offset + bold_prefix_start_offset,
                    start_offset + bold_prefix_start_offset + bold_prefix_length,
                )
            )

    def build(self) -> _RenderedDocument:
        return _RenderedDocument(
            text="".join(self._lines),
            paragraph_styles=tuple(self._paragraph_styles),
            bold_ranges=tuple(self._bold_ranges),
        )


def _utf16_length(text: str) -> int:
    """Return the UTF-16 code-unit count used by Google Docs text indices."""

    return len(text.encode("utf-16-le")) // 2


def _render(
    recipe: RecipeCandidate,
    queued_recipe: QueuedRecipe,
    presentation: RecipeDocumentPresentation | None = None,
) -> _RenderedDocument:
    builder = _DocumentRenderBuilder()
    builder.line(recipe.title or "Untitled recipe", named_style_type="HEADING_1")
    builder.line()
    builder.line(f"Source: {queued_recipe.source_url}")
    if queued_recipe.description:
        builder.line(f"Queue description: {queued_recipe.description}")
    builder.line()
    builder.line("Ingredients", named_style_type="HEADING_2")
    for ingredient in recipe.ingredients:
        builder.line(f"- {ingredient.original_text}")
    if presentation is not None and presentation.servings_text:
        builder.line()
        builder.line(
            f"Servings: {presentation.servings_text}", bold_prefix_length=len("Servings:")
        )
    if presentation is not None and presentation.nutrition_notes:
        builder.line()
        nutrition_label = "Nutrition (manual):"
        builder.line(
            f"{nutrition_label} {presentation.nutrition_notes}",
            bold_prefix_length=len(nutrition_label),
        )
    builder.line()
    builder.line("Instructions", named_style_type="HEADING_2")
    if (
        presentation is not None
        and presentation.instruction_format is RecipeInstructionFormat.RAW_TRANSCRIPT
    ):
        assert presentation.raw_instruction_text is not None
        builder.line(presentation.raw_instruction_text)
    else:
        for instruction in recipe.instructions:
            builder.line(f"{instruction.sequence}. {instruction.original_text}")
    return builder.build()


def _render_review(result: PipelineResult, queued_recipe: QueuedRecipe) -> _RenderedDocument:
    builder = _DocumentRenderBuilder()
    builder.line("Recipe review", named_style_type="HEADING_1")
    builder.line()
    builder.line(f"Source: {queued_recipe.source_url}")
    builder.line(f"Category: {queued_recipe.category}")
    if queued_recipe.description:
        builder.line(f"Queue description: {queued_recipe.description}")
    builder.line()
    builder.line("Validation findings", named_style_type="HEADING_2")
    for finding in result.validation.findings:
        builder.line(f"- [{finding.code}] {finding.message}")
    builder.line()
    builder.line("Candidate title", named_style_type="HEADING_2")
    builder.line(result.recipe.title or "(no supported title)")
    builder.line()
    builder.line("Ingredients", named_style_type="HEADING_2")
    for ingredient in result.recipe.ingredients:
        builder.line(f"- {ingredient.original_text}")
    builder.line()
    builder.line("Instructions", named_style_type="HEADING_2")
    for instruction in result.recipe.instructions:
        builder.line(f"{instruction.sequence}. {instruction.original_text}")
    if result.recipe.conflicts:
        builder.line()
        builder.line("Unresolved conflicts", named_style_type="HEADING_2")
        for conflict in result.recipe.conflicts:
            builder.line(f"- {conflict}")
    if result.recipe.completeness_findings:
        builder.line()
        builder.line("Completeness findings", named_style_type="HEADING_2")
        for completeness_finding in result.recipe.completeness_findings:
            builder.line(
                f"- [{completeness_finding.code}] {completeness_finding.message}"
            )
    builder.line()
    builder.line("Evidence excerpts", named_style_type="HEADING_2")
    if not result.evidence_segments:
        builder.line("- No source excerpts were retained for this result.")
    else:
        for segment in result.evidence_segments:
            evidence_label, excerpt = _render_evidence(segment)
            builder.line(
                f"- {evidence_label} {excerpt}",
                bold_prefix_start_offset=2,
                bold_prefix_length=len(evidence_label),
            )
    return builder.build()


def _render_evidence(segment: EvidenceSegment) -> tuple[str, str]:
    text = " ".join(segment.text.split())
    excerpt = text if len(text) <= 1_000 else text[:997] + "..."
    return f"{segment.source_kind.value} ({segment.evidence_id}):", excerpt
