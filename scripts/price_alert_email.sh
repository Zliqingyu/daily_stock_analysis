#!/usr/bin/env bash
# 触价邮件提醒 — 盘中每2分钟轮询, 触达日报买卖位即发邮件
# cron: */2 9-15 * * 1-5  (交易时段过滤在脚本内)
set -u
cd "$(dirname "$0")/.." || exit 1
source .venv/bin/activate
set -a; source .env 2>/dev/null; set +a
python scripts/price_alert_email.py >> "logs/price_alert_$(date +%Y%m%d).log" 2>&1
