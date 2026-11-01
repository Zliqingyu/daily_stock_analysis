#!/usr/bin/env python3
"""AI Wave ATS 策略回测 — 同花顺6个月日线
按 docs/AI_Wave_ATS_Core_Algorithm_v1.0.txt 的硬规则实现:
  Regime 状态机 / Setup(B类回踩+A类突破) / 评分75出手 / RR>=1.3
  仓位: 单笔风险1.5%权益, 上限30%; max_positions=2; 5日冷却
  出场: 硬止损-5% / 结构破位(MA20) / TP1+7% / TP2+12% / 时间止损5日
  成本: 佣金万2.5双边 + 印花税0.05%卖出 + 滑点0.1%; T+1
用法: .venv/bin/python scripts/backtest_aiwave.py
"""
import json, sys, os
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from data_provider.hithink_fetcher import HithinkFetcher

BASE = "http://127.0.0.1:9996/api/v1"
INIT_CAPITAL = 45000.0
MAX_POSITIONS = 3
SCORE_THRESHOLD = {1: 70, 2: 70, 3: 70, 4: 70, 5: 999}  # Regime→出手门槛
RISK_PCT, CAP_PCT = 0.015, 0.30
STOP_PCT, TP1_PCT, TP2_PCT, TIME_STOP = 0.05, 0.07, 0.12, 5
COOLDOWN_DAYS = 5
COMMISSION, STAMP, SLIP = 0.00025, 0.0005, 0.001


def load_symbols():
    syms = []
    try:
        import urllib.request
        with urllib.request.urlopen(f"{BASE}/portfolio/snapshot?include_realtime=false", timeout=20) as r:
            snap = json.load(r)
        for a in snap.get("accounts") or []:
            for p in a.get("positions") or []:
                if (p.get("quantity") or 0) > 0:
                    syms.append(p["symbol"])
    except Exception as e:
        print(f"[warn] 持仓读取失败: {e}")
    try:
        wl = json.load(open(WATCH_PATH)) if (WATCH_PATH := Path(__file__).resolve().parent.parent / "data" / "watchlist.json").exists() else []
        syms += [w for w in wl if w not in syms]
    except Exception:
        pass
    return syms


def fetch_bars(fetchers, sym, start, end):
    """多源兜底: 同花顺(股票) → 东财efinance(ETF/LOF兜底) → 腾讯"""
    for fetcher in fetchers:
        try:
            df = fetcher._normalize_data(fetcher._fetch_raw_data(sym, start, end), sym)
            if df is not None and not df.empty:
                return df
        except Exception:
            continue
    print(f"[warn] {sym} 全部数据源失败")
    return None


def regime_of(mkt_df, i):
    """R1强势/R2健康/R3震荡/R4弱势/R5极端 — 用组合等权指数近似大盘"""
    if i < 25:
        return 2
    c = mkt_df["close"]
    ma20 = c.rolling(20).mean().iloc[i]
    ret20 = c.iloc[i] / c.iloc[i - 20] - 1
    ret5 = c.iloc[i] / c.iloc[i - 5] - 1
    above = c.iloc[i] > ma20
    if ret5 < -0.05:
        return 5
    if above and ret20 > 0.03:
        return 1
    if above:
        return 2
    if ret20 > -0.03:
        return 3
    return 4


def score_setup(bars, i, regime):
    """AI Wave §12 评分的规则化实现, 返回 (分数, entry, stop, tp1, tp2, setup名)"""
    if i < 25:
        return None
    c, v = bars["close"], bars["volume"]
    o, h, l = bars["open"], bars["high"], bars["low"]
    ma5, ma10, ma20 = (c.rolling(w).mean().iloc[i] for w in (5, 10, 20))
    px = c.iloc[i]
    v_ma5 = v.rolling(5).mean().iloc[i]
    hi20, hi10 = h.iloc[i - 20:i].max(), h.iloc[i - 10:i].max()
    setup, s = "", 0.0

    # A类: 平台放量突破
    if px > hi20 and v.iloc[i] > v_ma5 * 1.5 and o.iloc[i] <= hi20:
        setup, s = "A突破", s + 30
    # B类: 强势后首次缩量回踩 MA10/MA20 不破
    elif (c.iloc[i - 10:i] > ma10).mean() > 0.7 and l.iloc[i] <= ma10 < c.iloc[i - 5] \
            and px > ma20 and v.iloc[i] < v_ma5 * 0.8:
        setup, s = "B回踩", s + 28
    else:
        cond = 0
        if px > ma20:
            cond += 1
        if ma10 > ma20:
            cond += 1
        if px > ma5:
            cond += 1
        if cond == 3 and v.iloc[i] > v_ma5:
            setup, s = "趋势", s + 18
        else:
            return None
    # 结构分
    s += 12 if px > ma20 else -15
    s += 8 if ma20 > c.rolling(20).mean().iloc[i - 5] else -5  # MA20 向上
    # 买点位置: 距 MA5 乖离小加分, 远离扣分
    dev = (px - ma5) / ma5
    s += 8 if abs(dev) < 0.03 else (-6 if dev > 0.08 else 0)
    # 环境分
    s += {1: 15, 2: 12, 3: 5, 4: 0, 5: -50}[regime]
    # 止损: 结构位 = min(MA20, 近5日低) 与 -5% 取宽者
    stop = min(ma20 * 0.995, l.iloc[i - 5:i].min())
    stop = max(stop, px * (1 - STOP_PCT - 0.02))
    risk = px - stop
    if risk <= 0 or risk / px < 0.015:
        return None
    # RR: 用前高做目标
    target = max(hi10, px * (1 + TP1_PCT))
    rr = (target - px) / risk
    s += 15 if rr >= 1.3 else 0
    s += 8 if rr >= 2.0 else 0
    return {"score": s, "entry": px, "stop": round(stop, 3),
            "tp1": round(px * (1 + TP1_PCT), 3), "tp2": round(px * (1 + TP2_PCT), 3),
            "setup": setup, "rr": round(rr, 2)}


def run():
    # 加载 .env (HITHINK_FINANCE_API_KEY 等)
    envf = Path(__file__).resolve().parent.parent / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    # 优先级: 同花顺(股票,免费档无ETF) → 腾讯(全类型,最稳) → 东财(间歇抽风,最后兜底)
    fetchers = [HithinkFetcher()]
    try:
        from data_provider.tencent_fetcher import TencentFetcher
        fetchers.append(TencentFetcher())
    except Exception:
        pass
    try:
        from data_provider.efinance_fetcher import EfinanceFetcher
        fetchers.append(EfinanceFetcher())
    except Exception:
        pass
    syms = load_symbols()
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
    print(f"标的: {syms}\n区间: {start} ~ {end} (同花顺日线)\n")
    bars = {}
    for s in syms:
        df = fetch_bars(fetchers, s, start, end)
        if df is not None and len(df) > 40:
            bars[s] = df.reset_index(drop=True)
    if not bars:
        print("无数据"); return
    dates = sorted(set().union(*[set(df["date"]) for df in bars.values()]))
    dates = [d for d in dates if all(d in set(df["date"]) for df in bars.values())][:0] or \
            sorted(set().union(*[set(df["date"]) for df in bars.values()]))
    mkt = pd.DataFrame({"date": dates, "close": [
        sum(bars[s].set_index("date")["close"].get(d) or 0 for s in bars) / len(bars) for d in dates]})

    cash, equity_peak, max_dd = INIT_CAPITAL, INIT_CAPITAL, 0.0
    positions, trades, cooldown = {}, [], {}
    no_fill = []
    dv = {s: bars[s].set_index("date") for s in bars}

    def mark(i):
        d = dates[i]
        mv = sum(pos["qty"] * (dv[pos["sym"]]["close"].get(d) or pos["entry"]) for pos in positions.values())
        return cash + mv

    for i, d in enumerate(dates):
        reg = regime_of(mkt, i)
        # ── 出场 (T+1) ──
        for pid in list(positions):
            pos = positions[pid]
            b = dv[pos["sym"]]
            if d not in b.index:
                continue
            px = float(b["close"].get(d))
            pos["days"] += 1
            if d <= pos["earliest_sell"]:
                continue
            reason = None
            if px <= pos["stop"]:
                reason = "止损"
            elif px >= pos["tp2"]:
                reason = "TP2"
            elif pos["days"] >= TIME_STOP and px < pos["entry"] * (1 + 0.02):
                reason = "时间止损"
            elif px < b["close"].rolling(20).mean().get(d, 0) and pos["days"] >= 2:
                reason = "破MA20"
            if reason:
                gross = pos["qty"] * px
                cost_rate = COMMISSION + SLIP + STAMP
                net = gross * (1 - cost_rate)
                cash += net
                pnl = net - pos["cost_basis"]
                trades.append({**{k: pos[k] for k in ("sym", "setup", "entry", "stop", "score", "regime")},
                               "exit_date": d, "exit": px, "pnl": round(pnl, 2),
                               "ret": pnl / pos["cost_basis"], "reason": reason, "days": pos["days"]})
                cooldown[pos["sym"]] = i
                del positions[pid]
        # ── 入场 ──
        if reg == 5 or len(positions) >= MAX_POSITIONS:
            continue
        cands = []
        for sym, b in bars.items():
            if sym in [p["sym"] for p in positions.values()]:
                continue
            loc = b.index[b["date"] == d]
            if len(loc) == 0 or loc[0] < 25:
                continue
            if i - cooldown.get(sym, -99) < COOLDOWN_DAYS:
                continue
            sc = score_setup(b, int(loc[0]), reg)
            if sc and sc["score"] >= SCORE_THRESHOLD[reg]:
                cands.append((sc["score"], sym, sc))
        for score, sym, sc in sorted(cands, reverse=True):
            if len(positions) >= MAX_POSITIONS:
                break
            # ── 次日限价单成交模拟 (禁止前视: 信号T日收盘出, T+1才挂单) ──
            if i + 1 >= len(dates):
                continue
            d1 = dates[i + 1]
            b1 = bars[sym]
            if d1 not in set(b1["date"].values):
                continue
            r1 = int(b1.index[b1["date"] == d1][0])
            o1, l1 = float(b1["open"].iloc[r1]), float(b1["low"].iloc[r1])
            prev_c = float(b1["close"].iloc[r1 - 1])
            gap = o1 / prev_c - 1
            if gap > 0.04:
                continue  # AI Wave §17: 高开>4% 取消追入
            # 限价单: 次日最低价触及 entry 才成交; 低开低于 entry 则按开盘价成交
            if l1 > sc["entry"]:
                no_fill.append((d1, sym, sc["entry"], l1))
                continue  # 限价没触到 → NO FILL, 不追
            fill_px = min(o1, sc["entry"])
            eq = mark(i)
            size = min(eq * RISK_PCT * fill_px / (fill_px - sc["stop"]), eq * CAP_PCT, cash * 0.95)
            qty = int(size / fill_px / 100) * 100
            if qty < 100:
                continue
            gross = qty * fill_px
            fee = gross * (COMMISSION + SLIP)
            if gross + fee > cash:
                continue
            cash -= gross + fee
            positions[id(sc)] = {"sym": sym, "qty": qty, "entry": fill_px, "stop": sc["stop"],
                                 "tp1": sc["tp1"], "tp2": sc["tp2"], "setup": sc["setup"],
                                 "score": score, "regime": reg, "days": 0,
                                 "cost_basis": gross + fee,
                                 "earliest_sell": dates[min(r1 + 1, len(dates) - 1)]}
        # ── 记账 ──
        eq = mark(i)
        equity_peak = max(equity_peak, eq)
        max_dd = max(max_dd, (equity_peak - eq) / equity_peak)

    final = mark(len(dates) - 1)
    # ── 统计 ──
    print("=" * 72)
    print(f"{'AI Wave 规则回测':^68}")
    print("=" * 72)
    print(f"初始资金 {INIT_CAPITAL:,.0f} → 期末 {final:,.0f}  收益 {(final/INIT_CAPITAL-1)*100:+.2f}%  最大回撤 {max_dd*100:.2f}%")
    if not trades:
        print("无成交交易"); return
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    pf = (sum(t["pnl"] for t in wins) / -sum(t["pnl"] for t in losses)) if losses else float("inf")
    print(f"交易 {len(trades)} 笔 | 胜率 {len(wins)/len(trades)*100:.0f}% | "
          f"盈亏比 {abs(sum(t['pnl'] for t in wins)/len(wins) / (sum(t['pnl'] for t in losses)/len(losses))) if losses else 0:.2f} | "
          f"Profit Factor {pf:.2f}")
    print(f"平均持仓 {sum(t['days'] for t in trades)/len(trades):.1f} 天")
    if no_fill:
        print(f"限价未成交 (NO FILL): {len(no_fill)} 次 — 挂单没触到就不追, 这是纪律不是损失")
    print(f"\n{'日期':<11}{'标的':<8}{'Setup':<6}{'买':>7}{'卖':>7}{'收益%':>7}{'盈亏':>9}{'出场':<6}{'Regime':>4}")
    print("-" * 72)
    for t in sorted(trades, key=lambda x: x["exit_date"]):
        print(f"{t['exit_date']:<11}{t['sym']:<8}{t['setup']:<6}{t['entry']:>7.2f}{t['exit']:>7.2f}"
              f"{t['ret']*100:>+7.1f}{t['pnl']:>+9.0f}{t['reason']:<6}{t['regime']:>4}")
    by_setup = {}
    for t in trades:
        by_setup.setdefault(t["setup"], []).append(t["pnl"])
    print("\n按 Setup:", {k: f"{len(v)}笔 {sum(v):+.0f}元" for k, v in by_setup.items()})
    by_reg = {}
    for t in trades:
        by_reg.setdefault(t["regime"], []).append(t["pnl"])
    print("按 Regime:", {f"R{k}": f"{len(v)}笔 {sum(v):+.0f}元" for k, v in sorted(by_reg.items())})
    # 买入持有对照
    bh = sum((dv[s]["close"].iloc[-1] / dv[s]["close"].iloc[0] - 1) for s in bars) / len(bars)
    print(f"\n对照·等权买入持有: {bh*100:+.2f}%  |  策略: {(final/INIT_CAPITAL-1)*100:+.2f}%")


if __name__ == "__main__":
    run()
