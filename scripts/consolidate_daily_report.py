#!/usr/bin/env python3
"""
Daily report consolidation — merges short momentum picks + AlphaSift summary
into a single daily extras email, avoiding notification fragmentation.

Reads:
  - reports/short_momentum_YYYYMMDD.json (short momentum screener output)
  - data/alpha_latest_picks.txt (AlphaSift picks, if any)
Sends one consolidated notification via DSA NotificationService.

Usage:
    python scripts/consolidate_daily_report.py
"""

import json
import logging
import os
import sys
from datetime import datetime

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logger = logging.getLogger("consolidate_daily")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main() -> int:
    today = datetime.now().strftime("%Y%m%d")
    sections = []

    # 1. Short momentum picks
    sm_file = f"reports/short_momentum_{today}.json"
    if os.path.exists(sm_file):
        try:
            with open(sm_file, encoding="utf-8") as f:
                data = json.load(f)
            report_md = data.get("report", "")
            if report_md:
                sections.append(report_md)
                logger.info(f"[汇总] 短线快筛: {len(data.get('picks', []))} 只候选")
        except Exception as e:
            logger.warning(f"[汇总] 读取短线快筛报告失败: {e}")

    # 2. AlphaSift picks summary (read from picks file)
    picks_file = "data/alpha_latest_picks.txt"
    if os.path.exists(picks_file):
        try:
            with open(picks_file, encoding="utf-8") as f:
                codes = [c.strip() for c in f.read().split(",") if c.strip()]
            if codes:
                sections.append(
                    f"## 🤖 AlphaSift 选股\n\n"
                    f"昨晚全市场扫描选出 {len(codes)} 只候选：{', '.join(codes)}\n\n"
                    f"深度分析报告请查看独立邮件。\n"
                )
                logger.info(f"[汇总] AlphaSift: {len(codes)} 只候选")
        except Exception as e:
            logger.warning(f"[汇总] 读取 AlphaSift picks 失败: {e}")

    if not sections:
        logger.info("[汇总] 无额外报告需合并（仅主线分析已发送）")
        return 0
    # If only AlphaSift picks (no short momentum report today),
    # skip sending — the picks already have their own deep-analysis email.
    has_short_momentum = any("短线" in s or "short" in s.lower() for s in sections)
    if not has_short_momentum:
        logger.info("[汇总] 今日无短线快筛，跳过补充报告（AlphaSift 已独立发送）")
        return 0

    header = f"## 📊 每日汇总 · 补充报告（{datetime.now().strftime('%m-%d')}）\n\n"
    body = header + "\n---\n\n".join(sections)

    # Send via DSA notification
    try:
        sys.path.insert(0, _PROJECT_ROOT)
        from src.notification import NotificationService

        notifier = NotificationService()
        notifier.send(body, route_type="daily_extras")
        logger.info("[汇总] 合并通知已发送")
    except Exception as e:
        logger.exception(f"[汇总] 发送失败: {e}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
