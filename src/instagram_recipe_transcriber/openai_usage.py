"""Reusable mapping of OpenAI Responses API usage into persisted application models."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from .models import ApiUsage

_TOKENS_PER_MILLION = Decimal("1000000")


class OpenAiUsageTracker:
    """Maps one successful OpenAI response into price-free, provider-reported usage."""

    provider = "openai"

    def capture(self, response: object, *, requested_model: str) -> ApiUsage:
        """Capture token counts without assuming a particular OpenAI SDK response class."""
        usage = _value(response, "usage")
        input_details = _value(usage, "input_tokens_details")
        output_details = _value(usage, "output_tokens_details")
        return ApiUsage(
            provider=self.provider,
            model=_string(_value(response, "model")) or requested_model,
            request_count=1,
            input_tokens=_nonnegative_int(_value(usage, "input_tokens")),
            cached_input_tokens=_nonnegative_int(_value(input_details, "cached_tokens")),
            cache_write_tokens=_nonnegative_int(_value(input_details, "cache_write_tokens")),
            output_tokens=_nonnegative_int(_value(usage, "output_tokens")),
            reasoning_tokens=_nonnegative_int(_value(output_details, "reasoning_tokens")),
            total_tokens=_nonnegative_int(_value(usage, "total_tokens")),
        )


class OpenAiTokenPricing(BaseModel):
    """Caller-maintained OpenAI text-token prices, expressed per million tokens."""

    model_config = ConfigDict(frozen=True)

    model: str = Field(min_length=1)
    input_usd_per_million: Decimal = Field(ge=0)
    cached_input_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)
    cache_write_usd_per_million: Decimal | None = Field(default=None, ge=0)


class OpenAiCostEstimate(BaseModel):
    """A reporting-layer cost estimate derived from recorded usage and explicit prices."""

    model_config = ConfigDict(frozen=True)

    usage: ApiUsage
    pricing: OpenAiTokenPricing
    cost_usd: Decimal = Field(ge=0)


class OpenAiCostCalculator:
    """Calculates costs from a caller-maintained pricing table, never from the extractor."""

    def __init__(self, pricing: Iterable[OpenAiTokenPricing]) -> None:
        self._pricing_by_model = {item.model: item for item in pricing}

    def estimate(self, usage: ApiUsage) -> OpenAiCostEstimate:
        if usage.provider != OpenAiUsageTracker.provider:
            raise ValueError("OpenAiCostCalculator requires OpenAI usage")
        try:
            pricing = self._pricing_by_model[usage.model]
        except KeyError as error:
            raise ValueError(f"No pricing configured for OpenAI model: {usage.model}") from error

        cached_tokens = min(usage.cached_input_tokens, usage.input_tokens)
        remaining_input_tokens = usage.input_tokens - cached_tokens
        cache_write_tokens = min(usage.cache_write_tokens, remaining_input_tokens)
        ordinary_input_tokens = remaining_input_tokens - cache_write_tokens
        cache_write_rate = pricing.cache_write_usd_per_million or pricing.input_usd_per_million
        cost = (
            Decimal(ordinary_input_tokens) * pricing.input_usd_per_million
            + Decimal(cached_tokens) * pricing.cached_input_usd_per_million
            + Decimal(cache_write_tokens) * cache_write_rate
            + Decimal(usage.output_tokens) * pricing.output_usd_per_million
        ) / _TOKENS_PER_MILLION
        return OpenAiCostEstimate(usage=usage, pricing=pricing, cost_usd=cost)


def _value(value: object | None, name: str) -> object | None:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _string(value: object | None) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _nonnegative_int(value: object | None) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0
