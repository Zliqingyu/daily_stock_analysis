# -*- coding: utf-8 -*-
"""
Tests for connectivity check fail-closed semantics.

Covers ZhuLinsen's requirements:
1. HTTP 200 + non-JSON body → WARN (not PASS)
2. Bocha returns {} → WARN (not PASS)
3. Anspire returns {} → WARN (not PASS)
4. --llm-only with config load failure → exit 1 (not exit 0)
"""

import pytest
import sys
import os
from unittest.mock import patch, MagicMock, mock_open

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestHttpProbeFailClosed:
    """Test that _http_probe fails closed on bad responses."""

    def _make_response(self, status_code=200, json_body=None, text="OK"):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text
        if json_body is not None:
            resp.json.return_value = json_body
        else:
            resp.json.side_effect = ValueError("not JSON")
        return resp

    def test_http_200_non_json_with_validate_body(self):
        """HTTP 200 + non-JSON body + validate_body provided → WARN."""
        from scripts.check_connectivity import _http_probe

        with patch("requests.request", return_value=self._make_response(200, json_body=None)):
            result = _http_probe(
                "TestAPI", "GET", "https://example.com/api",
                validate_body=lambda b: (True, "ok"),
            )
        assert result.status == "WARN"
        assert "not valid JSON" in result.detail

    def test_http_200_empty_dict_bocha_contract(self):
        """Bocha contract: {} → validate_body returns False → WARN."""
        from scripts.check_connectivity import _http_probe

        with patch("requests.request", return_value=self._make_response(200, json_body={})):
            result = _http_probe(
                "Bocha", "POST", "https://api.bocha.cn/v1/web-search",
                validate_body=lambda b: (
                    b.get("code") == 200,
                    f"code={b.get('code')!r} (expected 200)",
                ),
            )
        assert result.status == "WARN"
        assert "code=None" in result.detail

    def test_http_200_empty_dict_anspire_contract(self):
        """Anspire contract: {} → no 'results' key → WARN."""
        from scripts.check_connectivity import _http_probe

        with patch("requests.request", return_value=self._make_response(200, json_body={})):
            result = _http_probe(
                "Anspire", "GET", "https://plugin.anspire.cn/api/ntsearch/search",
                validate_body=lambda b: (
                    ("code" not in b or b.get("code") == 200) and "results" in b,
                    f"code={b.get('code')!r}, has_results={'results' in b}",
                ),
            )
        assert result.status == "WARN"
        assert "has_results=False" in result.detail

    def test_http_200_valid_bocha_response(self):
        """Bocha valid response: code=200 → PASS."""
        from scripts.check_connectivity import _http_probe

        with patch("requests.request", return_value=self._make_response(200, json_body={"code": 200, "data": {"webPages": {"value": []}}})):
            result = _http_probe(
                "Bocha", "POST", "https://api.bocha.cn/v1/web-search",
                validate_body=lambda b: (
                    b.get("code") == 200,
                    f"code={b.get('code')!r} (expected 200)",
                ),
            )
        assert result.status == "PASS"

    def test_http_200_valid_anspire_response(self):
        """Anspire valid response: has 'results' key, no 'code' → PASS."""
        from scripts.check_connectivity import _http_probe

        with patch("requests.request", return_value=self._make_response(200, json_body={"results": []})):
            result = _http_probe(
                "Anspire", "GET", "https://plugin.anspire.cn/api/ntsearch/search",
                validate_body=lambda b: (
                    ("code" not in b or b.get("code") == 200) and "results" in b,
                    f"code={b.get('code')!r}, has_results={'results' in b}",
                ),
            )
        assert result.status == "PASS"

    def test_http_200_anspire_error_code(self):
        """Anspire error: code=401 → WARN."""
        from scripts.check_connectivity import _http_probe

        with patch("requests.request", return_value=self._make_response(200, json_body={"code": 401, "msg": "unauthorized"})):
            result = _http_probe(
                "Anspire", "GET", "https://plugin.anspire.cn/api/ntsearch/search",
                validate_body=lambda b: (
                    ("code" not in b or b.get("code") == 200) and "results" in b,
                    f"code={b.get('code')!r}, has_results={'results' in b}",
                ),
            )
        assert result.status == "WARN"

    def test_http_500(self):
        """HTTP 500 → FAIL."""
        from scripts.check_connectivity import _http_probe

        with patch("requests.request", return_value=self._make_response(500, text="Internal Server Error")):
            result = _http_probe("TestAPI", "GET", "https://example.com/api")
        assert result.status == "FAIL"

    def test_network_error(self):
        """Network error → FAIL."""
        from scripts.check_connectivity import _http_probe

        with patch("requests.request", side_effect=ConnectionError("timeout")):
            result = _http_probe("TestAPI", "GET", "https://example.com/api")
        assert result.status == "FAIL"


class TestLlmOnlyFailClosed:
    """Test that --llm-only with config load failure exits 1."""

    def test_llm_only_config_fail_exits_1(self):
        """--llm-only with config load failure → exit 1 (not 0)."""
        from scripts.check_connectivity import main

        mock_config = MagicMock()
        mock_config.llm_channels = []

        with patch("sys.argv", ["check_connectivity.py", "--llm-only"]):
            # Make get_config raise, simulating broken config
            with patch("src.config.get_config", side_effect=Exception("config load failed")):
                exit_code = main()
        assert exit_code == 1

    def test_llm_only_config_ok_exits_0(self):
        """--llm-only with successful config load but no failures → exit 0."""
        from scripts.check_connectivity import main

        mock_config = MagicMock()
        mock_config.llm_channels = []

        with patch("sys.argv", ["check_connectivity.py", "--llm-only"]):
            with patch("src.config.get_config", return_value=mock_config):
                with patch("scripts.check_connectivity.check_llm_channels", return_value=[]):
                    exit_code = main()
        assert exit_code == 0


class TestValidateBodyContracts:
    """Test that validate_body lambdas match runtime search_service.py contracts."""

    def test_bocha_contract_matches_runtime(self):
        """Bocha runtime: data.get('code') != 200 → error.
        Our validate_body must reject anything that runtime would reject."""
        from scripts.check_connectivity import check_search_keys

        # Simulate Bocha returning {} (runtime would treat as error)
        with patch.dict("os.environ", {"BOCHA_API_KEYS": "test_key"}):
            with patch("requests.request", return_value=MagicMock(
                status_code=200,
                json=MagicMock(return_value={}),
                text="{}",
            )):
                results = check_search_keys()
        bocha_result = [r for r in results if r.name == "Bocha"]
        assert bocha_result, "Bocha check should exist"
        assert bocha_result[0].status == "WARN", f"Empty {{}} should be WARN, got {bocha_result[0].status}"

    def test_anspire_contract_matches_runtime(self):
        """Anspire runtime: checks 'code' field + 'results' field.
        Our validate_body must reject {} just like runtime."""
        from scripts.check_connectivity import check_search_keys

        with patch.dict("os.environ", {"ANSPIRE_API_KEYS": "test_key", "ANSPIRE_SEARCH_ENABLED": "true"}):
            with patch("requests.request", return_value=MagicMock(
                status_code=200,
                json=MagicMock(return_value={}),
                text="{}",
            )):
                results = check_search_keys()
        anspire_result = [r for r in results if r.name == "Anspire"]
        assert anspire_result, "Anspire check should exist"
        assert anspire_result[0].status == "WARN", f"Empty {{}} should be WARN, got {anspire_result[0].status}"


# ---------------------------------------------------------------------------
# Notification channel probe tests
# ---------------------------------------------------------------------------

class TestNotificationProbeConfig:
    """Notification probes: configured → probe, not configured → SKIP."""

    def _make_config(self, **kwargs):
        cfg = MagicMock(spec=[])
        for k, v in kwargs.items():
            setattr(cfg, k, v)
        return cfg

    def test_all_skip_when_nothing_configured(self):
        """Empty config → all 14 channels SKIP."""
        from scripts.check_connectivity import check_notification_channels
        config = self._make_config()
        results = check_notification_channels(config)
        skip_names = [r.name for r in results if r.status == "SKIP"]
        assert len(skip_names) >= 14, f"Expected 14+ SKIPs, got {len(skip_names)}"
        assert all(r.status == "SKIP" for r in results), "All should be SKIP"

    def test_wechat_configured_probes(self):
        """WeChat configured → not SKIP."""
        from scripts.check_connectivity import check_notification_channels
        config = self._make_config(wechat_webhook_url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test")
        with patch("scripts.check_connectivity.requests.head", return_value=MagicMock(status_code=200)):
            results = check_notification_channels(config)
        wechat = [r for r in results if r.name == "WeChat"]
        assert wechat and wechat[0].status != "SKIP"

    def test_email_configured_probes_smtp(self):
        """Email configured → SMTP probe (mocked)."""
        from scripts.check_connectivity import check_notification_channels, _probe_email
        with patch("smtplib.SMTP") as mock_smtp:
            mock_server = MagicMock()
            mock_smtp.return_value = mock_server
            result = _probe_email("Email", "test@gmail.com", "password")
        assert result.status == "PASS"
        assert "login OK" in result.detail

    def test_email_unreachable_fails(self):
        """Email SMTP unreachable → FAIL."""
        from scripts.check_connectivity import _probe_email
        with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("connection refused")):
            result = _probe_email("Email", "test@gmail.com", "password")
        assert result.status == "FAIL"
        assert "password" not in result.detail  # secret sanitized

    def test_telegram_configured_probes(self):
        """Telegram configured → getMe probe (mocked)."""
        from scripts.check_connectivity import _probe_telegram
        with patch("scripts.check_connectivity.requests.get",
                   return_value=MagicMock(json=MagicMock(return_value={"ok": True, "result": {"username": "testbot"}}))):
            result = _probe_telegram("Telegram", "fake_token", "fake_chat")
        assert result.status == "PASS"
        assert "testbot" in result.detail

    def test_telegram_invalid_token_fails(self):
        """Telegram invalid token → FAIL."""
        from scripts.check_connectivity import _probe_telegram
        with patch("scripts.check_connectivity.requests.get",
                   return_value=MagicMock(json=MagicMock(return_value={"ok": False, "description": "Unauthorized"}))):
            result = _probe_telegram("Telegram", "fake_token", "fake_chat")
        assert result.status == "FAIL"
        assert "fake_token" not in result.detail  # token sanitized

    def test_webhook_reachable_passes(self):
        """Webhook returns 200 → PASS."""
        from scripts.check_connectivity import _probe_webhook
        with patch("scripts.check_connectivity.requests.head", return_value=MagicMock(status_code=200)):
            result = _probe_webhook("TestWH", "https://example.com/hook")
        assert result.status == "PASS"

    def test_webhook_4xx_without_sendtest_passes(self):
        """Webhook returns 4xx without --send-test → PASS (reachable)."""
        from scripts.check_connectivity import _probe_webhook
        with patch("scripts.check_connectivity.requests.head", return_value=MagicMock(status_code=403)):
            result = _probe_webhook("TestWH", "https://example.com/hook", send_test=False)
        assert result.status == "PASS"
        assert "reachable" in result.detail

    def test_webhook_4xx_with_sendtest_fails(self):
        """Webhook returns 4xx WITH --send-test → FAIL."""
        from scripts.check_connectivity import _probe_webhook
        with patch("scripts.check_connectivity.requests.post", return_value=MagicMock(status_code=403, text="Forbidden")):
            result = _probe_webhook("TestWH", "https://example.com/hook", send_test=True)
        assert result.status == "FAIL"

    def test_webhook_network_error_fails(self):
        """Webhook network error → FAIL."""
        from scripts.check_connectivity import _probe_webhook
        with patch("scripts.check_connectivity.requests.head", side_effect=ConnectionError("timeout")):
            result = _probe_webhook("TestWH", "https://example.com/hook")
        assert result.status == "FAIL"

    def test_send_test_does_not_fire_without_flag(self):
        """Default mode must NOT POST (no test messages sent)."""
        from scripts.check_connectivity import check_notification_channels
        config = self._make_config(wechat_webhook_url="https://example.com/hook")
        with patch("scripts.check_connectivity.requests.head", return_value=MagicMock(status_code=200)) as mock_head, \
             patch("scripts.check_connectivity.requests.post") as mock_post:
            check_notification_channels(config, send_test=False)
        mock_post.assert_not_called()
        mock_head.assert_called()
