# -*- coding: utf-8 -*-
"""
盘中持仓监控（Intraday Position Monitor）

功能:
    交易时段内按固定间隔轮询 portfolio_positions 中的持仓实时价（新浪源，
    腾讯源备用），触发以下事件时通过邮件 + 飞书 webhook 推送:
      1. 跌破止损线（alert_rules 中 price_cross/below 规则）——每次跌破立即告警
      2. 逼近止损线（距离 <= warn_pct 且尚未跌破）——每标的每天最多提醒一次
      3. 盘中涨跌幅异动（相对昨收超 +/- swing_pct）——每标的每天最多一次

设计:
    - 只在 A 股交易时段工作（9:30-11:30, 13:00-15:00, 周一至周五，不含节假日判断，
      节假日轮询到的是静态收盘价，状态去重保证不会重复轰炸）
    - 首轮轮询只建立基线不告警（避免重启后把历史跌破当新事件轰炸）
    - 双数据源自动切换；单源失败不影响另一源
    - 无本机路径依赖、无 Windows 专属调用，可直接迁移 Linux 服务器（systemd）

用法:
    python scripts/intraday_monitor.py                # 常驻循环
    python scripts/intraday_monitor.py --once         # 单轮（调试用）
    python scripts/intraday_monitor.py --dry-run      # 只打印不发送通知
    python scripts/intraday_monitor.py --interval 120 # 覆盖轮询间隔（秒）

环境变量（读 dsa 仓库根目录 .env）:
    EMAIL_SENDER / EMAIL_PASSWORD / EMAIL_RECEIVERS   邮件通道（必须，用于告警）
    FEISHU_WEBHOOK_URL / FEISHU_WEBHOOK_SECRET        飞书通道（可选，配了就发）
    MONITOR_INTERVAL        轮询间隔秒数（默认 300）
    MONITOR_WARN_PCT        逼近止损告警距离百分比（默认 3.0）
    MONITOR_SWING_PCT       盘中异动告警阈值百分比（默认 5.0）
"""

from __future__ import annotations

import json
import logging
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass  # 环境变量由外部（systemd Environment=）注入

import os

logger = logging.getLogger("intraday_monitor")

DB_PATH = ROOT / "data" / "stock_analysis.db"
CN_TZ = timezone(timedelta(hours=8))  # 中国无夏令时，固定 UTC+8

SESSIONS = [  # A股交易时段（北京时间，含缓冲：9:15 集合竞价 / 15:05 收盘尾盘）
    ((9, 15), (11, 32)),
    ((12, 58), (15, 5)),
]


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def cfg_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


INTERVAL = int(cfg_float("MONITOR_INTERVAL", 300))
WARN_PCT = cfg_float("MONITOR_WARN_PCT", 3.0)
SWING_PCT = cfg_float("MONITOR_SWING_PCT", 5.0)


# --------------------------------------------------------------------------
# 数据层
# --------------------------------------------------------------------------

def load_holdings() -> Dict[str, dict]:
    """读持仓（symbol -> {quantity, avg_cost}）与止损规则（symbol -> stop_price）。"""
    import sqlite3
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        holdings: Dict[str, dict] = {}
        for sym, qty, cost in db.execute(
                "SELECT symbol, quantity, avg_cost FROM portfolio_positions WHERE quantity > 0"):
            holdings[sym] = {"quantity": qty, "avg_cost": cost, "stop": None}
        for sym, params_json in db.execute(
                "SELECT target, parameters FROM alert_rules "
                "WHERE alert_type='price_cross' AND enabled=1"):
            try:
                params = json.loads(params_json)
            except (TypeError, ValueError, KeyError):
                # 兼容：部分版本 parameters 直接是 TEXT 列名
                params = {}
            if params.get("direction") == "below" and sym in holdings:
                holdings[sym]["stop"] = float(params["price"])
        return holdings
    finally:
        db.close()


def load_watch_symbols() -> List[str]:
    """读自选股 + AlphaSift 候选（与持仓合并监控，仅报异动不带止损）。"""
    syms: List[str] = []
    try:
        from dotenv import dotenv_values
        raw = dotenv_values(Path(__file__).resolve().parent.parent / ".env").get("STOCK_LIST") or ""
        syms += [s.strip() for s in raw.split(",") if s.strip()]
    except Exception as exc:
        logger.warning("读取自选股失败(跳过): %s", exc)
    picks = Path(__file__).resolve().parent.parent / "data" / "alpha_latest_picks.txt"
    try:
        if picks.exists():
            first_line = picks.read_text(encoding="utf-8").splitlines()[0]
            if not first_line.startswith("#"):
                syms += [s.strip() for s in first_line.split(",") if s.strip()]
    except Exception as exc:
        logger.warning("读取 AlphaSift picks 失败(跳过): %s", exc)
    return sorted({s.zfill(6) for s in syms})


def sina_quotes(symbols: List[str]) -> Optional[Dict[str, dict]]:
    """新浪实时快照: 最新价 / 昨收 / 涨跌幅。失败返回 None（触发备用源）。"""
    query = ",".join(
        ("sh" if s.startswith(("5", "6", "9")) else "sz") + s for s in symbols)
    try:
        r = requests.get(
            f"https://hq.sinajs.cn/list={query}",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=10)
        r.encoding = "gbk"
        if r.status_code != 200:
            return None
        out: Dict[str, dict] = {}
        for sym, line in zip(symbols, r.text.strip().splitlines()):
            fields = line.split('"')[1].split(",") if '"' in line else []
            if len(fields) > 8:
                out[sym] = {
                    "price": float(fields[3]), "prev_close": float(fields[2]),
                    "name": fields[0], "source": "sina",
                }
        return out or None
    except requests.RequestException as exc:
        logger.warning("sina 源失败: %s", exc)
        return None


def tencent_quotes(symbols: List[str]) -> Optional[Dict[str, dict]]:
    """腾讯实时快照（备用源）。"""
    query = ",".join(
        ("sh" if s.startswith(("5", "6", "9")) else "sz") + s.lower() for s in symbols)
    try:
        r = requests.get(
            f"https://qt.gtimg.cn/q={query}",
            headers={"Referer": "https://gu.qq.com/"},
            timeout=10)
        r.encoding = "gbk"
        if r.status_code != 200:
            return None
        out: Dict[str, dict] = {}
        for sym, line in zip(symbols, r.text.strip().splitlines()):
            fields = line.split("~")
            if len(fields) > 4:
                out[sym] = {
                    "price": float(fields[3]), "prev_close": float(fields[4]),
                    "name": fields[1], "source": "tencent",
                }
        return out or None
    except requests.RequestException as exc:
        logger.warning("tencent 源失败: %s", exc)
        return None


def fetch_quotes(symbols: List[str]) -> Dict[str, dict]:
    return sina_quotes(symbols) or tencent_quotes(symbols) or {}


# --------------------------------------------------------------------------
# 通知层
# --------------------------------------------------------------------------

def notify(subject: str, body: str, dry_run: bool = False) -> None:
    if dry_run:
        print(f"[DRY-RUN] {subject}\n{body}\n")
        return
    sender = os.getenv("EMAIL_SENDER", "")
    password = os.getenv("EMAIL_PASSWORD", "")
    receivers = [r for r in os.getenv("EMAIL_RECEIVERS", "").split(",") if r.strip()]
    if sender and password and receivers:
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = f"{os.getenv('EMAIL_SENDER_NAME', 'DSA')} <{sender}>"
            msg["To"] = ", ".join(receivers)
            with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=20) as smtp:
                smtp.login(sender, password)
                smtp.sendmail(sender, receivers, msg.as_string())
            logger.info("邮件已发送: %s", subject)
        except Exception as exc:
            logger.error("邮件发送失败: %s", exc)

    webhook = os.getenv("FEISHU_WEBHOOK_URL", "")
    if webhook:
        secret = os.getenv("FEISHU_WEBHOOK_SECRET", "")
        payload: dict = {"msg_type": "text", "content": {"text": f"{subject}\n{body}"}}
        if secret:  # 签名模式
            import base64, hashlib, hmac
            ts = str(int(time.time()))
            sign = base64.b64encode(hmac.new(
                f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()).decode()
            payload.update({"timestamp": ts, "sign": sign})
        try:
            requests.post(webhook, json=payload, timeout=10).raise_for_status()
            logger.info("飞书已推送: %s", subject)
        except requests.RequestException as exc:
            logger.error("飞书推送失败: %s", exc)


# --------------------------------------------------------------------------
# 监控逻辑
# --------------------------------------------------------------------------

def in_trading_hours(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now(CN_TZ)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return any(
        start[0] * 60 + start[1] <= minutes <= end[0] * 60 + end[1]
        for start, end in SESSIONS)


class MonitorState:
    """跨轮询状态：去重、每日限频、重启静默。"""

    def __init__(self) -> None:
        self.seeded = False
        self.below_stop: set = set()       # 当前已跌破集合（状态告警，只报状态变化）
        self.warned_today: Dict[str, str] = {}  # symbol -> 事件key（每标的每天每类一次）
        self.day: str = ""

    def _roll_day_if_needed(self) -> None:
        today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
        if today != self.day:
            self.day = today
            self.warned_today.clear()

    def should_fire(self, symbol: str, event: str) -> bool:
        self._roll_day_if_needed()
        key = f"{symbol}:{event}"
        if self.warned_today.get(symbol) == key:
            return False
        self.warned_today[symbol] = key
        return True


def evaluate(state: MonitorState, holdings: Dict[str, dict],
             quotes: Dict[str, dict], dry_run: bool) -> None:
    events: List[str] = []
    now = datetime.now(CN_TZ).strftime("%H:%M:%S")

    for sym, info in holdings.items():
        quote = quotes.get(sym)
        if not quote:
            events.append(f"⚠ {sym} 未取到行情")
            continue
        price, stop, prev = quote["price"], info.get("stop"), quote["prev_close"]
        name = quote["name"]
        pct = (price / prev - 1) * 100 if prev else 0.0
        head = f"{name}({sym}) {price:.3f} [{pct:+.2f}%] @{quote['source']}"

        # 1) 止损线：状态变化才告警（跌破→告警；收复→告知解除）
        if stop:
            dist = (price / stop - 1) * 100
            if price < stop and sym not in state.below_stop:
                state.below_stop.add(sym)
                events.append(f"🔴 {head} | 已跌破止损线 {stop:.3f}（{dist:+.1f}%）——按纪律应清仓")
            elif price >= stop and sym in state.below_stop:
                state.below_stop.discard(sym)
                events.append(f"🟢 {head} | 收复止损线 {stop:.3f}（{dist:+.1f}%）")
            elif price < stop:
                logger.info("%s 仍在止损线下方（已告警过，静默）", sym)
            elif dist <= WARN_PCT and state.should_fire(sym, "approach"):
                events.append(f"🟡 {head} | 距止损线 {stop:.3f} 仅 {dist:.1f}%，做好准备")

        # 2) 盘中异动（相对昨收）
        if info.get("watch"):
            watch_pct = SWING_PCT / 2
            if abs(pct) >= watch_pct and state.should_fire(sym, "swing"):
                tag = "📈 异动上攻" if pct > 0 else "📉 异动回落"
                events.append(f"👀 {head} | {tag}（观察标的阈值 {watch_pct}%）")
        elif abs(pct) >= SWING_PCT and state.should_fire(sym, "swing"):
            events.append(f"⚡ {head} | 盘中异动（阈值 {SWING_PCT}%）")

    if events:
        notify(f"【盘中监控】{datetime.now(CN_TZ).strftime('%m-%d %H:%M')}",
               "\n".join(events), dry_run=dry_run)
    else:
        logger.info("%s 轮询完成：%d 只持仓无事件", now, len(holdings))


def run_once(state: MonitorState, dry_run: bool = False) -> None:
    holdings = load_holdings()
    watch = [s for s in load_watch_symbols() if s not in holdings]
    for sym in watch:
        holdings[sym] = {"quantity": 0, "avg_cost": None, "stop": None, "watch": True}
    if not holdings:
        logger.warning("无持仓且无自选/候选，跳过")
        return
    quotes = fetch_quotes(list(holdings))
    if not quotes:
        logger.error("新浪/腾讯双源均失败，本轮跳过")
        return
    if not state.seeded:
        # 首轮只建基线：把已跌破的记入集合但不轰炸（避免重启误报）
        for sym, info in holdings.items():
            q = quotes.get(sym)
            if q and info.get("stop") and q["price"] < info["stop"]:
                state.below_stop.add(sym)
        state.seeded = True
        logger.info("基线建立：%d 只持仓，其中 %d 只已处于止损线下方（静默）",
                    len(holdings), len(state.below_stop))
        return
    evaluate(state, holdings, quotes, dry_run)


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="A股盘中持仓监控")
    parser.add_argument("--once", action="store_true", help="单轮模式")
    parser.add_argument("--dry-run", action="store_true", help="只打印不发送")
    parser.add_argument("--interval", type=int, default=INTERVAL, help="轮询间隔秒")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(ROOT / "logs" / "intraday_monitor.log",
                                      encoding="utf-8")])

    os.environ.setdefault("NO_PROXY", "*")
    os.environ["NO_PROXY"] = "*"

    logger.info("盘中监控启动: interval=%ss warn=%.1f%% swing=%.1f%%",
                args.interval, WARN_PCT, SWING_PCT)
    state = MonitorState()

    while True:
        try:
            if in_trading_hours():
                run_once(state, dry_run=args.dry_run)
            else:
                now = datetime.now(CN_TZ)
                if (now.hour, now.minute) > (15, 5):
                    logger.info("已收盘，进程退出（明日由计划任务自动拉起）")
                    return 0
                logger.info("非交易时段，等待")
                if args.once:
                    break
                time.sleep(60)
                continue
        except KeyboardInterrupt:
            logger.info("手动停止")
            return 0
        except Exception as exc:
            logger.exception("轮询异常: %s", exc)
        if args.once:
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
