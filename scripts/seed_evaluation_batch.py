#!/usr/bin/env python3
"""
SafeBite Evaluation Batch Runner
=================================

Seeds a JSON batch of evaluation labels into the EXISTING `eval_labels` table
and triggers the EXISTING `run-evaluation` Supabase Edge Function for each one.

This script never calls Gemini or OpenRouter directly. The only model/evaluation
path is:

    this script -> run-evaluation Edge Function -> Gemini -> OpenRouter fallback -> eval_results

See evaluation/README.md for full documentation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants — must match the existing production schema/contract exactly.
# These are NOT configurable; they describe facts about the existing system.
# ---------------------------------------------------------------------------

# The 9 FDA major allergens, exactly as used in validation.html and the
# run-evaluation Edge Function's EVAL_PROMPT.
VALID_ALLERGENS = {
    "milk", "eggs", "fish", "shellfish", "tree nuts",
    "peanuts", "wheat", "soybeans", "sesame",
}

STORAGE_BUCKET = "food-scans"
STORAGE_PREFIX = "eval/"  # required by the eval_storage_admin_all RLS policy
EDGE_FUNCTION_NAME = "run-evaluation"
SEED_TAG_PREFIX = "[seed:"  # prepended to eval_labels.notes for idempotency

# Minimal magic-byte sniffing so an HTML error page saved with a .jpg
# extension (or served with image/jpeg by a misconfigured CDN) is rejected
# even though its Content-Type header might lie.
IMAGE_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # WEBP: RIFF....WEBP, checked more precisely below
]

BATCH_FILENAME_RE = re.compile(r"batch-(\d+)\.json$")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class BatchItem:
    id: int
    name: str
    image_url: str
    present: list[str]
    precautionary: list[str]
    notes: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0

    def seed_tag(self, batch_id: int) -> str:
        return f"{SEED_TAG_PREFIX}batch-{batch_id:02d}#{self.id}]"


class ValidationError(Exception):
    pass


class DownloadError(Exception):
    pass


class ConflictError(Exception):
    """Raised when a seed-tagged label already exists with different metadata."""


# ---------------------------------------------------------------------------
# Batch loading / validation (pure, offline — safe for --dry-run)
# ---------------------------------------------------------------------------

def parse_batch_id_from_filename(path: Path) -> int:
    match = BATCH_FILENAME_RE.search(path.name)
    if not match:
        raise ValidationError(
            f"Could not determine batch id from filename '{path.name}'. "
            f"Expected a name like 'batch-03.json'."
        )
    return int(match.group(1))


def load_batch_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ValidationError(f"Batch file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValidationError(f"Batch file is not valid JSON: {e}") from e
    if not isinstance(raw, list):
        raise ValidationError("Batch file must contain a JSON array at the top level.")
    return raw


def validate_item(raw: dict[str, Any]) -> BatchItem:
    """Validate one raw JSON object into a BatchItem. Never raises — collects
    errors on the item instead, so the caller can report ALL problems in one
    pass rather than stopping at the first bad entry."""
    errors: list[str] = []

    item_id = raw.get("id")
    if not isinstance(item_id, int):
        errors.append("'id' must be an integer")
        item_id = -1

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("'name' must be a non-empty string")
        name = ""

    image_url = raw.get("image_url")
    if not isinstance(image_url, str) or not re.match(r"^https?://", image_url or ""):
        errors.append("'image_url' must be a string starting with http:// or https://")
        image_url = ""

    present = raw.get("present", [])
    if not isinstance(present, list) or not all(isinstance(a, str) for a in present):
        errors.append("'present' must be a list of strings")
        present = []
    present_norm = [a.strip().lower() for a in present]
    for a in present_norm:
        if a not in VALID_ALLERGENS:
            errors.append(f"'present' contains unknown allergen '{a}' (must be one of {sorted(VALID_ALLERGENS)})")

    precautionary = raw.get("precautionary", [])
    if not isinstance(precautionary, list) or not all(isinstance(a, str) for a in precautionary):
        errors.append("'precautionary' must be a list of strings")
        precautionary = []
    precautionary_norm = [a.strip().lower() for a in precautionary]
    for a in precautionary_norm:
        if a not in VALID_ALLERGENS:
            errors.append(f"'precautionary' contains unknown allergen '{a}' (must be one of {sorted(VALID_ALLERGENS)})")

    notes = raw.get("notes", "")
    if notes is not None and not isinstance(notes, str):
        errors.append("'notes' must be a string if present")
        notes = ""

    item = BatchItem(
        id=item_id,
        name=name,
        image_url=image_url,
        present=present_norm,
        precautionary=precautionary_norm,
        notes=notes or "",
    )
    item.errors = errors
    return item


def validate_batch(raw_items: list[dict[str, Any]]) -> list[BatchItem]:
    items = [validate_item(r) for r in raw_items]
    ids = [i.id for i in items if i.is_valid]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        for item in items:
            if item.id in dupes:
                item.errors.append(f"duplicate id {item.id} within this batch file")
    return items


# ---------------------------------------------------------------------------
# Image download + validation (no DB/network side effects beyond the fetch)
# ---------------------------------------------------------------------------

def sniff_image_content_type(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def download_image(url: str, timeout: int = 20) -> tuple[bytes, str]:
    """Downloads and validates one image. Raises DownloadError with a clear
    message on any failure, including an HTML/error page masquerading as an
    image (the actual bytes are checked, not just the declared Content-Type)."""
    req = urllib.request.Request(url, headers={"User-Agent": "SafeBite-Eval-Runner/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            declared_type = resp.headers.get("Content-Type", "")
            data = resp.read()
    except urllib.error.HTTPError as e:
        raise DownloadError(f"HTTP {e.code} downloading {url}") from e
    except urllib.error.URLError as e:
        raise DownloadError(f"Could not reach {url}: {e.reason}") from e
    except TimeoutError as e:
        raise DownloadError(f"Timed out downloading {url}") from e

    if not data:
        raise DownloadError(f"Downloaded zero bytes from {url}")

    sniffed_type = sniff_image_content_type(data)
    if sniffed_type is None:
        snippet = data[:100].decode("utf-8", errors="replace")
        raise DownloadError(
            f"Content from {url} does not look like a real image "
            f"(declared Content-Type: '{declared_type}'). "
            f"First bytes: {snippet!r}"
        )

    return data, sniffed_type


# ---------------------------------------------------------------------------
# Supabase-backed operations (require live credentials — not used in --dry-run)
# ---------------------------------------------------------------------------

class SafeBiteClient:
    """Thin wrapper authenticating as the real admin user via email+password,
    exactly like validation.html does. No service-role key is ever used or
    accepted — every write goes through the same RLS the UI is subject to."""

    def __init__(self, url: str, publishable_key: str, admin_email: str, admin_password: str):
        try:
            from supabase import create_client
        except ImportError as e:
            raise RuntimeError(
                "The 'supabase' Python package is required. Install with: pip install supabase"
            ) from e

        self.url = url
        self.client = create_client(url, publishable_key)
        auth_resp = self.client.auth.sign_in_with_password(
            {"email": admin_email, "password": admin_password}
        )
        if not auth_resp.session:
            raise RuntimeError("Admin sign-in failed — check SAFEBITE_ADMIN_EMAIL/SAFEBITE_ADMIN_PASSWORD.")
        self.user_id = auth_resp.user.id
        self.access_token = auth_resp.session.access_token

        profile = (
            self.client.table("profiles")
            .select("is_admin")
            .eq("id", self.user_id)
            .single()
            .execute()
        )
        if not profile.data or not profile.data.get("is_admin"):
            raise RuntimeError(
                "Signed in successfully, but this account is not an admin (profiles.is_admin is not true). "
                "This script refuses to proceed, same as the Edge Function would refuse."
            )

    def find_seeded_label(self, seed_tag: str) -> dict[str, Any] | None:
        resp = (
            self.client.table("eval_labels")
            .select("*")
            .like("notes", f"{seed_tag}%")
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def upload_image(self, path: str, data: bytes, content_type: str) -> None:
        self.client.storage.from_(STORAGE_BUCKET).upload(
            path, data, {"content-type": content_type}
        )

    def create_label(self, item: BatchItem, batch_id: int, image_path: str) -> dict[str, Any]:
        tagged_notes = f"{item.seed_tag(batch_id)} {item.notes}".strip()
        resp = (
            self.client.table("eval_labels")
            .insert({
                "image_path": image_path,
                "ground_truth_food_name": item.name,
                "ground_truth_allergens": item.present,
                "ground_truth_precautionary_allergens": item.precautionary,
                "notes": tagged_notes,
                "created_by": self.user_id,
            })
            .execute()
        )
        return resp.data[0]

    def run_evaluation(self, label_id: str, timeout: int = 130) -> dict[str, Any]:
        """Invokes the EXISTING run-evaluation Edge Function and waits for its
        HTTP response. The function itself is synchronous (it returns only
        after the eval_results row is written or the attempt has definitively
        failed), so no separate polling loop is needed or appropriate here —
        polling would imply the function returns before finishing, which it
        does not."""
        import httpx

        resp = httpx.post(
            f"{self.url}/functions/v1/{EDGE_FUNCTION_NAME}",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            json={"eval_label_id": label_id},
            timeout=timeout,
        )
        return resp.json()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def classify_result(result: dict[str, Any]) -> str:
    if "error" in result:
        return f"MODEL/PROVIDER FAILURE: {result['error']}"
    fn = result.get("false_negative_allergens") or []
    fp = result.get("false_positive_allergens") or []
    if fn:
        return f"FALSE NEGATIVE: {', '.join(fn)}"
    if fp:
        return f"FALSE POSITIVE: {', '.join(fp)}"
    return "CLEAN PASS"


def print_report(batch_id: int, rows: list[dict[str, Any]]) -> int:
    print(f"\nBatch {batch_id:02d}")
    print("-" * 9)
    evaluated = 0
    clean = 0
    false_pos = 0
    false_neg = 0
    fallback_count = 0
    failures = 0

    for row in rows:
        name = row["name"]
        label = row["classification"]
        provider_bits = ""
        if row.get("provider"):
            provider_bits = f"  [{row['provider']}/{row.get('model', '?')}{', fallback' if row.get('fallback_used') else ''}]"
        print(f"#{row['id']:<4} {name:<40} {label}{provider_bits}")

        if row.get("skipped"):
            continue
        evaluated += 1
        if label == "CLEAN PASS":
            clean += 1
        elif label.startswith("FALSE POSITIVE"):
            false_pos += 1
        elif label.startswith("FALSE NEGATIVE"):
            false_neg += 1
        elif label.startswith("MODEL/PROVIDER FAILURE"):
            failures += 1
        if row.get("fallback_used"):
            fallback_count += 1

    print("\nSummary:")
    print(f"{evaluated} evaluated")
    print(f"{clean} clean passes")
    print(f"{false_pos} with false positives")
    print(f"{false_neg} false negatives")
    if evaluated:
        print(f"provider fallback used: {fallback_count}/{evaluated}")

    return 1 if failures > 0 else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="seed_evaluation_batch.py",
        description=(
            "Seeds a JSON batch of SafeBite evaluation labels and runs them through "
            "the existing run-evaluation Edge Function (Gemini -> OpenRouter fallback)."
        ),
    )
    p.add_argument("batch_file", type=Path, help="Path to a batch JSON file, e.g. evaluation/v2/batch-03.json")
    p.add_argument("--dry-run", action="store_true", help="Validate JSON/URLs/allergens only. No uploads, no DB writes, no model calls.")
    p.add_argument("--seed-only", action="store_true", help="Create missing eval_labels rows but do not invoke run-evaluation.")
    return p


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: required environment variable {name} is not set.", file=sys.stderr)
        sys.exit(2)
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        batch_id = parse_batch_id_from_filename(args.batch_file)
        raw_items = load_batch_file(args.batch_file)
    except ValidationError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    items = validate_batch(raw_items)
    invalid = [i for i in items if not i.is_valid]
    if invalid:
        print(f"Batch validation failed for {len(invalid)} of {len(items)} item(s):", file=sys.stderr)
        for i in invalid:
            label = i.name or f"id={i.id}"
            for err in i.errors:
                print(f"  - {label}: {err}", file=sys.stderr)
        return 2

    print(f"Batch {batch_id:02d}: {len(items)} item(s) passed JSON/allergen validation.")

    if args.dry_run:
        print("--dry-run: skipping URL fetch, uploads, DB writes, and model calls.")
        for i in items:
            print(f"  #{i.id} {i.name} -> {i.image_url} (present={i.present or '-'}, precautionary={i.precautionary or '-'})")
        return 0

    if not items:
        print("Nothing to seed (batch file is empty).")
        return 0

    # Credentials are only required past this point — --dry-run never touches them.
    supabase_url = require_env("SUPABASE_URL")
    publishable_key = require_env("SUPABASE_PUBLISHABLE_KEY")
    admin_email = require_env("SAFEBITE_ADMIN_EMAIL")
    admin_password = require_env("SAFEBITE_ADMIN_PASSWORD")

    client = SafeBiteClient(supabase_url, publishable_key, admin_email, admin_password)
    print(f"Authenticated as admin ({admin_email}).")

    report_rows: list[dict[str, Any]] = []

    for item in items:
        seed_tag = item.seed_tag(batch_id)
        existing = client.find_seeded_label(seed_tag)

        if existing:
            existing_present = sorted(existing.get("ground_truth_allergens") or [])
            existing_precautionary = sorted(existing.get("ground_truth_precautionary_allergens") or [])
            existing_name = existing.get("ground_truth_food_name") or ""
            matches = (
                existing_present == sorted(item.present)
                and existing_precautionary == sorted(item.precautionary)
                and existing_name == item.name
            )
            if not matches:
                print(
                    f"CONFLICT: #{item.id} '{item.name}' already exists as label {existing['id']} "
                    f"but its stored metadata differs from this batch file. Stopping — "
                    f"not overwriting production evaluation data. Resolve manually.",
                    file=sys.stderr,
                )
                return 3
            print(f"#{item.id} {item.name}: EXISTS (reusing label {existing['id']})")
            label_row = existing
        else:
            try:
                image_bytes, content_type = download_image(item.image_url)
            except DownloadError as e:
                print(f"#{item.id} {item.name}: DOWNLOAD FAILED: {e}", file=sys.stderr)
                report_rows.append({"id": item.id, "name": item.name, "classification": "SKIPPED: download failed", "skipped": True})
                continue

            ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}[content_type]
            # Content-addressed filename: re-running the batch with the same image never
            # produces a second distinct object even outside the notes-tag idempotency check.
            content_hash = hashlib.sha256(image_bytes).hexdigest()[:16]
            storage_path = f"{STORAGE_PREFIX}batch-{batch_id:02d}-{item.id}-{content_hash}.{ext}"

            client.upload_image(storage_path, image_bytes, content_type)
            label_row = client.create_label(item, batch_id, storage_path)
            print(f"#{item.id} {item.name}: seeded (label {label_row['id']})")

        if args.seed_only:
            report_rows.append({"id": item.id, "name": item.name, "classification": "SEEDED (not evaluated)", "skipped": True})
            continue

        print(f"#{item.id} {item.name}: evaluating...", flush=True)
        result = client.run_evaluation(label_row["id"])
        classification = classify_result(result)
        report_rows.append({
            "id": item.id,
            "name": item.name,
            "classification": classification,
            "provider": result.get("provider"),
            "model": result.get("model"),
            "fallback_used": result.get("fallback_used"),
        })

    return print_report(batch_id, report_rows)


if __name__ == "__main__":
    sys.exit(main())
