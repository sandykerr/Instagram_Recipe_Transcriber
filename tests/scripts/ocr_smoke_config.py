"""Configuration used by the exploratory OCR smoke test."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrameDeduplicationConfig:
    """Controls adjacent-frame perceptual duplicate removal."""

    enabled: bool = True
    hash_size: int = 16
    hamming_threshold: int = 60
    max_consecutive_skips: int = 2

    def __post_init__(self) -> None:
        if self.hash_size < 4:
            raise ValueError("hash_size must be at least 4")
        bit_count = self.hash_size * self.hash_size
        if not 0 <= self.hamming_threshold <= bit_count:
            raise ValueError(
                f"hamming_threshold must be between 0 and {bit_count}"
            )
        if self.max_consecutive_skips < 0:
            raise ValueError("max_consecutive_skips must be nonnegative")


@dataclass(frozen=True)
class OcrImageConfig:
    """Controls the in-memory image passed to OCR inference."""

    max_dimension: int | None = None

    def __post_init__(self) -> None:
        if self.max_dimension is not None and self.max_dimension <= 0:
            raise ValueError("max_dimension must be positive when set")
