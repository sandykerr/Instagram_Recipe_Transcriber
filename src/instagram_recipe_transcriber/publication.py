"""Local persisted publication state for retry-safe Google delivery."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from .errors import ArtifactPersistenceError
from .models import PublicationArtifact, ReviewArtifact, ReviewResolutionArtifact


class JsonPublicationStore:
    """Stores one publication checkpoint per recipe ID below the working directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def load(self, recipe_id: str) -> PublicationArtifact | None:
        path = self._path(recipe_id)
        if not path.is_file():
            return None
        try:
            return PublicationArtifact.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise ArtifactPersistenceError(f"Cannot read publication artifact: {path}") from error

    def save(self, publication: PublicationArtifact) -> None:
        path = self._path(publication.recipe_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = path.with_suffix(".json.tmp")
            temporary_path.write_text(publication.model_dump_json(indent=2), encoding="utf-8")
            temporary_path.replace(path)
        except OSError as error:
            raise ArtifactPersistenceError(f"Cannot write publication artifact: {path}") from error

    def _path(self, recipe_id: str) -> Path:
        return self._root / recipe_id / "publication.json"


class JsonReviewStore:
    """Stores one review-delivery checkpoint per recipe ID."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def load(self, recipe_id: str) -> ReviewArtifact | None:
        path = self._path(recipe_id)
        if not path.is_file():
            return None
        try:
            return ReviewArtifact.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise ArtifactPersistenceError(f"Cannot read review artifact: {path}") from error

    def save(self, review: ReviewArtifact) -> None:
        path = self._path(review.recipe_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = path.with_suffix(".json.tmp")
            temporary_path.write_text(review.model_dump_json(indent=2), encoding="utf-8")
            temporary_path.replace(path)
        except OSError as error:
            raise ArtifactPersistenceError(f"Cannot write review artifact: {path}") from error

    def _path(self, recipe_id: str) -> Path:
        return self._root / recipe_id / "review.json"


class JsonReviewResolutionStore:
    """Stores completed manual review decisions for retry-safe promotion."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def load(self, recipe_id: str) -> ReviewResolutionArtifact | None:
        path = self._path(recipe_id)
        if not path.is_file():
            return None
        try:
            return ReviewResolutionArtifact.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise ArtifactPersistenceError(
                f"Cannot read review resolution artifact: {path}"
            ) from error

    def save(self, resolution: ReviewResolutionArtifact) -> None:
        path = self._path(resolution.recipe_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = path.with_suffix(".json.tmp")
            temporary_path.write_text(resolution.model_dump_json(indent=2), encoding="utf-8")
            temporary_path.replace(path)
        except OSError as error:
            raise ArtifactPersistenceError(
                f"Cannot write review resolution artifact: {path}"
            ) from error

    def _path(self, recipe_id: str) -> Path:
        return self._root / recipe_id / "resolution.json"
