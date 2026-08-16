from __future__ import annotations

from pathlib import Path

from pydantic import HttpUrl

from instagram_recipe_transcriber.adapters import FasterWhisperTranscriber, LocalFileSourceLoader
from instagram_recipe_transcriber.models import AudioArtifact, RecipeJob, SourceKind


class FakeSegment:
    def __init__(self, start: float, end: float, text: str) -> None:
        self.start = start
        self.end = end
        self.text = text


class FakeInfo:
    language = "en"
    language_probability = 0.97


class FakeWhisperPipeline:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def transcribe(
        self, media_path: str, *, beam_size: int
    ) -> tuple[list[FakeSegment], FakeInfo]:
        self.calls.append((media_path, beam_size))
        return [FakeSegment(0.0, 1.25, " Add pasta. "), FakeSegment(1.25, 2.0, " ")], FakeInfo()


def test_local_file_source_loader_preserves_caption_as_evidence(tmp_path: Path) -> None:
    media_path = tmp_path / "input.mp4"
    media_path.write_bytes(b"video fixture")
    job = RecipeJob(
        recipe_id="test-recipe",
        source_url=HttpUrl("https://www.instagram.com/reel/DZdXIrXOklf/"),
        media_path=media_path,
        caption_text="  Cook this tonight.  ",
    )

    source = LocalFileSourceLoader().load(job)

    assert source.media_path == media_path.resolve()
    assert source.caption is not None
    assert source.caption.source_kind is SourceKind.CAPTION
    assert source.caption.text == "Cook this tonight."
    assert source.media_sha256 is not None
    assert len(source.media_sha256) == 64


def test_faster_whisper_transcriber_preserves_timestamped_segments(tmp_path: Path) -> None:
    media_path = tmp_path / "input.mp4"
    media_path.write_bytes(b"video fixture")
    source = LocalFileSourceLoader().load(
        RecipeJob(
            recipe_id="test-recipe",
            source_url=HttpUrl("https://www.instagram.com/reel/DZdXIrXOklf/"),
            media_path=media_path,
        )
    )
    fake_pipeline = FakeWhisperPipeline()
    transcriber = FasterWhisperTranscriber(
        pipeline_factory=lambda _model, _device, _compute: fake_pipeline
    )

    assert source.media_sha256 is not None
    audio = AudioArtifact(
        audio_path=media_path.resolve(),
        audio_sha256=source.media_sha256,
        sample_rate_hz=16_000,
        channels=1,
    )
    transcript = transcriber.transcribe(audio)

    assert fake_pipeline.calls == [(str(media_path.resolve()), 5)]
    assert transcript.language == "en"
    assert transcript.language_probability == 0.97
    assert transcript.model_name == "turbo"
    assert len(transcript.segments) == 1
    assert transcript.segments[0].text == "Add pasta."
    assert transcript.segments[0].start_seconds == 0.0
    assert transcript.segments[0].end_seconds == 1.25
