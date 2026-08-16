"""Injectable Google Sheets, Docs, and Drive adapters for recipe delivery."""

from __future__ import annotations

from collections.abc import Iterable
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
                    range=_queue_range(queued_recipe.category),
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={
                        "values": [
                            [
                                str(queued_recipe.source_url),
                                queued_recipe.description or "",
                                QueueStatus.REVIEW.value,
                                str(document.url),
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
            for row_number, row in enumerate(reader._get_values(_queue_range(category)), start=2):
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
                    range=_queue_range(decision.category),
                    valueInputOption="RAW",
                    insertDataOption="INSERT_ROWS",
                    body={
                        "values": [
                            [
                                str(decision.source_url),
                                decision.description or "",
                                ReviewDecisionStatus.REJECTED.value,
                                str(document.url),
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
            self._service.documents().batchUpdate(
                documentId=document_id,
                body={
                    "requests": [
                        {
                            "insertText": {
                                "endOfSegmentLocation": {},
                                "text": _render(recipe, queued_recipe, presentation),
                            }
                        }
                    ]
                },
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
            self._service.documents().batchUpdate(
                documentId=document_id,
                body={
                    "requests": [
                        {
                            "insertText": {
                                "endOfSegmentLocation": {},
                                "text": _render_review(result, queued_recipe),
                            }
                        }
                    ]
                },
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


def _render(
    recipe: RecipeCandidate,
    queued_recipe: QueuedRecipe,
    presentation: RecipeDocumentPresentation | None = None,
) -> str:
    lines = [recipe.title or "Untitled recipe", "", f"Source: {queued_recipe.source_url}"]
    if queued_recipe.description:
        lines.extend([f"Queue description: {queued_recipe.description}", ""])
    lines.extend(["", "Ingredients"])
    lines.extend(f"- {ingredient.original_text}" for ingredient in recipe.ingredients)
    lines.extend(["", "Instructions"])
    if (
        presentation is not None
        and presentation.instruction_format is RecipeInstructionFormat.RAW_TRANSCRIPT
    ):
        assert presentation.raw_instruction_text is not None
        lines.append(presentation.raw_instruction_text)
    else:
        lines.extend(
            f"{instruction.sequence}. {instruction.original_text}"
            for instruction in recipe.instructions
        )
    return "\n".join(lines) + "\n"


def _render_review(result: PipelineResult, queued_recipe: QueuedRecipe) -> str:
    lines = [
        "Recipe review",
        "",
        f"Source: {queued_recipe.source_url}",
        f"Category: {queued_recipe.category}",
    ]
    if queued_recipe.description:
        lines.append(f"Queue description: {queued_recipe.description}")
    lines.extend(["", "Validation findings"])
    lines.extend(f"- [{finding.code}] {finding.message}" for finding in result.validation.findings)
    lines.extend(["", "Candidate title", result.recipe.title or "(no supported title)"])
    lines.extend(["", "Ingredients"])
    lines.extend(f"- {ingredient.original_text}" for ingredient in result.recipe.ingredients)
    lines.extend(["", "Instructions"])
    lines.extend(
        f"{instruction.sequence}. {instruction.original_text}"
        for instruction in result.recipe.instructions
    )
    if result.recipe.conflicts:
        lines.extend(["", "Unresolved conflicts"])
        lines.extend(f"- {conflict}" for conflict in result.recipe.conflicts)
    if result.recipe.completeness_findings:
        lines.extend(["", "Completeness findings"])
        lines.extend(
            f"- [{finding.code}] {finding.message}"
            for finding in result.recipe.completeness_findings
        )
    lines.extend(["", "Evidence excerpts"])
    if not result.evidence_segments:
        lines.append("- No source excerpts were retained for this result.")
    else:
        lines.extend(_render_evidence(segment) for segment in result.evidence_segments)
    return "\n".join(lines) + "\n"


def _render_evidence(segment: EvidenceSegment) -> str:
    text = " ".join(segment.text.split())
    excerpt = text if len(text) <= 1_000 else text[:997] + "..."
    return f"- {segment.source_kind.value} ({segment.evidence_id}): {excerpt}"
