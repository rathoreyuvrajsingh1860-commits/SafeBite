"""
Unit tests for seed_evaluation_batch.py.

These are OFFLINE tests of the script's pure logic — JSON/allergen validation,
idempotency-tag computation, image-content sniffing, and the dry-run code path.

They do NOT hit Supabase, Gemini, OpenRouter, or any real network endpoint,
and they never read or require SAFEBITE_ADMIN_EMAIL/PASSWORD or any Supabase
key. That's intentional: proving the runner is actually wired to the real
run-evaluation Edge Function requires live admin credentials this test suite
deliberately does not have and should not have.
"""

import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import seed_evaluation_batch as mod  # noqa: E402


class TestAllergenValidation(unittest.TestCase):
    def test_valid_allergens_accepted(self):
        item = mod.validate_item({
            "id": 1, "name": "Test", "image_url": "https://example.com/a.jpg",
            "present": ["milk", "Eggs"], "precautionary": ["Tree Nuts"],
        })
        self.assertTrue(item.is_valid, item.errors)
        self.assertEqual(item.present, ["milk", "eggs"])  # normalized lowercase
        self.assertEqual(item.precautionary, ["tree nuts"])

    def test_unknown_allergen_rejected(self):
        item = mod.validate_item({
            "id": 2, "name": "Test", "image_url": "https://example.com/a.jpg",
            "present": ["gluten"], "precautionary": [],
        })
        self.assertFalse(item.is_valid)
        self.assertTrue(any("unknown allergen" in e for e in item.errors))


class TestJsonValidation(unittest.TestCase):
    def test_missing_fields_reported(self):
        item = mod.validate_item({"id": 3})
        self.assertFalse(item.is_valid)
        self.assertTrue(any("name" in e for e in item.errors))
        self.assertTrue(any("image_url" in e for e in item.errors))

    def test_non_http_url_rejected(self):
        item = mod.validate_item({
            "id": 4, "name": "Test", "image_url": "ftp://example.com/a.jpg",
            "present": [], "precautionary": [],
        })
        self.assertFalse(item.is_valid)
        self.assertTrue(any("image_url" in e for e in item.errors))

    def test_duplicate_ids_in_batch_rejected(self):
        items = mod.validate_batch([
            {"id": 5, "name": "A", "image_url": "https://x/a.jpg", "present": [], "precautionary": []},
            {"id": 5, "name": "B", "image_url": "https://x/b.jpg", "present": [], "precautionary": []},
        ])
        self.assertFalse(items[0].is_valid)
        self.assertFalse(items[1].is_valid)

    def test_malformed_json_raises(self):
        with mock.patch.object(Path, "exists", return_value=True), \
             mock.patch.object(Path, "read_text", return_value="{not valid json"):
            with self.assertRaises(mod.ValidationError):
                mod.load_batch_file(Path("fake.json"))

    def test_non_array_top_level_raises(self):
        with mock.patch.object(Path, "exists", return_value=True), \
             mock.patch.object(Path, "read_text", return_value=json.dumps({"not": "a list"})):
            with self.assertRaises(mod.ValidationError):
                mod.load_batch_file(Path("fake.json"))


class TestBatchIdFromFilename(unittest.TestCase):
    def test_parses_batch_number(self):
        self.assertEqual(mod.parse_batch_id_from_filename(Path("evaluation/v2/batch-03.json")), 3)
        self.assertEqual(mod.parse_batch_id_from_filename(Path("batch-21.json")), 21)

    def test_rejects_unrecognized_filename(self):
        with self.assertRaises(mod.ValidationError):
            mod.parse_batch_id_from_filename(Path("not-a-batch-file.json"))


class TestSeedTagIdempotency(unittest.TestCase):
    def test_seed_tag_is_deterministic(self):
        item = mod.BatchItem(id=21, name="X", image_url="https://x/a.jpg", present=[], precautionary=[])
        self.assertEqual(item.seed_tag(3), "[seed:batch-03#21]")
        # same inputs -> same tag, every time
        self.assertEqual(item.seed_tag(3), item.seed_tag(3))

    def test_different_batch_or_id_yields_different_tag(self):
        item21 = mod.BatchItem(id=21, name="X", image_url="u", present=[], precautionary=[])
        item22 = mod.BatchItem(id=22, name="X", image_url="u", present=[], precautionary=[])
        self.assertNotEqual(item21.seed_tag(3), item22.seed_tag(3))
        self.assertNotEqual(item21.seed_tag(3), item21.seed_tag(4))


class TestImageContentSniffing(unittest.TestCase):
    def test_detects_jpeg(self):
        self.assertEqual(mod.sniff_image_content_type(b"\xff\xd8\xff\xe0rest"), "image/jpeg")

    def test_detects_png(self):
        self.assertEqual(mod.sniff_image_content_type(b"\x89PNG\r\n\x1a\nrest"), "image/png")

    def test_rejects_html_masquerading_as_image(self):
        html = b"<!DOCTYPE html><html><body>404 Not Found</body></html>"
        self.assertIsNone(mod.sniff_image_content_type(html))

    def test_download_raises_on_html_payload(self):
        html = b"<!DOCTYPE html><html>error page</html>"
        fake_resp = mock.MagicMock()
        fake_resp.headers.get.return_value = "image/jpeg"  # lies about content-type
        fake_resp.read.return_value = html
        fake_resp.__enter__.return_value = fake_resp
        fake_resp.__exit__.return_value = False
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            with self.assertRaises(mod.DownloadError):
                mod.download_image("https://example.com/fake.jpg")


class TestDryRunHasNoSideEffects(unittest.TestCase):
    """Confirms --dry-run never imports/constructs anything that would need
    network or Supabase credentials, by running main() with no env vars set
    and no network mocked — it must not crash looking for them."""

    def test_dry_run_on_empty_batch_succeeds_with_no_env(self):
        batch_path = Path(__file__).parent.parent / "evaluation" / "v2" / "batch-03.json"
        with mock.patch.dict("os.environ", {}, clear=True):
            captured = io.StringIO()
            with mock.patch("sys.stdout", captured):
                exit_code = mod.main([str(batch_path), "--dry-run"])
        self.assertEqual(exit_code, 0)
        self.assertIn("dry-run", captured.getvalue())

    def test_dry_run_with_items_prints_each_without_network(self):
        with mock.patch.object(Path, "exists", return_value=True), \
             mock.patch.object(Path, "read_text", return_value=json.dumps([
                 {"id": 21, "name": "Widget", "image_url": "https://example.com/w.jpg",
                  "present": ["milk"], "precautionary": []},
             ])), \
             mock.patch.object(mod, "parse_batch_id_from_filename", return_value=3), \
             mock.patch("urllib.request.urlopen") as mocked_fetch:
            captured = io.StringIO()
            with mock.patch("sys.stdout", captured):
                exit_code = mod.main([str(Path("evaluation/v2/batch-03.json")), "--dry-run"])
        self.assertEqual(exit_code, 0)
        mocked_fetch.assert_not_called()  # dry-run must never touch the network
        self.assertIn("Widget", captured.getvalue())


class TestNoSecretLeakage(unittest.TestCase):
    def test_require_env_never_prints_value(self):
        with mock.patch.dict("os.environ", {"SOME_SECRET": "super-secret-value"}):
            captured = io.StringIO()
            with mock.patch("sys.stdout", captured), mock.patch("sys.stderr", captured):
                value = mod.require_env("SOME_SECRET")
        self.assertEqual(value, "super-secret-value")
        self.assertNotIn("super-secret-value", captured.getvalue())

    def test_missing_env_error_names_var_not_value(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            captured = io.StringIO()
            with mock.patch("sys.stderr", captured):
                with self.assertRaises(SystemExit):
                    mod.require_env("SAFEBITE_ADMIN_PASSWORD")
        self.assertIn("SAFEBITE_ADMIN_PASSWORD", captured.getvalue())

    def test_source_never_contains_a_hardcoded_secret_literal(self):
        # Checks for an actual hardcoded credential pattern (a JWT-shaped string,
        # or a `SUPABASE_SERVICE_ROLE_KEY = "..."`-style assignment), not the
        # words "service role" — the script's own comments legitimately explain
        # that it does NOT use one, and a word-ban would wrongly flag that.
        source = (Path(__file__).parent / "seed_evaluation_batch.py").read_text()
        jwt_like = re.compile(r"eyJ[A-Za-z0-9_-]{20,}")
        self.assertIsNone(jwt_like.search(source), "Source appears to contain a hardcoded JWT/key literal.")
        hardcoded_assignment = re.compile(r'(SERVICE_ROLE|API_KEY)\s*=\s*["\']\S{10,}["\']')
        self.assertIsNone(hardcoded_assignment.search(source), "Source appears to hardcode a credential value.")


class TestClassifyResult(unittest.TestCase):
    def test_clean_pass(self):
        self.assertEqual(mod.classify_result({"false_negative_allergens": [], "false_positive_allergens": []}), "CLEAN PASS")

    def test_false_positive(self):
        label = mod.classify_result({"false_negative_allergens": [], "false_positive_allergens": ["soybeans"]})
        self.assertTrue(label.startswith("FALSE POSITIVE"))

    def test_false_negative_takes_priority_over_false_positive(self):
        label = mod.classify_result({"false_negative_allergens": ["peanuts"], "false_positive_allergens": ["soybeans"]})
        self.assertTrue(label.startswith("FALSE NEGATIVE"))

    def test_provider_error_surfaced(self):
        label = mod.classify_result({"error": "All providers failed. Gemini: ... | OpenRouter: ..."})
        self.assertTrue(label.startswith("MODEL/PROVIDER FAILURE"))
        self.assertIn("All providers failed", label)


if __name__ == "__main__":
    unittest.main()
