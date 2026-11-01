#!/usr/bin/env python3
"""次日交易计划生成器 — 基于 AI Wave ATS 规范
用法: python3 scripts/next_day_plan.py [报告路径]
产出: ① 次日开盘区间预测 ② 挂单成交性校验 ③ 条件单/取消条件
接入: daily_full.sh (盘后) 与 morning_briefing.sh (盘前) 末尾追加
"""
import json, re, sys, os, urllib.request
from pathlib import Path
from datetime import date, timedelta

BASE = "http://127.0.0.1:9996/api/v1"
WATCH_FILE = Path(__file__).resolve().parent.parent / "data" / "watchlist.json"
RST, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"

# AI Wave 参数
OPEN_BAND_FACTOR = 0.6    # 次日开盘预测带宽 = 今日振幅 × 0.6
FILL_NEAR_PCT = 1.5       # 限价距预测开盘 ±1.5% 内 → 易成交
FILL_FAR_PCT = 3.0        # 超过 3% → 大概率不成交, 需调整
MIN_SPACING_PCT = 3.0     # 买入位-止损最小间距


def api(path):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=20) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


def fetch_quotes(symbols):
    """腾讯实时: {sym: {price, prev_close, open, high, low, amp}}"""
    if not symbols:
        return {}
    codes = [s if s.startswith(("sh", "sz")) else (f"sh{s}" if s[0] in "56" else f"sz{s}") for s in symbols]
    out = {}
    try:
        req = urllib.request.Request(f"https://qt.gtimg.cn/q={','.join(codes)}",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            for line in r.read().decode("gbk").strip().split(";"):
                if "=" not in line:
                    continue
                p = line.split("~")
                if len(p) > 38:
                    k = p[0].split("=")[0][-8:]
                    sym = k[2:] if k[:2] in ("sh", "sz") else k
                    try:
                        price, prev = float(p[3]), float(p[4])
                        high, low = float(p[33]), float(p[34])
                        amp = (high - low) / prev * 100 if prev else 0
                        out[sym] = {"price": price, "prev_close": prev, "name": p[1],
                                    "open": float(p[5]), "high": high, "low": low, "amp": amp}
                    except (ValueError, ZeroDivisionError):
                        pass
    except Exception:
        pass
    return out


def classify_fill(entry, predicted_open_mid):
    """成交性评级 (AI Wave: 评分不是第一步, 可执行性才是)"""
    if entry <= 0 or predicted_open_mid <= 0:
        return "?", "无数据"
    gap = abs(entry - predicted_open_mid) / predicted_open_mid * 100
    if gap <= FILL_NEAR_PCT:
        return "✓易成交", f"限价距预测开盘仅 {gap:.1f}%"
    if gap <= FILL_FAR_PCT:
        return "◐可能成交", f"距预测开盘 {gap:.1f}%, 需回落到位"
    return "✗难成交", f"距预测开盘 {gap:.1f}%, 挂了大概率白挂"


def extract_levels(report_path):
    """从报告摘要提取 (name, code, action, score, entry, stop, tp)"""
    if not report_path or not Path(report_path).exists():
        return []
    text = Path(report_path).read_text(errors="ignore")
    out = []
    tp_re = re.compile(re.escape("__NAME__") + "")
    for m in re.finditer(
        r"([\u4e00-\u9fa5A-Za-z]{2,14})\((\d{6})\)\s*\*{0,2}\s*[:：]?\s*\*{0,2}\s*(买入|持有|观望|回避|减仓)"
        r"\s*\|\s*评分\s*(\d+)\s*\|", text):
        name, code, action, score = m.group(1), m.group(2), m.group(3), int(m.group(4))
        seg = text[m.start():m.start() + 320]

        def grab(label, seg=seg):
            mm = re.search(label + r"[：:]?\s*([\d.]+)元", seg)
            return float(mm.group(1)) if mm else 0.0

        entry = grab("回落买入位") or grab("理想建仓位") or grab("买入区间") and 0 or 0
        stop = grab("清仓止损位")
        tp = grab("全部止盈位") or grab("目标位")
        out.append({"name": name, "code": code, "action": action,
                    "score": score, "entry": entry, "stop": stop, "tp": tp})
    return out


def _ma_status(sym):
    """9条均线规则的持仓体检: 返回 (状态标签, 明日条件单建议文本) 或 None"""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from data_provider.tencent_fetcher import TencentFetcher
        f = TencentFetcher()
        code = sym if sym.startswith(("sh", "sz")) else (f"sh{sym}" if sym[0] in "56" else f"sz{sym}")
        df = f._fetch_range(code, (date.today() - timedelta(days=200)).isoformat(),
                           date.today().isoformat(), stock_code=code)
        c = [float(x) for x in df["close"]]
        if len(c) < 65:
            return None
        ma = lambda w: sum(c[-w:]) / w
        ma5, ma10, ma20, ma60 = ma(5), ma(10), ma(20), ma(60)
        px = c[-1]
        above5, above10, above20, above60 = px > ma5, px > ma10, px > ma20, px > ma60
        if above5:
            return ("正常", None, dict(ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60, px=px))
        if above10:
            return ("破5日线", f"触发 R3: 条件卖单 触发{ma10:.2f}下方 → 减半仓 (破10清仓兜底)", dict(ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60, px=px))
        if above60:
            return ("破10日线", f"触发 R4: 明日开盘集合竞价 清仓离场", dict(ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60, px=px))
        return ("60日线下", "R6: 躺60日线下, 不加仓, 反弹至MA20附近减仓", dict(ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60, px=px))
    except Exception:
        return None

def build_plan(report_path=""):
    plan = []
    snap = api("/portfolio/snapshot?include_realtime=false")
    held = {}
    cash = 0.0
    if "error" not in (snap or {}):
        cash = snap.get("total_cash", 0)
        for a in snap.get("accounts") or []:
            for p in a.get("positions") or []:
                if (p.get("quantity") or 0) > 0:
                    held[p["symbol"]] = p

    all_syms = sorted(set(held) | set(json.load(open(WATCH_FILE)) if WATCH_FILE.exists() else []))
    quotes = fetch_quotes(all_syms)
    levels = extract_levels(report_path)
    lvl_by_code = {l["code"]: l for l in levels}

    plan.append("# 🌅 次日交易计划\n")

    # --- 持仓管理: 保护 + 加仓机会 ---
    if held:
        plan.append("**持仓管理** (预测开盘 = 收盘 ± 今日振幅×0.6)\n")
        for sym, p in sorted(held.items()):
            q = quotes.get(sym)
            if not q:
                continue
            band = q["price"] * q["amp"] / 100 * OPEN_BAND_FACTOR
            lo, hi = q["price"] - band, q["price"] + band
            pnl = (q["price"] - p["avg_cost"]) / p["avg_cost"] * 100
            disp = f"{q.get('name','')}({sym})" if q.get("name") else sym
            plan.append(
                f"- **{disp}** {p['quantity']:.0f}股 成本{p['avg_cost']:.2f} 现价{q['price']:.2f} ({pnl:+.1f}%)\n"
                f"  预测次日开盘区间 {lo:.2f}~{hi:.2f}"
                f"{'  ⚠️ 今日振幅大, 开盘可能跳空' if q['amp'] > 4 else ''}\n")
            # 均线持仓体检 (9条规则): 触发时给明日条件单指令
            st = _ma_status(sym)
            if st:
                label, advice, m = st
                if advice:
                    plan.append(f"  🔔 均线体检: {label} (MA5 {m['ma5']:.2f}/MA10 {m['ma10']:.2f}/MA20 {m['ma20']:.2f}) → {advice}\n")
                else:
                    plan.append(f"  ✅ 均线体检: 站稳MA5 ({m['ma5']:.2f}), 持有不动\n")
    if not held:
        plan.append("（无持仓）\n")

    # --- 挂单成交性校验 ---
    plan.append("\n**挂单成交性校验**\n")
    any_order = False
    for sym, q in sorted(quotes.items()):
        lvl = lvl_by_code.get(sym)
        if not lvl or lvl["entry"] <= 0 or lvl["stop"] <= 0:
            continue
        mid = (q["price"] + q["prev_close"]) / 2  # 预测开盘中枢
        rating, why = classify_fill(lvl["entry"], mid)
        spacing = (lvl["entry"] - lvl["stop"]) / lvl["entry"] * 100
        rr = (lvl["tp"] - lvl["entry"]) / (lvl["entry"] - lvl["stop"]) if lvl["tp"] > lvl["entry"] > lvl["stop"] else 0
        ok_spacing = spacing >= MIN_SPACING_PCT
        ok_rr = rr == 0 or rr >= 1.3
        verdict = "可挂" if (rating.startswith("✓") and ok_spacing and ok_rr) else "不挂"
        any_order = True
        notes = []
        if not ok_spacing:
            notes.append(f"间距{spacing:.1f}%<3%")
        if not ok_rr:
            notes.append(f"RR{rr:.1f}<1.3")
        plan.append(
            f"- {sym} {lvl['name']}：买入位 {lvl['entry']:.2f} / 止损 {lvl['stop']:.2f} / 止盈 {lvl['tp']:.2f}\n"
            f"  成交性 {rating}（{why}）· 间距 {spacing:.1f}% · RR {rr:.1f} → **{verdict}**"
            + (f"（{ '、'.join(notes) }）" if notes else "") + "\n"
            + (f"  取消条件：跳空高开超 {FILL_FAR_PCT:.0f}% 不追；直接击穿 {lvl['stop']:.2f} 不接\n" if verdict == "可挂" else ""))
    if not any_order:
        plan.append("（今日无带完整价位的信号）\n")

    # --- 现金与仓位提示 (AI Wave 仓位模型) ---
    if cash > 0:
        plan.append(f"\n**弹药**: 可用现金 {cash:,.0f}，单笔风险预算 ≤{cash*0.05:,.0f}，"
                    f"留 20%~50% 现金是常态。\n")
    return "\n".join(plan)


def main():
    report = sys.argv[1] if len(sys.argv) > 1 else ""
    print(build_plan(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
