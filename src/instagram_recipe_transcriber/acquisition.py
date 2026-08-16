"""Best-effort media acquisition adapters for queued recipe URLs."""

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
    """Downloads media and yt-dlp metadata, including a best-effort caption."""

    version = "yt-dlp-acquirer-v1:info-json"

    def __init__(self, working_root: Path, *, command_runner: CommandRunner | None = None) -> None:
        self._working_root = working_root
        self._command_runner = command_runner or _run_command

    def acquire(self, queued_recipe: QueuedRecipe) -> AcquiredRecipe:
        output_dir = self._working_root / queued_recipe.recipe_id
        output_dir.mkdir(parents=True, exist_ok=True)
        output_template = output_dir / "source.%(ext)s"
        self._command_runner(
            [
                "yt-dlp",
                "--no-playlist",
                "--write-info-json",
                "--output",
                str(output_template),
                str(queued_recipe.source_url),
            ]
        )
        metadata_path = output_dir / "source.info.json"
        if not metadata_path.is_file():
            raise PipelineOperationalError("yt-dlp did not create an info JSON artifact")
        media_path = _find_media_file(output_dir)
        metadata = _read_metadata(metadata_path)
        caption = metadata.get("description")
        return AcquiredRecipe(
            queued_recipe=queued_recipe,
            job=RecipeJob(
                recipe_id=queued_recipe.recipe_id,
                source_url=queued_recipe.source_url,
                media_path=media_path,
                caption_text=caption if isinstance(caption, str) else None,
            ),
            metadata_path=metadata_path,
        )


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
        if path.is_file() and path.name != "source.info.json" and path.suffix != ".part"
    )
    if len(candidates) != 1:
        raise PipelineOperationalError("yt-dlp did not produce exactly one media file")
    return candidates[0]


def _read_metadata(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PipelineOperationalError("Cannot read yt-dlp metadata") from error
    if not isinstance(data, dict):
        raise PipelineOperationalError("yt-dlp metadata must be a JSON object")
    return data
