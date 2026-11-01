#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
回测: 日线MACD定方向 + 5分钟KDJ找点位 (ETF做T)
规则 (严格防未来函数):
  - 闸门: 昨日日线MACD DIF>0 → 今日允许做多T; 否则不交易
  - 买入: 5min J<0 → 次根K线开盘价买入 (+0.05%滑点+0.01%佣金)
  - 卖出: 5min J>100 或 当日尾盘 → 次根开盘/收盘价卖出
  - 一次只持一单, 日内必平 (T+0合规)
数据: 腾讯财经 (日线600根 + 5min翻页~6个月)
"""
import json
import urllib.request
from datetime import datetime

UA = {'User-Agent': 'Mozilla/5.0'}
ETFS = [('sh501018', '南方原油'), ('sh513310', '中韩半导体'), ('sz160644', '港美互联网'),
        ('sz161116', '黄金LOF'), ('sh511260', '十年国债')]
FEE = 0.0006  # 单边: 万1佣金 + 0.05%滑点


def get(url):
    req = urllib.request.Request(url, headers=UA)
    return json.load(urllib.request.urlopen(req, timeout=15))


def fetch_daily(code):
    d = get(f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,650,qfq")
    k = d['data'][code].get('qfqday') or d['data'][code]['day']
    return [(r[0], float(r[2])) for r in k]   # (date, close)


def fetch_5min(code, pages=6):
    all_k, end = [], ''
    for _ in range(pages):
        url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={code},m5,{end},800"
        d = get(url)
        k = d['data'][code].get('m5') or []
        if not k:
            break
        all_k = k + all_k
        end = k[0][0]
        if len(k) < 800:
            break
    # [ts, open, close, high, low, vol]
    bars = [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in all_k]
    return bars


def macd_dif(closes, fast=12, slow=26):
    ema_f, ema_s, out = closes[0], closes[0], []
    for i, c in enumerate(closes):
        ema_f += (c - ema_f) * 2 / (fast + 1)
        ema_s += (c - ema_s) * 2 / (slow + 1)
        out.append(ema_f - ema_s)
    return out


def kdj_j(bars, n=9):
    out, k_, d_ = [], 50.0, 50.0
    for i in range(len(bars)):
        lo = min(bars[j][4] for j in range(max(0, i - n + 1), i + 1))
        hi = max(bars[j][3] for j in range(max(0, i - n + 1), i + 1))
        rsv = 50.0 if hi == lo else (bars[i][2] - lo) / (hi - lo) * 100
        k_ = k_ * 2 / 3 + rsv / 3
        d_ = d_ * 2 / 3 + k_ / 3
        out.append(3 * k_ - 2 * d_)
    return out


def backtest(bars, gate_map, buy_th=0, sell_th=100, fee=FEE):
    trades, pos, entry = [], None, 0.0
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for i in range(len(bars) - 1):
        ts, o, c, h, l, _j = bars[i]
        day = ts[:8]
        gate = gate_map.get(day, False)
        j = bars[i][5]
        is_last = (i + 1 >= len(bars)) or (bars[i + 1][0][:8] != day)
        nxt_o = bars[i + 1][1] if i + 1 < len(bars) else c
        if pos is None and gate and j < buy_th and not is_last:
            pos, entry = 'long', nxt_o * (1 + fee)
        elif pos == 'long':
            if j > sell_th or is_last:
                exit_ = nxt_o * (1 - fee) if not is_last else c * (1 - fee)
                pnl = exit_ / entry - 1
                trades.append(pnl)
                equity *= (1 + pnl)
                peak = max(peak, equity)
                max_dd = max(max_dd, 1 - equity / peak)
                pos = None
    return trades, equity, max_dd


print(f"{'品种':<12}{'5min区间':<22}{'笔数':>4}{'胜率':>7}{'累计':>9}{'均笔':>8}{'回撤':>7}{'持有':>9}{'超额':>8}")
print('─' * 92)
for code, name in ETFS:
    daily = fetch_daily(code)
    dts = [d[0] for d in daily]
    dif = macd_dif([d[1] for d in daily])
    # 昨日DIF>0 → 今日开门 (防未来)
    gate_map = {}
    for i in range(1, len(daily)):
        gate_map[daily[i][0].replace('-', '')] = dif[i - 1] > 0
    bars5 = fetch_5min(code, pages=6)
    if len(bars5) < 100:
        print(f"{name:<12}数据不足")
        continue
    js = kdj_j(bars5)
    bars5 = [b + (j,) for b, j in zip(bars5, js)]
    trades, eq, dd = backtest(bars5, gate_map)
    span = f"{bars5[0][0][:4]}-{bars5[0][0][4:6]}-{bars5[0][0][6:8]}~{bars5[-1][0][:8]}"
    hold = bars5[-1][2] / bars5[0][1] - 1
    win = sum(1 for t in trades if t > 0) / len(trades) * 100 if trades else 0
    avg = sum(trades) / len(trades) * 100 if trades else 0
    span_fmt = f"{bars5[0][0][2:4]}{bars5[0][0][4:6]}.{bars5[0][0][6:8]}~{bars5[-1][0][4:6]}.{bars5[-1][0][6:8]}"
    print(f"{name:<12}{span_fmt:<22}{len(trades):>4}{win:>6.0f}%{(eq-1)*100:>+8.2f}%{avg:>+7.3f}%{dd*100:>6.1f}%{hold*100:>+8.2f}%{(eq-1-hold)*100:>+7.2f}%")

print('─' * 92)
print("规则: 昨日日线DIF>0才开仓 | J<0买 J>100卖/尾盘平 | 单边费0.06% | 次根开盘成交")
