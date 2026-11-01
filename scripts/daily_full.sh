#!/usr/bin/env bash
# VPS 版每晚全量分析 (替代 GH Actions 00-daily-analysis.yml)
# cron: 周日~周四 北京时间 19:00 → 产出次日挂单计划 + 邮件推送
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
LOGDATE=$(date +%Y%m%d)
LOG="logs/daily_full_${LOGDATE}.log"
echo "[$(date +%T)] daily full analysis start" >> "$LOG"

source .venv/bin/activate

# 加载 .env 所有变量到环境（供子进程使用）
set -a; source .env 2>/dev/null; set +a

# 自选股: 优先 watchlist.json, fallback 到 .env STOCK_LIST
STOCKS=""   # 预初始化 (set -u 下未定义即崩, 恢复期 watchlist.json 缺失时曾致每晚静默失败)
if [ -f data/watchlist.json ]; then
  STOCKS=$(python3 -c "import json; print(','.join(json.load(open('data/watchlist.json'))))" 2>/dev/null)
fi
if [ -z "$STOCKS" ]; then
  STOCKS=$(grep '^STOCK_LIST=' .env 2>/dev/null | cut -d= -f2)
fi
# 合并持仓股: 持仓必须每次分析 (与自选去重)
HELD=$(set -a; source .env 2>/dev/null; set +a; python3 -c "
try:
    from src.services.portfolio_service import PortfolioService
    snap = PortfolioService().get_portfolio_snapshot(include_realtime=False)
    codes = [p['symbol'] for a in (snap.get('accounts') or []) for p in (a.get('positions') or []) if (p.get('quantity') or 0) > 0]
    print(','.join(codes))
except Exception:
    pass" 2>/dev/null)
if [ -n "$HELD" ]; then
  STOCKS=$(python3 -c "print(','.join(dict.fromkeys('$STOCKS,$HELD'.split(','))))" 2>/dev/null)
fi
echo "[$(date +%T)] watchlist=$STOCKS" >> "$LOG"

# 第一轮: 大盘复盘
timeout 4500 python main.py --market-review >> "$LOG" 2>&1
echo "[$(date +%T)] market-review exit $?" >> "$LOG"
# 第二轮: 自选股分析
timeout 3300 python main.py --stocks "$STOCKS" --no-market-review >> "$LOG" 2>&1
echo "[$(date +%T)] full exit $?" >> "$LOG"
# 第三轮: AlphaSift 选股
timeout 3300 python scripts/alphasift_screen_and_analyze.py >> "$LOG" 2>&1
echo "[$(date +%T)] alphasift exit $?" >> "$LOG"
