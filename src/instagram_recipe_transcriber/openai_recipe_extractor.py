"""OpenAI-backed recipe extraction with evidence-grounding checks."""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel

from .errors import PipelineOperationalError
from .models import (
    ApiUsage,
    EvidenceReference,
    EvidenceSegment,
    Ingredient,
    Instruction,
    OcrArtifact,
    RecipeCandidate,
    RecipeExtractionArtifact,
    SourceArtifact,
    SourceKind,
    TranscriptArtifact,
)
from .openai_usage import OpenAiUsageTracker

_PROMPT_VERSION = "2"
_SYSTEM_PROMPT = """Extract a recipe only from the supplied evidence segments.

Return a proposed structured recipe. Every original_text value must be copied verbatim
from one of its cited evidence segments. Every cited evidence_id must come from the
input. Do not infer ingredients, quantities, units, instructions, or a title. If the
evidence contains contradictory recipe claims, describe only that contradiction in
conflicts rather than filling in a value. A conflict must identify incompatible recipe
facts that prevent a reliable structured recipe.

Ignore emojis completely: do not include them in extracted fields and do not mention
them in conflicts. Also ignore macros, calories, protein counts, serving counts,
marketing text, and optional tips unless they are explicitly needed as an ingredient
quantity or an instruction. Their presence or omission is not a conflict. Use the
evidence order for instruction order."""


class _ProposedIngredient(BaseModel):
    original_text: str
    name: str
    quantity_original: str | None
    unit_original: str | None
    evidence_ids: list[str]


class _ProposedInstruction(BaseModel):
    original_text: str
    evidence_ids: list[str]


class _ProposedRecipe(BaseModel):
    title: str | None
    title_evidence_ids: list[str]
    ingredients: list[_ProposedIngredient]
    instructions: list[_ProposedInstruction]
    conflicts: list[str]


class OpenAiRecipeExtractor:
    """Use the Responses API, then independently verify its evidence citations.

    The API response is deliberately treated as a proposed interpretation. Recipe
    fields are discarded when their cited source is unknown or does not contain the
    proposed verbatim source text.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "gpt-5.4-mini",
        client: Any | None = None,
        usage_tracker: OpenAiUsageTracker | None = None,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        self._client = client
        self._model = model
        self._usage_tracker = usage_tracker or OpenAiUsageTracker()

    @property
    def version(self) -> str:
        return f"openai-recipe-extractor-v{_PROMPT_VERSION}:model={self._model}"

    def extract(
        self,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        ocr: OcrArtifact | None,
    ) -> RecipeCandidate:
        return self.extract_artifact(source, transcript, ocr).recipe

    def extract_artifact(
        self,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        ocr: OcrArtifact | None,
    ) -> RecipeExtractionArtifact:
        evidence = _all_evidence(source, transcript, ocr)
        proposed, usage = self._propose(evidence)
        evidence_by_id = {segment.evidence_id: segment for segment in evidence}
        conflicts = list(proposed.conflicts)
        title = _verified_title(proposed, evidence_by_id, conflicts)
        ingredients = _verified_ingredients(proposed, evidence_by_id, conflicts)
        instructions = _verified_instructions(proposed, evidence_by_id, conflicts)
        return RecipeExtractionArtifact(
            recipe=RecipeCandidate(
                title=title,
                ingredients=tuple(ingredients),
                instructions=tuple(instructions),
                conflicts=tuple(conflicts),
            ),
            usage=usage,
        )

    def _propose(self, evidence: tuple[EvidenceSegment, ...]) -> tuple[_ProposedRecipe, ApiUsage]:
        try:
            response = self._get_client().responses.parse(
                model=self._model,
                input=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": _serialized_evidence(evidence)},
                ],
                text_format=_ProposedRecipe,
            )
        except PipelineOperationalError:
            raise
        except Exception as error:
            raise PipelineOperationalError("OpenAI recipe extraction request failed.") from error
        if not isinstance(response.output_parsed, _ProposedRecipe):
            raise PipelineOperationalError(
                "OpenAI recipe extraction returned no structured result."
            )
        return response.output_parsed, self._usage_tracker.capture(
            response, requested_model=self._model
        )

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key:
            raise PipelineOperationalError(
                "OpenAI recipe extraction requires OPENAI_API_KEY or an injected API key."
            )
        try:
            from openai import OpenAI
        except ImportError as error:
            raise PipelineOperationalError(
                "OpenAI recipe extraction requires the optional 'openai' package."
            ) from error
        return OpenAI(api_key=self._api_key)


def _all_evidence(
    source: SourceArtifact, transcript: TranscriptArtifact, ocr: OcrArtifact | None
) -> tuple[EvidenceSegment, ...]:
    segments: list[EvidenceSegment] = []
    if source.caption is not None:
        segments.append(source.caption)
    segments.extend(transcript.segments)
    if ocr is not None:
        segments.extend(ocr.segments)
    return tuple(segments)


def _serialized_evidence(evidence: tuple[EvidenceSegment, ...]) -> str:
    return json.dumps(
        {
            "evidence": [
                {
                    "evidence_id": segment.evidence_id,
                    "source_kind": segment.source_kind,
                    "text": segment.text,
                }
                for segment in evidence
            ]
        },
        ensure_ascii=False,
    )


def _verified_title(
    proposed: _ProposedRecipe,
    evidence_by_id: dict[str, EvidenceSegment],
    conflicts: list[str],
) -> str | None:
    if proposed.title is None:
        return None
    evidence = _supported_evidence(
        proposed.title, proposed.title_evidence_ids, evidence_by_id
    )
    if evidence:
        return proposed.title
    conflicts.append("OpenAI proposed a title without supporting source evidence.")
    return None


def _verified_ingredients(
    proposed: _ProposedRecipe,
    evidence_by_id: dict[str, EvidenceSegment],
    conflicts: list[str],
) -> list[Ingredient]:
    ingredients: list[Ingredient] = []
    for item in proposed.ingredients:
        evidence = _supported_evidence(item.original_text, item.evidence_ids, evidence_by_id)
        if not evidence:
            conflicts.append(
                f"OpenAI proposed an ingredient without supporting source evidence: {item.name}"
            )
            continue
        ingredients.append(
            Ingredient(
                original_text=item.original_text,
                name=item.name,
                quantity_original=item.quantity_original,
                unit_original=item.unit_original,
                evidence=tuple(
                    EvidenceReference(evidence_id=segment.evidence_id) for segment in evidence
                ),
                confidence=max(_evidence_confidence(segment) for segment in evidence),
            )
        )
    return ingredients


def _verified_instructions(
    proposed: _ProposedRecipe,
    evidence_by_id: dict[str, EvidenceSegment],
    conflicts: list[str],
) -> list[Instruction]:
    instructions: list[Instruction] = []
    for item in proposed.instructions:
        evidence = _supported_evidence(item.original_text, item.evidence_ids, evidence_by_id)
        if not evidence:
            conflicts.append("OpenAI proposed an instruction without supporting source evidence.")
            continue
        instructions.append(
            Instruction(
                original_text=item.original_text,
                sequence=len(instructions) + 1,
                evidence=tuple(
                    EvidenceReference(evidence_id=segment.evidence_id) for segment in evidence
                ),
                confidence=max(_evidence_confidence(segment) for segment in evidence),
            )
        )
    return instructions


def _supported_evidence(
    original_text: str,
    evidence_ids: list[str],
    evidence_by_id: dict[str, EvidenceSegment],
) -> tuple[EvidenceSegment, ...]:
    if not original_text.strip():
        return ()
    normalized_text = _normalized(original_text)
    supported: list[EvidenceSegment] = []
    for evidence_id in dict.fromkeys(evidence_ids):
        segment = evidence_by_id.get(evidence_id)
        if segment is not None and normalized_text in _normalized(segment.text):
            supported.append(segment)
    return tuple(supported)


def _evidence_confidence(segment: EvidenceSegment) -> float:
    if segment.confidence is not None:
        return segment.confidence
    if segment.source_kind is SourceKind.CAPTION:
        return 0.9
    return 0.7


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())
