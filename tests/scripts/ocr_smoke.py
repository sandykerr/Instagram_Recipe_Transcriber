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


@dataclass(frozen=True)
class OcrDetection:
    text: str
    confidence: float
    polygon: list[list[int]]


@dataclass(frozen=True)
class OcrFrame:
    path: str
    timestamp_seconds: float
    elapsed_seconds: float
    detections: list[OcrDetection]


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


def recognize_frame(ocr: Any, frame_path: Path, min_confidence: float) -> list[OcrDetection]:
    results = list(ocr.predict(str(frame_path)))
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
                polygon=[[int(value) for value in point] for point in polygon],
            )
        )
    return detections


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
    frame_entries = manifest["frames"]
    if args.max_frames is not None:
        frame_entries = frame_entries[: args.max_frames]

    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else manifest_path.parent / "ocr.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    model_started = time.perf_counter()
    ocr = create_ocr(args)
    model_elapsed = time.perf_counter() - model_started

    inference_started = time.perf_counter()
    frames: list[OcrFrame] = []
    for index, entry in enumerate(frame_entries, start=1):
        frame_path = manifest_path.parent / entry["path"]
        frame_started = time.perf_counter()
        detections = recognize_frame(ocr, frame_path, args.min_confidence)
        elapsed = time.perf_counter() - frame_started
        frame = OcrFrame(
            path=entry["path"],
            timestamp_seconds=float(entry["timestamp_seconds"]),
            elapsed_seconds=elapsed,
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
    print(f"OCR model initialization elapsed: {model_elapsed:.2f}s")
    print(f"OCR frame processing elapsed: {inference_elapsed:.2f}s")
    print(f"Artifact writing elapsed: {artifact_elapsed:.2f}s")
    print(f"Whole script elapsed: {script_elapsed:.2f}s")


if __name__ == "__main__":
    main()
