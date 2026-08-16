from __future__ import annotations

from pathlib import Path

from instagram_recipe_transcriber.adapters import PaddleOcrExtractor
from instagram_recipe_transcriber.models import (
    ExtractedFrame,
    FrameExtractionArtifact,
    VideoProbe,
)


class FakeOcrResult:
    json: dict[str, object] = {
        "res": {
            "rec_texts": [" 1 cup pasta ", "ignored"],
            "rec_scores": [0.91, 0.2],
            "rec_polys": [
                [[10, 20], [30, 20], [30, 40], [10, 40]],
                [[1, 1], [2, 1], [2, 2], [1, 2]],
            ],
        }
    }


class FakeOcrEngine:
    def __init__(self) -> None:
        self.inputs: list[str | object] = []

    def predict(self, image: str | object) -> list[FakeOcrResult]:
        self.inputs.append(image)
        return [FakeOcrResult()]


def test_paddle_ocr_extractor_emits_timestamped_evidence(tmp_path: Path) -> None:
    frame_path = tmp_path / "frame_000001.jpg"
    frame_path.write_bytes(b"not read because downscaling is disabled")
    frames = FrameExtractionArtifact(
        source=VideoProbe(duration_seconds=2.0, width=1080, height=1920),
        sampling_fps=2.0,
        frames=(ExtractedFrame(frame_path=frame_path, timestamp_seconds=0.5),),
    )
    engine = FakeOcrEngine()
    extractor = PaddleOcrExtractor(
        maximum_image_dimension=None,
        deduplicate=False,
        engine_factory=lambda: engine,
    )
    artifact = extractor.extract(frames)

    assert engine.inputs == [str(frame_path)]
    assert artifact.candidate_frame_count == 1
    assert artifact.selected_frame_count == 1
    assert len(artifact.segments) == 1
    evidence = artifact.segments[0]
    assert evidence.text == "1 cup pasta"
    assert evidence.confidence == 0.91
    assert evidence.frame_timestamp_seconds == 0.5
    assert evidence.bounding_polygon == ((10, 20), (30, 20), (30, 40), (10, 40))
