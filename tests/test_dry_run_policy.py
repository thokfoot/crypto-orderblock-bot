from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from dry_run_data_check import is_fyers_check_acceptable


class FyersPolicyTests(unittest.TestCase):
    def test_missing_credentials_are_skipped_when_optional(self):
        self.assertTrue(is_fyers_check_acceptable({"ok": False, "status": "missing_credentials"}, required=False))

    def test_missing_credentials_fail_when_required(self):
        self.assertFalse(is_fyers_check_acceptable({"ok": False, "status": "missing_credentials"}, required=True))

    def test_valid_fyers_connection_passes(self):
        self.assertTrue(is_fyers_check_acceptable({"ok": True}, required=True))

    def test_bad_credentials_do_not_get_skipped(self):
        self.assertFalse(is_fyers_check_acceptable({"ok": False, "status": "unauthorized"}, required=False))


if __name__ == "__main__":
    unittest.main()
