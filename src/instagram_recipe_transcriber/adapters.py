"""Concrete local adapters for the first end-to-end vertical slice."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Protocol, cast

from .artifacts import sha256_file
from .errors import PipelineOperationalError
from .models import (
    AudioArtifact,
    CropRegion,
    EvidenceSegment,
    ExtractedFrame,
    FrameExtractionArtifact,
    OcrArtifact,
    OcrDecisionArtifact,
    OcrPolicy,
    RecipeCandidate,
    RecipeJob,
    SourceArtifact,
    SourceKind,
    TranscriptArtifact,
    VideoProbe,
)


class _WhisperSegment(Protocol):
    start: float
    end: float
    text: str


class _WhisperInfo(Protocol):
    language: str
    language_probability: float


class _BatchedWhisperPipeline(Protocol):
    def transcribe(
        self, media_path: str, *, beam_size: int
    ) -> tuple[Iterable[_WhisperSegment], _WhisperInfo]: ...


class WhisperPipelineFactory(Protocol):
    def __call__(
        self, model_name: str, device: str, compute_type: str
    ) -> _BatchedWhisperPipeline: ...


class _PaddleOcrResult(Protocol):
    json: dict[str, object]


class _PaddleOcrEngine(Protocol):
    def predict(self, image: str | object) -> Iterable[_PaddleOcrResult]: ...


class LocalFileSourceLoader:
    """Creates a source artifact from user-supplied local media and caption text."""

    version = "local-file-source-loader-v1"

    def load(self, job: RecipeJob) -> SourceArtifact:
        media_path = job.media_path.expanduser().resolve() if job.media_path else None
        if media_path is not None and not media_path.is_file():
            raise PipelineOperationalError(f"Local media file does not exist: {media_path}")
        if media_path is None and job.caption_text is None:
            raise PipelineOperationalError("A local media file or caption text is required")

        caption = None
        if job.caption_text is not None:
            caption = EvidenceSegment(
                evidence_id="caption-1",
                source_kind=SourceKind.CAPTION,
                text=job.caption_text,
            )
        return SourceArtifact(
            recipe_id=job.recipe_id,
            source_url=job.source_url,
            media_path=media_path,
            media_sha256=sha256_file(media_path) if media_path else None,
            caption=caption,
        )


class FfmpegAudioExtractor:
    """Extracts reproducible, mono PCM WAV audio for local transcription."""

    version = "ffmpeg-audio-extractor-v1:pcm_s16le:16000hz:mono"

    def __init__(self, working_root: Path, *, sample_rate_hz: int = 16_000) -> None:
        if sample_rate_hz <= 0:
            raise ValueError("sample_rate_hz must be positive")
        self._working_root = working_root
        self._sample_rate_hz = sample_rate_hz

    def extract(self, source: SourceArtifact) -> AudioArtifact:
        if source.media_path is None:
            raise PipelineOperationalError("Audio extraction requires local source media")
        ffmpeg = _require_command("ffmpeg")
        output_dir = self._working_root / source.recipe_id
        output_dir.mkdir(parents=True, exist_ok=True)
        audio_path = output_dir / "audio.wav"
        temporary_path = output_dir / "audio.tmp.wav"
        _run_command(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source.media_path),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(self._sample_rate_hz),
                "-c:a",
                "pcm_s16le",
                str(temporary_path),
            ]
        )
        temporary_path.replace(audio_path)
        return AudioArtifact(
            audio_path=audio_path,
            audio_sha256=sha256_file(audio_path),
            sample_rate_hz=self._sample_rate_hz,
            channels=1,
        )


class FfmpegFrameExtractor:
    """Samples timestamped JPEG frames for a later targeted OCR stage."""

    def __init__(
        self,
        working_root: Path,
        *,
        sampling_fps: float = 2.0,
        crop: CropRegion | None = None,
    ) -> None:
        if sampling_fps <= 0:
            raise ValueError("sampling_fps must be positive")
        self._working_root = working_root
        self._sampling_fps = sampling_fps
        self._crop = crop

    @property
    def version(self) -> str:
        crop_value = self._crop.model_dump_json() if self._crop else "none"
        return f"ffmpeg-frame-extractor-v1:fps={self._sampling_fps:g}:crop={crop_value}"

    def extract(self, source: SourceArtifact) -> FrameExtractionArtifact:
        if source.media_path is None or source.media_sha256 is None:
            raise PipelineOperationalError("Frame extraction requires local source media")
        ffmpeg = _require_command("ffmpeg")
        probe = self._probe_video(source.media_path)
        self._validate_crop(probe)
        output_dir = self._working_root / source.recipe_id
        output_dir.mkdir(parents=True, exist_ok=True)
        temporary_dir = Path(tempfile.mkdtemp(prefix=".frames-", dir=output_dir))
        filters = self._filters()
        try:
            _run_command(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    str(source.media_path),
                    "-vf",
                    filters,
                    "-pix_fmt",
                    "yuvj444p",
                    "-q:v",
                    "2",
                    str(temporary_dir / "frame_%06d.jpg"),
                ]
            )
            frame_paths = sorted(temporary_dir.glob("frame_*.jpg"))
            if not frame_paths:
                raise PipelineOperationalError("FFmpeg extracted no frames")
            frame_dir = output_dir / f"frames-{source.media_sha256[:12]}"
            if frame_dir.exists():
                shutil.rmtree(temporary_dir)
            else:
                temporary_dir.replace(frame_dir)
            return FrameExtractionArtifact(
                source=probe,
                sampling_fps=self._sampling_fps,
                crop=self._crop,
                frames=tuple(
                    ExtractedFrame(
                        frame_path=frame_dir / frame_path.name,
                        timestamp_seconds=(index - 1) / self._sampling_fps,
                    )
                    for index, frame_path in enumerate(frame_paths, start=1)
                ),
            )
        except PipelineOperationalError:
            raise
        except OSError as error:
            raise PipelineOperationalError("FFmpeg frame extraction failed") from error

    def _probe_video(self, media_path: Path) -> VideoProbe:
        ffprobe = _require_command("ffprobe")
        result = _run_command(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=width,height",
                "-of",
                "json",
                str(media_path),
            ]
        )
        try:
            payload = json.loads(result.stdout)
            stream = next(
                item for item in payload["streams"] if "width" in item and "height" in item
            )
            return VideoProbe(
                duration_seconds=float(payload["format"]["duration"]),
                width=int(stream["width"]),
                height=int(stream["height"]),
            )
        except (KeyError, StopIteration, TypeError, ValueError) as error:
            raise PipelineOperationalError("FFprobe returned invalid video metadata") from error

    def _filters(self) -> str:
        filters: list[str] = []
        if self._crop is not None:
            filters.append(
                f"crop={self._crop.width}:{self._crop.height}:{self._crop.x}:{self._crop.y}:exact=1"
            )
        filters.append(f"fps={self._sampling_fps:g}")
        return ",".join(filters)

    def _validate_crop(self, probe: VideoProbe) -> None:
        if self._crop is None:
            return
        if self._crop.x + self._crop.width > probe.width:
            raise PipelineOperationalError("Configured frame crop exceeds the source video width")
        if self._crop.y + self._crop.height > probe.height:
            raise PipelineOperationalError("Configured frame crop exceeds the source video height")


class DeterministicOcrGate:
    """Conservative policy that requests OCR whenever evidence is incomplete."""

    version = "deterministic-ocr-gate-v1"
    _SCREEN_REFERENCE_MARKERS = (
        "on screen",
        "see ingredients",
        "ingredients above",
        "amounts above",
        "shown above",
    )

    def __init__(
        self,
        *,
        policy: OcrPolicy = OcrPolicy.WHEN_NEEDED,
        minimum_language_probability: float = 0.6,
    ) -> None:
        if not 0 <= minimum_language_probability <= 1:
            raise ValueError("minimum_language_probability must be between 0 and 1")
        self._policy = policy
        self._minimum_language_probability = minimum_language_probability

    def decide(
        self, source: SourceArtifact, transcript: TranscriptArtifact, recipe: RecipeCandidate
    ) -> OcrDecisionArtifact:
        if self._policy is OcrPolicy.NEVER:
            return self._decision(False, "OCR policy is never")
        if self._policy is OcrPolicy.ALWAYS:
            return self._decision(True, "OCR policy is always")
        if not recipe.ingredients:
            return self._decision(True, "No ingredients were extracted from caption and transcript")
        if not recipe.instructions:
            return self._decision(
                True, "No instructions were extracted from caption and transcript"
            )
        if recipe.conflicts:
            return self._decision(True, "Caption and transcript contain unresolved conflicts")
        if transcript.language_probability is not None and (
            transcript.language_probability < self._minimum_language_probability
        ):
            return self._decision(
                True, "Transcript language confidence is below the configured threshold"
            )
        source_text = " ".join(
            segment.text.lower() for segment in (source.caption, *transcript.segments) if segment
        )
        if any(marker in source_text for marker in self._SCREEN_REFERENCE_MARKERS):
            return self._decision(
                True, "Source text explicitly refers to information shown on screen"
            )
        return self._decision(False, "Caption and transcript satisfy the deterministic OCR gate")

    def _decision(self, should_run: bool, reason: str) -> OcrDecisionArtifact:
        return OcrDecisionArtifact(should_run=should_run, policy=self._policy, reason=reason)


class PaddleOcrExtractor:
    """Runs targeted PaddleOCR and maps detections to original-frame evidence."""

    def __init__(
        self,
        *,
        minimum_confidence: float = 0.5,
        language: str = "en",
        ocr_version: str = "PP-OCRv5",
        maximum_image_dimension: int | None = 960,
        enable_mkldnn: bool = False,
        deduplicate: bool = True,
        difference_hash_size: int = 16,
        duplicate_hamming_threshold: int = 60,
        max_consecutive_duplicate_skips: int = 2,
        engine_factory: Callable[[], _PaddleOcrEngine] | None = None,
    ) -> None:
        if not 0 <= minimum_confidence <= 1:
            raise ValueError("minimum_confidence must be between 0 and 1")
        if maximum_image_dimension is not None and maximum_image_dimension <= 0:
            raise ValueError("maximum_image_dimension must be positive when set")
        if difference_hash_size < 4:
            raise ValueError("difference_hash_size must be at least 4")
        if not 0 <= duplicate_hamming_threshold <= difference_hash_size**2:
            raise ValueError("duplicate_hamming_threshold is outside the hash bit range")
        if max_consecutive_duplicate_skips < 0:
            raise ValueError("max_consecutive_duplicate_skips must be nonnegative")
        self._minimum_confidence = minimum_confidence
        self._language = language
        self._ocr_version = ocr_version
        self._maximum_image_dimension = maximum_image_dimension
        self._enable_mkldnn = enable_mkldnn
        self._deduplicate = deduplicate
        self._difference_hash_size = difference_hash_size
        self._duplicate_hamming_threshold = duplicate_hamming_threshold
        self._max_consecutive_duplicate_skips = max_consecutive_duplicate_skips
        self._engine_factory = engine_factory
        self._engine: _PaddleOcrEngine | None = None

    @property
    def version(self) -> str:
        maximum = self._maximum_image_dimension or "original"
        return (
            "paddleocr-v1:"
            f"language={self._language}:model={self._ocr_version}:min-confidence="
            f"{self._minimum_confidence}:max-dimension={maximum}:mkldnn={self._enable_mkldnn}:"
            f"deduplicate={self._deduplicate}:hash-size={self._difference_hash_size}:"
            f"threshold={self._duplicate_hamming_threshold}:"
            f"max-skips={self._max_consecutive_duplicate_skips}"
        )

    def extract(self, frames: FrameExtractionArtifact) -> OcrArtifact:
        if not frames.frames:
            raise PipelineOperationalError(
                "OCR cannot run because frame extraction returned no frames"
            )
        try:
            selected_frames = self._select_distinct_frames(frames.frames)
            evidence_segments = tuple(
                detection
                for frame in selected_frames
                for detection in self._recognize_frame(frame)
            )
        except PipelineOperationalError:
            raise
        except Exception as error:
            raise PipelineOperationalError("PaddleOCR extraction failed") from error
        return OcrArtifact(
            status="completed",
            segments=evidence_segments,
            candidate_frame_count=len(frames.frames),
            selected_frame_count=len(selected_frames),
            engine="paddleocr",
            engine_version=self._engine_version(),
        )

    def _recognize_frame(self, frame: ExtractedFrame) -> tuple[EvidenceSegment, ...]:
        image, scale_x, scale_y = self._prepare_input(frame.frame_path)
        results = tuple(self._get_engine().predict(image))
        if len(results) != 1:
            raise PipelineOperationalError(
                f"Expected one OCR result for {frame.frame_path}, received {len(results)}"
            )
        try:
            result = results[0].json["res"]
            if not isinstance(result, dict):
                raise TypeError("OCR result payload is not a mapping")
            texts = result["rec_texts"]
            scores = result["rec_scores"]
            polygons = result["rec_polys"]
            if not (
                isinstance(texts, list)
                and isinstance(scores, list)
                and isinstance(polygons, list)
            ):
                raise TypeError("OCR result has invalid detection lists")
        except (KeyError, TypeError) as error:
            message = "PaddleOCR returned an unexpected result schema"
            raise PipelineOperationalError(message) from error

        detections: list[EvidenceSegment] = []
        detections_with_index = enumerate(zip(texts, scores, polygons, strict=True), start=1)
        for index, (text, score, polygon) in detections_with_index:
            normalized_text = str(text).strip()
            confidence = float(score)
            if not normalized_text or confidence < self._minimum_confidence:
                continue
            detections.append(
                EvidenceSegment(
                    evidence_id=f"ocr-{frame.timestamp_seconds:.3f}-{index}",
                    source_kind=SourceKind.OCR,
                    text=normalized_text,
                    confidence=confidence,
                    frame_timestamp_seconds=frame.timestamp_seconds,
                    frame_path=frame.frame_path,
                    bounding_polygon=self._map_polygon(polygon, scale_x, scale_y),
                )
            )
        return tuple(detections)

    def _select_distinct_frames(
        self, frames: tuple[ExtractedFrame, ...]
    ) -> tuple[ExtractedFrame, ...]:
        if not self._deduplicate:
            return frames
        selected: list[ExtractedFrame] = []
        previous_hash: int | None = None
        consecutive_skips = 0
        for frame in frames:
            frame_hash = self._difference_hash(frame.frame_path)
            if previous_hash is None:
                selected.append(frame)
                previous_hash = frame_hash
                continue
            distance = (frame_hash ^ previous_hash).bit_count()
            if (
                distance <= self._duplicate_hamming_threshold
                and consecutive_skips < self._max_consecutive_duplicate_skips
            ):
                consecutive_skips += 1
                continue
            selected.append(frame)
            previous_hash = frame_hash
            consecutive_skips = 0
        return tuple(selected)

    def _difference_hash(self, frame_path: Path) -> int:
        try:
            from PIL import Image
        except ImportError as error:
            raise PipelineOperationalError("Pillow is required for frame deduplication") from error
        with Image.open(frame_path) as image:
            grayscale = image.convert("L").resize(
                (self._difference_hash_size + 1, self._difference_hash_size),
                Image.Resampling.LANCZOS,
            )
            pixels = cast(list[int], list(grayscale.get_flattened_data()))
        result = 0
        row_width = self._difference_hash_size + 1
        for row in range(self._difference_hash_size):
            offset = row * row_width
            for column in range(self._difference_hash_size):
                result <<= 1
                result |= pixels[offset + column] > pixels[offset + column + 1]
        return result

    def _prepare_input(self, frame_path: Path) -> tuple[str | object, float, float]:
        if self._maximum_image_dimension is None:
            return str(frame_path), 1.0, 1.0
        try:
            import numpy as np
            from PIL import Image
        except ImportError as error:
            raise PipelineOperationalError(
                "Pillow and NumPy are required to prepare downscaled OCR input"
            ) from error
        with Image.open(frame_path) as image:
            original_width, original_height = image.size
            maximum = max(original_width, original_height)
            if maximum <= self._maximum_image_dimension:
                return str(frame_path), 1.0, 1.0
            scale = self._maximum_image_dimension / maximum
            width = max(1, round(original_width * scale))
            height = max(1, round(original_height * scale))
            resized = image.convert("RGB").resize((width, height), Image.Resampling.LANCZOS)
            return (
                np.asarray(resized)[:, :, ::-1].copy(),
                width / original_width,
                height / original_height,
            )

    def _get_engine(self) -> _PaddleOcrEngine:
        if self._engine is not None:
            return self._engine
        factory = self._engine_factory or self._default_engine_factory
        self._engine = factory()
        return self._engine

    def _default_engine_factory(self) -> _PaddleOcrEngine:
        try:
            from paddleocr import PaddleOCR  # type: ignore[import-untyped]
        except ImportError as error:
            message = "PaddleOCR is not installed in the active environment"
            raise PipelineOperationalError(message) from error
        return cast(
            _PaddleOcrEngine,
            PaddleOCR(
                lang=self._language,
                ocr_version=self._ocr_version,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                enable_mkldnn=self._enable_mkldnn,
            ),
        )

    @staticmethod
    def _map_polygon(
        polygon: object, scale_x: float, scale_y: float
    ) -> tuple[tuple[int, int], ...]:
        if not isinstance(polygon, list):
            raise PipelineOperationalError("PaddleOCR returned an invalid detection polygon")
        points: list[tuple[int, int]] = []
        for point in polygon:
            if not isinstance(point, list) or len(point) != 2:
                raise PipelineOperationalError("PaddleOCR returned an invalid polygon point")
            points.append((round(float(point[0]) / scale_x), round(float(point[1]) / scale_y)))
        return tuple(points)

    @staticmethod
    def _engine_version() -> str | None:
        try:
            return distribution_version("paddleocr")
        except ModuleNotFoundError:
            return None


class FasterWhisperTranscriber:
    """Local CPU transcription adapter backed by faster-whisper.

    The model is created only on the first transcription request. This avoids
    model initialization during configuration checks and unit tests.
    """

    def __init__(
        self,
        *,
        model_name: str = "turbo",
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 5,
        local_files_only: bool = False,
        pipeline_factory: WhisperPipelineFactory | None = None,
    ) -> None:
        if beam_size < 1:
            raise ValueError("beam_size must be at least 1")
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.local_files_only = local_files_only
        self._pipeline_factory = pipeline_factory
        self._pipeline: _BatchedWhisperPipeline | None = None

    @property
    def version(self) -> str:
        return (
            "faster-whisper-v1:"
            f"model={self.model_name}:device={self.device}:compute={self.compute_type}:"
            f"beam={self.beam_size}:local-only={self.local_files_only}"
        )

    def transcribe(self, audio: AudioArtifact) -> TranscriptArtifact:
        try:
            segments, info = self._get_pipeline().transcribe(
                str(audio.audio_path), beam_size=self.beam_size
            )
            evidence_segments = tuple(
                EvidenceSegment(
                    evidence_id=f"transcript-{index}",
                    source_kind=SourceKind.TRANSCRIPT,
                    text=segment.text.strip(),
                    start_seconds=segment.start,
                    end_seconds=segment.end,
                )
                for index, segment in enumerate(segments, start=1)
                if segment.text.strip()
            )
        except PipelineOperationalError:
            raise
        except Exception as error:
            raise PipelineOperationalError("Local faster-whisper transcription failed") from error

        return TranscriptArtifact(
            segments=evidence_segments,
            language=info.language,
            language_probability=info.language_probability,
            model_name=self.model_name,
            compute_type=self.compute_type,
        )

    def _get_pipeline(self) -> _BatchedWhisperPipeline:
        if self._pipeline is not None:
            return self._pipeline
        factory = self._pipeline_factory
        if factory is None:
            factory = self._create_default_pipeline
        self._pipeline = factory(self.model_name, self.device, self.compute_type)
        return self._pipeline

    def _create_default_pipeline(
        self, model_name: str, device: str, compute_type: str
    ) -> _BatchedWhisperPipeline:
        return self._default_pipeline_factory(
            model_name,
            device,
            compute_type,
            local_files_only=self.local_files_only,
        )

    @staticmethod
    def _default_pipeline_factory(
        model_name: str, device: str, compute_type: str, *, local_files_only: bool
    ) -> _BatchedWhisperPipeline:
        try:
            from faster_whisper import (  # type: ignore[import-untyped]
                BatchedInferencePipeline,
                WhisperModel,
            )
        except ImportError as error:
            message = "faster-whisper is not installed in the active environment"
            raise PipelineOperationalError(message) from error
        model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            local_files_only=local_files_only,
        )
        return cast(_BatchedWhisperPipeline, BatchedInferencePipeline(model=model))


def _require_command(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise PipelineOperationalError(f"Required command is not available on PATH: {name}")
    return executable


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        detail = error.stderr.strip() or error.stdout.strip() or "no diagnostic output"
        raise PipelineOperationalError(f"External command failed: {detail}") from error
