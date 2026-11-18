#!/usr/bin/env python3
"""触价邮件提醒 — 盘中轮询实时价, 触达日报买卖位即发邮件 (2026-11-17 v3)

定位 (Nathaniel 定): 盘中提醒只做一件事 —— 报告"日报挂单计划的价位被触达了"。
方向和操作以日报计划为唯一权威, 本脚本不叠加任何自己的分析 (v2 的 MA 破位
卖出层已删除, 它会与日报的加仓建议打架)。

价位 (来自每日日报 extract_levels):
  回落买入位 → 触达 = 可挂单买入 (含股数建议: 5%风险预算/止损距离, 上限现金30%)
  清仓止损位 → 触达 = 计划止损 (持仓则提示按计划止损处理)
  全部止盈位 → 触达 = 计划止盈
防重复: ~/.price_alert_state.json 每日每码每价位只发一次
cron: */2 9-15 * * 1-5 (时段过滤在脚本内: 09:30-11:30 / 13:00-15:00)
用法: python3 scripts/price_alert_email.py [--dry] [--force]"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(HOME / "scripts"))

LOG = HOME / "logs" / f"price_alert_{datetime.now():%Y%m%d}.log"
STATE = os.path.expanduser("~/.price_alert_state.json")
DRY = "--dry" in sys.argv
FORCE = "--force" in sys.argv


def log(msg):
    with open(LOG, "a") as f:
        f.write(f"[{datetime.now():%H:%M:%S}] {msg}\n")


def in_trading_hours(now):
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return 930 <= hm <= 1130 or 1300 <= hm <= 1500


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(state):
    with open(STATE, "w") as f:
        json.dump(state, f, ensure_ascii=False)


def latest_report():
    reps = sorted((HOME / "reports").glob("report_*.md"), key=lambda p: p.stat().st_mtime)
    return reps[-1] if reps else None


def tencent_quotes(codes):
    codes = [c for c in dict.fromkeys(codes) if c]
    out = {}
    if not codes:
        return out
    full = ",".join((f"sh{c}" if c[0] in "56" else f"sz{c}") for c in codes)
    import urllib.request
    req = urllib.request.Request(f"https://qt.gtimg.cn/q={full}", headers={"User-Agent": "Mozilla/5.0"})
    for line in urllib.request.urlopen(req, timeout=8).read().decode("gbk").strip().split(";"):
        if "=" not in line:
            continue
        p = line.split("~")
        if len(p) > 3:
            k = p[0].split("=")[0][-8:]
            sym = k[2:] if k[:2] in ("sh", "sz") else k
            try:
                out[sym] = {"price": float(p[3]), "name": p[1]}
            except ValueError:
                pass
    return out


def portfolio():
    """(持仓{sym:{qty,cost}}, 可用现金) — 直连PortfolioService, 不依赖9996 API"""
    from src.services.portfolio_service import PortfolioService
    snap = PortfolioService().get_portfolio_snapshot(include_realtime=False)
    held, cash = {}, 0.0
    for a in (snap.get("accounts") or []):
        cash += (a.get("cash") or 0)
        for p in (a.get("positions") or []):
            if (p.get("quantity") or 0) > 0:
                held[p["symbol"]] = {"qty": p.get("quantity") or 0, "cost": p.get("avg_cost") or 0}
    return held, cash


def main():
    from next_day_plan import extract_levels

    now = datetime.now()
    today = now.date().isoformat()
    if not in_trading_hours(now) and not FORCE:
        return 0
    report = latest_report()
    if not report:
        log("无日报文件, 跳过")
        return 0
    levels = extract_levels(str(report))
    held, cash = portfolio()

    quote_codes = {l["code"] for l in levels}
    quote_codes |= set(held)
    quotes = tencent_quotes(quote_codes)

    state = load_state()
    day = state.setdefault(today, {})
    if FORCE:
        for k in list(day):
            day[k] = {}
    triggers = []

    for l in levels:
        code = l["code"]
        q = quotes.get(code)
        if not q:
            continue
        px = q["price"]
        st = day.setdefault(code, {})
        nm = f"{q.get('name')}({code})" if q.get("name") else f"{l.get('name')}({code})"
        held_qty = held.get(code, {}).get("qty")

        # 回落买入位: 触达 = 可挂单买入
        if l.get("entry", 0) > 0 and not st.get("entry") and px <= l["entry"]:
            if held_qty:
                qty_txt = f"已持仓 {held_qty:,}股, 加仓数量按你的资金计划执行"
            elif l.get("stop", 0) > 0 and l["entry"] > l["stop"]:
                budget = cash * 0.05
                lots = int(budget / (l["entry"] - l["stop"]) // 100) * 100
                cap = int(cash * 0.3 / l["entry"] // 100) * 100
                lots = min(lots, cap) if cap else lots
                qty_txt = f"风险预算建议 {lots}股" if lots >= 100 else "资金不足一手风险预算, 自行斟酌"
            else:
                qty_txt = "挂单买入"
            triggers.append(
                f"🟢 买入位触达 | {nm} 现价 {px:.2f} ≤ 计划买入位 {l['entry']:.2f}\n"
                f"   → 可挂单买入 ({qty_txt}; 计划止损 {l.get('stop', 0) or '—'} / 止盈 {l.get('tp', 0) or '—'})")
            st["entry"] = True

        # 清仓止损位: 触达 = 计划止损
        if l.get("stop", 0) > 0 and not st.get("stop") and px < l["stop"]:
            q_txt = f"持仓 {held_qty:,}股 — 按计划止损处理" if held_qty else "该信号作废, 勿接"
            triggers.append(
                f"🔴 止损位触达 | {nm} 现价 {px:.2f} < 计划止损位 {l['stop']:.2f}\n"
                f"   → {q_txt}")
            st["stop"] = True

        # 全部止盈位: 触达 = 计划止盈
        if l.get("tp", 0) > 0 and not st.get("tp") and px >= l["tp"]:
            q_txt = f"持仓 {held_qty:,}股 — 按计划止盈处理" if held_qty else ""
            triggers.append(
                f"🎯 止盈位触达 | {nm} 现价 {px:.2f} ≥ 计划止盈位 {l['tp']:.2f}\n"
                f"   → {q_txt}")
            st["tp"] = True

    if DRY:
        if triggers:
            log(f"[DRY] 触发 {len(triggers)} 条(不发送不落状态):\n" + "\n".join(f"- {t}" for t in triggers))
        else:
            log(f"[DRY] 轮询: {len(levels)}价位/{len(held)}持仓, 无触发")
        return 0

    if not triggers:
        log(f"轮询: {len(levels)}价位/{len(held)}持仓, 无触发")
        return 0
    body = "\n".join(f"- {t}" for t in triggers)
    log(f"触发 {len(triggers)} 条:\n" + body)
    save_state(state)
    try:
        from src.config import Config
        from src.notification_sender.email_sender import EmailSender
        ok = EmailSender(Config._load_from_env()).send_to_email(body, subject=f"📈 触价提醒 ({now:%H:%M}) {len(triggers)}条")
        log(f"邮件发送: {'成功' if ok else '失败'}")
    except Exception as e:
        log(f"邮件发送异常: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
