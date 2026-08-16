"""Manually inspect whether fixed-rate frame sampling captures recipe overlays.

This is an exploratory script, not a pytest test. It requires ``ffmpeg`` and
``ffprobe`` on PATH, produces a JPEG contact sheet, and writes its output
outside the repository by default.

Run from the repository root with:

    python tests/scripts/frame_extraction_smoke.py video.mp4 \
        --crop 624,196,338,589
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_FRAMES_PER_SECOND = 2.0


@dataclass(frozen=True)
class ExtractedFrame:
    path: str
    timestamp_seconds: float


@dataclass(frozen=True)
class CropRegion:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class VideoMetadata:
    duration_seconds: float
    width: int
    height: int


def parse_crop_region(value: str) -> CropRegion:
    try:
        values = [int(part.strip()) for part in value.split(",")]
        if len(values) != 4:
            raise ValueError
        x, y, width, height = values
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "crop must use X,Y,WIDTH,HEIGHT with integer values"
        ) from error

    if x < 0 or y < 0:
        raise argparse.ArgumentTypeError("crop X and Y must be nonnegative")
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("crop width and height must be positive")
    return CropRegion(x=x, y=y, width=width, height=height)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract video frames and build a JPEG contact sheet.",
    )
    parser.add_argument("video", type=Path, help="Local source video")
    parser.add_argument(
        "--fps",
        type=float,
        default=DEFAULT_FRAMES_PER_SECOND,
        help=f"Frames to sample per second (default: {DEFAULT_FRAMES_PER_SECOND})",
    )
    parser.add_argument(
        "--crop",
        type=parse_crop_region,
        metavar="X,Y,WIDTH,HEIGHT",
        help="Crop each frame before sampling",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory (default: a new directory under /tmp)",
    )
    return parser.parse_args()


def require_command(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise RuntimeError(f"Required command is not available on PATH: {name}")
    return executable


def probe_video(video: Path) -> VideoMetadata:
    ffprobe = require_command("ffprobe")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=width,height",
            "-of",
            "json",
            str(video),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    probe = json.loads(result.stdout)
    video_stream = next(
        stream
        for stream in probe["streams"]
        if "width" in stream and "height" in stream
    )
    return VideoMetadata(
        duration_seconds=float(probe["format"]["duration"]),
        width=int(video_stream["width"]),
        height=int(video_stream["height"]),
    )


def validate_crop(crop: CropRegion, metadata: VideoMetadata) -> None:
    if crop.x + crop.width > metadata.width:
        raise ValueError(
            f"crop exceeds video width {metadata.width}: "
            f"x + width = {crop.x + crop.width}"
        )
    if crop.y + crop.height > metadata.height:
        raise ValueError(
            f"crop exceeds video height {metadata.height}: "
            f"y + height = {crop.y + crop.height}"
        )


def extract_frames(
    video: Path,
    output_dir: Path,
    fps: float,
    crop: CropRegion | None,
) -> list[ExtractedFrame]:
    ffmpeg = require_command("ffmpeg")
    frames_dir = output_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    filters: list[str] = []
    if crop is not None:
        filters.append(
            f"crop={crop.width}:{crop.height}:{crop.x}:{crop.y}:exact=1"
        )
    filters.append(f"fps={fps}")

    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            ",".join(filters),
            "-pix_fmt",
            "yuvj444p",
            "-q:v",
            "2",
            str(frames_dir / "frame_%06d.jpg"),
        ],
        check=True,
    )

    frame_paths = sorted(frames_dir.glob("frame_*.jpg"))
    return [
        ExtractedFrame(
            path=str(path.relative_to(output_dir)),
            timestamp_seconds=(index - 1) / fps,
        )
        for index, path in enumerate(frame_paths, start=1)
    ]


def write_manifest(
    output_dir: Path,
    video: Path,
    metadata: VideoMetadata,
    fps: float,
    crop: CropRegion | None,
    frames: list[ExtractedFrame],
) -> Path:
    manifest_path = output_dir / "manifest.json"
    manifest = {
        "source_video": str(video.resolve()),
        "source": asdict(metadata),
        "crop": asdict(crop) if crop is not None else None,
        "sampling_fps": fps,
        "frame_count": len(frames),
        "frames": [asdict(frame) for frame in frames],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def write_contact_sheet(
    output_dir: Path,
    frames: list[ExtractedFrame],
    columns: int = 5,
) -> Path:
    if not frames:
        raise RuntimeError("No frames were extracted")

    ffmpeg = require_command("ffmpeg")
    rows = math.ceil(len(frames) / columns)
    contact_sheet_path = output_dir / "contact_sheet.jpg"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            "1",
            "-i",
            str(output_dir / "frames" / "frame_%06d.jpg"),
            "-vf",
            (
                f"scale=320:-1,tile={columns}x{rows}:"
                "padding=8:margin=8:color=black"
            ),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(contact_sheet_path),
        ],
        check=True,
    )
    return contact_sheet_path


def main() -> None:
    script_started = time.perf_counter()
    args = parse_args()
    video = args.video.expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(f"Video does not exist: {video}")
    if args.fps <= 0:
        raise ValueError("--fps must be greater than zero")

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else Path(tempfile.mkdtemp(prefix="instagram-recipe-frames-"))
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    probe_started = time.perf_counter()
    metadata = probe_video(video)
    probe_elapsed = time.perf_counter() - probe_started
    if args.crop is not None:
        validate_crop(args.crop, metadata)

    extraction_started = time.perf_counter()
    frames = extract_frames(video, output_dir, args.fps, args.crop)
    extraction_elapsed = time.perf_counter() - extraction_started

    manifest_started = time.perf_counter()
    manifest_path = write_manifest(
        output_dir,
        video,
        metadata,
        args.fps,
        args.crop,
        frames,
    )
    manifest_elapsed = time.perf_counter() - manifest_started

    contact_sheet_started = time.perf_counter()
    contact_sheet_path = write_contact_sheet(output_dir, frames)
    contact_sheet_elapsed = time.perf_counter() - contact_sheet_started
    script_elapsed = time.perf_counter() - script_started

    print(
        f"Video: {metadata.width}x{metadata.height}, "
        f"{metadata.duration_seconds:.2f}s"
    )
    if args.crop is not None:
        print(
            f"Crop: x={args.crop.x}, y={args.crop.y}, "
            f"width={args.crop.width}, height={args.crop.height}"
        )
    print(f"Extracted frames: {len(frames)} at {args.fps:g} FPS")
    print(f"Manifest: {manifest_path}")
    print(f"Contact sheet: {contact_sheet_path}")
    print(f"Probe elapsed: {probe_elapsed:.2f}s")
    print(f"Frame extraction elapsed: {extraction_elapsed:.2f}s")
    print(f"Manifest writing elapsed: {manifest_elapsed:.2f}s")
    print(f"Contact sheet elapsed: {contact_sheet_elapsed:.2f}s")
    print(f"Whole script elapsed: {script_elapsed:.2f}s")


if __name__ == "__main__":
    main()
