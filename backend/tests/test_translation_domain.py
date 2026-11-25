import unittest

from app.translation import TranslationError, normalize_translation_unit


class TranslationDomainTests(unittest.TestCase):
    def test_normalization_is_provider_independent_and_keeps_balanced_closers(self):
        self.assertEqual(normalize_translation_unit("  Hello!!!  "), "Hello")
        self.assertEqual(normalize_translation_unit("  Japanese text...  "), "Japanese text")
        self.assertEqual(normalize_translation_unit("Meaning? (yes)"), "Meaning? (yes)")

    def test_normalization_rejects_invalid_unicode_and_empty_output(self):
        with self.assertRaisesRegex(TranslationError, "translation_invalid_unicode"):
            normalize_translation_unit("\ud800")
        with self.assertRaisesRegex(TranslationError, "translation_empty_after_normalization"):
            normalize_translation_unit("!?...")


if __name__ == "__main__":
    unittest.main()

