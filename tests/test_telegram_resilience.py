from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import scripts.v325_nse_cloud_runner as nse_runner
import scripts.v325_us_cloud_runner as us_runner


class TestTelegramResilience(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {
            "AUTOMATION_TELEGRAM_BOT_TOKEN": "mock_token_12345",
            "AUTOMATION_TELEGRAM_CHAT_ID": "mock_chat_67890",
        })
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_nse_send_telegram_markdown_http_400_plain_text_fallback(self):
        """
        Verify that an unescaped entity (such as GoogleFinance_Telemetry in Markdown mode)
        that produces an HTTP 400 Bad Request is caught and automatically retried
        as plain text without parse_mode, preventing runner termination.
        """
        failed_production_msg = (
            "⚠️ [NSE STALE DATA] TITAN: latest completed 1-minute bar is older than 180 seconds "
            "(Ref Spot: ₹4697.10 via GoogleFinance_Telemetry). Signal monitoring is paused safely "
            "(no unverified fills)."
        )

        call_history = []

        def mock_urlopen(req, timeout=15):
            call_history.append(req)
            payload = json.loads(req.data.decode("utf-8"))
            if "parse_mode" in payload and payload["parse_mode"] == "Markdown":
                # Simulate Telegram rejecting unescaped underscore in GoogleFinance_Telemetry
                raise urllib.error.HTTPError(
                    url="https://api.telegram.org/botmock_token/sendMessage",
                    code=400,
                    msg="Bad Request: can't parse entities: Character '_' is reserved and must be escaped with '\\'",
                    hdrs=None,
                    fp=io.BytesIO(b'{"ok":false,"error_code":400,"description":"Bad Request: can\'t parse entities"}'),
                )
            # Second attempt: Plain-text fallback without parse_mode
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"ok": True, "result": {"message_id": 8888}}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = nse_runner.send_telegram(failed_production_msg)

        self.assertEqual(len(call_history), 2, "Expected exactly 2 attempts: 1 markdown, 1 plain-text fallback")
        # First attempt had parse_mode = Markdown
        first_payload = json.loads(call_history[0].data.decode("utf-8"))
        self.assertEqual(first_payload.get("parse_mode"), "Markdown")

        # Second attempt omitted parse_mode (plain text)
        second_payload = json.loads(call_history[1].data.decode("utf-8"))
        self.assertNotIn("parse_mode", second_payload)
        self.assertIn("GoogleFinance_Telemetry", second_payload["text"])
        self.assertEqual(res, {"ok": True, "message_id": 8888})

    def test_nse_send_telegram_sources_with_underscores(self):
        """
        Test that all telemetry fallback sources with underscores (TwelveData_REST,
        Yahoo_v8_REST, GoogleFinance_Telemetry) safely deliver via fallback when HTTP 400 is returned.
        """
        sources = ["TwelveData_REST", "Yahoo_v8_REST", "GoogleFinance_Telemetry"]

        for src in sources:
            msg = f"⚠️ [NSE STALE DATA] RELIANCE: Ref Spot via {src}"
            attempts = 0

            def mock_urlopen(req, timeout=15):
                nonlocal attempts
                attempts += 1
                payload = json.loads(req.data.decode("utf-8"))
                if "parse_mode" in payload:
                    raise urllib.error.HTTPError(
                        url="https://api.telegram.org",
                        code=400,
                        msg="Bad Request",
                        hdrs=None,
                        fp=io.BytesIO(b'{"ok":false}'),
                    )
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps({"ok": True, "result": {"message_id": 7777}}).encode("utf-8")
                mock_resp.__enter__.return_value = mock_resp
                return mock_resp

            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                res = nse_runner.send_telegram(msg)
                self.assertEqual(attempts, 2)
                self.assertEqual(res["ok"], True)

    def test_nse_send_telegram_non_400_http_error_still_raises(self):
        """
        Verify that non-400 HTTP errors (e.g. 401 Unauthorized, 403 Forbidden, 500)
        are NOT swallowed and correctly raise RuntimeError to preserve failure alerting.
        """
        def mock_urlopen(req, timeout=15):
            raise urllib.error.HTTPError(
                url="https://api.telegram.org",
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=io.BytesIO(b'{"ok":false,"error_code":401}'),
            )

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                nse_runner.send_telegram("Test message")
            self.assertIn("Telegram alert delivery failed", str(ctx.exception))

    def test_us_send_telegram_http_400_plain_text_fallback(self):
        """
        Verify that the US runner also falls back to plain text if HTML entity parsing
        returns an HTTP 400 error.
        """
        msg_with_bad_html = "⚠️ [US STALE DATA] AAPL: <unclosed tag Ref Spot: $220.50"
        attempts = 0

        def mock_urlopen(req, timeout=10):
            nonlocal attempts
            attempts += 1
            payload = json.loads(req.data.decode("utf-8"))
            if "parse_mode" in payload and payload["parse_mode"] == "HTML":
                raise urllib.error.HTTPError(
                    url="https://api.telegram.org",
                    code=400,
                    msg="Bad Request: can't parse entities",
                    hdrs=None,
                    fp=io.BytesIO(b'{"ok":false}'),
                )
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"ok": True, "result": {"message_id": 5555}}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            res = us_runner.send_telegram(msg_with_bad_html)

        self.assertEqual(attempts, 2)
        self.assertEqual(res["ok"], True)


if __name__ == "__main__":
    unittest.main()
