"""Filesystem persistence for versioned, hash-addressed JSON artifacts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .errors import ArtifactPersistenceError
from .models import ArtifactEnvelope, StageName


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    """Return the content fingerprint used to invalidate changed local media."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: object) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return sha256_bytes(serialized.encode("utf-8"))


class JsonArtifactStore:
    """Stores the latest valid stage result at ``root/{recipe_id}/{stage}.json``."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def load(
        self, recipe_id: str, stage: str, input_hash: str, model_type: type[BaseModel]
    ) -> BaseModel | None:
        path = self._path(recipe_id, stage)
        if not path.exists():
            return None
        try:
            envelope = ArtifactEnvelope.model_validate_json(path.read_text(encoding="utf-8"))
            if envelope.stage.value != stage or envelope.input_hash != input_hash:
                return None
            return model_type.model_validate(envelope.payload)
        except (OSError, ValidationError) as error:
            raise ArtifactPersistenceError(f"Cannot read artifact: {path}") from error

    def save(self, recipe_id: str, stage: str, input_hash: str, payload: BaseModel) -> None:
        try:
            envelope = ArtifactEnvelope(
                stage=StageName(stage),
                input_hash=input_hash,
                payload=payload.model_dump(mode="json"),
            )
            path = self._path(recipe_id, stage)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = path.with_suffix(".json.tmp")
            temporary_path.write_text(envelope.model_dump_json(indent=2), encoding="utf-8")
            temporary_path.replace(path)
        except (OSError, ValidationError, ValueError) as error:
            message = f"Cannot write artifact for {recipe_id}/{stage}"
            raise ArtifactPersistenceError(message) from error

    def _path(self, recipe_id: str, stage: str) -> Path:
        return self.root / recipe_id / f"{stage}.json"
