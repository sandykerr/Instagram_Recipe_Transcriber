"""Validated local configuration for the one-item Google delivery workflow."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class GoogleWorkflowConfig(BaseModel):
    """Private configuration paths and IDs; values must not be committed to Git."""

    model_config = ConfigDict(frozen=True)

    queue_spreadsheet_id: str = Field(min_length=1)
    master_spreadsheet_id: str = Field(min_length=1)
    category_tabs: tuple[str, ...] = Field(min_length=1)
    oauth_client_path: Path
    oauth_token_path: Path
    working_root: Path
    drive_folder_id: str | None = None

    @field_validator("category_tabs")
    @classmethod
    def validate_categories(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(category.strip() for category in value if category.strip())
        if not cleaned:
            raise ValueError("category_tabs must contain at least one tab name")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("category_tabs must not contain duplicates")
        return cleaned
