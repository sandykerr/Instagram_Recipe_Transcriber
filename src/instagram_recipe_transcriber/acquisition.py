"""Best-effort caption and media acquisition adapters for queued recipe URLs."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import PipelineOperationalError
from .models import AcquiredRecipe, QueuedRecipe, RecipeJob

CommandRunner = Callable[[list[str]], None]


class YtDlpAcquirer:
    """Inspect metadata first, then download media only when fallback is possible.

    The metadata command uses flat playlist inspection and ``--skip-download``:
    an Instagram carousel remains a single top-level source item and its child
    images are never expanded or downloaded merely to obtain the creator caption.
    """

    version = "yt-dlp-acquirer-v2:metadata-first"

    def __init__(
        self,
        working_root: Path,
        *,
        metadata_runner: CommandRunner | None = None,
        media_runner: CommandRunner | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        if command_runner is not None and (metadata_runner is not None or media_runner is not None):
            raise ValueError(
                "command_runner cannot be combined with metadata_runner or media_runner"
            )
        self._working_root = working_root
        self._metadata_runner = metadata_runner or command_runner or _run_command
        self._media_runner = media_runner or command_runner or _run_command

    def acquire(self, queued_recipe: QueuedRecipe) -> AcquiredRecipe:
        output_dir = self._working_root / queued_recipe.recipe_id
        output_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = self._inspect_metadata(queued_recipe, output_dir)
        metadata = _read_metadata(metadata_path)
        caption = _caption_from_metadata(metadata)

        if not _has_usable_video(metadata):
            if caption is None:
                raise PipelineOperationalError(
                    "yt-dlp found neither usable video media nor a usable caption."
                )
            return AcquiredRecipe(
                queued_recipe=queued_recipe,
                job=RecipeJob(
                    recipe_id=queued_recipe.recipe_id,
                    source_url=queued_recipe.source_url,
                    caption_text=caption,
                ),
                metadata_path=metadata_path,
            )

        media_path = self._download_media(queued_recipe, output_dir)
        return AcquiredRecipe(
            queued_recipe=queued_recipe,
            job=RecipeJob(
                recipe_id=queued_recipe.recipe_id,
                source_url=queued_recipe.source_url,
                media_path=media_path,
                caption_text=caption,
            ),
            metadata_path=metadata_path,
        )

    def _inspect_metadata(self, queued_recipe: QueuedRecipe, output_dir: Path) -> Path:
        output_template = output_dir / "source.%(ext)s"
        self._metadata_runner(
            [
                "yt-dlp",
                "--flat-playlist",
                "--skip-download",
                "--ignore-no-formats-error",
                "--write-info-json",
                "--write-playlist-metafiles",
                "--output",
                str(output_template),
                str(queued_recipe.source_url),
            ]
        )
        metadata_path = _find_metadata_file(output_dir)
        if metadata_path is None:
            raise PipelineOperationalError("yt-dlp did not create an info JSON artifact")
        return metadata_path

    def _download_media(self, queued_recipe: QueuedRecipe, output_dir: Path) -> Path:
        output_template = output_dir / "source.%(ext)s"
        self._media_runner(
            [
                "yt-dlp",
                "--no-playlist",
                "--no-write-info-json",
                "--output",
                str(output_template),
                str(queued_recipe.source_url),
            ]
        )
        return _find_media_file(output_dir)


def recipe_id_for_url(url: str) -> str:
    """Create a stable, filename-safe ID that makes URL retries reuse artifacts."""
    return f"reel-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:20]}"


def _run_command(command: list[str]) -> None:
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as error:
        raise PipelineOperationalError("yt-dlp acquisition failed") from error


def _find_media_file(output_dir: Path) -> Path:
    candidates = sorted(
        path
        for path in output_dir.iterdir()
        if path.is_file()
        and path.name != "source.info.json"
        and path.suffix not in {".part", ".json"}
    )
    if len(candidates) != 1:
        raise PipelineOperationalError("yt-dlp did not produce exactly one media file")
    return candidates[0]


def _find_metadata_file(output_dir: Path) -> Path | None:
    preferred = output_dir / "source.info.json"
    if preferred.is_file():
        return preferred
    candidates = sorted(path for path in output_dir.glob("*.info.json") if path.is_file())
    if len(candidates) == 1:
        return candidates[0]
    return None


def _caption_from_metadata(metadata: dict[str, Any]) -> str | None:
    description = metadata.get("description")
    if not isinstance(description, str):
        return None
    return description.strip() or None


def _has_usable_video(metadata: dict[str, Any]) -> bool:
    formats = metadata.get("formats")
    if isinstance(formats, list):
        for item in formats:
            if isinstance(item, dict) and item.get("vcodec") not in (None, "none"):
                return True
    return metadata.get("vcodec") not in (None, "none")


def _read_metadata(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineOperationalError("Cannot read yt-dlp metadata") from error
    if not isinstance(data, dict):
        raise PipelineOperationalError("yt-dlp metadata must be a JSON object")
    return data
