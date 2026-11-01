#!/usr/bin/env python3
"""早盘持仓+AlphaSift 分析 — 不跑自选 watchlist。

从 portfolio_positions 读取当前持仓，加上最近一次 AlphaSift 候选，
合并后交给 main.py 做深度分析与邮件推送。
"""

import os
import subprocess
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "stock_analysis.db"
ALPHA_PICKS = ROOT / "data" / "alpha_latest_picks.txt"


def load_holdings() -> list[str]:
    """从 portfolio_positions 读取持仓代码（quantity > 0）。"""
    import sqlite3

    if not DB_PATH.exists():
        print(f"[morning] DB not found: {DB_PATH}")
        return []
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM portfolio_positions WHERE quantity > 0"
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception as e:
        print(f"[morning] 读取持仓失败: {e}")
        return []
    finally:
        conn.close()


def load_alpha_picks() -> list[str]:
    """读取最近一次 AlphaSift 候选股。"""
    if not ALPHA_PICKS.exists():
        return []
    line = ALPHA_PICKS.read_text().strip().splitlines()
    if not line:
        return []
    first = line[0].strip()
    if not first or first.startswith("#"):
        return []
    return [s.strip() for s in first.split(",") if s.strip()]


def main() -> int:
    holdings = load_holdings()
    alpha = load_alpha_picks()

    # 合并去重，保持顺序
    seen: set[str] = set()
    stocks: list[str] = []
    for code in holdings + alpha:
        if code not in seen:
            seen.add(code)
            stocks.append(code)

    if not stocks:
        print("[morning] 无持仓无 AlphaSift 候选，跳过")
        return 0

    stock_arg = ",".join(stocks)
    print(f"[morning] 持仓={holdings}  AlphaSift={alpha}  合并={stock_arg}")

    env = os.environ.copy()
    env["STOCK_LIST"] = stock_arg

    # 调用 main.py 做深度分析 + 邮件推送
    result = subprocess.run(
        [sys.executable, str(ROOT / "main.py"),
         "--stocks", stock_arg,
         "--no-market-review",
         "--max-runtime", "3240"],
        cwd=str(ROOT),
        env=env,
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
