# -*- coding: utf-8 -*-
"""Hithink (同花顺扶摇 fuyao.aicubes.cn) official daily K-line fetcher.

Requires HITHINK_FINANCE_API_KEY (X-api-key header). Covers A-share stocks
only (SH/SZ/BJ); ETF/fund codes are rejected so the manager falls through
to the next source. Endpoint: GET /api/a-share/prices/historical
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import pandas as pd
import requests

from .base import BaseFetcher, DataFetchError, STANDARD_COLUMNS, normalize_stock_code, is_bse_code

logger = logging.getLogger(__name__)

_MAX_WINDOW_YEARS = 10  # API hard limit: [start, end] span <= 10 years
_DEFAULT_BASE_URL = "https://fuyao.aicubes.cn"


def _to_thscode(stock_code: str) -> str:
    """Map normalized 6-digit code to THS suffix form. '' = unsupported.

    A-share stocks: 6xxxxx/688xxx->SH, 0/3xxxxx->SZ, 8/4/92xxxx->BJ.
    On-exchange funds (ETF/LOF): 5xxxxx->SH, 1xxxxx->SZ — served by the
    /api/fund/market endpoints (daily K supports ETF only; LOF will be
    rejected server-side with 3004 and fall through the chain).
    """
    code = normalize_stock_code(stock_code)
    if not code or not code.isdigit() or len(code) != 6:
        return ""
    if is_bse_code(code):
        return f"{code}.BJ"
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith("5"):
        return f"{code}.SH"  # SH ETF/LOF
    if code.startswith("1"):
        return f"{code}.SZ"  # SZ ETF/LOF
    # 204x bonds etc. -> not covered
    return ""


def _is_fund_code(stock_code: str) -> bool:
    """True for on-exchange fund codes (SH 5xxxxx / SZ 1xxxxx) routed to /api/fund."""
    code = normalize_stock_code(stock_code)
    return bool(code) and code.startswith(("5", "1"))


def _to_ms(date_text: str) -> Optional[int]:
    try:
        return int(time.mktime(time.strptime(date_text, "%Y-%m-%d")) * 1000)
    except (ValueError, TypeError):
        return None


class HithinkFetcher(BaseFetcher):
    """Fetch qfq daily K-line from Hithink Finance API (official, quota-based)."""

    name = "HithinkFetcher"
    priority = 2
    allow_empty_daily_data = True

    _HTTP_TIMEOUT_SECONDS = 20

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> None:
        self.api_key = (api_key or os.getenv("HITHINK_FINANCE_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("HITHINK_FINANCE_BASE_URL", "") or _DEFAULT_BASE_URL).rstrip("/")
        if priority is not None:
            self.priority = priority
        if not self.api_key:
            raise DataFetchError("HithinkFetcher requires HITHINK_FINANCE_API_KEY")

    # ------------------------------------------------------------------ daily
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        thscode = _to_thscode(stock_code)
        if not thscode:
            raise DataFetchError(f"HithinkFetcher unsupported code (A-share stocks only): {stock_code}")
        start_ms, end_ms = _to_ms(start_date), _to_ms(end_date)
        if not start_ms or not end_ms:
            raise DataFetchError(f"HithinkFetcher invalid date range: {start_date}~{end_date}")

        is_fund = _is_fund_code(stock_code)
        if is_fund:
            # ETF daily K endpoint; window hard limit 5 calendar years, no adjust param (qfq fixed)
            endpoint = f"{self.base_url}/api/fund/market/historical"
            params = {"thscode": thscode, "interval": "1d", "start": start_ms, "end": end_ms}
        else:
            endpoint = f"{self.base_url}/api/a-share/prices/historical"
            params = {
                "thscode": thscode,
                "interval": "1d",
                "start": start_ms,
                "end": end_ms,
                "adjust": "forward",  # 前复权, 与其他源保持一致
            }

        response = requests.get(
            endpoint,
            headers={"X-api-key": self.api_key, "Accept": "application/json"},
            params=params,
            timeout=self._HTTP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        code = payload.get("code")
        if code in (1002, 3004):
            # unsupported symbol server-side (delisted stock / LOF on fund endpoint) -> let chain fall through
            raise DataFetchError(f"HithinkFetcher server rejected {stock_code}: {payload.get('message')}")
        if code != 0:
            raise DataFetchError(f"HithinkFetcher API error code={code}: {payload.get('message')}")

        items = (payload.get("data") or {}).get("item") or []
        if not items:
            logger.info("HithinkFetcher empty daily history for %s", stock_code)
            return pd.DataFrame(columns=STANDARD_COLUMNS)

        rows = []
        for it in items:
            date_ms = it.get("date_ms")
            if not date_ms:
                continue
            rows.append(
                {
                    "date": time.strftime("%Y-%m-%d", time.localtime(int(date_ms) / 1000)),
                    "open": it.get("open_price"),
                    "high": it.get("high_price"),
                    "low": it.get("low_price"),
                    "close": it.get("close_price"),
                    "volume": it.get("volume"),   # shares (no lot conversion needed)
                    "amount": it.get("turnover"),
                }
            )
        df = pd.DataFrame(rows)
        df = df[(df["date"] >= start_date) & (df["date"] <= end_date)]
        if df.empty:
            logger.info(
                "HithinkFetcher daily history outside requested range for %s: %s~%s",
                stock_code, start_date, end_date,
            )
        return df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        normalized = df.copy()
        for column in ("open", "high", "low", "close", "volume", "amount"):
            if column in normalized.columns:
                normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
        if "pct_chg" not in normalized.columns:
            normalized["pct_chg"] = normalized["close"].pct_change().fillna(0.0) * 100
        return normalized[["date", "open", "high", "low", "close", "volume", "amount", "pct_chg"]]

    # ------------------------------------------------------------- availability
    def _is_available(self) -> bool:
        return bool(self.api_key)
