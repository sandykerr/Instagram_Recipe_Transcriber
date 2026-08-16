"""Local persisted publication state for retry-safe Google delivery."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from .errors import ArtifactPersistenceError
from .models import PublicationArtifact


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
