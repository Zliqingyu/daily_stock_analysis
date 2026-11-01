#!/usr/bin/env python
"""买入信号二次验证：同花顺数据 + LLM 挂单建议。

流程:
1. 从本地 DB 读取当日 decision_signals 中 action=买入 的信号
2. 调同花顺 REST 验证行情快照 + 估值
3. LLM 综合给出 挂单建议（买/不买/等 + 入场/止损/股数）
4. 有建议买入的才发邮件「🎯 买入挂单建议」；全观望不打扰
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

HITHINK_BASE = os.getenv("HITHINK_FINANCE_BASE_URL", "https://fuyao.aicubes.cn")
TODAY = datetime.now().strftime("%Y-%m-%d")


def _load_hithink_key() -> str:
    key = os.getenv("HITHINK_FINANCE_API_KEY", "").strip()
    if key:
        return key
    cred = Path(os.environ.get("APPDATA", "")) / "hithink-finance" / "credentials.env"
    if cred.exists():
        for line in cred.read_text(encoding="utf-8").splitlines():
            if line.startswith("HITHINK_FINANCE_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def _no_proxy():
    for k in list(os.environ):
        if "proxy" in k.lower():
            os.environ.pop(k, None)


def thscode(code: str) -> str:
    digits = "".join(c for c in code if c.isdigit())
    if digits.startswith(("6",)):
        return f"{digits}.SH"
    return f"{digits}.SZ"


def fetch_today_buy_signals() -> list[dict]:
    dbs = glob.glob(str(ROOT / "data" / "*.db"))
    if not dbs:
        return []
    con = sqlite3.connect(dbs[0])
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT stock_code, stock_name, score, entry_low, entry_high,
               stop_loss, target_price, reason, risk_summary,
               decision_profile, source_agent
        FROM decision_signals
        WHERE (action LIKE '%buy%' OR action LIKE '%买入%')
          AND date(created_at) = ?
        ORDER BY score DESC
        """,
        (TODAY,),
    ).fetchall()
    out = [dict(r) for r in rows]
    # 标注是否已持仓（买入建议撞上已持仓 = 应转为加仓/持有视角）
    try:
        from src.storage import PortfolioPosition, PortfolioAccount, get_db
        db = get_db()
        session = db.get_session()
        try:
            rows_pos = (
                session.query(PortfolioPosition)
                .join(PortfolioAccount, PortfolioAccount.id == PortfolioPosition.account_id)
                .filter(PortfolioAccount.is_active == True)  # noqa: E712
                .all()
            )
            positions = {}
            for r in rows_pos:
                code = "".join(c for c in r.symbol if c.isdigit())
                if code and (r.quantity or 0) > 0:
                    positions[code] = {
                        "quantity": r.quantity,
                        "avg_cost": r.avg_cost or 0,
                        "stop_loss": 0,
                    }
        finally:
            session.close()
        for s in out:
            code = "".join(c for c in str(s["stock_code"]) if c.isdigit())
            pos = positions.get(code)
            if pos:
                s["held"] = True
                s["held_quantity"] = pos["quantity"]
                s["held_avg_cost"] = pos["avg_cost"]
                s["held_stop_loss"] = pos["stop_loss"]
    except Exception as exc:  # noqa: BLE001
        print(f"持仓标注跳过: {exc}")
    return out


def hithink_fact(key: str, code: str) -> dict:
    """返回 {行情快照, 估值} 精简事实。失败返回空 dict。"""
    out: dict = {}
    tc = thscode(code)
    for label, path in (("行情", "/api/a-share/prices/snapshot"),
                        ("估值", "/api/a-share/valuations/snapshot")):
        try:
            r = requests.get(
                HITHINK_BASE + path,
                headers={"X-api-key": key},
                params={"thscodes": tc},
                timeout=20,
            )
            d = r.json().get("data") or {}
            items = d.get("items") or d.get("item") or []
            if isinstance(items, dict):
                items = [items]
            for it in items:
                if str(it.get("thscode", "")) == tc:
                    out[label] = {k: v for k, v in it.items() if v is not None}
                    break
        except Exception as exc:  # noqa: BLE001
            out[label] = {"error": str(exc)}
    return out


def llm_channels() -> list[dict]:
    """从 env 解析 LLM 渠道（与 src.config 同构的精简版）。"""
    names = [c.strip() for c in os.getenv("LLM_CHANNELS", "primary").split(",") if c.strip()]
    out = []
    for name in names:
        prefix = f"LLM_{name.upper()}_"
        base = os.getenv(prefix + "BASE_URL")
        key = os.getenv(prefix + "API_KEY")
        models = [m for m in (os.getenv(prefix + "MODELS") or "").split(",") if m.strip()]
        if base and key and models and os.getenv(prefix + "ENABLED", "true").lower() == "true":
            out.append({"name": name, "base": base.rstrip("/"), "key": key, "models": models})
    return out


def llm_ask(prompt: str, attempts: int = 2) -> str | None:
    _no_proxy()
    for attempt in range(attempts):
        for ch in llm_channels():
            for model in ch["models"]:
                try:
                    r = requests.post(
                        f"{ch['base']}/chat/completions",
                        headers={"Authorization": f"Bearer {ch['key']}"},
                        json={
                            "model": model,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.3,
                            "max_tokens": 900,
                        },
                        timeout=90,
                    )
                    if r.status_code != 200:
                        print(f"[LLM] {ch['name']}/{model} -> {r.status_code} {r.text[:120]}")
                        continue
                    msg = r.json()["choices"][0]["message"]
                    content = (msg.get("content") or "").strip()
                    if content:
                        return content
                    # reasoning 模型：答案可能在 reasoning_content 里，剥掉思考过程取结论
                    reasoning = (msg.get("reasoning_content") or "").strip()
                    tail = reasoning.split("</think>")[-1].strip()
                    if tail:
                        print(f"[LLM] {ch['name']}/{model} content 为空，改用 reasoning_content")
                        # 优先提取「结论：」所在行到结尾；否则截取尾部 1200 字
                        if "结论" in tail:
                            idx = tail.find("结论")
                            return tail[idx:]
                        return tail[-1200:]
                    print(f"[LLM] {ch['name']}/{model} -> 200 但内容为空")
                except Exception as exc:  # noqa: BLE001
                    print(f"[LLM] {ch['name']}/{model} -> {type(exc).__name__}: {exc}")
        if attempt < attempts - 1:
            import time
            print(f"[LLM] 全渠道第 {attempt + 1} 轮失败，等待 60 秒后重试…")
            time.sleep(60)
    return None


def build_prompt(sig: dict, fact: dict, equity: float, max_pos: float) -> str:
    strategy_hint = ""
    profile = str(sig.get("decision_profile") or "").lower()
    if "pullback" in profile or "shrink" in profile:
        strategy_hint = "\n注意：该信号出自低吸策略（缩量回踩），挂单方式应以回落限价单为主，禁止追价突破。"
    elif "reversal" in profile or "oversold" in profile:
        strategy_hint = "\n注意：该信号出自超跌反转策略，应等企稳信号（缩量止跌/放量反包）再挂单，勿在下跌途中接飞刀。"
    elif "heat" in profile or "momentum" in profile:
        strategy_hint = "\n注意：该信号出自进攻型策略（热度/动量），若现价已超入场区间上限，宁可放弃也不要追高。"
    held_hint = ""
    if sig.get("held"):
        held_hint = (
            f"\n注意：用户当前已持有该标的 {sig.get('held_quantity')} 股（成本 {sig.get('held_avg_cost')}，"
            f"止损 {sig.get('held_stop_loss')}）。建议应从 加仓/持有/减仓 角度出发，且加仓后总仓位不得超上限。"
        )
    return f"""你是交易执行审核员。下面是一只股票的买入信号与同花顺官方数据验证结果，请独立判断该信号是否值得执行挂单。{strategy_hint}{held_hint}

## 买入信号
{json.dumps(sig, ensure_ascii=False, indent=1)}

## 同花顺数据验证（{TODAY}）
{json.dumps(fact, ensure_ascii=False, indent=1)}

## 账户纪律
- 总权益约 {equity:.0f} 元，单票仓位上限 {max_pos:.0f} 元（30%）
- 单笔风险 1.5%，100 股整手，A股 T+1

## 输出要求（严格 Markdown，不超过 15 行）
第一行：**结论：买入挂单 / 暂缓观察 / 放弃**（三选一）
然后依次给出：
- 若买入：入场价区间、止损价、建议股数（按纪律算整手）、有效期建议
- 2-3 条核心理由（引用同花顺数据）
- 1-2 条主要风险
不要复述信号原文，直接给判断。"""


def read_equity() -> tuple[float, float]:
    """从组合数据库读权益与单票上限，缺省 38000/11400。"""
    equity, max_pos = 38000.0, 11400.0
    try:
        from src.services.portfolio_service import PortfolioService
        svc = PortfolioService()
        snap = svc.get_portfolio_snapshot(include_realtime=False)
        equity = snap.get("total_equity", equity)
        max_pos = equity * 0.30
    except Exception:
        pass
    return equity, max_pos


STRAT_LABELS = {
    "shrink_pullback": ("🟢 缩量回踩（低吸）", "回落限价单，只在入场区间内成交，不追价"),
    "oversold_reversal": ("🟡 超跌反转（低吸）", "等企稳再挂回落单，不接飞刀"),
    "capital_heat": ("🔥 资本热度（进攻）", "限价挂在入场区间上限，超上限宁可不买"),
}


def trade_card(sig: dict, equity: float, max_pos: float) -> str:
    """可直接照抄的挂单卡片：类型/区间/止损目标/股数/挂单方式。"""
    profile = str(sig.get("decision_profile") or "").lower()
    label, method = "❓ 未知策略", "参考信号区间挂限价单"
    for k, (lab, m) in STRAT_LABELS.items():
        if k in profile:
            label, method = lab, m
            break
    lo, hi, stop = sig.get("entry_low"), sig.get("entry_high"), sig.get("stop_loss")
    rng = f"{lo} ~ {hi}" if lo and hi else "—"
    tgt = sig.get("target_price") or "—"
    stop_txt = f"{stop}" if stop else "—"
    shares = "—"
    try:
        if lo and stop and float(lo) > float(stop) and equity:
            n = int(equity * 0.015 / (float(lo) - float(stop))) // 100 * 100
            if n and float(lo) * n <= (max_pos or float("inf")):
                shares = f"{n} 股"
            elif n:
                shares = f"{n} 股 → 超30%上限，降到 {int(max_pos // float(lo) // 100) * 100} 股"
    except (TypeError, ValueError):
        pass
    held = f"\n> ⚠️ 已持仓 {sig.get('held_quantity')} 股（成本 {sig.get('held_avg_cost')}）——按加仓/持有角度执行\n" if sig.get("held") else ""
    return (f"> 🏷 **类型：{label}** ｜ 挂单方式：{method}\n"
            f"> 入场区间：**{rng}** ｜ 止损：**{stop_txt}** ｜ 目标：**{tgt}** ｜ 股数参考：**{shares}**\n"
            f"> （股数按 权益1.5%风险/30%上限/整手 计算，LLM 建议若不同以其为准）{held}\n\n")


def market_gate() -> tuple[bool, str]:
    """环境闸门: 沪深300 收盘 > MA20 才允许买入。返回 (放行?, 说明)。拉不到数据则放行并告警。"""
    try:
        import akshare as ak
        hs = ak.stock_zh_index_daily(symbol="sh000300")
        ma20 = hs["close"].rolling(20).mean().iloc[-1]
        close = hs["close"].iloc[-1]
        d = hs["date"].iloc[-1]
        if close > ma20:
            return True, f"沪深300 {close:.0f} > MA20 {ma20:.0f} ({d.date() if hasattr(d,'date') else d}) — 环境允许"
        return False, f"沪深300 {close:.0f} < MA20 {ma20:.0f} — 空仓期, 拦截全部买入信号"
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ 环境数据拉取失败({exc}), 保守放行并提示")
        return True, "⚠️ 环境数据不可用, 本封未过滤"


def main() -> int:
    _no_proxy()
    signals = fetch_today_buy_signals()
    if not signals:
        print(f"[{TODAY}] 今日无买入信号，不发邮件（观望不打扰）。")
        return 0
    gate_ok, gate_msg = market_gate()
    print(f"[环境闸门] {gate_msg}")
    gate_banner = "" if gate_ok else (
        f"> ⛔ **环境闸门：空仓期** — {gate_msg}\n"
        f"> 按回测规则（止损-7%在大盘弱势期连亏概率高），此时段不建议开新仓。\n"
        f"> 以下信号照常展示，**是否执行由你自行判断**。\n\n")
    print(f"[{TODAY}] 发现 {len(signals)} 条买入信号，开始同花顺验证…")

    key = _load_hithink_key()
    equity, max_pos = read_equity()
    blocks = []
    for sig in signals:
        fact = hithink_fact(key, sig["stock_code"]) if key else {"error": "未配置 HITHINK_FINANCE_API_KEY"}
        verdict = llm_ask(build_prompt(sig, fact, equity, max_pos)) or "（LLM 全渠道失败，无建议）"
        blocks.append(f"## {sig['stock_name']}({sig['stock_code']}) 信号评分 {sig['score']}\n\n{trade_card(sig, equity, max_pos)}\n{verdict}\n")

    content = (
        f"# 🎯 买入挂单建议 {TODAY}{' 【空仓期】' if not gate_ok else ''}\n\n"
        f"> 由同花顺数据二次验证 + LLM 独立审核生成，仅含买入信号。非投资建议。\n\n"
        + gate_banner
        + "\n---\n\n".join(blocks)
    )

    try:
        from src.notification_sender.email_sender import EmailSender
        ok = EmailSender().send_to_email(content, subject=f"🎯 买入挂单建议 {TODAY}")
        print("邮件发送:", "成功" if ok else "失败")
    except Exception as exc:  # noqa: BLE001
        print(f"邮件发送异常: {exc}")
        print(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
