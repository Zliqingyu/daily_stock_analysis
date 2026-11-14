#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF 双动量轮动信号 — 每日晨报附加卡片。

上游引擎: src/core/etf_rotation.py (ZhuLinsen/daily_stock_analysis)
规则: 周度调仓, 60日动量排名, 只持第1名且动量须为正, 否则回货币ETF;
      现任持仓有 2% 缓冲带防反复横跳; 信号日次日收盘成交。

数据: 走 DataFetcherManager 全链 (efinance→akshare→hithink→pytdx...)
推送: pi-feishu-bridge/feishu_send.py (与晨报 watchdog 同通道)
cron: 08:37 由 scripts/morning_briefing.sh 兜底调用 (失败不阻塞主报)
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="[etf-rotation] %(levelname)s %(message)s")
log = logging.getLogger("etf-rotation")

# ---------------------------------------------------------------- 配置
RISK_POOL = {
    "513310": "纳指ETF",
    "160644": "标普消费LOF",
    "510300": "沪深300",
    "512890": "红利低波",
    "518880": "黄金ETF",
}
SAFE_ASSET = "511990"  # 银华日利 货币ETF
SAFE_NAME = "货币ETF"
LOOKBACK_DAYS = 400    # 日历天 ≈ 270 交易日, 够 60 日动量 + 一段回测
PARAMS_LOOKBACK = 60
PARAMS_TOP_N = 1
PARAMS_BUFFER = 2.0

FEISHU_SENDER = Path.home() / "pi-feishu-bridge" / "feishu_send.py"


def fetch_closes(manager) -> dict[str, "pd.Series"]:
    import pandas as pd

    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    series: dict[str, pd.Series] = {}
    for code in list(RISK_POOL) + [SAFE_ASSET]:
        try:
            result = manager.get_daily_data(code, start, end)
            df = result[0] if isinstance(result, tuple) else result
            if df is not None and len(df) >= PARAMS_LOOKBACK + 5:
                s = df.set_index(pd.to_datetime(df["date"]))["close"].astype(float)
                series[code] = s[~s.index.duplicated(keep="last")]
                log.info("%s: %d 根K线", code, len(s))
            else:
                log.warning("%s: 数据不足 (%s根)", code, 0 if df is None else len(df))
        except Exception as exc:  # 单标的失败不拖垮整卡
            log.warning("%s 获取失败: %s", code, exc)
    return series


def main() -> int:
    import pandas as pd
    from src.core.etf_rotation import (
        RotationParams,
        is_rebalance_day,
        latest_ranking,
        prepare_closes,
        run_backtest,
    )
    from data_provider.base import DataFetcherManager

    manager = DataFetcherManager()
    series = fetch_closes(manager)
    closes = prepare_closes(pd.DataFrame(series))
    risk = [c for c in RISK_POOL if c in closes.columns]
    if len(risk) < 2:
        print(f"[etf-rotation] 风险资产数据不足({len(risk)}只), 跳过")
        return 1

    params = RotationParams(
        lookback_days=PARAMS_LOOKBACK,
        rebalance="weekly",
        top_n=PARAMS_TOP_N,
        switch_buffer_pct=PARAMS_BUFFER,
    )
    safe = SAFE_ASSET if SAFE_ASSET in closes.columns else None

    asof, ranking = latest_ranking(closes, risk, params)
    result = run_backtest(closes, risk, safe, params)
    model_now = result.final_holdings[0] if result.final_holdings else (safe or "CASH")

    # 下一交易日是否为调仓执行日 (每周最后交易日发信号, 次日成交)
    nxt = closes.index.searchsorted(asof) + 1
    next_session = closes.index[nxt] if nxt < len(closes.index) else asof
    signal_day = is_rebalance_day(asof, next_session, "weekly")

    # 模型"应当"持有谁: 排名第一且动量>0, 否则 safe/cash
    top_code = ranking.index[0] if len(ranking) and pd.notna(ranking.iloc[0]) else None
    target = top_code if (top_code and ranking.iloc[0] > 0) else (safe or "CASH")
    switching = target != model_now

    lines = [f"🔄 ETF轮动信号 ({asof.date()})"]
    rank_txt = " | ".join(
        f"{RISK_POOL.get(c, c)} {('%+.1f%%' % (v * 100)) if pd.notna(v) and v == v else 'N/A'}"
        for c, v in ranking.items()
    )
    lines.append(f"动量排名({PARAMS_LOOKBACK}日): {rank_txt}")
    held_weeks = 0
    if result.trades:
        last_exec = result.trades[-1].exec_date
        held_weeks = max(0, int((asof - last_exec).days // 7))
    hold_name = RISK_POOL.get(model_now, SAFE_NAME if model_now == SAFE_ASSET else model_now)
    lines.append(f"模型持仓: {hold_name}" + (f" (已持{held_weeks}周)" if held_weeks else ""))
    tgt_name = RISK_POOL.get(target, SAFE_NAME if target in (SAFE_ASSET, "CASH") else target)
    if target == "CASH" or (safe and target == safe):
        lines.append(f"信号: 动量转负 → 回避风险资产, 建议 {tgt_name}")
    elif switching:
        lines.append(f"信号: 切换 {hold_name} → {tgt_name}" + (" (周五收盘执行)" if signal_day else " (待本周五确认)"))
    else:
        lines.append(f"信号: 继续持有 {tgt_name}, 无调仓")
    if len(result.equity) > 60:
        ret = result.equity.iloc[-1] - 1
        bench = result.benchmark_equity.iloc[-1] - 1
        lines.append(
            f"近{LOOKBACK_DAYS}天回测: 轮动{ret:+.1%} vs 等权基准{bench:+.1%} ({len(result.trades)}笔)"
        )
    lines.append("⚠️ 模型信号仅供参考, 非投资建议")

    msg = "\n".join(lines)
    print(msg)
    if FEISHU_SENDER.exists():
        import subprocess

        try:
            subprocess.run(
                [sys.executable, str(FEISHU_SENDER), msg],
                timeout=30, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            log.error("飞书推送失败: %s", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
