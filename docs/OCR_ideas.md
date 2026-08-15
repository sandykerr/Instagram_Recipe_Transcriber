OCR should be a conditional enrichment stage, not part of the default critical path.

A better pipeline is:

```plaintext
Download
→ caption/metadata extraction
→ transcription
→ preliminary recipe extraction
→ deterministic completeness validation
        ├── sufficient → render
        └── incomplete/conflicting → targeted OCR
                                    → re-extract
                                    → revalidate
```

The OCR decision must be based on explicit rules rather than an LLM simply saying “this looks good.”

### Suggested OCR gate

Skip OCR only when caption plus transcript provide:

- A recognizable recipe
- An ingredient list with usable quantities where quantities are claimed
- Coherent instructions
- Evidence references for every extracted fact
- No unresolved caption/transcript conflicts
- Acceptable transcription confidence
- No obvious references such as “amounts on screen” or “see ingredients above”
- All necessary temperatures and timings mentioned when the instructions depend on them

Run OCR when:

- The caption withholds the recipe, as the current Reel does
- Ingredients appear without quantities
- Narration says “add this,” “these seasonings,” or similar visual references
- Transcript quality is poor
- Caption and narration disagree
- Important temperatures or timings are absent
- The recipe validator returns INCOMPLETE or CONFLICTING

### Make the behavior configurable

```python
class OcrPolicy(str, Enum):
    NEVER = "never"
    WHEN_NEEDED = "when_needed"
    ALWAYS = "always"
```

Default: `WHEN_NEEDED`

- `NEVER` helps test caption/transcript-only extraction.
- `ALWAYS` helps evaluation and creates ground-truth comparisons.
- `WHEN_NEEDED` is the production behavior.

### Targeted OCR

Even when OCR is needed, we should not process every sampled frame:

1. Sample frames.
2. Remove perceptual duplicates.
3. Detect meaningful visual changes.
4. Prefer frames where overlays remain stable for multiple samples.
5. OCR one representative frame from each cluster.
6. OCR adjacent frames only when the first result is low-confidence or ambiguous.

For example, instead of OCRing 108 frames:

108 sampled frames
→ 24 visually distinct clusters
→ 10 likely text-bearing clusters
→ 10 primary OCR calls
→ 4 adjacent-frame retries

That could reduce OCR work dramatically.

### Preserve the decision

Even skipped OCR should produce an artifact:

```json
{
    "status": "skipped",
    "policy": "when_needed",
    "decision_version": "1",
    "reason": "Caption and transcript passed completeness validation",
    "input_artifacts": [
        "caption.json",
        "transcript.json"
    ]
}
```

This keeps retries and debugging deterministic. If validation rules change later, the decision version changes and OCR can be reconsidered.

One caveat: “quality recipe” must mean evidence-complete, not merely plausible. A polished LLM draft can look complete while quietly inventing a missing
quantity. Deterministic evidence coverage and conflict checks should control the OCR gate.

### Downscale OCR inputs

Spatial downscaling should be evaluated alongside frame deduplication. JPEG
compression mainly reduces storage size; reducing image dimensions lowers the
number of pixels processed by OCR and should have a much larger effect on
runtime.

Keep the original extracted frame as the source artifact and create the
downscaled OCR input in memory. Do not create another persisted collection of
lossy frames solely for inference.

Suggested maximum-side benchmark sizes:

| Maximum side | Approximate 9:16 dimensions | Purpose |
| ---: | ---: | --- |
| 1920 | 1080x1920 | Original-resolution baseline |
| 1280 | 720x1280 | Conservative downscale |
| 960 | 540x960 | Initial recommended candidate |
| 720 | 405x720 | Aggressive downscale |

Evaluate the same text-bearing frames at each resolution. Compare:

- OCR inference time;
- detected-line count;
- exact quantities and units;
- ingredient-name accuracy;
- confidence scores.

Detection count alone is not sufficient. A resized image may still produce a
line while changing a recipe-critical value such as `1360G` into an incorrect
quantity.

For every OCR frame, preserve:

```json
{
    "original_dimensions": [1080, 1920],
    "ocr_dimensions": [540, 960],
    "scale_x": 0.5,
    "scale_y": 0.5
}
```

Map detected polygons back to original-frame coordinates before saving the
canonical OCR artifact. This keeps evidence coordinates consistent regardless
of inference resolution.

The initial comparison fixture should use frames 7, 20, and 51 from the
`DZdXIrXOklf` Reel because they contain substantial text in different screen
regions. Compare original resolution with a 960-pixel maximum side first, then
test 1280 or 720 only if the result warrants it.
