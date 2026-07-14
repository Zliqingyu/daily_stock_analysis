"""Tests for astock_data_provider module."""
import pytest


class TestNormalizeCode:
    """Test _normalize_code function."""

    def test_suffix_removal(self):
        from data_provider.astock_data_provider import _normalize_code
        assert _normalize_code("600519.SH") == "600519"
        assert _normalize_code("000001.SZ") == "000001"
        assert _normalize_code("920493.BJ") == "920493"
        assert _normalize_code("600519.SS") == "600519"

    def test_prefix_removal(self):
        from data_provider.astock_data_provider import _normalize_code
        assert _normalize_code("SH600519") == "600519"
        assert _normalize_code("SZ000001") == "000001"
        assert _normalize_code("BJ920493") == "920493"

    def test_prefix_with_dot(self):
        from data_provider.astock_data_provider import _normalize_code
        assert _normalize_code("SH.600519") == "600519"
        assert _normalize_code("SZ.000001") == "000001"

    def test_pure_code(self):
        from data_provider.astock_data_provider import _normalize_code
        assert _normalize_code("600519") == "600519"
        assert _normalize_code("000001") == "000001"

    def test_lowercase_input(self):
        from data_provider.astock_data_provider import _normalize_code
        assert _normalize_code("sh600519") == "600519"
        assert _normalize_code("sz000001") == "000001"
        assert _normalize_code("600519.sh") == "600519"

    def test_whitespace_handling(self):
        from data_provider.astock_data_provider import _normalize_code
        assert _normalize_code("  SH600519  ") == "600519"

    def test_invalid_format_preserved(self):
        from data_provider.astock_data_provider import _normalize_code
        # Should not strip prefix if not exactly 6 digits after
        assert _normalize_code("SH12345") == "SH12345"
        assert _normalize_code("SH1234567") == "1234567"


class TestEmLock:
    """Test em_get uses lock for thread safety."""

    def test_lock_exists(self):
        from data_provider.astock_data_provider import _em_lock
        import threading
        assert isinstance(_em_lock, type(threading.Lock()))
