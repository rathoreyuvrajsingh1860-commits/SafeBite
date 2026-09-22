# SafeBite Evaluation Baseline Report — v1

## Dataset Definition

This report uses **exactly one row per label — the most recent `eval_results` row for each of the 10 `eval_labels`** — not the raw historical run count. Sample size = 10.

## Core Metrics (numerator/denominator shown for every calculation)

| # | Metric | Calculation | Result |
|---|---|---|---|
| 1 | Total evaluation labels | — | 10 |
| 2 | Labels with a latest result | 10 / 10 | 10 |
| 3 | Clean-pass rate | 10 clean / 10 latest results | **100%** (10/10) |
| 4 | Total major-allergen false negatives | sum of `false_negative_allergens` length across 10 latest rows | **0** |
| 5 | Total false positives | sum of `false_positive_allergens` length across 10 latest rows | **0** |
| 6 | Major-allergen recall | (present-allergen instances detected) / (total present-allergen instances) = (13 − 0) / 13 | **100%** (13/13) |
| 7 | Precision | (detections not false-positive) / (total detections) = (19 − 0) / 19 | **100%** (19/19) |
| 8 | Precautionary-match share | detected allergens that matched a *precautionary* GT entry rather than a *present* one = 6 / 19 total detections | **32%** (6/19) |

**Metric 6 denominator (13)** = sum of `ground_truth_allergens` (present) array lengths across all 10 labels: Peanut Butter 1, Milk 1, Egg Beaters 1, Eggo Waffles 4, Almond 2, Tuna 1, Soy Milk 1, Shrimp 1, White Rice 0, Sesame Tahini 1.

**Metric 7 denominator (19)** = sum of `detected_allergens` array lengths across the 10 latest rows.

**Metric 8** is included because it's directly computable from stored fields and is the clearest evidence that the present/precautionary split is doing real work in this sample: all 6 precautionary-matched detections are concentrated in White Rice (4) and Sesame Tahini (2) — the same two labels whose ground truth had to be corrected earlier in this session.

## Per-Label Table

| Label | Present GT | Precautionary GT | Detected | FP | FN | Provider | Model | Fallback | Clean Pass |
|---|---|---|---|---|---|---|---|---|---|
| Peanut Butter | peanuts | — | peanuts | — | — | *null* | *null* | false | ✅ |
| Milk | milk | — | milk | — | — | *null* | *null* | false | ✅ |
| Egg Beaters | eggs | — | eggs | — | — | *null* | *null* | false | ✅ |
| Eggo Waffles | milk, eggs, wheat, soybeans | — | wheat, milk, eggs, soybeans | — | — | *null* | *null* | false | ✅ |
| Almond | tree nuts, soybeans | tree nuts | tree nuts, soybeans | — | — | openrouter | `google/gemma-4-26b-a4b-it:free` | true | ✅ |
| Tuna | fish | — | fish | — | — | *null* | *null* | false | ✅ |
| Soy Milk | soybeans | — | soybeans | — | — | *null* | *null* | false | ✅ |
| Shrimp | shellfish | — | shellfish | — | — | *null* | *null* | false | ✅ |
| White Rice | — | tree nuts, wheat, soybeans, sesame | tree nuts, wheat, sesame, soybeans | — | — | gemini | `gemini-flash-latest` | false | ✅ |
| Sesame Tahini | sesame | tree nuts, peanuts | sesame, peanuts, tree nuts | — | — | openrouter | `google/gemma-4-31b-it:free` | true | ✅ |

(FP/FN columns show `—` where the array is empty, i.e., zero.)

## Provider Analysis

- **Gemini (direct, no fallback):** 1 label (White Rice)
- **OpenRouter (fallback):** 2 labels (Almond, Sesame Tahini)
- **Fallback used:** 2 labels — identical to the OpenRouter count above, since no OpenRouter result in this sample occurred without Gemini failing first
- **Actual OpenRouter models that responded:** `google/gemma-4-26b-a4b-it:free` (Almond), `google/gemma-4-31b-it:free` (Sesame Tahini)
- **Legacy runs (`provider`/`model` = null):** 7 labels (Peanut Butter, Milk, Egg Beaters, Eggo Waffles, Tuna, Soy Milk, Shrimp)

**These 7 legacy results predate the multi-provider architecture entirely.** They demonstrate that the underlying detection logic worked on single-allergen and simple multi-allergen labels at some earlier point in development, but say nothing about the current Gemini→OpenRouter fallback chain's behavior — because that chain didn't exist yet when those rows were written. Only 3 of the 10 labels (White Rice, Almond, Sesame Tahini) were evaluated through the architecture as it currently stands.

## Historical Runs

- **Total historical `eval_results` rows:** 34
- **Per-label run counts:** Sesame Tahini 9, Almond 8, Soy Milk 3, Tuna 2, Shrimp 2, Eggo Waffles 2, White Rice 2, Milk 2, Egg Beaters 2, Peanut Butter 2
- **Why 34 ≠ 10:** The baseline sample size (10) counts one evaluation per label — the current state of each label's testing. The historical count (34) includes every run ever executed, including runs made before ground-truth corrections were applied, runs made while diagnosing the OpenRouter timeout/UX issue, and runs superseded by later re-runs on the same label.
- **Duplicate/repeated runs exist, concentrated exactly where expected:** Sesame Tahini (9 runs) and Almond (8 runs) account for 17 of the 34 historical rows — consistent with the duplicate-request UI bug diagnosed and fixed earlier in this session (missing state-based Run-button locking, since fixed in the `validation.html` update). The other 8 labels show only 2 runs each, consistent with one original run plus one legacy/verification re-run, not repeated failures.
- No rows were deleted or altered in producing this report.

## Ground-Truth Corrections

**White Rice** — Present: none. Precautionary: tree nuts, wheat, soybeans, sesame.
The label's only ingredient is rice; there is no allergen ingredient. The label separately states it was "packaged in a facility that also handles tree nuts, wheat, sesame, soybeans" — a cross-contact disclosure, not an ingredient claim, which is why those four allergens are recorded as precautionary rather than present.

**Sesame Tahini** — Present: sesame. Precautionary: peanuts, tree nuts.
Sesame is the product's ingredient (tahini is made from sesame seeds), so it's present. The label separately carries an allergy-advice statement that the product is "not suitable for peanut and other nut allergy sufferers due to manufacturing methods" — a cross-contact disclosure covering peanuts and tree nuts, not an ingredient claim.

**Almond** — Present: tree nuts, soybeans. Precautionary: tree nuts.
Almonds are themselves a tree nut, so tree nuts is present by definition of the product. Soybeans appears as an actual ingredient on this label (not yet independently re-verified against the image in this session — see Known Limitations). The precautionary tree nuts entry reflects an additional cross-contact statement distinct from the ingredient-level tree nut classification; both are legitimately recorded since they represent different claims on the same label.

## Known Limitations

- **Sample size is 10 labels.** This is not statistically sufficient to claim any general accuracy rate for SafeBite's allergen detection.
- **Repeated evaluations of the same label are not independent samples.** The 34 historical runs cannot be treated as 34 independent tests of the model — many are duplicate attempts on the same image caused by a now-fixed UI bug, not fresh evidence.
- **7 of 10 labels were evaluated before provider/model tracking existed.** Their results are real, but can't be attributed to Gemini, OpenRouter, or any specific model — they simply predate that architecture.
- **This dataset does not establish broad real-world accuracy.** All 10 labels are relatively clear, single-language-dominant packaged goods; none tests small/blurry text, curved packaging, or non-English-primary labels — categories explicitly identified as important in earlier planning but not yet represented here.
- **Free-tier model availability changes which model actually answers.** The `openrouter/free` router has already returned different underlying models (`inclusionai/ling-3.0-flash-vl:free`, `nex-agi/nex-n2.5-pro:free`, `dots-studio/dots-3-note-preview:free`) across different runs of the same label — meaning a "clean pass" today isn't a guarantee that the same request produces a clean pass tomorrow, since the responding model isn't fixed.
- **Results depend entirely on the current annotation set.** The 100% recall/precision figures reflect that every currently-stored ground-truth annotation now matches what the model detects — including because two of the ten labels' ground truth was corrected mid-session specifically because the model's original "false positives" turned out to be real label content the annotation had missed. This is a property of an actively-curated small dataset, not independent validation.
- **Almond's ground truth has not been re-verified against its image** the way White Rice and Sesame Tahini were. It's listed as reviewed in this report's ground-truth section because the user's brief described it that way, but no image-inspection record for Almond exists in this session's history the way it does for the other two — flagged here rather than silently treated as equivalent.

## SafeBite Evaluation Baseline — v1

This dataset demonstrates that, as of this baseline, SafeBite's allergen-detection pipeline correctly identified every present and precautionary allergen across 10 curated test labels with zero recorded false negatives and zero false positives, and that the Gemini→OpenRouter fallback architecture successfully produced valid, schema-compliant results on the 3 labels that exercised it (including 2 that required the fallback chain to work).

It does **not** demonstrate general-purpose accuracy, reliability across label types not yet tested (small text, poor lighting, non-English labels, curved packaging), consistency across repeated calls to the same free-tier model, or readiness for any claim resembling production-grade or investor-facing validation. The sample is small, partially retrospective (7 of 10 results predate the current architecture), and was iteratively curated — two of its ten labels had their ground truth corrected specifically because the model surfaced something the original annotation missed. That's evidence the evaluation *process* works as intended; it is not evidence of the underlying model's accuracy at scale.
