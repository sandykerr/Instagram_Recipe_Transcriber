"""Run PaddleOCR over timestamped frames from the extraction smoke test.

This is an exploratory script, not a pytest test. Run frame extraction first,
then pass the generated manifest:

    python tests/scripts/ocr_smoke.py /tmp/recipe-frames/manifest.json

Use ``--max-frames`` for a quick trial before processing the entire video.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

from ocr_smoke_config import FrameDeduplicationConfig, OcrImageConfig


@dataclass(frozen=True)
class OcrDetection:
    text: str
    confidence: float
    polygon: list[list[int]]


@dataclass(frozen=True)
class ImageTransform:
    original_width: int
    original_height: int
    ocr_width: int
    ocr_height: int
    scale_x: float
    scale_y: float


@dataclass(frozen=True)
class OcrFrame:
    path: str
    timestamp_seconds: float
    elapsed_seconds: float
    image_transform: ImageTransform
    detections: list[OcrDetection]


@dataclass(frozen=True)
class SkippedDuplicate:
    path: str
    timestamp_seconds: float
    duplicate_of: str
    hamming_distance: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run PaddleOCR over a frame-extraction manifest.",
    )
    parser.add_argument("manifest", type=Path, help="Frame manifest JSON")
    parser.add_argument(
        "--output",
        type=Path,
        help="Output JSON path (default: ocr.json beside the manifest)",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        help="Process only the first N frames",
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
    parser.add_argument(
        "--enable-mkldnn",
        action="store_true",
        help="Enable oneDNN; disabled by default due to a Paddle 3.3 CPU error",
    )
    parser.add_argument(
        "--no-deduplicate",
        action="store_true",
        help="OCR every candidate frame instead of removing perceptual duplicates",
    )
    parser.add_argument(
        "--hash-size",
        type=int,
        default=FrameDeduplicationConfig.hash_size,
        help=f"Difference-hash width (default: {FrameDeduplicationConfig.hash_size})",
    )
    parser.add_argument(
        "--duplicate-threshold",
        type=int,
        default=FrameDeduplicationConfig.hamming_threshold,
        help=(
            "Maximum Hamming distance treated as a duplicate "
            f"(default: {FrameDeduplicationConfig.hamming_threshold})"
        ),
    )
    parser.add_argument(
        "--max-consecutive-skips",
        type=int,
        default=FrameDeduplicationConfig.max_consecutive_skips,
        help=(
            "Maximum adjacent duplicates skipped before forcing a frame "
            f"(default: {FrameDeduplicationConfig.max_consecutive_skips})"
        ),
    )
    parser.add_argument(
        "--selection-only",
        action="store_true",
        help="Write frame-selection results without initializing or running OCR",
    )
    parser.add_argument(
        "--max-image-dimension",
        type=int,
        help="Downscale the OCR input so its longest side is at most this value",
    )
    return parser.parse_args()


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Manifest does not exist: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest.get("frames"), list):
        raise ValueError("Manifest does not contain a frames list")
    return manifest


def create_ocr(args: argparse.Namespace) -> Any:
    try:
        from paddleocr import PaddleOCR
    except ImportError as error:
        raise RuntimeError(
            "PaddleOCR is not installed in the active environment"
        ) from error

    return PaddleOCR(
        lang=args.lang,
        ocr_version=args.ocr_version,
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_mkldnn=args.enable_mkldnn,
    )


def difference_hash(path: Path, hash_size: int) -> int:
    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError("Pillow is required for frame deduplication") from error

    with Image.open(path) as image:
        grayscale = image.convert("L").resize(
            (hash_size + 1, hash_size),
            Image.Resampling.LANCZOS,
        )
        pixels = list(grayscale.get_flattened_data())

    result = 0
    row_width = hash_size + 1
    for row in range(hash_size):
        offset = row * row_width
        for column in range(hash_size):
            result <<= 1
            result |= pixels[offset + column] > pixels[offset + column + 1]
    return result


def select_distinct_frames(
    entries: list[dict[str, Any]],
    base_dir: Path,
    config: FrameDeduplicationConfig,
) -> tuple[list[dict[str, Any]], list[SkippedDuplicate]]:
    if not config.enabled:
        return entries, []

    selected: list[dict[str, Any]] = []
    skipped: list[SkippedDuplicate] = []
    last_selected_hash: int | None = None
    last_selected_path: str | None = None
    consecutive_skips = 0

    for entry in entries:
        frame_hash = difference_hash(base_dir / entry["path"], config.hash_size)
        if last_selected_hash is None:
            selected.append(entry)
            last_selected_hash = frame_hash
            last_selected_path = entry["path"]
            consecutive_skips = 0
            continue

        distance = (frame_hash ^ last_selected_hash).bit_count()
        if (
            distance <= config.hamming_threshold
            and consecutive_skips < config.max_consecutive_skips
        ):
            assert last_selected_path is not None
            skipped.append(
                SkippedDuplicate(
                    path=entry["path"],
                    timestamp_seconds=float(entry["timestamp_seconds"]),
                    duplicate_of=last_selected_path,
                    hamming_distance=distance,
                )
            )
            consecutive_skips += 1
            continue

        selected.append(entry)
        last_selected_hash = frame_hash
        last_selected_path = entry["path"]
        consecutive_skips = 0

    return selected, skipped


def prepare_ocr_input(
    frame_path: Path,
    config: OcrImageConfig,
) -> tuple[Any, ImageTransform]:
    try:
        import numpy as np
        from PIL import Image
    except ImportError as error:
        raise RuntimeError(
            "Pillow and NumPy are required for OCR input preparation"
        ) from error

    with Image.open(frame_path) as image:
        original_width, original_height = image.size
        maximum = max(original_width, original_height)
        if config.max_dimension is None or maximum <= config.max_dimension:
            transform = ImageTransform(
                original_width=original_width,
                original_height=original_height,
                ocr_width=original_width,
                ocr_height=original_height,
                scale_x=1.0,
                scale_y=1.0,
            )
            return str(frame_path), transform

        scale = config.max_dimension / maximum
        ocr_width = max(1, round(original_width * scale))
        ocr_height = max(1, round(original_height * scale))
        resized = image.convert("RGB").resize(
            (ocr_width, ocr_height),
            Image.Resampling.LANCZOS,
        )
        # PaddleOCR interprets ndarray inputs in OpenCV's BGR channel order.
        ocr_input = np.asarray(resized)[:, :, ::-1].copy()

    return ocr_input, ImageTransform(
        original_width=original_width,
        original_height=original_height,
        ocr_width=ocr_width,
        ocr_height=ocr_height,
        scale_x=ocr_width / original_width,
        scale_y=ocr_height / original_height,
    )


def recognize_frame(
    ocr: Any,
    frame_path: Path,
    min_confidence: float,
    image_config: OcrImageConfig,
) -> tuple[list[OcrDetection], ImageTransform]:
    ocr_input, transform = prepare_ocr_input(frame_path, image_config)
    results = list(ocr.predict(ocr_input))
    if len(results) != 1:
        raise RuntimeError(
            f"Expected one OCR result for {frame_path}, received {len(results)}"
        )

    payload = results[0].json["res"]
    texts = payload["rec_texts"]
    scores = payload["rec_scores"]
    polygons = payload["rec_polys"]
    detections = []
    for text, score, polygon in zip(texts, scores, polygons, strict=True):
        confidence = float(score)
        if confidence < min_confidence:
            continue
        detections.append(
            OcrDetection(
                text=str(text).strip(),
                confidence=confidence,
                polygon=[
                    [
                        round(float(point[0]) / transform.scale_x),
                        round(float(point[1]) / transform.scale_y),
                    ]
                    for point in polygon
                ],
            )
        )
    return detections, transform


def summarize(frames: list[OcrFrame]) -> list[dict[str, Any]]:
    readings: dict[str, list[OcrDetection]] = defaultdict(list)
    for frame in frames:
        for detection in frame.detections:
            normalized = " ".join(detection.text.upper().split())
            if normalized:
                readings[normalized].append(detection)

    return [
        {
            "normalized_text": text,
            "occurrences": len(detections),
            "max_confidence": max(item.confidence for item in detections),
        }
        for text, detections in sorted(
            readings.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )
    ]


def main() -> None:
    script_started = time.perf_counter()
    args = parse_args()
    if args.max_frames is not None and args.max_frames <= 0:
        raise ValueError("--max-frames must be greater than zero")
    if not 0 <= args.min_confidence <= 1:
        raise ValueError("--min-confidence must be between zero and one")

    manifest_path = args.manifest.expanduser().resolve()
    manifest_started = time.perf_counter()
    manifest = load_manifest(manifest_path)
    manifest_elapsed = time.perf_counter() - manifest_started
    candidate_entries = manifest["frames"]
    if args.max_frames is not None:
        candidate_entries = candidate_entries[: args.max_frames]

    deduplication_config = FrameDeduplicationConfig(
        enabled=not args.no_deduplicate,
        hash_size=args.hash_size,
        hamming_threshold=args.duplicate_threshold,
        max_consecutive_skips=args.max_consecutive_skips,
    )
    image_config = OcrImageConfig(max_dimension=args.max_image_dimension)
    selection_started = time.perf_counter()
    frame_entries, skipped_duplicates = select_distinct_frames(
        candidate_entries,
        manifest_path.parent,
        deduplication_config,
    )
    selection_elapsed = time.perf_counter() - selection_started
    selection = {
        "config": asdict(deduplication_config),
        "candidate_frame_count": len(candidate_entries),
        "selected_frame_count": len(frame_entries),
        "skipped_duplicate_count": len(skipped_duplicates),
        "selected_frames": frame_entries,
        "skipped_duplicates": [asdict(item) for item in skipped_duplicates],
    }

    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else manifest_path.parent
        / ("frame_selection.json" if args.selection_only else "ocr.json")
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"Frame selection: {len(candidate_entries)} candidates -> "
        f"{len(frame_entries)} selected; {len(skipped_duplicates)} duplicates removed "
        f"in {selection_elapsed:.2f}s"
    )
    if args.selection_only:
        output = {
            "status": "selection_only",
            "source_manifest": str(manifest_path),
            "image_config": asdict(image_config),
            "selection": selection,
        }
        artifact_started = time.perf_counter()
        output_path.write_text(
            json.dumps(output, indent=2) + "\n",
            encoding="utf-8",
        )
        artifact_elapsed = time.perf_counter() - artifact_started
        script_elapsed = time.perf_counter() - script_started
        print(f"Frame-selection artifact: {output_path}")
        print(f"Manifest loading elapsed: {manifest_elapsed:.2f}s")
        print(f"Frame selection elapsed: {selection_elapsed:.2f}s")
        print(f"Artifact writing elapsed: {artifact_elapsed:.2f}s")
        print(f"Whole script elapsed: {script_elapsed:.2f}s")
        return

    model_started = time.perf_counter()
    ocr = create_ocr(args)
    model_elapsed = time.perf_counter() - model_started

    inference_started = time.perf_counter()
    frames: list[OcrFrame] = []
    for index, entry in enumerate(frame_entries, start=1):
        frame_path = manifest_path.parent / entry["path"]
        frame_started = time.perf_counter()
        detections, image_transform = recognize_frame(
            ocr,
            frame_path,
            args.min_confidence,
            image_config,
        )
        elapsed = time.perf_counter() - frame_started
        frame = OcrFrame(
            path=entry["path"],
            timestamp_seconds=float(entry["timestamp_seconds"]),
            elapsed_seconds=elapsed,
            image_transform=image_transform,
            detections=detections,
        )
        frames.append(frame)
        print(
            f"[{index}/{len(frame_entries)}] {frame.timestamp_seconds:6.2f}s: "
            f"{len(detections)} detections in {elapsed:.2f}s"
        )
    inference_elapsed = time.perf_counter() - inference_started

    output = {
        "source_manifest": str(manifest_path),
        "engine": "paddleocr",
        "engine_version": version("paddleocr"),
        "ocr_version": args.ocr_version,
        "language": args.lang,
        "minimum_confidence": args.min_confidence,
        "mkldnn_enabled": args.enable_mkldnn,
        "image_config": asdict(image_config),
        "selection": selection,
        "elapsed_seconds": inference_elapsed,
        "processed_frame_count": len(frames),
        "frames": [asdict(frame) for frame in frames],
        "summary": summarize(frames),
    }
    artifact_started = time.perf_counter()
    output_path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    artifact_elapsed = time.perf_counter() - artifact_started
    script_elapsed = time.perf_counter() - script_started
    print(f"OCR artifact: {output_path}")
    print(f"Manifest loading elapsed: {manifest_elapsed:.2f}s")
    print(f"Frame selection elapsed: {selection_elapsed:.2f}s")
    print(f"OCR model initialization elapsed: {model_elapsed:.2f}s")
    print(f"OCR frame processing elapsed: {inference_elapsed:.2f}s")
    print(f"Artifact writing elapsed: {artifact_elapsed:.2f}s")
    print(f"Whole script elapsed: {script_elapsed:.2f}s")


if __name__ == "__main__":
    main()
