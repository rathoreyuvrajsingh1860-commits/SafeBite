# SafeBite Evaluation Batch Runner

Code-driven tooling for seeding SafeBite Evaluation Lab labels and running
them through the **existing production evaluation pipeline**. This does not
replace the Evaluation Lab UI (`validation.html`) or the `run-evaluation`
Edge Function — it drives them.

```
seed_evaluation_batch.py -> run-evaluation Edge Function -> Gemini -> OpenRouter fallback -> eval_results
```

This script never calls Gemini or OpenRouter directly, and never implements
its own copy of the evaluation/comparison logic.

## Batch JSON format

`evaluation/v2/batch-NN.json` (NN = two-digit batch number, used for
idempotency — see below):

```json
[
  {
    "id": 21,
    "name": "Product name",
    "image_url": "https://example.com/label-photo.jpg",
    "present": ["milk"],
    "precautionary": ["tree nuts"],
    "notes": "Source/label context"
  }
]
```

- `id` — integer, unique within the batch file.
- `name` — product name. Maps to `eval_labels.ground_truth_food_name`.
- `image_url` — direct link to a downloadable image.
- `present` — allergens actually in the product (an ingredient). Maps to
  `eval_labels.ground_truth_allergens`. Must be a subset of the 9 FDA major
  allergens: `milk, eggs, fish, shellfish, tree nuts, peanuts, wheat,
  soybeans, sesame`.
- `precautionary` — allergens mentioned only as cross-contact/"may contain"
  statements. Maps to `eval_labels.ground_truth_precautionary_allergens`.
  Same allergen list as above.
- `notes` — optional free text about the label's source/context.

`batch-03.json` currently ships empty (`[]`) — Batch 3's five real products
have not been added yet, per instruction. Populate it with the format above
once the runner is verified.

## Required environment variables

| Variable | Purpose | Secret? |
|---|---|---|
| `SUPABASE_URL` | `https://jqmkouezphjbfdptausv.supabase.co` | No |
| `SUPABASE_PUBLISHABLE_KEY` | The same publishable key already embedded in `validation.html` | No — it's a public, browser-safe key by design |
| `SAFEBITE_ADMIN_EMAIL` | The admin account's email | No |
| `SAFEBITE_ADMIN_PASSWORD` | The admin account's password | **Yes** |

**No service-role key is used anywhere in this tool.** The script
authenticates as the real admin user via email+password (identical to
`validation.html`'s login), and every database/storage write goes through
the exact same Row Level Security policies the UI is subject to — this
tool has no more database access than a human admin logged into the
Evaluation Lab would.

Set these in your shell, not in any file committed to the repo:

```bash
export SUPABASE_URL="https://jqmkouezphjbfdptausv.supabase.co"
export SUPABASE_PUBLISHABLE_KEY="sb_publishable_..."
export SAFEBITE_ADMIN_EMAIL="rathoreyuvrajsingh1860@gmail.com"
export SAFEBITE_ADMIN_PASSWORD="..."
```

## Install

```bash
pip install supabase httpx
```

## Usage

```bash
# Validate JSON/URLs/allergens only. No network fetch, no uploads, no DB
# writes, no model calls. Safe to run with zero environment variables set.
python scripts/seed_evaluation_batch.py evaluation/v2/batch-03.json --dry-run

# Create eval_labels rows (download + upload images) but do not evaluate yet.
python scripts/seed_evaluation_batch.py evaluation/v2/batch-03.json --seed-only

# Full run: seed any missing labels, then evaluate each sequentially,
# then print the batch report.
python scripts/seed_evaluation_batch.py evaluation/v2/batch-03.json
```

Evaluations run **sequentially, one at a time** — never in parallel — to
keep provider usage controlled and debugging simple, per the batch design.

## How idempotency works

The batch number comes from the filename (`batch-03.json` → batch 3). Each
item's `id` combines with the batch number into a tag written at the start
of `eval_labels.notes`:

```
[seed:batch-03#21] <your notes text>
```

Before creating a label, the script searches existing `eval_labels.notes`
for this exact tag:

- **No match** → downloads the image, uploads it to the `food-scans` bucket
  under `eval/batch-03-21-<content-hash>.<ext>` (the hash means re-running
  with the same image never creates a duplicate storage object either), and
  inserts a new `eval_labels` row.
- **Match found, and its `ground_truth_food_name` / `ground_truth_allergens`
  / `ground_truth_precautionary_allergens` are identical to the batch file**
  → reports `EXISTS`, reuses the existing label, does not re-upload or
  re-insert.
- **Match found, but metadata differs** → stops immediately with a
  `CONFLICT` error and makes no changes. This is deliberate: the script will
  never silently overwrite a label that may have been manually corrected
  (the way White Rice, Sesame Tahini, and Almond were, earlier in this
  dataset's history).

No new database column or table was added for this — it reuses the
existing `notes` field rather than altering the evaluation schema.

**Known limitation:** the "metadata matches" check compares name and
allergen lists, not image bytes. It does not re-download and compare the
stored image against `image_url` on each run. If you need to change an
existing seeded label's image specifically, do it through the Evaluation
Lab's Edit flow or by removing the seed tag manually — this script will
refuse rather than guess.

## How results are reported

```
Batch 03
--------
#21 Great Value Powdered Peanut Butter   CLEAN PASS  [gemini/gemini-flash-latest]
#22 Simply Tera's Vanilla Whey            FALSE POSITIVE: soybeans  [openrouter/google/gemma-4-31b-it:free, fallback]
#23 Mott's Fruitsations                   MODEL/PROVIDER FAILURE: All providers failed. ...

Summary:
3 evaluated
1 clean passes
1 with false positives
0 false negatives
provider fallback used: 1/3
```

The script exits non-zero only if at least one evaluation ended in a
**MODEL/PROVIDER FAILURE** (every provider in the chain failed, or the
script itself hit a validation/conflict error) — a legitimate false
positive or false negative from a working evaluation is not treated as a
tool failure, since that's exactly the kind of result this dataset exists
to surface.

## Security precautions

- No secret is ever hardcoded in source.
- No secret value is ever printed, logged, or included in the batch report —
  only environment variable *names* appear in error messages.
- No service-role key is accepted or used anywhere.
- Image downloads are sniffed by actual file signature (magic bytes), not
  trusted Content-Type headers, so an HTML error page served with a
  misleading header is rejected rather than silently uploaded as a "label
  photo."
