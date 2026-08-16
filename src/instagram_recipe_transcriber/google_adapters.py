"""Injectable Google Sheets, Docs, and Drive adapters for recipe delivery."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import HttpUrl

from .acquisition import recipe_id_for_url
from .errors import PipelineOperationalError
from .models import QueuedRecipe, RecipeCandidate, RecipeDocument


class GoogleSheetsRecipeQueueReader:
    """Reads the first URL from configured category tabs, skipping their header rows."""

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
                return QueuedRecipe(
                    recipe_id=recipe_id_for_url(url),
                    source_url=HttpUrl(url),
                    category=category,
                    queue_row_number=row_number,
                    description=description or None,
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


class GoogleDocsRecipeWriter:
    """Creates a plain, evidence-preserving Google Doc for a validated recipe."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def create(self, recipe: RecipeCandidate, queued_recipe: QueuedRecipe) -> RecipeDocument:
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
                                "text": _render(recipe, queued_recipe),
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
    return f"{_a1_tab(category)}!A2:B"


def _master_range(category: str) -> str:
    return f"{_a1_tab(category)}!A:B"


def _a1_tab(category: str) -> str:
    return "'" + category.replace("'", "''") + "'"


def _render(recipe: RecipeCandidate, queued_recipe: QueuedRecipe) -> str:
    lines = [recipe.title or "Untitled recipe", "", f"Source: {queued_recipe.source_url}"]
    if queued_recipe.description:
        lines.extend([f"Queue description: {queued_recipe.description}", ""])
    lines.extend(["", "Ingredients"])
    lines.extend(f"- {ingredient.original_text}" for ingredient in recipe.ingredients)
    lines.extend(["", "Instructions"])
    lines.extend(
        f"{instruction.sequence}. {instruction.original_text}"
        for instruction in recipe.instructions
    )
    return "\n".join(lines) + "\n"
