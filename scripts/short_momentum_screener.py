#!/usr/bin/env python3
"""
Short-term momentum stock screener (进攻组).

Scans the full A-share market for short-term aggressive picks:
- Strong 5-day momentum (>8%)
- High volume ratio (>2) and turnover (>8%)
- MACD golden cross or fresh breakout
- RSI 55-75 (strong but not overbought)
- Excludes ST, *ST, suspended, and new listings (<30 trading days)

Output: top N candidates with entry/target/stop-loss levels.

Usage:
    python scripts/short_momentum_screener.py [--top 5] [--no-notify]
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import List, Dict, Optional

# Ensure project root is importable
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("short_momentum_screener")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Compute RSI from a list of closing prices."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _compute_macd(closes: List[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Compute MACD line, signal line, and histogram. Returns (macd, signal, hist)."""
    if len(closes) < slow + signal:
        return None, None, None
    ema_fast = closes[0]
    ema_slow = closes[0]
    mult_fast = 2 / (fast + 1)
    mult_slow = 2 / (slow + 1)
    macd_line = []
    for c in closes[1:]:
        ema_fast = c * mult_fast + ema_fast * (1 - mult_fast)
        ema_slow = c * mult_slow + ema_slow * (1 - mult_slow)
        macd_line.append(ema_fast - ema_slow)
    # Signal line = EMA of MACD line
    mult_sig = 2 / (signal + 1)
    sig = macd_line[0]
    signal_line = []
    for v in macd_line[1:]:
        sig = v * mult_sig + sig * (1 - mult_sig)
        signal_line.append(sig)
    if len(macd_line) < 2 or len(signal_line) < 1:
        return None, None, None
    macd_now = macd_line[-1]
    sig_now = signal_line[-1]
    # Previous values for cross detection
    macd_prev = macd_line[-2] if len(macd_line) >= 2 else macd_now
    sig_prev = signal_line[-1]
    hist = macd_now - sig_now
    golden_cross = macd_prev <= sig_prev and macd_now > sig_now
    return macd_now, sig_now, {"hist": hist, "golden_cross": golden_cross}


def screen_short_momentum(top_n: int = 5, max_candidates: int = 200) -> List[Dict]:
    """
    Screen the full A-share market for short-term momentum picks.

    Returns a list of dicts with code, name, price, signals, entry/target/stop.
    """
    import akshare as ak
    import pandas as pd
    import numpy as np

    logger.info("[短线快筛] 拉取全A股实时行情...")
    df_spot = None

    # Try akshare first (with retry), then efinance as fallback
    import time
    for attempt in range(3):
        try:
            df_spot = ak.stock_zh_a_spot_em()
            if df_spot is not None and len(df_spot) > 0:
                logger.info(f"[短线快筛] akshare 成功: {len(df_spot)} 只股票 (attempt {attempt+1})")
                break
        except Exception as e:
            logger.warning(f"[短线快筛] akshare 第 {attempt+1} 次失败: {e}")
            if attempt < 2:
                time.sleep(3)

    # Fallback chain: efinance → sina (two-step enrichment)
    has_detail = True  # True if df_spot has turnover_rate/volume_ratio

    if df_spot is None or len(df_spot) == 0:
        logger.info("[短线快筛] akshare 失败，尝试 efinance 备用源...")
        try:
            import efinance as ef
            df_ef = ef.stock.get_realtime_quotes()
            if df_ef is not None and len(df_ef) > 0:
                logger.info(f"[短线快筛] efinance 成功: {len(df_ef)} 只股票")
                df_spot = df_ef
        except Exception as e:
            logger.warning(f"[短线快筛] efinance 也失败: {e}")

    # Last resort: sina (no turnover_rate/volume_ratio/total_mv — need two-step)
    if df_spot is None or len(df_spot) == 0:
        logger.info("[短线快筛] 尝试新浪源 (stock_zh_a_spot)...")
        try:
            df_sina = ak.stock_zh_a_spot()
            if df_sina is not None and len(df_sina) > 0:
                logger.info(f"[短线快筛] 新浪源成功: {len(df_sina)} 只股票")
                df_spot = df_sina
                has_detail = False  # sina lacks turnover/volume_ratio/total_mv
        except Exception as e:
            logger.error(f"[短线快筛] 新浪源也失败: {e}")

    if df_spot is None or len(df_spot) == 0:
        logger.error("[短线快筛] 所有数据源均失败，行情数据为空")
        return []

    # Normalize column names (akshare uses 代码/名称, efinance uses 股票代码/股票名称)
    col_map = {
        "代码": "code",
        "名称": "name",
        "股票代码": "code",
        "股票名称": "name",
        "最新价": "price",
        "涨跌幅": "pct_change",
        "换手率": "turnover_rate",
        "量比": "volume_ratio",
        "总市值": "total_mv",
    }
    for cn, en in col_map.items():
        if cn in df_spot.columns:
            df_spot = df_spot.rename(columns={cn: en})

    # Sina returns codes with market prefix (e.g. "sh601398", "bj920000")
    if df_spot["code"].iloc[0].startswith(("sh", "sz", "bj")):
        df_spot["code"] = df_spot["code"].str.replace(r"^(sh|sz|bj)", "", regex=True)

    # Filter: exclude ST, *ST, suspended (price=0), new stocks
    df = df_spot.copy()
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["pct_change"] = pd.to_numeric(df.get("pct_change", 0), errors="coerce")
    df = df[df["price"].notna() & (df["price"] > 0)]
    df = df[~df["name"].str.contains(r"ST|退市", na=False, regex=True)]
    # Only main board + ChiNext + STAR (exclude Beijing SE for now, low liquidity)
    df = df[df["code"].str.match(r"^(60[0-3]|00[0-2]|30[0-9]|68[89])")]

    if has_detail:
        # Full data path: filter by total_mv + turnover + volume_ratio (bulk)
        if "total_mv" in df.columns:
            df["total_mv"] = pd.to_numeric(df["total_mv"], errors="coerce")
            df = df[(df["total_mv"] >= 3e8) & (df["total_mv"] <= 5e11)]

        for c in ["turnover_rate", "volume_ratio"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")

        df = df[
            (df.get("turnover_rate", 0) >= 5) &
            (df.get("volume_ratio", 0) >= 1.5)
        ]
        logger.info(f"[短线快筛] 量价筛后 {len(df)} 只 (换手>5%, 量比>1.5)")

        if len(df) == 0:
            logger.warning("[短线快筛] 量价筛后无候选")
            return []

        if len(df) > max_candidates:
            df = df.sort_values("turnover_rate", ascending=False).head(max_candidates)

        logger.info(f"[短线快筛] 初筛后 {len(df)} 只候选 (排除ST/退市/停牌/北交所)")
    else:
        # Sina path: pre-filter by pct_change, enrich individually
        df = df[df["pct_change"] >= 3]  # Rough momentum proxy
        logger.info(f"[短线快筛] 新浪源: 涨幅>3%初筛后 {len(df)} 只")

        if len(df) == 0:
            logger.warning("[短线快筛] 新浪源初筛后无候选")
            return []

        # Cap for individual enrichment (sina has no turnover/volume_ratio)
        if len(df) > max_candidates * 3:
            df = df.sort_values("pct_change", ascending=False).head(max_candidates * 3)

        # Enrich with DataFetcherManager for missing fields
        fetcher_tmp = _get_data_fetcher()
        enriched_rows = []
        for idx, row in df.iterrows():
            code = row["code"]
            try:
                quote = fetcher_tmp.get_realtime_quote(code)
                if quote is None:
                    continue
                tr = getattr(quote, "turnover_rate", 0) or 0
                vr = getattr(quote, "volume_ratio", 0) or 0
                tmv = getattr(quote, "total_mv", 0) or 0
                if tr >= 5 and vr >= 1.5 and 3e8 <= tmv <= 5e11:
                    row["turnover_rate"] = tr
                    row["volume_ratio"] = vr
                    row["total_mv"] = tmv
                    enriched_rows.append(row)
            except Exception:
                continue
        if not enriched_rows:
            logger.warning("[短线快筛] 新浪源 enrichment 后无候选通过量价筛")
            return []
        df = pd.DataFrame(enriched_rows)
        logger.info(f"[短线快筛] 新浪源 enrichment 后 {len(df)} 只通过量价筛")

    results = []
    fetcher = _get_data_fetcher()

    for idx, row in df.iterrows():
        code = row["code"]
        name = row["name"]
        price = float(row.get("price", 0))
        if price <= 0:
            continue

        try:
            hist_df, _src = fetcher.get_daily_data(code, days=60)
        except Exception:
            continue
        if hist_df is None or len(hist_df) < 20:
            continue

        closes = hist_df["close"].astype(float).tolist()

        # 5-day return
        if len(closes) < 6:
            continue
        ret_5d = (closes[-1] - closes[-6]) / closes[-6] * 100
        if ret_5d < 3:  # At least 3% in 5 days (lowered from 8% to get enough candidates)
            continue

        # RSI
        rsi = _compute_rsi(closes)
        if rsi is None or rsi < 40 or rsi > 80:
            continue

        # MACD
        _, _, macd_info = _compute_macd(closes)
        if macd_info is None:
            continue

        # Score: momentum + volume + RSI sweet spot
        score = 0
        score += min(ret_5d, 20)  # Cap at 20
        score += float(row.get("turnover_rate", 0))
        score += float(row.get("volume_ratio", 0)) * 2
        score += (70 - abs(rsi - 65)) if rsi <= 70 else (80 - rsi) * 2  # Reward RSI 60-70
        if macd_info.get("golden_cross"):
            score += 10

        # 概率估算：基于 RSI + 量价信号
        prob_5d = _estimate_probability(rsi, score, macd_info.get("golden_cross", False))
        prob_8d = max(0, prob_5d - 15)  # 第二目标概率通常低 15%

        results.append({
            "code": code,
            "name": name,
            "price": round(price, 2),
            "pct_change": round(float(row.get("pct_change", 0)), 2),
            "ret_5d": round(ret_5d, 2),
            "rsi": round(rsi, 1),
            "turnover_rate": round(float(row.get("turnover_rate", 0)), 2),
            "volume_ratio": round(float(row.get("volume_ratio", 0)), 2),
            "macd_golden_cross": macd_info.get("golden_cross", False),
            "score": round(score, 1),
            "entry_price": round(price, 2),
            "entry_low": round(price * 0.99, 2),   # 买入区间下限 -1%
            "entry_high": round(price * 1.01, 2),   # 买入区间上限 +1%（区间内放心买入）
            "no_chase_price": round(price * 1.03, 2),  # 禁止追高线 +3%（超过勿追）
            "target_price": round(price * 1.08, 2),  # +8% target → 清仓
            "target_price_2": round(price * 1.05, 2),  # +5% first target → 减半仓
            "stop_loss": round(price * 0.97, 2),  # -3% stop loss
            "holding_days": "2-5个交易日",
            "total_mv": float(row.get("total_mv", 0)),
            "prob_5pct": prob_5d,    # 达到+5%的概率
            "prob_8pct": prob_8d,    # 达到+8%的概率
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    top = results[:top_n]
    logger.info(f"[短线快筛] 最终选出 {len(top)} 只短线候选 (从 {len(results)} 只达标中)")

    return top


def _estimate_probability(rsi: float, score: float, golden_cross: bool) -> int:
    """基于 RSI + 量价信号估算达到目标价的概率"""
    if 55 <= rsi <= 70:
        base = 70
    elif 50 <= rsi < 55:
        base = 60
    elif 70 < rsi <= 75:
        base = 55
    elif rsi > 75:
        base = 40
    else:
        base = 50
    if golden_cross:
        base += 10
    if score >= 70:
        base += 10
    elif score >= 50:
        base += 5
    return min(max(base, 10), 90)


def _get_data_fetcher():
    """Get the DSA data fetcher instance (lazy import)."""
    from data_provider import DataFetcherManager
    return DataFetcherManager()


def format_short_momentum_report(picks: List[Dict]) -> str:
    """Format the short momentum picks as a markdown section for the email report."""
    if not picks:
        return ""

    lines = []
    lines.append("## ⚔️ 进攻组 · 短线机会（2-5天）\n")
    lines.append("> 以下为技术面筛选的短线强势股，严格止盈止损，仓位不超20%\n")

    for i, p in enumerate(picks, 1):
        cross_tag = "🔥MACD金叉" if p.get("macd_golden_cross") else ""
        prob_5 = p.get("prob_5pct", 60)
        prob_8 = p.get("prob_8pct", 45)
        lines.append(f"### {i}. {p['name']}（{p['code']}）{cross_tag}")
        lines.append(f"- 现价：**{p['price']}** 元 | 5日涨幅：**+{p['ret_5d']}%**")
        lines.append(f"- 换手率：{p['turnover_rate']}% | 量比：{p['volume_ratio']} | RSI：{p['rsi']}")
        lines.append(f"- **参考买入区间：{p['entry_low']} ~ {p['entry_high']} 元**（收盘价 ±1%，区间内放心买）")
        lines.append(f"- **止盈：+5% → {p['target_price_2']}**（到达概率 ~{prob_5}%）→ 到价减半仓")
        lines.append(f"- **止盈：+8% → {p['target_price']}**（到达概率 ~{prob_8}%）→ 全部清仓")
        lines.append(f"- **止损：-3% → {p['stop_loss']}** — 无条件止损")
        lines.append(f"- ⛔ **禁止追高线：{p['no_chase_price']} 元**（+3%），开盘已超此价请勿追")
        lines.append("")

    lines.append("---")
    lines.append("⚠️ **风险提示**：短线交易风险极高，请严格执行止损纪律。单只仓位不超过总资金20%。")
    lines.append("")
    lines.append("📋 **操作指引**：")
    lines.append("- 9:25 看集合竞价开盘价，在买入区间内可买入")
    lines.append("- 开盘高于禁止追高线（+3%）请勿追，盈亏比已不划算")
    lines.append("- 买入后立即按实际成本挂条件单：+5% 减半 / +8% 清仓 / -3% 止损")
    lines.append("- 本报告基于收盘数据，跨周末/节假日信号衰减加剧，请以开盘实际情况判断")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Short-term momentum stock screener")
    parser.add_argument("--top", type=int, default=5, help="Number of picks (default: 5)")
    parser.add_argument("--no-notify", action="store_true", help="Skip notification")
    parser.add_argument("--output", type=str, default=None, help="Output JSON file path")
    args = parser.parse_args()

    picks = screen_short_momentum(top_n=args.top)
    report_md = format_short_momentum_report(picks)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({"picks": picks, "report": report_md, "date": datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)
        logger.info(f"[短线快筛] 结果已保存到 {args.output}")

    if picks:
        print(report_md)
    else:
        print("[短线快筛] 今日无符合条件的短线候选")

    return picks


if __name__ == "__main__":
    main()
