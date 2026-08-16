"""Conservative deterministic recipe extraction and validation."""

from __future__ import annotations

import re
from collections import defaultdict

from .models import (
    EvidenceReference,
    EvidenceSegment,
    Ingredient,
    Instruction,
    OcrArtifact,
    RecipeCandidate,
    RecipeOutcome,
    SourceArtifact,
    SourceKind,
    TranscriptArtifact,
    ValidationArtifact,
    ValidationFinding,
)

_UNITS = (
    "cups?|c|tablespoons?|tbsp|teaspoons?|tsp|milliliters?|ml|liters?|l|"
    "ounces?|oz|grams?|g|kilograms?|kg|pounds?|lbs?|cloves?|cans?|"
    "packages?|pkgs?|scoops?|slices?"
)
_QUANTITY = r"(?:\d+(?:\.\d+|/\d+|\s+\d+/\d+)?|[¼½¾⅓⅔]|one|two|three|four)"
_MEASURED_INGREDIENT_PATTERN = re.compile(
    rf"^\s*[-•*]?\s*(?P<quantity>{_QUANTITY})\s*"
    rf"(?P<unit>{_UNITS})\b\s*(?P<name>.+?)\s*$",
    flags=re.IGNORECASE,
)
_COUNT_INGREDIENT_PATTERN = re.compile(
    rf"^\s*[-•*]?\s*(?P<quantity>{_QUANTITY})\s+(?P<name>[A-Za-z][A-Za-z0-9 ,&'()/-]+?)\s*$",
    flags=re.IGNORECASE,
)
_TO_TASTE_PATTERN = re.compile(
    r"^\s*[-•*]?\s*(?P<name>[A-Za-z][A-Za-z0-9 ,&'()/-]+?)\s+(?P<quantity>to taste|as needed)\s*$",
    flags=re.IGNORECASE,
)
_NUMERIC_DESCRIPTOR_PATTERN = re.compile(
    r"^\s*[-•*]?\s*(?P<name>(?:\d+(?:\.\d+)?%|\d+/\d+\s+fat)\s+.+?)\s*$",
    flags=re.IGNORECASE,
)
_UNRESOLVED_NUMERIC_NAME_PATTERN = re.compile(r"^(?:\d|[¼½¾⅓⅔])")
_INSTRUCTION_PATTERN = re.compile(
    r"^\s*(?:[-•*]|\d+[.)])?\s*(?:first|then|next|finally)?[,:]?\s*"
    r"(?:add|bake|blend|boil|combine|cook|drain|fold|heat|knead|mix|pour|"
    r"preheat|reduce|remove|roast|saute|season|serve|simmer|stir|whisk)\b",
    flags=re.IGNORECASE,
)
_NON_INGREDIENT_NAMES = {"cal", "cals", "calorie", "calories", "kcal"}


class RuleBasedRecipeExtractor:
    """Extracts only explicit, high-signal recipe claims from source evidence."""

    version = "rule-based-recipe-extractor-v4"

    def extract(
        self,
        source: SourceArtifact,
        transcript: TranscriptArtifact,
        ocr: OcrArtifact | None,
    ) -> RecipeCandidate:
        evidence = self._all_evidence(source, transcript, ocr)
        title = self._extract_title(evidence)
        ingredients = self._extract_ingredients(evidence)
        instructions = self._extract_instructions(evidence)
        conflicts = self._find_quantity_conflicts(ingredients, evidence)
        return RecipeCandidate(
            title=title,
            ingredients=ingredients,
            instructions=instructions,
            conflicts=conflicts,
        )

    @staticmethod
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

    @staticmethod
    def _extract_title(evidence: tuple[EvidenceSegment, ...]) -> str | None:
        for segment in evidence:
            text = segment.text.strip()
            words = text.split()
            if (
                segment.source_kind is SourceKind.OCR
                and len(words) >= 3
                and text == text.upper()
                and any(character.isalpha() for character in text)
            ):
                return text
        return None

    def _extract_ingredients(self, evidence: tuple[EvidenceSegment, ...]) -> tuple[Ingredient, ...]:
        extracted: list[Ingredient] = []
        for segment in evidence:
            for line in segment.text.splitlines():
                ingredient = self._parse_ingredient(line, segment)
                if ingredient is not None:
                    extracted.append(ingredient)
        return self._merge_ingredients(extracted)

    @staticmethod
    def _parse_ingredient(line: str, segment: EvidenceSegment) -> Ingredient | None:
        descriptor = _NUMERIC_DESCRIPTOR_PATTERN.fullmatch(line)
        if descriptor is not None:
            name = descriptor.group("name").strip()
            return Ingredient(
                original_text=line.strip(),
                name=name,
                evidence=(EvidenceReference(evidence_id=segment.evidence_id),),
                confidence=_evidence_confidence(segment),
            )
        match = _MEASURED_INGREDIENT_PATTERN.fullmatch(line)
        if match is None:
            match = _COUNT_INGREDIENT_PATTERN.fullmatch(line)
        if match is None:
            match = _TO_TASTE_PATTERN.fullmatch(line)
        if match is None:
            return None
        name = match.group("name").strip()
        if (
            not name[0].isalnum()
            or name.casefold() in _NON_INGREDIENT_NAMES
            or re.search(r"\bservings?\b", name, re.I)
        ):
            return None
        quantity = match.group("quantity").strip()
        unit = match.groupdict().get("unit")
        return Ingredient(
            original_text=line.strip(),
            name=name,
            quantity_original=quantity,
            unit_original=unit.strip() if unit is not None else None,
            evidence=(EvidenceReference(evidence_id=segment.evidence_id),),
            confidence=_evidence_confidence(segment),
        )

    @staticmethod
    def _merge_ingredients(ingredients: list[Ingredient]) -> tuple[Ingredient, ...]:
        grouped: dict[tuple[str, str | None, str | None], list[Ingredient]] = defaultdict(list)
        for ingredient in ingredients:
            key = (
                _normalized(ingredient.name),
                _normalized(ingredient.quantity_original) if ingredient.quantity_original else None,
                _normalized(ingredient.unit_original) if ingredient.unit_original else None,
            )
            grouped[key].append(ingredient)

        merged: list[Ingredient] = []
        for values in grouped.values():
            first = values[0]
            references = tuple(
                EvidenceReference(evidence_id=evidence_id)
                for evidence_id in dict.fromkeys(
                    reference.evidence_id for value in values for reference in value.evidence
                )
            )
            merged.append(
                first.model_copy(
                    update={
                        "evidence": references,
                        "confidence": max(value.confidence for value in values),
                    }
                )
            )
        return tuple(merged)

    @staticmethod
    def _extract_instructions(evidence: tuple[EvidenceSegment, ...]) -> tuple[Instruction, ...]:
        found: dict[str, tuple[str, list[EvidenceReference], float]] = {}
        for segment in evidence:
            for line in segment.text.splitlines():
                text = line.strip()
                if not _INSTRUCTION_PATTERN.match(text):
                    continue
                key = _normalized(text)
                if key not in found:
                    found[key] = (text, [], _evidence_confidence(segment))
                found[key][1].append(EvidenceReference(evidence_id=segment.evidence_id))

        return tuple(
            Instruction(
                original_text=text,
                sequence=index,
                evidence=tuple(
                    EvidenceReference(evidence_id=evidence_id)
                    for evidence_id in dict.fromkeys(
                        reference.evidence_id for reference in references
                    )
                ),
                confidence=confidence,
            )
            for index, (text, references, confidence) in enumerate(found.values(), start=1)
        )

    @staticmethod
    def _find_quantity_conflicts(
        ingredients: tuple[Ingredient, ...], evidence: tuple[EvidenceSegment, ...]
    ) -> tuple[str, ...]:
        evidence_by_id = {segment.evidence_id: segment for segment in evidence}
        values_by_name: dict[str, list[Ingredient]] = defaultdict(list)
        for ingredient in ingredients:
            values_by_name[_normalized(ingredient.name)].append(ingredient)
        conflicts: list[str] = []
        for name, claims in values_by_name.items():
            trusted_values = {
                (claim.quantity_original, claim.unit_original)
                for claim in claims
                if _is_trusted_quantity_claim(claim, evidence_by_id)
            }
            # Weak OCR may be malformed. Preserve it in the OCR artifact, but do
            # not create a recipe conflict unless it is corroborated or high confidence.
            if len(trusted_values) > 1:
                conflicts.append(f"Conflicting explicit quantities for ingredient: {name}")
        return tuple(conflicts)


class RecipeValidator:
    """Routes missing or conflicting recipe content to review deterministically."""

    def __init__(self, *, minimum_instruction_count: int = 2) -> None:
        if minimum_instruction_count < 1:
            raise ValueError("minimum_instruction_count must be at least 1")
        self._minimum_instruction_count = minimum_instruction_count

    @property
    def version(self) -> str:
        return f"recipe-validator-v3:minimum-instructions={self._minimum_instruction_count}"

    def validate(self, recipe: RecipeCandidate) -> ValidationArtifact:
        findings: list[ValidationFinding] = []
        if recipe.title is None:
            findings.append(
                _finding("missing_title", "No recognizable recipe title was extracted.")
            )
        if not recipe.ingredients:
            findings.append(
                _finding("missing_ingredients", "No explicit ingredients were extracted.")
            )
        if not recipe.instructions:
            findings.append(
                _finding("missing_instructions", "No explicit instructions were extracted.")
            )
        elif len(recipe.instructions) < self._minimum_instruction_count:
            findings.append(
                _finding(
                    "insufficient_instructions",
                    "Too few explicit instructions were extracted for an automatic result.",
                )
            )
        for ingredient in recipe.ingredients:
            if not ingredient.evidence:
                findings.append(
                    _finding("missing_evidence", "An ingredient has no evidence reference.")
                )
            if (
                ingredient.quantity_original is None
                and _UNRESOLVED_NUMERIC_NAME_PATTERN.match(ingredient.name)
                and not _NUMERIC_DESCRIPTOR_PATTERN.match(ingredient.name)
            ):
                findings.append(
                    _finding(
                        "ambiguous_ingredient_name",
                        f"Ingredient name begins with an unresolved quantity: {ingredient.name}",
                    )
                )
        for instruction in recipe.instructions:
            if not instruction.evidence:
                findings.append(
                    _finding("missing_evidence", "An instruction has no evidence reference.")
                )
        findings.extend(
            _finding(finding.code, finding.message) for finding in recipe.completeness_findings
        )
        findings.extend(_finding("source_conflict", conflict) for conflict in recipe.conflicts)
        outcome = RecipeOutcome.READY if not findings else RecipeOutcome.REVIEW
        return ValidationArtifact(outcome=outcome, findings=tuple(findings))


def _evidence_confidence(segment: EvidenceSegment) -> float:
    if segment.confidence is not None:
        return segment.confidence
    if segment.source_kind is SourceKind.CAPTION:
        return 0.9
    return 0.7


def _is_trusted_quantity_claim(
    ingredient: Ingredient, evidence_by_id: dict[str, EvidenceSegment]
) -> bool:
    for reference in ingredient.evidence:
        segment = evidence_by_id.get(reference.evidence_id)
        if segment is None:
            continue
        if segment.source_kind is not SourceKind.OCR:
            return True
        if _evidence_confidence(segment) >= 0.85:
            return True
    return False


def _finding(code: str, message: str) -> ValidationFinding:
    return ValidationFinding(code=code, message=message)


def _normalized(value: str | None) -> str:
    return " ".join((value or "").casefold().split())
