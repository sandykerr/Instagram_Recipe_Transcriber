"""Run the real local pipeline against one existing video file.

This is intentionally a manually invoked smoke test, not part of pytest. Generated
audio, frames, and JSON artifacts are written below the ignored data/working tree.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from instagram_recipe_transcriber.adapters import (
    DeterministicOcrGate,
    FasterWhisperTranscriber,
    FfmpegAudioExtractor,
    FfmpegFrameExtractor,
    LocalFileSourceLoader,
    PaddleOcrExtractor,
)
from instagram_recipe_transcriber.artifacts import JsonArtifactStore
from instagram_recipe_transcriber.models import OcrPolicy, RecipeJob
from instagram_recipe_transcriber.pipeline import PipelineRunner
from instagram_recipe_transcriber.recipe_processing import (
    RecipeValidator,
    RuleBasedRecipeExtractor,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "media",
        nargs="?",
        type=Path,
        default=REPOSITORY_ROOT / "DZdXIrXOklf.mp4",
        help="Local MP4 input (default: repository DZdXIrXOklf.mp4)",
    )
    parser.add_argument("--recipe-id", default="dzd-xir-xoklf-local-slice")
    parser.add_argument(
        "--source-url",
        default="https://www.instagram.com/reel/DZdXIrXOklf/",
    )
    parser.add_argument("--caption", help="Optional manually supplied caption text")
    parser.add_argument("--model", default="turbo")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument(
        "--sampling-fps",
        type=float,
        default=0.2,
        help="OCR frame sampling rate; 0.2 fps keeps this smoke test bounded",
    )
    parser.add_argument("--ocr-max-dimension", type=int, default=960)
    parser.add_argument(
        "--ocr-policy",
        type=OcrPolicy,
        choices=list(OcrPolicy),
        default=OcrPolicy.WHEN_NEEDED,
    )
    parser.add_argument(
        "--working-root",
        type=Path,
        default=REPOSITORY_ROOT / "data" / "working",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    media_path = args.media.expanduser().resolve()
    working_root = args.working_root.expanduser().resolve()
    if not media_path.is_file():
        raise FileNotFoundError(f"Video input does not exist: {media_path}")

    audio_extractor = FfmpegAudioExtractor(working_root)
    frame_extractor = FfmpegFrameExtractor(
        working_root,
        sampling_fps=args.sampling_fps,
    )
    runner = PipelineRunner(
        artifact_store=JsonArtifactStore(working_root / "artifacts"),
        source_loader=LocalFileSourceLoader(),
        audio_extractor=audio_extractor,
        transcriber=FasterWhisperTranscriber(
            model_name=args.model,
            device="cpu",
            compute_type=args.compute_type,
        ),
        ocr_gate=DeterministicOcrGate(policy=args.ocr_policy),
        frame_extractor=frame_extractor,
        ocr_extractor=PaddleOcrExtractor(
            maximum_image_dimension=args.ocr_max_dimension,
        ),
        recipe_extractor=RuleBasedRecipeExtractor(),
        recipe_validator=RecipeValidator(),
    )
    job = RecipeJob(
        recipe_id=args.recipe_id,
        source_url=args.source_url,
        media_path=media_path,
        caption_text=args.caption,
    )

    started = time.perf_counter()
    result = runner.run(job)
    elapsed = time.perf_counter() - started

    result_path = working_root / args.recipe_id / "pipeline_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")

    print(f"Outcome: {result.validation.outcome.value}")
    print(f"Title: {result.recipe.title or '(not extracted)'}")
    print(f"Ingredients: {len(result.recipe.ingredients)}")
    print(f"Instructions: {len(result.recipe.instructions)}")
    print(f"Validation findings: {len(result.validation.findings)}")
    print(f"Whole pipeline elapsed: {elapsed:.2f}s")
    print(f"Artifacts: {working_root}")
    print(f"Pipeline result: {result_path}")
    print(json.dumps(result.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
