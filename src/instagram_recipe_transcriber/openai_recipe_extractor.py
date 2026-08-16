"""OpenAI-backed recipe extraction with evidence-grounding checks."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from pydantic import BaseModel, Field

from .errors import PipelineOperationalError
from .models import (
    ApiUsage,
    CompletenessFinding,
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

_PROMPT_VERSION = "5"
_SYSTEM_PROMPT = """Extract a recipe only from the supplied evidence segments.

Return a proposed structured recipe. Every original_text value must be copied verbatim
from one of its cited evidence segments. Every cited evidence_id must come from the
input. Do not infer ingredients, quantities, units, instructions, or a title. If the
evidence contains contradictory recipe claims, describe only that contradiction in
conflicts rather than filling in a value. A conflict must identify incompatible recipe
facts that prevent a reliable structured recipe.

Prefer caption and transcript evidence when they agree. Low-confidence, malformed, or
obviously garbled OCR must not by itself create a conflict against that agreement. Keep
such OCR out of the recipe fields and conflicts; it remains in the supplied artifact for
human review and debugging. Extract a title only when the creator's title is explicitly
present in cited caption, transcript, or OCR evidence; prefer caption or transcript.

Also return completeness_findings for every supported reason the proposed recipe is
unsafe to auto-publish. Use unquantified_core_ingredient when a main protein, starch,
produce component, or other essential component lacks an explicit amount. Do not use
that finding for minor flexible items such as cooking spray, garnish lettuce/tomatoes,
onion, seasoning, salt, pepper, or "to taste" items. Use missing_critical_step when
the evidence lacks an essential preparation or final cooking/baking/serving step (for
example, formed burger patties without a supported instruction to cook them). Cite the
evidence that establishes the incomplete context. Do not invent missing values.

Automatic publication also requires creator-stated serving and nutrition information.
Return missing_servings when no explicit recipe yield or serving count is present.
Return missing_calories when no explicit calorie value is present. Return missing_macros
when any of protein, carbohydrates/carbs, or fat is absent. These are completeness
findings only: do not extract nutrition into recipe fields yet. For an absence finding,
cite the evidence segment that establishes the recipe context; never claim a value that
is not present.

Ignore emojis completely: do not include them in extracted fields and do not mention
them in conflicts. Do not include macros, calories, protein counts, or serving counts
in recipe fields. Marketing text and optional tips are not recipe fields unless they
are explicitly needed as an ingredient quantity or an instruction. Use the evidence
order for instruction order."""


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
    completeness_findings: list[_ProposedCompletenessFinding] = Field(default_factory=list)


class _ProposedCompletenessFinding(BaseModel):
    code: str
    message: str
    evidence_ids: list[str]


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
        conflicts = _supported_conflicts(proposed.conflicts, evidence)
        title = _verified_title(proposed, evidence_by_id, conflicts)
        ingredients = _verified_ingredients(proposed, evidence_by_id, conflicts)
        instructions = _verified_instructions(proposed, evidence_by_id, conflicts)
        completeness_findings = _verified_completeness_findings(
            proposed.completeness_findings,
            evidence_by_id,
        )
        return RecipeExtractionArtifact(
            recipe=RecipeCandidate(
                title=title,
                ingredients=tuple(ingredients),
                instructions=tuple(instructions),
                conflicts=tuple(conflicts),
                completeness_findings=tuple(completeness_findings),
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
    evidence = _supported_title_evidence(
        proposed.title, proposed.title_evidence_ids, evidence_by_id
    )
    if evidence:
        return proposed.title
    conflicts.append("OpenAI proposed a title without supporting source evidence.")
    return None


def _supported_conflicts(
    proposed_conflicts: list[str], evidence: tuple[EvidenceSegment, ...]
) -> list[str]:
    """Discard conflicts whose only identifiable source is weak OCR.

    OCR evidence is intentionally retained in the OCR artifact. This guard avoids
    allowing a malformed low-confidence reading (for example ``MAES 10``) to
    overturn matching caption/transcript evidence in a structured recipe.
    """
    weak_ocr_words = {
        word
        for segment in evidence
        if segment.source_kind is SourceKind.OCR and _evidence_confidence(segment) < 0.85
        for word in _title_normalized(segment.text).split()
        if any(character.isalpha() for character in word)
    }
    if not weak_ocr_words:
        return list(proposed_conflicts)
    return [
        conflict
        for conflict in proposed_conflicts
        if not weak_ocr_words.intersection(_title_normalized(conflict).split())
    ]


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


def _verified_completeness_findings(
    proposed: list[_ProposedCompletenessFinding],
    evidence_by_id: dict[str, EvidenceSegment],
) -> list[CompletenessFinding]:
    findings: list[CompletenessFinding] = []
    for finding in proposed:
        evidence = tuple(
            evidence_by_id[evidence_id]
            for evidence_id in dict.fromkeys(finding.evidence_ids)
            if evidence_id in evidence_by_id
        )
        if not evidence:
            continue
        findings.append(
            CompletenessFinding(
                code=finding.code,
                message=finding.message,
                evidence=tuple(
                    EvidenceReference(evidence_id=segment.evidence_id) for segment in evidence
                ),
            )
        )
    return findings


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


def _supported_title_evidence(
    title: str,
    evidence_ids: list[str],
    evidence_by_id: dict[str, EvidenceSegment],
) -> tuple[EvidenceSegment, ...]:
    normalized_title = _title_normalized(title)
    if not normalized_title:
        return ()
    supported: list[EvidenceSegment] = []
    for evidence_id in dict.fromkeys(evidence_ids):
        segment = evidence_by_id.get(evidence_id)
        if segment is not None and normalized_title in _title_normalized(segment.text):
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


def _title_normalized(value: str) -> str:
    return " ".join(re.findall(r"[\w%]+", value.casefold()))
