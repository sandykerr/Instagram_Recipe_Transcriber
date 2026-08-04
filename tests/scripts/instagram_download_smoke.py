"""Try downloading one public Instagram Reel with yt-dlp.

This is an exploratory script, not a pytest test. It intentionally does not
load browser cookies or Instagram credentials. Only download media you are
authorized to use.

Example:

    python tests/scripts/instagram_download_smoke.py \
        https://www.instagram.com/reel/DbjBsEfx3j5/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


ALLOWED_HOSTS = {"instagram.com", "www.instagram.com"}
REEL_PATH_PREFIXES = {"reel", "reels"}


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: float | None
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and validate one public Instagram Reel.",
    )
    parser.add_argument("url", help="Public Instagram Reel URL")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory (default: a new directory under /tmp)",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Resolve metadata without downloading media",
    )
    return parser.parse_args()


def reel_shortcode(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("URL must be an HTTPS URL on instagram.com")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0].lower() not in REEL_PATH_PREFIXES:
        raise ValueError("URL path must look like /reel/SHORTCODE/")

    shortcode = parts[1]
    if not shortcode.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"Invalid Reel shortcode: {shortcode}")
    return shortcode


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_media(path: Path) -> MediaProbe:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    video = next(
        (stream for stream in streams if stream.get("codec_type") == "video"),
        None,
    )
    audio = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )
    if video is None:
        raise RuntimeError(f"Downloaded file has no video stream: {path}")
    if audio is None:
        raise RuntimeError(f"Downloaded file has no audio stream: {path}")

    raw_duration = payload.get("format", {}).get("duration")
    return MediaProbe(
        duration_seconds=float(raw_duration) if raw_duration is not None else None,
        width=int(video["width"]) if video.get("width") is not None else None,
        height=int(video["height"]) if video.get("height") is not None else None,
        video_codec=video.get("codec_name"),
        audio_codec=audio.get("codec_name"),
    )


def find_download(output_dir: Path, shortcode: str) -> Path:
    ignored_suffixes = {".json", ".part", ".ytdl"}
    candidates = sorted(
        path
        for path in output_dir.glob(f"{shortcode}.*")
        if path.is_file() and path.suffix not in ignored_suffixes
    )
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected one downloaded media file for {shortcode}, found {candidates}"
        )
    return candidates[0]


def download(url: str, output_dir: Path, shortcode: str, metadata_only: bool) -> dict[str, Any]:
    try:
        import yt_dlp
    except ImportError as error:
        raise RuntimeError("yt-dlp is not installed in the active environment") from error

    options: dict[str, Any] = {
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "outtmpl": str(output_dir / f"{shortcode}.%(ext)s"),
        "restrictfilenames": True,
        "writeinfojson": True,
        # Deliberately omit cookiefile and cookiesfrombrowser.
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        return downloader.extract_info(url, download=not metadata_only)


def main() -> None:
    script_started = time.perf_counter()
    args = parse_args()
    shortcode = reel_shortcode(args.url)
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else Path(tempfile.mkdtemp(prefix="instagram-recipe-download-"))
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    download_started = time.perf_counter()
    try:
        info = download(args.url, output_dir, shortcode, args.metadata_only)
    except Exception as error:
        raise RuntimeError(
            "Unauthenticated Instagram extraction failed. This is an expected "
            "outcome when Instagram requires login or changes its page format."
        ) from error
    download_elapsed = time.perf_counter() - download_started

    print(f"Shortcode: {shortcode}")
    print(f"Title: {info.get('title')}")
    print(f"Uploader: {info.get('uploader') or info.get('uploader_id')}")
    print(f"Metadata/download elapsed: {download_elapsed:.2f}s")
    if args.metadata_only:
        print("Metadata resolution succeeded; media download was skipped.")
        print(f"Whole script elapsed: {time.perf_counter() - script_started:.2f}s")
        return

    discovery_started = time.perf_counter()
    media_path = find_download(output_dir, shortcode)
    discovery_elapsed = time.perf_counter() - discovery_started

    probe_started = time.perf_counter()
    probe = probe_media(media_path)
    probe_elapsed = time.perf_counter() - probe_started

    hash_started = time.perf_counter()
    media_sha256 = sha256(media_path)
    hash_elapsed = time.perf_counter() - hash_started
    artifact = {
        "source_url": args.url,
        "shortcode": shortcode,
        "acquisition_method": "yt-dlp-unauthenticated",
        "media_path": str(media_path),
        "size_bytes": media_path.stat().st_size,
        "sha256": media_sha256,
        "probe": asdict(probe),
    }
    artifact_started = time.perf_counter()
    artifact_path = output_dir / "download.json"
    artifact_path.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
    artifact_elapsed = time.perf_counter() - artifact_started
    script_elapsed = time.perf_counter() - script_started

    print(f"Media: {media_path}")
    print(f"Resolution: {probe.width}x{probe.height}")
    print(f"Duration: {probe.duration_seconds}s")
    print(f"Codecs: video={probe.video_codec}, audio={probe.audio_codec}")
    print(f"Artifact: {artifact_path}")
    print(f"File discovery elapsed: {discovery_elapsed:.2f}s")
    print(f"Media probe elapsed: {probe_elapsed:.2f}s")
    print(f"SHA-256 elapsed: {hash_elapsed:.2f}s")
    print(f"Artifact writing elapsed: {artifact_elapsed:.2f}s")
    print(f"Whole script elapsed: {script_elapsed:.2f}s")


if __name__ == "__main__":
    main()
