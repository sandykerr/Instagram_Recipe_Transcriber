"""Compare original-resolution and downscaled OCR on selected frames.

Example:

    python tests/scripts/ocr_downscale_comparison_smoke.py \
        DZdXIrXOklf-frames/manifest.json --frames 7,20,51
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ocr_smoke import create_ocr, load_manifest, recognize_frame
from ocr_smoke_config import OcrImageConfig


def parse_frame_numbers(value: str) -> list[int]:
    try:
        numbers = [int(part.strip()) for part in value.split(",")]
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "frames must be a comma-separated list of integers"
        ) from error
    if not numbers or any(number <= 0 for number in numbers):
        raise argparse.ArgumentTypeError("frame numbers must be positive")
    if len(set(numbers)) != len(numbers):
        raise argparse.ArgumentTypeError("frame numbers must be unique")
    return numbers


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare OCR at original and reduced image dimensions.",
    )
    parser.add_argument("manifest", type=Path, help="Frame manifest JSON")
    parser.add_argument(
        "--frames",
        type=parse_frame_numbers,
        default=parse_frame_numbers("7,20,51"),
        help="One-based frame numbers (default: 7,20,51)",
    )
    parser.add_argument(
        "--downscaled-max",
        type=int,
        default=960,
        help="Maximum side for the reduced input (default: 960)",
    )
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.5,
        help="Discard detections below this score (default: 0.5)",
    )
    parser.add_argument("--lang", default="en", help="OCR language (default: en)")
    parser.add_argument(
        "--ocr-version",
        default="PP-OCRv5",
        help="PaddleOCR model family (default: PP-OCRv5)",
    )
    parser.add_argument("--output", type=Path, help="Comparison JSON output path")
    return parser.parse_args()


def normalized_texts(detections: list[Any]) -> list[str]:
    return [" ".join(item.text.upper().split()) for item in detections]


def main() -> None:
    script_started = time.perf_counter()
    args = parse_args()
    if args.downscaled_max <= 0:
        raise ValueError("--downscaled-max must be positive")
    if not 0 <= args.min_confidence <= 1:
        raise ValueError("--min-confidence must be between zero and one")

    manifest_path = args.manifest.expanduser().resolve()
    manifest = load_manifest(manifest_path)
    entries = manifest["frames"]
    if max(args.frames) > len(entries):
        raise ValueError(
            f"Requested frame {max(args.frames)}, but manifest has {len(entries)} frames"
        )
    selected_entries = [(number, entries[number - 1]) for number in args.frames]

    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else Path(tempfile.mkdtemp(prefix="ocr-downscale-comparison-"))
        / "comparison.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    model_started = time.perf_counter()
    ocr = create_ocr(
        SimpleNamespace(
            lang=args.lang,
            ocr_version=args.ocr_version,
            enable_mkldnn=False,
        )
    )
    model_elapsed = time.perf_counter() - model_started

    modes = {
        "original": OcrImageConfig(max_dimension=None),
        f"max_{args.downscaled_max}": OcrImageConfig(
            max_dimension=args.downscaled_max
        ),
    }
    results: dict[str, list[dict[str, Any]]] = {}
    mode_timings: dict[str, float] = {}
    for mode_name, image_config in modes.items():
        mode_started = time.perf_counter()
        mode_results = []
        for frame_number, entry in selected_entries:
            frame_path = manifest_path.parent / entry["path"]
            frame_started = time.perf_counter()
            detections, transform = recognize_frame(
                ocr,
                frame_path,
                args.min_confidence,
                image_config,
            )
            frame_elapsed = time.perf_counter() - frame_started
            mode_results.append(
                {
                    "frame_number": frame_number,
                    "path": entry["path"],
                    "timestamp_seconds": float(entry["timestamp_seconds"]),
                    "elapsed_seconds": frame_elapsed,
                    "image_transform": asdict(transform),
                    "detection_count": len(detections),
                    "detections": [asdict(item) for item in detections],
                    "normalized_texts": normalized_texts(detections),
                }
            )
            print(
                f"{mode_name} frame {frame_number}: {len(detections)} detections "
                f"in {frame_elapsed:.2f}s"
            )
        mode_timings[mode_name] = time.perf_counter() - mode_started
        results[mode_name] = mode_results

    original_time = mode_timings["original"]
    downscaled_name = f"max_{args.downscaled_max}"
    downscaled_time = mode_timings[downscaled_name]
    speedup = original_time / downscaled_time if downscaled_time else None
    artifact = {
        "source_manifest": str(manifest_path),
        "frame_numbers": args.frames,
        "minimum_confidence": args.min_confidence,
        "ocr_version": args.ocr_version,
        "model_initialization_seconds": model_elapsed,
        "mode_timings_seconds": mode_timings,
        "downscaled_speedup": speedup,
        "results": results,
    }
    output_path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    script_elapsed = time.perf_counter() - script_started

    print(f"Original total: {original_time:.2f}s")
    print(f"Downscaled total: {downscaled_time:.2f}s")
    print(f"Downscaled speedup: {speedup:.2f}x")
    print(f"Model initialization: {model_elapsed:.2f}s")
    print(f"Whole script: {script_elapsed:.2f}s")
    print(f"Comparison artifact: {output_path}")


if __name__ == "__main__":
    main()
