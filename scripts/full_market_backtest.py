# -*- coding: utf-8 -*-
"""全市场三策略回测（GH Actions 上运行）。
数据: akshare 全市场日K -> data/backtest/kline/{code}.parquet (缓存增量)
策略: 缩量回踩 / 超跌反转 / 资本热度 (与 backtest_stocklist.py 同参数)
执行: T日收盘信号 -> T+1开盘买入(涨停开盘放弃) -> 止损-7%/止盈+10%/10日到期, 双边0.15%
产出: docs/backtest/report_YYYYMMDD.md + 控制台摘要
"""
import os
import sys
import time
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

import socket

import numpy as np
import pandas as pd
import akshare as ak

socket.setdefaulttimeout(15)  # 防单请求挂死占满线程

KLINE_DIR = os.path.join("data", "backtest", "kline")
REPORT_DIR = os.path.join("docs", "backtest")
START = "20210101"
STOP_PCT, TARGET_PCT, MAX_HOLD, COST = -0.07, 0.10, 10, 0.0015


def get_universe() -> pd.DataFrame:
    """全市场 A 股列表 + 快照过滤: 剔除 ST/退市标记/低价股。东财被拉黑时用新浪。"""
    try:
        spot = ak.stock_zh_a_spot_em()
        spot = spot[["代码", "名称", "最新价", "成交额"]].rename(
            columns={"代码": "code", "名称": "name", "最新价": "price", "成交额": "amount"})
        src = "东财"
    except Exception:
        spot = ak.stock_zh_a_spot()
        # 新浪列: 代码/名称/最新价/涨跌幅/成交量(股)/成交额(元)
        spot = spot[["代码", "名称", "最新价", "成交额"]].rename(
            columns={"代码": "code", "名称": "name", "最新价": "price", "成交额": "amount"})
        src = "新浪"
    # 新浪源代码带交易所前缀(sz000001/bj920002), 统一剥掉
    spot["code"] = spot["code"].astype(str).str.extract(r"(?:sh|sz|bj)?(\d{6})$", expand=False)
    spot = spot.dropna(subset=["code"])
    # 只留 00主板/60沪主板 (003035类); 剔除创业板30/科创板68/北交所(不可交易权限)
    spot = spot[spot["code"].str[:2].isin(("00", "60"))]
    spot = spot[~spot["name"].astype(str).str.contains("ST|退", na=False)]
    spot["price"] = pd.to_numeric(spot["price"], errors="coerce")
    spot["amount"] = pd.to_numeric(spot["amount"], errors="coerce")
    spot = spot[(spot["price"] >= 2) & (spot["amount"].fillna(0) > 5e7)]  # 流动性底线
    print(f"宇宙({src}): {len(spot)} 只 (剔除ST/低价/低流动性)")
    return spot


def _hithink_key() -> str:
    p = os.path.expandvars(r"%APPDATA%\hithink-finance\credentials.env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if line.startswith("HITHINK_FINANCE_API_KEY"):
                return line.split("=", 1)[1].strip().strip('"')
    return os.getenv("HITHINK_FINANCE_API_KEY", "")


def hithink_hist(code: str, start: str, end: str):
    """同花顺日K (前复权), 返回标准化 df 或 None。"""
    import requests
    key = _hithink_key()
    if not key:
        return None
    ths = code + (".SH" if code[0] in "569" else ".SZ")
    ms = lambda d: int(dt.datetime.strptime(d, "%Y%m%d").timestamp() * 1000)
    r = requests.get("https://fuyao.aicubes.cn/api/a-share/prices/historical",
                     headers={"X-api-key": key},
                     params={"thscode": ths, "interval": "1d", "start": ms(start),
                             "end": ms(end), "adjust": "forward"}, timeout=30)
    d = r.json().get("data") or {}
    items = d.get("item") or []
    if not items:
        return None
    return pd.DataFrame({
        "date": pd.to_datetime([it["date_ms"] for it in items], unit="ms"),
        "open": [it["open_price"] for it in items],
        "close": [it["close_price"] for it in items],
        "high": [it["high_price"] for it in items],
        "low": [it["low_price"] for it in items],
        "vol": [it["volume"] for it in items],
        "amount": [it["turnover"] for it in items],
    })


def fetch_code(code: str, fpath: str) -> str:
    """东财 -> 腾讯 降级, 返回 'code new_rows' 或 'code cached'。"""
    today = dt.date.today().strftime("%Y%m%d")
    time.sleep(0.6)  # 同花顺限流防护
    if os.path.exists(fpath):
        old = pd.read_parquet(fpath)
        last = pd.Timestamp(old["date"].max()).strftime("%Y%m%d")
        if last >= today:
            return f"{code} cached"
        start = (pd.Timestamp(old["date"].max()) + pd.Timedelta(days=1)).strftime("%Y%m%d")
    else:
        start, old = START, None
    df = None
    try:
        df = hithink_hist(code, start, today)          # 主源: 同花顺付费API
        if df is None or not len(df):
            raise RuntimeError("hithink empty")
    except Exception:
        if old is not None:                             # 已有缓存, 无新数据不算失败
            return f"{code} cached"
        try:                                            # 备源: 腾讯
            pre = "sh" if code[0] in "569" else "sz"
            df = ak.stock_zh_a_hist_tx(symbol=pre + code, start_date=start,
                                       end_date=today, adjust="qfq")
            if df is not None and len(df):
                df = df.rename(columns={"amount": "vol"})
                df["vol"] = df["vol"] * 100
                df["amount"] = df["close"] * df["vol"]
            if df is None or not len(df):
                raise RuntimeError("tencent empty")
        except Exception:
            df = None
    if df is None or not len(df):
        return f"{code} empty"
    df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
                            "最低": "low", "成交量": "vol", "成交额": "amount"})
    df["date"] = pd.to_datetime(df["date"])
    df = df[["date", "open", "close", "high", "low", "vol", "amount"]]
    df["code"] = code
    if old is not None and len(old):
        df = pd.concat([old[df.columns], df], ignore_index=True)
    df = df.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    df.to_parquet(fpath, index=False)
    return f"{code} {len(df)}"


def download_all(codes: list) -> None:
    os.makedirs(KLINE_DIR, exist_ok=True)
    done = 0
    with ThreadPoolExecutor(max_workers=1) as ex:
        futs = {ex.submit(fetch_code, c, os.path.join(KLINE_DIR, f"{c}.parquet")): c for c in codes}
        ok = empty = 0
        errs = []
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
                if "empty" in r:
                    empty += 1
                    if len(errs) < 5:
                        errs.append(r)
                else:
                    ok += 1
            except Exception as exc:
                empty += 1
                if len(errs) < 5:
                    errs.append(f"{futs[fut]} EXC {type(exc).__name__}: {str(exc)[:60]}")
            if done % 200 == 0:
                print(f"  进度 {done}/{len(codes)} 成功{ok} 空/败{empty}", flush=True)
        print(f"  样本错误: {errs}", flush=True)
    print(f"数据下载完成: {done} 只")


def load_all() -> pd.DataFrame:
    frames = []
    for f in os.listdir(KLINE_DIR):
        d = pd.read_parquet(os.path.join(KLINE_DIR, f))
        if len(d) >= 120:
            frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values(["code", "date"]).reset_index(drop=True)
    print(f"加载 {df['code'].nunique()} 只 / {len(df)} 行")
    return df


def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("code")
    c = df["close"]
    df["ma20"] = g["close"].transform(lambda s: s.rolling(20).mean())
    df["ma60"] = g["close"].transform(lambda s: s.rolling(60).mean())
    df["vma20"] = g["vol"].transform(lambda s: s.rolling(20).mean())
    df["pct"] = g["close"].transform(lambda s: s.pct_change())
    df["ret20"] = g["close"].transform(lambda s: s.pct_change(20))
    diff = df["close"] - g["close"].transform(lambda s: s.shift(1))
    up = diff.clip(lower=0).groupby(df["code"]).transform(lambda s: s.ewm(alpha=1/14, adjust=False).mean())
    dn = (-diff.clip(upper=0)).groupby(df["code"]).transform(lambda s: s.ewm(alpha=1/14, adjust=False).mean())
    df["rsi14"] = 100 - 100 / (1 + up / dn.replace(0, 1e-9))
    df["hi60"] = g["close"].transform(lambda s: s.rolling(60).max()).groupby(df["code"]).shift(1)
    df["prev_close"] = g["close"].shift(1)
    df["next_open"] = g["open"].shift(-1)

    pullback = c / df["ma20"] - 1
    df["sig_shrink_pullback"] = ((df["ma20"] > df["ma60"]) & (df["ret20"] > 0) & (c > df["ma20"])
                                 & pullback.between(-0.02, 0.08) & (df["vol"] / df["vma20"] <= 1.5))
    df["sig_oversold_reversal"] = (df["pct"].between(-0.08, -0.01) & (df["rsi14"] < 32)
                                   & (df["amount"] > 8e7))
    df["sig_capital_heat"] = ((df["pct"] >= 0.03) & (df["vol"] >= 1.8 * df["vma20"])
                              & (c >= df["hi60"] * 0.98))
    return df


def simulate(df: pd.DataFrame, sig_col: str, stop_pct: float = STOP_PCT,
             max_hold: int = MAX_HOLD, target_pct: float | None = TARGET_PCT,
             exit_ma20: bool = False) -> pd.DataFrame:
    """逐信号模拟。向量化取信号行, 前瞻用 numpy 按组切片。"""
    sig = df[df[sig_col] & df["next_open"].notna()].copy()
    codes = {k: v.reset_index(drop=True) for k, v in df.groupby("code")}
    out = []
    for row in sig.itertuples():
        d = codes.get(row.code)
        i = d.index[d["date"] == row.date]
        if not len(i):
            continue
        i = i[0]
        if i + 1 >= len(d):
            continue
        entry = d.at[i + 1, "open"]
        pc = d.at[i, "close"]
        if entry >= pc * 1.098:      # 次日涨停开盘, 买不进
            continue
        stop_px = entry * (1 + stop_pct)
        tgt_px = entry * (1 + target_pct) if target_pct else None
        exit_px, exit_i, reason = None, None, ""
        for j in range(i + 1, min(i + 1 + max_hold, len(d))):
            o, lo, hi = d.at[j, "open"], d.at[j, "low"], d.at[j, "high"]
            if lo <= stop_px:
                exit_px, exit_i, reason = min(o, stop_px), j, "止损"; break
            if tgt_px and hi >= tgt_px:
                exit_px, exit_i, reason = max(o, tgt_px), j, "止盈"; break
            if exit_ma20 and j > i + 1 and "ma20" in d and d.at[j, "close"] < d.at[j, "ma20"]:
                exit_px, exit_i, reason = d.at[j, "close"], j, "破MA20"; break
            if j == i + max_hold:
                exit_px, exit_i, reason = d.at[j, "close"], j, "到期"
        if exit_px is None:
            exit_px, exit_i, reason = d.at[len(d)-1, "close"], len(d)-1, "尾仓"
        # 止盈当日涨停卖不出/跌停卖不出忽略(尾差), 保留主口径
        out.append({"code": row.code, "date": row.date, "ret": (exit_px / entry - 1 - COST) * 100,
                    "days": exit_i - i, "reason": reason, "year": row.date.year})
    return pd.DataFrame(out)


def summarize(tr: pd.DataFrame, label: str) -> dict:
    if not len(tr):
        return {"策略": label, "笔数": 0}
    r = tr["ret"]
    return {"策略": label, "笔数": len(r), "胜率": f"{(r > 0).mean() * 100:.0f}%",
            "均收益": f"{r.mean():+.2f}%", "中位": f"{r.median():+.2f}%",
            "盈亏比": f"{r[r > 0].mean() / abs(r[r <= 0].mean()):.2f}" if (r <= 0).any() and (r > 0).any() else "—",
            "平均天数": round(tr["days"].mean(), 1)}


def main():
    t0 = time.time()
    uni = get_universe()
    download_all(uni["code"].tolist())
    df = load_all()
    df = add_signals(df)

    report, lines = {}, []
    # 三组对照: A旧退出 / C(TSP杂交: 紧止损+MA20趋势退出) / 热度对照
    configs = [
        ("缩量回踩·C(止损5%+MA20趋势退出)", "sig_shrink_pullback",
         dict(stop_pct=-0.05, max_hold=15, target_pct=None, exit_ma20=True)),
        ("缩量回踩·A(固定-7%/+10%/10日)", "sig_shrink_pullback", dict()),
        ("资本热度·A(对照)", "sig_capital_heat", dict()),
    ]
    for strat, col, kw in configs:
        tr = simulate(df, col, **kw)
        report[strat] = tr
        all_row = summarize(tr, f"{strat} 全期")
        ins = summarize(tr[tr["date"] < "2024-01-01"], f"{strat} 样本内≤23")
        outs = summarize(tr[tr["date"] >= "2024-01-01"], f"{strat} 样本外≥24")
        for x in (all_row, ins, outs):
            lines.append(x)

    res = pd.DataFrame(lines)
    print("\n" + res.to_string(index=False))

    os.makedirs(REPORT_DIR, exist_ok=True)
    today = dt.date.today().isoformat()
    with open(os.path.join(REPORT_DIR, f"report_{today}.md"), "w", encoding="utf-8") as f:
        f.write(f"# 全市场三策略回测 {today}\n\n")
        f.write(f"- 宇宙: 全A剔除ST/低价/低流动性, {START}→今, 数据 akshare(东财→腾讯降级)\n")
        f.write(f"- 执行: T收盘信号→T+1开盘买(涨停放弃)→止损-7%/止盈+10%/10日到期, 双边0.15%\n")
        f.write(f"- 耗时 {int(time.time() - t0)}s\n\n")
        f.write(res.to_markdown(index=False) + "\n\n")
        for strat, tr in report.items():
            f.write(f"\n## {strat} 分年度\n\n")
            if len(tr):
                g = tr.groupby("year")["ret"]
                f.write(pd.DataFrame({"笔数": g.count(), "胜率%": (g.apply(lambda s: (s > 0).mean() * 100)).round(0),
                                      "均收益%": g.mean().round(2)}).to_markdown() + "\n")
    print(f"\n报告: {REPORT_DIR}/report_{today}.md | 总耗时 {int(time.time() - t0)}s")


if __name__ == "__main__":
    main()
