from decimal import Decimal

import pytest

from instagram_recipe_transcriber.models import ApiUsage
from instagram_recipe_transcriber.openai_usage import OpenAiCostCalculator, OpenAiTokenPricing


def test_cost_calculator_applies_input_cached_and_output_rates() -> None:
    usage = ApiUsage(
        provider="openai",
        model="gpt-5.4-mini-2026-03-17",
        input_tokens=812,
        output_tokens=728,
        total_tokens=1540,
    )
    calculator = OpenAiCostCalculator(
        [
            OpenAiTokenPricing(
                model="gpt-5.4-mini-2026-03-17",
                input_usd_per_million=Decimal("0.75"),
                cached_input_usd_per_million=Decimal("0.075"),
                output_usd_per_million=Decimal("4.50"),
            )
        ]
    )

    estimate = calculator.estimate(usage)

    assert estimate.cost_usd == Decimal("0.003885")


def test_cost_calculator_uses_configured_cache_write_rate() -> None:
    usage = ApiUsage(
        provider="openai",
        model="example-model",
        input_tokens=1000,
        cached_input_tokens=200,
        cache_write_tokens=300,
        output_tokens=100,
    )
    calculator = OpenAiCostCalculator(
        [
            OpenAiTokenPricing(
                model="example-model",
                input_usd_per_million=Decimal("1"),
                cached_input_usd_per_million=Decimal("0.1"),
                cache_write_usd_per_million=Decimal("1.25"),
                output_usd_per_million=Decimal("2"),
            )
        ]
    )

    estimate = calculator.estimate(usage)

    assert estimate.cost_usd == Decimal("0.001095")


def test_cost_calculator_requires_an_explicit_model_price() -> None:
    calculator = OpenAiCostCalculator([])

    with pytest.raises(ValueError, match="No pricing configured"):
        calculator.estimate(ApiUsage(provider="openai", model="unknown-model"))
