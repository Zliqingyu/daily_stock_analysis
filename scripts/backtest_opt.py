#!/usr/bin/env python3
"""AI Wave 参数扫描 — 1年数据, N日评分→N+1挂单执行 (无前视)
扫描: 评分阈值/持仓数/止盈/时间止损, 输出年化排名 + 最优参数全记录
"""
import sys, os, json, itertools
from datetime import datetime, timedelta
from pathlib import Path
import pandas as pd

sys.path.insert(0, "/home/kos/dsa")
from data_provider.hithink_fetcher import HithinkFetcher
from data_provider.tencent_fetcher import TencentFetcher
from data_provider.efinance_fetcher import EfinanceFetcher

INIT, COMMISSION, STAMP, SLIP = 45000.0, 0.00025, 0.0005, 0.001
STOP_PCT = 0.05


def load_all():
    for line in Path("/home/kos/dsa/.env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    fetchers = [HithinkFetcher(), TencentFetcher(), EfinanceFetcher()]
    syms = []
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:9996/api/v1/portfolio/snapshot?include_realtime=false", timeout=15) as r:
            snap = json.load(r)
        for a in snap.get("accounts") or []:
            syms += [p["symbol"] for p in (a.get("positions") or []) if (p.get("quantity") or 0) > 0]
    except Exception:
        pass
    wl = Path("/home/kos/dsa/data/watchlist.json")
    if wl.exists():
        syms += [w for w in json.load(open(wl)) if w not in syms]
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=380)).strftime("%Y-%m-%d")
    bars = {}
    for s in syms:
        for f in fetchers:
            try:
                df = f._normalize_data(f._fetch_raw_data(s, start, end), s)
                if df is not None and len(df) > 200:
                    bars[s] = df.reset_index(drop=True)
                    break
            except Exception:
                continue
    return bars


def regime_of(mkt, i):
    if i < 25:
        return 2
    c = mkt["close"]
    ma20 = c.rolling(20).mean().iloc[i]
    ret20 = c.iloc[i] / c.iloc[i - 20] - 1
    ret5 = c.iloc[i] / c.iloc[i - 5] - 1
    if ret5 < -0.05:
        return 5
    if c.iloc[i] > ma20 and ret20 > 0.03:
        return 1
    if c.iloc[i] > ma20:
        return 2
    if ret20 > -0.03:
        return 3
    return 4


def score_setup(bars, i, regime, th_unused=None):
    if i < 25:
        return None
    c, v = bars["close"], bars["volume"]
    o, h, l = bars["open"], bars["high"], bars["low"]
    ma5, ma10, ma20 = (c.rolling(w).mean().iloc[i] for w in (5, 10, 20))
    px = c.iloc[i]
    v_ma5 = v.rolling(5).mean().iloc[i]
    hi20, hi10 = h.iloc[i - 20:i].max(), h.iloc[i - 10:i].max()
    setup, s = "", 0.0
    if px > hi20 and v.iloc[i] > v_ma5 * 1.5 and o.iloc[i] <= hi20:
        setup, s = "A突破", 30
    elif (c.iloc[i - 10:i] > ma10).mean() > 0.7 and l.iloc[i] <= ma10 < c.iloc[i - 5] \
            and px > ma20 and v.iloc[i] < v_ma5 * 0.8:
        setup, s = "B回踩", 28
    else:
        cond = sum([px > ma20, ma10 > ma20, px > ma5])
        if cond == 3 and v.iloc[i] > v_ma5:
            setup, s = "趋势", 18
        else:
            return None
    s += 12 if px > ma20 else -15
    s += 8 if ma20 > c.rolling(20).mean().iloc[i - 5] else -5
    dev = (px - ma5) / ma5
    s += 8 if abs(dev) < 0.03 else (-6 if dev > 0.08 else 0)
    s += {1: 15, 2: 12, 3: 5, 4: 0, 5: -50}[regime]
    stop = min(ma20 * 0.995, l.iloc[i - 5:i].min())
    stop = max(stop, px * (1 - STOP_PCT - 0.02))
    risk = px - stop
    if risk <= 0 or risk / px < 0.015:
        return None
    target = max(hi10, px * 1.07)
    rr = (target - px) / risk
    s += 15 if rr >= 1.3 else 0
    s += 8 if rr >= 2.0 else 0
    return {"score": s, "entry": px, "stop": round(stop, 3), "setup": setup}


def simulate(bars, dates, dv, mkt, P):
    cash = INIT
    equity_peak, max_dd = INIT, 0.0
    positions, trades, cooldown = {}, [], {}
    th = P["th"]

    def mark(i):
        d = dates[i]
        mv = sum(p["qty"] * (dv[p["sym"]]["close"].get(d) or p["entry"]) for p in positions.values())
        return cash + mv

    for i, d in enumerate(dates):
        reg = regime_of(mkt, i)
        for pid in list(positions):
            pos = positions[pid]
            b = dv[pos["sym"]]
            if d not in b.index:
                continue
            px = float(b["close"].get(d))
            pos["days"] += 1
            if d <= pos["earliest_sell"]:
                continue
            tp1 = pos["entry"] * (1 + P["tp1"])
            reason = None
            if px <= pos["stop"]:
                reason = "止损"
            elif px >= pos["entry"] * (1 + P["tp2"]):
                reason = "TP2"
            elif pos["days"] >= P["time_stop"] and px < pos["entry"] * 1.02:
                reason = "时间止损"
            elif px < float(b["close"].rolling(20).mean().get(d, 0)) and pos["days"] >= 2:
                reason = "破MA20"
            if reason:
                net = pos["qty"] * px * (1 - COMMISSION - SLIP - STAMP)
                cash += net
                pnl = net - pos["cost_basis"]
                trades.append({"sym": pos["sym"], "pnl": pnl, "ret": pnl / pos["cost_basis"],
                               "days": pos["days"], "reason": reason, "exit_date": d,
                               "regime": pos["regime"], "setup": pos["setup"]})
                cooldown[pos["sym"]] = i
                del positions[pid]
        if reg == 5 or len(positions) >= P["max_pos"]:
            continue
        cands = []
        for sym, b in bars.items():
            if sym in [p["sym"] for p in positions.values()]:
                continue
            loc = b.index[b["date"] == d]
            if len(loc) == 0 or loc[0] < 25:
                continue
            if i - cooldown.get(sym, -99) < P["cooldown"]:
                continue
            sc = score_setup(b, int(loc[0]), reg)
            if sc and sc["score"] >= th:
                cands.append((sc["score"], sym, sc))
        for score, sym, sc in sorted(cands, reverse=True):
            if len(positions) >= P["max_pos"]:
                break
            if i + 1 >= len(dates):
                continue
            d1 = dates[i + 1]
            b1 = bars[sym]
            if d1 not in set(b1["date"].values):
                continue
            r1 = int(b1.index[b1["date"] == d1][0])
            o1, l1 = float(b1["open"].iloc[r1]), float(b1["low"].iloc[r1])
            prev_c = float(b1["close"].iloc[r1 - 1])
            if o1 / prev_c - 1 > 0.04:
                continue
            if l1 > sc["entry"]:
                continue  # NO FILL
            fill = min(o1, sc["entry"])
            eq = mark(i)
            size = min(eq * P["risk"] * fill / (fill - sc["stop"]), eq * P["cap"], cash * 0.95)
            qty = int(size / fill / 100) * 100
            if qty < 100:
                continue
            gross = qty * fill
            fee = gross * (COMMISSION + SLIP)
            if gross + fee > cash:
                continue
            cash -= gross + fee
            positions[id(sc)] = {"sym": sym, "qty": qty, "entry": fill, "stop": sc["stop"],
                                 "setup": sc["setup"], "regime": reg, "days": 0,
                                 "cost_basis": gross + fee,
                                 "earliest_sell": dates[min(r1 + 1, len(dates) - 1)]}
        eq = mark(i)
        equity_peak = max(equity_peak, eq)
        max_dd = max(max_dd, (equity_peak - eq) / equity_peak)
    final = mark(len(dates) - 1)
    return final, max_dd, trades


def main():
    print("拉取1年数据 (同花顺→腾讯→东财)...")
    bars = load_all()
    print(f"成功: {list(bars)}")
    dates = sorted(set().union(*[set(df["date"]) for df in bars.values()]))
    dv = {s: b.set_index("date") for s, b in bars.items()}
    mkt = pd.DataFrame({"date": dates, "close": [
        sum(dv[s]["close"].get(d) or 0 for s in bars) / len(bars) for d in dates]})
    ndays = (datetime.now() - datetime.strptime(dates[0], "%Y-%m-%d")).days
    print(f"区间 {dates[0]}~{dates[-1]} ({len(dates)}个交易日)\n")

    grid = []
    for th, mp, tp1, tp2, ts in itertools.product(
            (65, 70, 75), (2, 3), (0.06, 0.08), (0.10, 0.15), (5, 10)):
        grid.append({"th": th, "max_pos": mp, "tp1": tp1, "tp2": tp2, "time_stop": ts,
                     "risk": 0.015, "cap": 0.30, "cooldown": 5})
    results = []
    for P in grid:
        final, mdd, trades = simulate(bars, dates, dv, mkt, P)
        ret = final / INIT - 1
        ann = (final / INIT) ** (365 / max(ndays, 1)) - 1
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] <= 0]
        pf = (sum(t["pnl"] for t in wins) / -sum(t["pnl"] for t in losses)) if losses else 99
        results.append({**P, "ret": ret, "ann": ann, "mdd": mdd, "n": len(trades),
                        "wr": len(wins) / len(trades) * 100 if trades else 0, "pf": pf,
                        "final": final, "trades": trades})
    results.sort(key=lambda x: x["ret"], reverse=True)
    print(f"{'排名':<4}{'阈值':<5}{'仓':<3}{'TP1':<6}{'TP2':<6}{'时间止':<5}{'收益':>8}{'年化':>8}{'回撤':>7}{'笔':>4}{'胜率':>6}{'PF':>6}")
    print("-" * 78)
    for k, r in enumerate(results[:10], 1):
        print(f"{k:<4}{r['th']:<5}{r['max_pos']:<3}{r['tp1']:<6}{r['tp2']:<6}{r['time_stop']:<5}"
              f"{r['ret']*100:>7.2f}%{r['ann']*100:>7.2f}%{r['mdd']*100:>6.2f}%{r['n']:>4}{r['wr']:>5.0f}%{r['pf']:>6.2f}")
    best = results[0]
    print(f"\n★ 最优参数: 阈值{best['th']} 持仓{best['max_pos']} TP1+{best['tp1']*100:.0f}% "
          f"TP2+{best['tp2']*100:.0f}% 时间止损{best['time_stop']}日")
    print(f"  期末 {best['final']:,.0f} 年化 {best['ann']*100:+.2f}% (区间收益 {best['ret']*100:+.2f}%)")
    over10 = [r for r in results if r["ann"] >= 0.10]
    print(f"  年化≥10%的参数组合: {len(over10)}/{len(results)}")
    print(f"\n最优参数交易明细:")
    for t in sorted(best["trades"], key=lambda x: x["exit_date"]):
        print(f"  {t['exit_date']} {t['sym']} {t['setup']} {t['ret']*100:+.1f}% ({t['reason']}, {t['days']}天)")


if __name__ == "__main__":
    main()
