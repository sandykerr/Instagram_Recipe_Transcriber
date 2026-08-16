from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import HttpUrl

from instagram_recipe_transcriber.acquisition import YtDlpAcquirer
from instagram_recipe_transcriber.errors import PipelineOperationalError
from instagram_recipe_transcriber.models import QueuedRecipe


def _queued_recipe() -> QueuedRecipe:
    return QueuedRecipe(
        recipe_id="acquisition-test",
        source_url=HttpUrl("https://www.instagram.com/reel/DVJBGzyk8E5/"),
        category="Main Courses",
        queue_row_number=2,
    )


def _metadata_runner(
    metadata: dict[str, object], calls: list[list[str]], output_dir: Path
) -> Callable[[list[str]], None]:
    def runner(command: list[str]) -> None:
        calls.append(command)
        assert "--skip-download" in command
        assert "--flat-playlist" in command
        (output_dir / "source.info.json").write_text(json.dumps(metadata), encoding="utf-8")

    return runner


def test_standard_reel_downloads_video_and_keeps_caption(tmp_path: Path) -> None:
    output_dir = tmp_path / "acquisition-test"
    metadata_calls: list[list[str]] = []
    media_calls: list[list[str]] = []

    def media_runner(command: list[str]) -> None:
        media_calls.append(command)
        assert "--skip-download" not in command
        (output_dir / "source.mp4").write_bytes(b"video")

    acquired = YtDlpAcquirer(
        tmp_path,
        metadata_runner=_metadata_runner(
            {"description": "Caption", "formats": [{"vcodec": "avc1"}]},
            metadata_calls,
            output_dir,
        ),
        media_runner=media_runner,
    ).acquire(_queued_recipe())

    assert acquired.job.media_path == output_dir / "source.mp4"
    assert acquired.job.caption_text == "Caption"
    assert acquired.metadata_path.is_file()
    assert len(metadata_calls) == len(media_calls) == 1


def test_standard_reel_downloads_video_without_caption(tmp_path: Path) -> None:
    output_dir = tmp_path / "acquisition-test"

    def media_runner(_command: list[str]) -> None:
        (output_dir / "source.mp4").write_bytes(b"video")

    acquired = YtDlpAcquirer(
        tmp_path,
        metadata_runner=_metadata_runner(
            {"formats": [{"vcodec": "avc1"}]}, [], output_dir
        ),
        media_runner=media_runner,
    ).acquire(_queued_recipe())

    assert acquired.job.media_path == output_dir / "source.mp4"
    assert acquired.job.caption_text is None


def test_image_carousel_with_caption_does_not_download_or_expand_children(tmp_path: Path) -> None:
    output_dir = tmp_path / "acquisition-test"
    metadata_calls: list[list[str]] = []
    media_calls: list[list[str]] = []
    acquired = YtDlpAcquirer(
        tmp_path,
        metadata_runner=_metadata_runner(
            {
                "_type": "playlist",
                "description": "Carousel caption",
                "entries": [{"id": "child-1"}, {"id": "child-2"}],
                "formats": [],
            },
            metadata_calls,
            output_dir,
        ),
        media_runner=lambda command: media_calls.append(command),
    ).acquire(_queued_recipe())

    assert acquired.job.media_path is None
    assert acquired.job.caption_text == "Carousel caption"
    assert acquired.metadata_path.is_file()
    assert len(metadata_calls) == 1
    assert "--flat-playlist" in metadata_calls[0]
    assert media_calls == []


def test_image_carousel_without_caption_is_a_clear_operational_error(tmp_path: Path) -> None:
    output_dir = tmp_path / "acquisition-test"
    with pytest.raises(
        PipelineOperationalError, match="neither usable video media nor a usable caption"
    ):
        YtDlpAcquirer(
            tmp_path,
            metadata_runner=_metadata_runner({"formats": []}, [], output_dir),
            media_runner=lambda _command: pytest.fail("media download must not run"),
        ).acquire(_queued_recipe())


def test_metadata_retrieval_failure_is_propagated(tmp_path: Path) -> None:
    def failure(_command: list[str]) -> None:
        raise PipelineOperationalError("metadata retrieval failed")

    with pytest.raises(PipelineOperationalError, match="metadata retrieval failed"):
        YtDlpAcquirer(tmp_path, metadata_runner=failure).acquire(_queued_recipe())


def test_video_download_failure_after_metadata_is_propagated(tmp_path: Path) -> None:
    output_dir = tmp_path / "acquisition-test"

    def failure(_command: list[str]) -> None:
        raise PipelineOperationalError("video download failed")

    with pytest.raises(PipelineOperationalError, match="video download failed"):
        YtDlpAcquirer(
            tmp_path,
            metadata_runner=_metadata_runner(
                {"description": "Caption", "formats": [{"vcodec": "avc1"}]}, [], output_dir
            ),
            media_runner=failure,
        ).acquire(_queued_recipe())
