import unittest

from app.translation.runtime import _supported_runtime_version


class LocalRuntimeVersionTests(unittest.TestCase):
    def test_accepts_build_reported_on_stderr(self):
        self.assertTrue(_supported_runtime_version(0, "", "version: 1.0.0-dev (build 12676, commit fixture)"))

    def test_rejects_other_builds_and_failed_commands(self):
        self.assertFalse(_supported_runtime_version(0, "build 99999", ""))
        self.assertFalse(_supported_runtime_version(1, "", "build 12676"))


if __name__ == "__main__":
    unittest.main()
