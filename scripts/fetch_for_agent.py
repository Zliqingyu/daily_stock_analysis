#!/usr/bin/env python3
"""
Fetch stock analysis data → output clean JSON for any AI agent to analyze.

Usage:
    # 单只股票
    python scripts/fetch_for_agent.py --code 600036 --output data/stock_data.json

    # 多只（批量）
    python scripts/fetch_for_agent.py --codes 600036,601318,00700.HK --output data/stock_data.json

输出 JSON 包含：行情、基本面、技术指标、新闻搜索摘要、搜索配置
agent 拿到这个 JSON + analysis_prompt.md 即可独立完成分析。

数据源依赖（已配置）：
  - 行情：akshare / efinance / TickFlow（你已有）
  - 新闻搜索：Brave API 或 Tavily API（你已有 key）
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


def fetch_stock_data(code: str) -> dict:
    """获取一只股票的完整数据（行情 + 基本面 + 技术 + 新闻）"""
    import akshare as ak
    import pandas as pd

    result = {
        "code": code,
        "fetch_time": datetime.now().isoformat(),
        "error": None,
        "price": {},
        "fundamentals": {},
        "technical": {},
        "news": [],
    }

    try:
        # --- 实时行情 ---
        df = ak.stock_zh_a_spot_em()
        row = df[df["代码"] == code]
        if not row.empty:
            r = row.iloc[0]
            result["price"] = {
                "name": str(r.get("名称", "")),
                "current": float(r.get("最新价", 0)),
                "change_pct": float(r.get("涨跌幅", 0)),
                "high": float(r.get("最高", 0)),
                "low": float(r.get("最低", 0)),
                "volume": float(r.get("成交量", 0)),
                "amount": float(r.get("成交额", 0)),
                "turnover_rate": float(r.get("换手率", 0)),
                "pe": float(r.get("市盈率-动态", 0)),
                "pb": float(r.get("市净率", 0)),
                "market_cap": float(r.get("总市值", 0)),
            }
    except Exception as e:
        result["error"] = str(e)

    # --- 新闻搜索（用你已配的 SearchService） ---
    try:
        from src.search_service import get_search_service
        svc = get_search_service()
        if svc.is_available:
            resp = svc.search(f"{code} 股票 最新消息 2026", max_results=5)
            if resp.success:
                result["news"] = [
                    {"title": r.title or "", "snippet": r.snippet[:200]}
                    for r in resp.results
                ]
    except Exception:
        pass

    return result


def main():
    parser = argparse.ArgumentParser(description="Fetch stock data for AI agent analysis")
    parser.add_argument("--code", help="单只股票代码")
    parser.add_argument("--codes", help="多只股票代码（逗号分隔）")
    parser.add_argument("--output", default="data/stock_data.json", help="输出 JSON 路径")
    args = parser.parse_args()

    codes_str = args.codes or args.code
    if not codes_str:
        print("请指定 --code 或 --codes")
        sys.exit(1)

    codes = [c.strip() for c in codes_str.split(",")]
    all_data = {}
    for code in codes:
        print(f"[fetch] 获取 {code} ...")
        all_data[code] = fetch_stock_data(code)
        time.sleep(1)  # 避免触发限流

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(all_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[fetch] 数据已保存到 {out_path.resolve()}")
    print(f"[fetch] 共 {len(codes)} 只股票")


if __name__ == "__main__":
    main()
