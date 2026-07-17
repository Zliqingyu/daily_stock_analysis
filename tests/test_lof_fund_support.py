"""LOF fund support: dispatch, empty-response fallback, exception fallback, classification contract.

Covers the scenarios ZhuLinsen required:
- LOF dispatch calls fund_lof_hist_em
- Empty LOF response falls back to ETF
- LOF exception falls back to ETF
- Classification: LOF and ETF prefixes are mutually exclusive where they should be
- Fallback actually calls fund_etf_hist_em
"""
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.akshare_fetcher import AkshareFetcher, _is_lof_code, _is_etf_code


def _make_fetcher() -> AkshareFetcher:
    from types import SimpleNamespace
    with patch(
        "data_provider.akshare_fetcher.get_config",
        return_value=SimpleNamespace(enable_eastmoney_patch=False),
    ):
        return AkshareFetcher(sleep_min=0, sleep_max=0)


def _history_frame(code: str = "161116") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "日期": pd.date_range("2026-01-01", periods=5).strftime("%Y-%m-%d"),
            "开盘": [1.0, 1.01, 1.02, 1.03, 1.04],
            "收盘": [1.01, 1.02, 1.03, 1.04, 1.05],
            "最高": [1.02, 1.03, 1.04, 1.05, 1.06],
            "最低": [0.99, 1.0, 1.01, 1.02, 1.03],
            "成交量": [10000, 11000, 12000, 13000, 14000],
            "成交额": [10100, 11220, 12360, 13520, 14700],
            "涨跌幅": [0.0, 0.99, 0.98, 0.97, 0.96],
        }
    )


# ---------------------------------------------------------------------------
# Classification contract
# ---------------------------------------------------------------------------

class TestLofClassification:
    """LOF 与 ETF 代码段互斥分类"""

    @pytest.mark.parametrize("code", [
        "160123", "161116", "162411", "163406",
        "164105", "165309", "166009", "167001",
        "168101", "169101",
        "501018", "501009", "502000", "506000",
    ])
    def test_lof_codes(self, code: str):
        assert _is_lof_code(code) is True

    @pytest.mark.parametrize("code", [
        "510010", "512400", "513310", "515000", "516000",
        "520500", "526000", "530000",
        "560010", "561000", "562000", "563230",
        "588000", "589000",
        "159919",
    ])
    def test_etf_codes(self, code: str):
        assert _is_etf_code(code) is True
        assert _is_lof_code(code) is False

    @pytest.mark.parametrize("code", [
        "000001", "600519", "300750", "002050",
    ])
    def test_normal_stocks_neither(self, code: str):
        assert _is_lof_code(code) is False
        assert _is_etf_code(code) is False

    def test_prefixed_codes(self):
        assert _is_lof_code("161116.SZ")
        assert _is_etf_code("513310.SH")

    def test_invalid_codes(self):
        assert not _is_lof_code("12345")
        assert not _is_lof_code("abcdef")
        assert not _is_etf_code("12345")
        assert not _is_etf_code("abcdef")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

class TestLofDispatch:
    """LOF 代码走 fund_lof_hist_em，不走 fund_etf_hist_em"""

    def test_lof_dispatch_calls_fund_lof_hist_em(self):
        fetcher = _make_fetcher()
        fake_lof = MagicMock(return_value=_history_frame())
        fake_etf = MagicMock(return_value=_history_frame())
        fake_akshare = types.SimpleNamespace(
            fund_lof_hist_em=fake_lof,
            fund_etf_hist_em=fake_etf,
        )

        with patch.dict(sys.modules, {"akshare": fake_akshare}):
            with patch.object(fetcher, "_set_random_user_agent"), \
                 patch.object(fetcher, "_enforce_rate_limit"):
                df = fetcher._fetch_raw_data("161116", "2026-01-01", "2026-01-05")

        assert df is not None and not df.empty
        fake_lof.assert_called_once()
        fake_etf.assert_not_called()

    def test_lof_dispatch_with_501_prefix(self):
        """上交所 LOF 501xxx 也走 LOF 接口"""
        fetcher = _make_fetcher()
        fake_lof = MagicMock(return_value=_history_frame("501018"))
        fake_etf = MagicMock(return_value=_history_frame("501018"))
        fake_akshare = types.SimpleNamespace(
            fund_lof_hist_em=fake_lof,
            fund_etf_hist_em=fake_etf,
        )

        with patch.dict(sys.modules, {"akshare": fake_akshare}):
            with patch.object(fetcher, "_set_random_user_agent"), \
                 patch.object(fetcher, "_enforce_rate_limit"):
                df = fetcher._fetch_raw_data("501018", "2026-01-01", "2026-01-05")

        assert df is not None and not df.empty
        fake_lof.assert_called_once()
        fake_etf.assert_not_called()


# ---------------------------------------------------------------------------
# Empty response fallback
# ---------------------------------------------------------------------------

class TestLofEmptyFallback:
    """LOF 返回空 DataFrame 时回退到 ETF 接口"""

    def test_empty_lof_response_falls_back_to_etf(self):
        fetcher = _make_fetcher()
        empty_df = pd.DataFrame()
        etf_df = _history_frame()
        fake_lof = MagicMock(return_value=empty_df)
        fake_etf = MagicMock(return_value=etf_df)
        fake_akshare = types.SimpleNamespace(
            fund_lof_hist_em=fake_lof,
            fund_etf_hist_em=fake_etf,
        )

        with patch.dict(sys.modules, {"akshare": fake_akshare}):
            with patch.object(fetcher, "_set_random_user_agent"), \
                 patch.object(fetcher, "_enforce_rate_limit"):
                df = fetcher._fetch_lof_data("161116", "2026-01-01", "2026-01-05")

        # ETF fallback was called
        fake_lof.assert_called_once()
        fake_etf.assert_called_once()
        # Got ETF data back
        assert df is etf_df

    def test_empty_lof_via_dispatch_falls_back(self):
        """端到端：通过 _fetch_raw_data dispatch 后空响应也走 fallback"""
        fetcher = _make_fetcher()
        empty_df = pd.DataFrame()
        etf_df = _history_frame()
        fake_lof = MagicMock(return_value=empty_df)
        fake_etf = MagicMock(return_value=etf_df)
        fake_akshare = types.SimpleNamespace(
            fund_lof_hist_em=fake_lof,
            fund_etf_hist_em=fake_etf,
        )

        with patch.dict(sys.modules, {"akshare": fake_akshare}):
            with patch.object(fetcher, "_set_random_user_agent"), \
                 patch.object(fetcher, "_enforce_rate_limit"):
                df = fetcher._fetch_raw_data("162411", "2026-01-01", "2026-01-05")

        fake_lof.assert_called_once()
        fake_etf.assert_called_once()
        assert df is etf_df


# ---------------------------------------------------------------------------
# Exception fallback
# ---------------------------------------------------------------------------

class TestLofExceptionFallback:
    """LOF API 异常时回退到 ETF 接口"""

    def test_lof_exception_falls_back_to_etf(self):
        fetcher = _make_fetcher()
        etf_df = _history_frame()
        fake_lof = MagicMock(side_effect=RuntimeError("network error"))
        fake_etf = MagicMock(return_value=etf_df)
        fake_akshare = types.SimpleNamespace(
            fund_lof_hist_em=fake_lof,
            fund_etf_hist_em=fake_etf,
        )

        with patch.dict(sys.modules, {"akshare": fake_akshare}):
            with patch.object(fetcher, "_set_random_user_agent"), \
                 patch.object(fetcher, "_enforce_rate_limit"):
                df = fetcher._fetch_lof_data("163406", "2026-01-01", "2026-01-05")

        fake_lof.assert_called_once()
        fake_etf.assert_called_once()
        assert df is etf_df

    def test_lof_rate_limit_does_not_fallback(self):
        """限流异常不应该 fallback，而是直接抛出"""
        from data_provider.base import RateLimitError

        fetcher = _make_fetcher()
        fake_lof = MagicMock(side_effect=RuntimeError("访问频率超限"))
        fake_etf = MagicMock(return_value=_history_frame())
        fake_akshare = types.SimpleNamespace(
            fund_lof_hist_em=fake_lof,
            fund_etf_hist_em=fake_etf,
        )

        with patch.dict(sys.modules, {"akshare": fake_akshare}):
            with patch.object(fetcher, "_set_random_user_agent"), \
                 patch.object(fetcher, "_enforce_rate_limit"):
                with pytest.raises(RateLimitError):
                    fetcher._fetch_lof_data("161116", "2026-01-01", "2026-01-05")

        fake_etf.assert_not_called()


# ---------------------------------------------------------------------------
# Realtime quote dispatch
# ---------------------------------------------------------------------------

class TestLofRealtimeDispatch:
    """LOF 实时行情也走 ETF 实时接口"""

    def test_lof_realtime_uses_etf_realtime(self):
        fetcher = _make_fetcher()
        # Mock circuit_breaker via the getter function
        fake_cb = MagicMock()
        fake_cb.is_available.return_value = True
        with patch("data_provider.akshare_fetcher.get_realtime_circuit_breaker", return_value=fake_cb):
            with patch.object(fetcher, "_get_etf_realtime_quote", return_value={"price": 1.05}) as mock_rt:
                result = fetcher.get_realtime_quote("161116")

        assert result is not None
        mock_rt.assert_called_once_with("161116")


# ---------------------------------------------------------------------------
# Network tests (real API calls, marked for manual/CI opt-in)
# ---------------------------------------------------------------------------

@pytest.mark.network
class TestLofNetworkValidation:
    """真实 API 调用验证 LOF dispatch 和 fallback。

    标记为 network，CI 默认不执行。手动验证命令：
        pytest tests/test_lof_fund_support.py::TestLofNetworkValidation -v -m network

    验证目标：
    - fund_lof_hist_em 对真实 LOF 代码返回非空数据
    - fund_etf_hist_em 对真实 ETF 代码返回非空数据
    - fund_lof_hist_em 对真实 ETF 代码（如 159919）返回空 → fallback 成立
    """

    def test_real_lof_returns_data(self):
        """真实 LOF 代码 161116 走 fund_lof_hist_em 返回数据"""
        import akshare as ak
        df = ak.fund_lof_hist_em(
            symbol="161116", period="daily",
            start_date="20250101", end_date="20250301", adjust="qfq",
        )
        assert df is not None and not df.empty, "fund_lof_hist_em 应对 161116 返回数据"

    def test_real_etf_returns_data(self):
        """真实 ETF 代码 159919 走 fund_etf_hist_em 返回数据"""
        import akshare as ak
        df = ak.fund_etf_hist_em(
            symbol="159919", period="daily",
            start_date="20250101", end_date="20250301", adjust="qfq",
        )
        assert df is not None and not df.empty, "fund_etf_hist_em 应对 159919 返回数据"

    def test_lof_api_returns_empty_for_etf_code(self):
        """fund_lof_hist_em 对 ETF 代码返回空（证明 fallback 必要性）"""
        import akshare as ak
        df = ak.fund_lof_hist_em(
            symbol="159919", period="daily",
            start_date="20250101", end_date="20250301", adjust="qfq",
        )
        assert df is None or df.empty, "fund_lof_hist_em 对 ETF 代码应返回空"


# ---------------------------------------------------------------------------
# End-to-end semantic tests: LOF codes recognized across the full stack
# ---------------------------------------------------------------------------

class TestLofFullStackSemantics:
    """Verify that LOF codes (especially 501/502/506) are recognized as
    funds everywhere — not just in AkshareFetcher, but also in
    DataFetcherManager, SearchService, and pipeline-level checks.
    """

    def test_base_is_etf_code_includes_sh_lof(self):
        """base._is_etf_code must include 501/502/506 for manager-level routing."""
        from data_provider.base import _is_etf_code as base_is_etf
        assert base_is_etf("501018"), "501018 should be recognized as fund in base"
        assert base_is_etf("502000"), "502000 should be recognized as fund in base"
        assert base_is_etf("506000"), "506000 should be recognized as fund in base"

    def test_base_is_etf_code_includes_sz_lof(self):
        """base._is_etf_code must include 160-169 for manager-level routing."""
        from data_provider.base import _is_etf_code as base_is_etf
        for code in ("160105", "161116", "164105", "169101"):
            assert base_is_etf(code), f"{code} should be recognized as fund in base"

    def test_search_service_recognizes_sh_lof_as_fund(self):
        """SearchService.is_index_or_etf must return True for 501/502/506."""
        from src.search_service import SearchService
        assert SearchService.is_index_or_etf("501018", "南方原油LOF")
        assert SearchService.is_index_or_etf("502000", "test")
        assert SearchService.is_index_or_etf("506000", "test")

    def test_search_service_recognizes_sz_lof_as_fund(self):
        """SearchService.is_index_or_etf must return True for 16xxxx."""
        from src.search_service import SearchService
        assert SearchService.is_index_or_etf("161116", "test")
        assert SearchService.is_index_or_etf("164105", "test")

    def test_efinance_secid_for_sh_lof(self):
        """EfinanceFetcher must build correct secid for Shanghai LOF."""
        from data_provider.efinance_fetcher import _build_eastmoney_etf_secid
        assert _build_eastmoney_etf_secid("501018") == "1.501018"
        assert _build_eastmoney_etf_secid("502000") == "1.502000"

    def test_normal_stock_not_recognized_as_fund(self):
        """Normal A-share stocks must not be recognized as fund."""
        from data_provider.base import _is_etf_code as base_is_etf
        from src.search_service import SearchService
        assert not base_is_etf("600519")
        assert not SearchService.is_index_or_etf("600519", "贵州茅台")

    def test_18xxxx_not_in_fund_prefixes(self):
        """18xxxx (traditional closed-end) must NOT be in FUND_PREFIXES.
        Consistent with akshare_fetcher._is_etf_code which removed it."""
        from data_provider.base import ETF_PREFIXES, FUND_PREFIXES
        # 18 should not match any prefix in the tuple
        for prefix in FUND_PREFIXES:
            assert not "180003".startswith(prefix), f"180003 matched by prefix {prefix}"
        assert "180003" not in FUND_PREFIXES
