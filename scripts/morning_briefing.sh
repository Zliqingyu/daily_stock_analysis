#!/usr/bin/env bash
# VPS 早盘 briefing — 只跑持仓 + AlphaSift，不跑自选
# cron: 周一~周五 北京时间 08:35 (开盘前)
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
LOGDATE=$(date +%Y%m%d)
LOG="logs/morning_briefing_${LOGDATE}.log"
echo "[$(date +%T)] morning briefing start" >> "$LOG"

source .venv/bin/activate

# 加载 .env 所有变量到环境（供子进程使用）
set -a; source .env 2>/dev/null; set +a
export DSA_SKIP_TRADE_PLAN=1  # 晨报不生成挂单清单, 只在晚盘出

# 第一轮: 持仓 + AlphaSift 选股（不跑自选 watchlist）
timeout 3300 python scripts/morning_holdings_alpha.py >> "$LOG" 2>&1
echo "[$(date +%T)] morning holdings+alpha exit $?" >> "$LOG"

# WATCHDOG: 早报结束时验证推送产物, 失败立即飞书告警
TODAY=$(date +%Y%m%d)
REPORT_TODAY=$(ls -t reports/ 2>/dev/null | grep -c "$TODAY" || true)
if [ "$REPORT_TODAY" -eq 0 ]; then
  echo "[$(date +%T)] WATCHDOG: 今日早报报告未生成, 推送失败!" >> "$LOG"
  .venv/bin/python /home/kos/pi-feishu-bridge/feishu_send.py "⚠️ DSA早报推送失败 ($(date +%H:%M)) — 分析已完成但发送环节卡死, 请查看 morning_briefing_${LOGDATE}.log 或等我补推" 2>> "$LOG"
fi

# ETF 双动量轮动信号卡片 (上游 etf_rotation 移植, 失败不阻塞主报)
echo "[$(date +%T)] etf rotation signal start" >> "$LOG"
timeout 600 python scripts/etf_rotation_signal.py >> "$LOG" 2>&1
echo "[$(date +%T)] etf rotation signal exit $?" >> "$LOG"
