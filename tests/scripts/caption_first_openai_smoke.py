"""Paid/manual caption-first OpenAI vertical-slice smoke test.

This script makes one real OpenAI API request. It is deliberately outside the
normal pytest suite and must be invoked explicitly by a developer.
"""

from __future__ import annotations

import argparse
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import HttpUrl

from instagram_recipe_transcriber.adapters import (
    FasterWhisperTranscriber,
    FfmpegAudioExtractor,
    FfmpegFrameExtractor,
    LocalFileSourceLoader,
    PaddleOcrExtractor,
)
from instagram_recipe_transcriber.artifacts import JsonArtifactStore
from instagram_recipe_transcriber.models import (
    ApiUsage,
    AudioArtifact,
    FrameExtractionArtifact,
    OcrArtifact,
    OcrDecisionArtifact,
    RecipeCandidate,
    RecipeJob,
    RecipeOutcome,
    SourceArtifact,
    TranscriptArtifact,
)
from instagram_recipe_transcriber.openai_recipe_extractor import OpenAiRecipeExtractor
from instagram_recipe_transcriber.openai_usage import (
    OpenAiCostCalculator,
    OpenAiTokenPricing,
    OpenAiUsageTracker,
)
from instagram_recipe_transcriber.pipeline import PipelineRunner
from instagram_recipe_transcriber.recipe_processing import RecipeValidator

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
REEL_URL = "https://www.instagram.com/reel/DVJBGzyk8E5/?igsh=MTdicnEzaTM3aGE5ag=="
CAPTION = """Easy High Protein Honey BBQ Chicken Mac & Cheese 🍗🧀
59g Protein Meal Prep💪🏼

(Macros Per Serve, Makes 4)
587 Calories
67.5gC | 9gF | 59gP

Ingredients:
• 700g Diced Chicken Breast (raw weight)
• 1 Tsp Each, Salt, Onion Powder, Smoked Paprika, Garlic Powder
• 1 Tsp Olive Oil
• 25g Honey
• 85g Reduced Sugar or Sugar Free BBQ Sauce (Hughes Sugar Free)
• 320g Macaroni Pasta (uncooked weight)
• 340ml Fat Free Evaporated Milk (Carnation Light & Creamy)
• 80g Light Cream Cheese
• 1.5 Tsp Chicken Stock Powder
• 100g Grated Light Cheddar Cheese (Dairyworks Natural)

Instructions:
""" + "\n".join(
    (
        "1. Season diced chicken with salt, onion powder, smoked paprika & garlic powder, "
        "add olive oil & mix well.",
        "2. Cook chicken on medium to high heat for around 4 minutes each side or until "
        "fully cooked. Remove from heat then add honey & BBQ sauce.",
        "3. Mix until the chicken is evenly coated in the sauce.",
        "4. Cook macaroni in boiling water according to packet instructions.",
        "5. While pasta cooks, add evaporated milk, light cream cheese, chicken stock powder "
        "& grated cheese to a pan on medium heat, stir until smooth & creamy.",
        "6. Add drained pasta into the cheese sauce & mix until fully coated.",
        "7. Divide the Honey BBQ Chicken & Mac & Cheese evenly into 4 meal prep servings.",
        "",
        "Pro Tip:",
        "Add a splash of milk before reheating to keep the mac & cheese creamy & mix through well.",
    )
)


class CountingResponses:
    def __init__(self, delegate: Any, model: str) -> None:
        self._delegate = delegate
        self._model = model
        self._usage_tracker = OpenAiUsageTracker()
        self.parse_calls = 0
        self.recorded_usage: list[ApiUsage] = []

    def parse(self, *args: object, **kwargs: object) -> Any:
        self.parse_calls += 1
        response = self._delegate.parse(*args, **kwargs)
        usage = self._usage_tracker.capture(response, requested_model=self._model)
        self.recorded_usage.append(usage)
        return response

    def totals(self) -> tuple[int, int, int, int, int, int, int]:
        return (
            sum(record.request_count for record in self.recorded_usage),
            sum(record.input_tokens for record in self.recorded_usage),
            sum(record.cached_input_tokens for record in self.recorded_usage),
            sum(record.cache_write_tokens for record in self.recorded_usage),
            sum(record.output_tokens for record in self.recorded_usage),
            sum(record.reasoning_tokens for record in self.recorded_usage),
            sum(record.total_tokens for record in self.recorded_usage),
        )


class CountingOpenAiClient:
    def __init__(self, delegate: Any, model: str) -> None:
        self.responses = CountingResponses(delegate.responses, model)


class ForbiddenAudioExtractor(FfmpegAudioExtractor):
    def __init__(self, working_root: Path) -> None:
        super().__init__(working_root)
        self.calls = 0

    def extract(self, source: SourceArtifact) -> AudioArtifact:
        self.calls += 1
        raise AssertionError("Caption-first READY path invoked audio extraction")


class ForbiddenFasterWhisper(FasterWhisperTranscriber):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def transcribe(self, audio: AudioArtifact) -> TranscriptArtifact:
        self.calls += 1
        raise AssertionError("Caption-first READY path invoked Faster-Whisper")


class ForbiddenFrameExtractor(FfmpegFrameExtractor):
    def __init__(self, working_root: Path) -> None:
        super().__init__(working_root)
        self.calls = 0

    def extract(self, source: SourceArtifact) -> FrameExtractionArtifact:
        self.calls += 1
        raise AssertionError("Caption-first READY path invoked frame extraction")


class ForbiddenOcrExtractor(PaddleOcrExtractor):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def extract(self, frames: FrameExtractionArtifact) -> OcrArtifact:
        self.calls += 1
        raise AssertionError("Caption-first READY path invoked OCR")


class ForbiddenOcrGate:
    version = "forbidden-caption-first-ocr-gate-v1"

    def __init__(self) -> None:
        self.calls = 0

    def decide(
        self,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        recipe: RecipeCandidate,
    ) -> OcrDecisionArtifact:
        self.calls += 1
        raise AssertionError("Caption-first READY path invoked the OCR gate")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gpt-5.4-mini")
    parser.add_argument("--input-usd-per-million", type=Decimal, default=Decimal("0.75"))
    parser.add_argument("--cached-input-usd-per-million", type=Decimal, default=Decimal("0.075"))
    parser.add_argument("--output-usd-per-million", type=Decimal, default=Decimal("4.50"))
    parser.add_argument(
        "--working-root",
        type=Path,
        default=REPOSITORY_ROOT / "data" / "working" / "manual-openai-smokes",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        raise RuntimeError("OPENAI_API_KEY must be present for this paid/manual smoke test")

    try:
        from openai import OpenAI
    except ImportError as error:
        message = "Install the project OpenAI dependency before running this test"
        raise RuntimeError(message) from error

    run_name = f"dvjbgzyk8e5-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    run_root = args.working_root.expanduser().resolve() / run_name
    counting_client = CountingOpenAiClient(OpenAI(), args.model)
    extractor = OpenAiRecipeExtractor(client=counting_client, model=args.model)
    audio = ForbiddenAudioExtractor(run_root)
    transcriber = ForbiddenFasterWhisper()
    frames = ForbiddenFrameExtractor(run_root)
    ocr = ForbiddenOcrExtractor()
    ocr_gate = ForbiddenOcrGate()
    runner = PipelineRunner(
        artifact_store=JsonArtifactStore(run_root / "artifacts"),
        source_loader=LocalFileSourceLoader(),
        audio_extractor=audio,
        transcriber=transcriber,
        ocr_gate=ocr_gate,
        frame_extractor=frames,
        ocr_extractor=ocr,
        recipe_extractor=extractor,
        recipe_validator=RecipeValidator(),
    )
    job = RecipeJob(
        recipe_id="dvjbgzyk8e5-caption-first",
        source_url=HttpUrl(REEL_URL),
        caption_text=CAPTION,
    )

    assert job.caption_text == CAPTION
    assert isinstance(extractor, OpenAiRecipeExtractor)

    started = time.perf_counter()
    first = runner.run(job)
    first_elapsed = time.perf_counter() - started
    assert first.validation.outcome is RecipeOutcome.READY
    assert counting_client.responses.parse_calls == 1
    assert first.recipe_usage is not None
    first_usage = first.recipe_usage
    cost_estimate = OpenAiCostCalculator(
        [
            OpenAiTokenPricing(
                model=first_usage.model,
                input_usd_per_million=args.input_usd_per_million,
                cached_input_usd_per_million=args.cached_input_usd_per_million,
                output_usd_per_million=args.output_usd_per_million,
            )
        ]
    ).estimate(first_usage)
    first_totals = counting_client.responses.totals()
    assert first_totals[0] == first_usage.request_count
    assert first_totals[-1] == first_usage.total_tokens
    forbidden_calls = (audio.calls, transcriber.calls, frames.calls, ocr.calls, ocr_gate.calls)
    assert forbidden_calls == (0, 0, 0, 0, 0)

    started = time.perf_counter()
    second = runner.run(job)
    cached_elapsed = time.perf_counter() - started
    assert second == first
    assert counting_client.responses.parse_calls == 1, "Cached rerun made an OpenAI API call"
    assert second.recipe_usage == first_usage
    assert counting_client.responses.totals() == first_totals, (
        "Cached rerun increased OpenAI request or token counts"
    )
    forbidden_calls = (audio.calls, transcriber.calls, frames.calls, ocr.calls, ocr_gate.calls)
    assert forbidden_calls == (0, 0, 0, 0, 0)

    result_path = run_root / "pipeline_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(first.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print("PASS: caption was supplied to RecipeJob.caption_text")
    print(f"PASS: OpenAiRecipeExtractor used model {args.model}")
    print("PASS: OPENAI_API_KEY is present")
    print("PASS: result reached READY")
    print("PASS: Faster-Whisper, frame extraction, OCR, and the OCR gate were not invoked")
    print("PASS: cached rerun made no OpenAI API call")
    print("PASS: cached rerun did not increase recorded OpenAI request or token counts")
    print(f"OpenAI usage: {first_usage.model_dump_json()}")
    print(f"Estimated API cost (USD): ${cost_estimate.cost_usd}")
    print(f"First run: {first_elapsed:.2f}s; cached rerun: {cached_elapsed:.2f}s")
    print(
        f"Ingredients: {len(first.recipe.ingredients)}; "
        f"instructions: {len(first.recipe.instructions)}"
    )
    print(f"Result: {result_path}")


if __name__ == "__main__":
    main()
