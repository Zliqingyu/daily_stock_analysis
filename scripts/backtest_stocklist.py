# -*- coding: utf-8 -*-
"""STOCK_LIST 三策略历史回测：T日收盘出信号 -> T+1开盘买入 -> 止损-7%/止盈+10%/10日到期。
数据源 akshare，缓存到 data/kline_cache/。
用法: python scripts/backtest_stocklist.py [--start 2021-01-01]
"""
import os
import sys
import time
import argparse

import pandas as pd
import akshare as ak

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "kline_cache")

STOCK_LIST = ["513310", "161116", "003816", "160644", "600021", "003035", "501018"]
NAMES = {
    "513310": "中证500ETF?", "161116": "LOF-黄金?", "003816": "中国广核",
    "160644": "LOF-鹏华?", "600021": "上海电力", "003035": "南网能源", "501018": "LOF-南方?",
}

STOP_PCT = -0.07
TARGET_PCT = 0.10
MAX_HOLD = 10
COST = 0.0015  # 双边成本


def _sina_symbol(code: str) -> str:
    # 5/50/51/56/58 开头 -> 沪；15/16 开头 -> 深
    return ("sh" if code[0] in "5567" or code.startswith(("50", "51", "56", "58")) else "sz") + code


def fetch_one(code: str, start: str) -> pd.DataFrame:
    """按代码类型选数据源（东财被本机 IP 拉黑时自动落到 腾讯/新浪），带本地 CSV 缓存。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache = os.path.join(CACHE_DIR, f"{code}.csv")
    if os.path.exists(cache):
        df = pd.read_csv(cache)
    else:
        df, last_exc = None, None
        is_fund = not code[0] in "036" or code.startswith(("15", "16", "50", "51", "56", "58"))
        sources = []
        if is_fund:
            sources.append(("sina", lambda: ak.fund_etf_hist_sina(symbol=_sina_symbol(code))))
        else:
            sources.append(("tencent", lambda: ak.stock_zh_a_hist_tx(
                symbol=_sina_symbol(code), start_date=start.replace("-", ""), end_date="20991231", fq="qfq")))
            sources.append(("sina", lambda: ak.fund_etf_hist_sina(symbol=_sina_symbol(code))))
        for name, fn in sources:
            for attempt in range(3):
                try:
                    d = fn()
                    if d is not None and len(d):
                        df = d
                        print(f"  ({name} 源, {len(df)} 行)")
                        break
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    time.sleep(2)
            if df is not None and len(df):
                break
        if df is None or not len(df):
            raise RuntimeError(f"{code} 无数据 {last_exc or ''}")
        df.to_csv(cache, index=False)

    df = df.rename(columns={"volume": "vol"})
    # 腾讯源无 volume：它的 amount 实际是成交量(手)
    if "vol" not in df and "amount" in df:
        df = df.rename(columns={"amount": "vol"})
        df["vol"] = df["vol"] * 100  # 手 -> 股
    for col in ("open", "close", "high", "low"):
        if col not in df:
            raise RuntimeError(f"{code} 缺列 {col}: {list(df.columns)}")
    if "vol" not in df:
        df["vol"] = float("nan")
    if "amount" not in df:
        df["amount"] = float("nan")
    df["amount"] = df["amount"].fillna(df["close"] * df["vol"])  # 缺成交额用 收盘*量 近似
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    diff = close.diff()
    up = diff.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-diff.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, 1e-9)
    return 100 - 100 / (1 + rs)


def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    df["ma20"] = c.rolling(20).mean()
    df["ma60"] = c.rolling(60).mean()
    df["vol_ma20"] = df["vol"].rolling(20).mean()
    df["pct"] = c.pct_change()
    df["rsi14"] = rsi(c)
    df["hi60"] = c.rolling(60).max().shift(1)
    df["ret20"] = c.pct_change(20)

    up_trend = (df["ma20"] > df["ma60"]) & (df["ret20"] > 0)
    pullback = c / df["ma20"] - 1
    df["sig_shrink_pullback"] = (up_trend & (c > df["ma20"])
                                 & pullback.between(-0.02, 0.08)
                                 & (df["vol"] / df["vol_ma20"] <= 1.5))

    df["sig_oversold_reversal"] = ((df["pct"].between(-0.08, -0.01))
                                   & (df["rsi14"] < 32)
                                   & (df["amount"] > 8e7))

    df["sig_capital_heat"] = ((df["pct"] >= 0.03)
                              & (df["vol"] >= 1.8 * df["vol_ma20"])
                              & (c >= df["hi60"] * 0.98))
    return df


def run_backtest(df: pd.DataFrame, sig_col: str, stop_pct: float = STOP_PCT,
                 max_hold: int = MAX_HOLD, target_pct: float | None = TARGET_PCT,
                 exit_ma20: bool = False) -> list:
    """逐信号模拟：T 收盘信号 -> T+1 开盘买 -> 止损/止盈/趋势退出/到期。返回每笔收益率列表。"""
    trades = []
    n = len(df)
    busy_until = -1  # 同标的不重叠持仓
    for t in range(60, n - 1):
        if t <= busy_until or not bool(df.at[t, sig_col]):
            continue
        entry = df.at[t + 1, "open"]
        if not entry or entry <= 0:
            continue
        stop_px = entry * (1 + stop_pct)
        tgt_px = entry * (1 + target_pct) if target_pct else None
        exit_px, exit_i, reason = None, None, ""
        for j in range(t + 1, min(t + 1 + max_hold, n)):
            o, lo, hi = df.at[j, "open"], df.at[j, "low"], df.at[j, "high"]
            if lo <= stop_px:  # 跳空低开按开盘价，否则按止损价
                exit_px, exit_i, reason = min(o, stop_px), j, "止损"
                break
            if tgt_px and hi >= tgt_px:
                exit_px, exit_i, reason = max(o, tgt_px), j, "止盈"
                break
            if exit_ma20 and j > t + 1 and "ma20" in df and df.at[j, "close"] < df.at[j, "ma20"]:
                exit_px, exit_i, reason = df.at[j, "close"], j, "破MA20"
                break
            if j == t + max_hold:
                exit_px, exit_i, reason = df.at[j, "close"], j, "到期"
        if exit_px is None:  # 数据尾部未平仓，按最后收盘强平
            exit_px, exit_i, reason = df.at[n - 1, "close"], n - 1, "尾仓"
        ret = exit_px / entry - 1 - COST
        trades.append({"code": df.at[t, "code"], "signal_date": df.at[t, "date"].date(),
                       "entry": round(entry, 3), "exit": round(exit_px, 3),
                       "ret": round(ret * 100, 2), "days": exit_i - t, "reason": reason})
        busy_until = exit_i
    return trades


def summarize(trades: list, label: str) -> dict:
    if not trades:
        return {"策略": label, "信号数": 0}
    r = pd.Series([t["ret"] for t in trades])
    wins = (r > 0).sum()
    return {"策略": label, "信号数": len(trades), "胜率": f"{wins / len(r) * 100:.0f}%",
            "平均收益": f"{r.mean():+.2f}%", "中位": f"{r.median():+.2f}%",
            "最好": f"{r.max():+.1f}%", "最差": f"{r.min():+.1f}%",
            "平均天数": round(pd.Series([t['days'] for t in trades]).mean(), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    args = ap.parse_args()

    all_trades = {"shrink_pullback 缩量回踩": [], "oversold_reversal 超跌反转": [], "capital_heat 资本热度": []}
    for code in STOCK_LIST:
        print(f"抓取 {code} {NAMES.get(code, '')} ...")
        try:
            df = fetch_one(code, args.start)
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ 跳过: {exc}")
            time.sleep(1)
            continue
        df["code"] = code
        df = add_signals(df)
        for key, col in (("shrink_pullback 缩量回踩", "sig_shrink_pullback"),
                         ("oversold_reversal 超跌反转", "sig_oversold_reversal"),
                         ("capital_heat 资本热度", "sig_capital_heat")):
            tr = run_backtest(df, col)
            for x in tr:
                x["name"] = NAMES.get(code, code)
            all_trades[key].extend(tr)
        time.sleep(1)

    print("\n" + "=" * 88)
    print(f"回测区间 {args.start} ~ 今 | 买入=T+1开盘 | 止损-7% 止盈+10% 10日到期 | 成本0.15%")
    print("=" * 88)
    rows = [summarize(v, k) for k, v in all_trades.items()]
    print(pd.DataFrame(rows).to_string(index=False))

    print("\n── 每策略最近 5 笔明细 ──")
    for k, v in all_trades.items():
        print(f"\n[{k}]")
        if v:
            print(pd.DataFrame(v[-5:])[["code", "name", "signal_date", "entry", "exit", "ret", "days", "reason"]].to_string(index=False))
        else:
            print("  无信号")

    # 合并简评
    total = [t for v in all_trades.values() for t in v]
    if total:
        r = pd.Series([t["ret"] for t in total])
        print(f"\n合计 {len(total)} 笔 | 总胜率 {(r > 0).mean() * 100:.0f}% | 单笔均收益 {r.mean():+.2f}% | "
              f"若每笔全仓滚动复利 ≈ {(1 + r / 100).prod() - 1:+.1%}")


if __name__ == "__main__":
    main()
