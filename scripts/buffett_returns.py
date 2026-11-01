#!/usr/bin/env python3
"""巴菲特组合收益计算: picks_hist + 腾讯日线 → 年度收益 vs 沪深300"""
import json, sys, datetime
sys.path.insert(0, "/home/kos/dsa")
from data_provider.tencent_fetcher import TencentFetcher

picks = json.load(open("/tmp/buffett_picks.json"))  # {year: [codes]}

import urllib.request, time as _t
def em_kline(code, lmt=2600):
    """东财日K (前复权, 一次全历史) — code: 6位"""
    mkt = "1" if code[0] in "569" else "0"
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={mkt}.{code}&klt=101&fqt=1&lmt={lmt}"
           f"&fields1=f1,f2,f3&fields2=f51,f53")
    req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/"})
    d = json.loads(urllib.request.urlopen(req, timeout=15).read())
    kl = d.get("data", {}).get("klines", [])
    import pandas as pd
    if not kl: return None
    recs = [l.split(",") for l in kl]
    return pd.Series({r[0]: float(r[1]) for r in recs})

allcodes = sorted({c for cs in picks.values() for c in cs} | {"510300"})
# HithinkFetcher (同花顺) + 磁盘缓存 + 限速 (腾讯被风控后的替代源, 2018-07起)
import os
CACHE_DIR = "/home/kos/dsa/data/buffett_px_cache"
os.makedirs(CACHE_DIR, exist_ok=True)
from data_provider.hithink_fetcher import HithinkFetcher
hf = HithinkFetcher()
px = {}
for i, c in enumerate(allcodes):
    cf = f"{CACHE_DIR}/{c}.json"
    if os.path.exists(cf):
        px[c] = json.load(open(cf))
        continue
    code = c[2:] if c[:2] in ("sh","sz") else c
    try:
        df = hf.get_daily_data(code, days=1500)
        if df is not None and len(df) > 100:
            col = "date" if "date" in df.columns else df.index
            s = df.set_index(col)["close"] if "date" in df.columns else df["close"]
            s.index = [str(x)[:10] for x in s.index]
            px[c] = dict(s)
            json.dump(dict(s), open(cf, "w"))
    except Exception:
        pass
    _t.sleep(1.5)
    if i % 10 == 9:
        print(f"  价格 {i+1}/{len(allcodes)}", file=sys.stderr, flush=True)
import pandas as pd
px = {c: pd.Series(v).sort_index() for c, v in px.items()}
print(f"价格就绪: {len(px)}/{len(allcodes)} 只", file=sys.stderr)
bclose = px.pop("510300", None)

def px_on(series, d):
    s = series[series.index <= d]
    return float(s.iloc[-1]) if len(s) else None

# 沪深300 官方年度收益 (公开数据硬编码; 同花顺源无指数)
BENCH_YEARLY = {2019: 0.361, 2020: 0.272, 2021: -0.052, 2022: -0.216,
                2023: -0.114, 2024: 0.147, 2025: 0.148}  # 2025 YTD 1-9月

years = sorted(int(y) for y in picks)
print("\n══ 巴菲特组合 (连续3年ROE>15%+盈利+增长, ROE Top15, 等权, 年调) ══")
print(f"{'持有年':<10}{'组合':>9}{'沪深300':>9}{'超额':>8}")
tot_eq, tot_b = 1.0, 1.0
navs = []
for i in range(len(years)-1):
    y0, y1 = years[i], years[i+1]
    d0, d1 = f"{y0}-01-05", f"{y1}-01-05"
    rets = []
    for c in picks[str(y0)]:
        if c not in px: continue
        p0, p1 = px_on(px[c], d0), px_on(px[c], d1)
        if p0 and p1: rets.append(p1/p0 - 1)
    if len(rets) < 5:
        print(f"{y0}->{y1}: 有效样本不足({len(rets)})"); continue
    pr = sum(rets)/len(rets)
    br = BENCH_YEARLY.get(y0)
    if br is None:
        print(f"{y0}->{y1}: 基准缺失, 跳过"); continue
    tot_eq *= (1+pr); tot_b *= (1+br)
    navs.append((y0, pr, br))
    print(f"{y0}->{y1}  {pr*100:+7.1f}%  {br*100:+7.1f}%  {(pr-br)*100:+6.1f}pp   (n={len(rets)})")

print(f"\n累计 ({years[0]}->{years[-1]}): 组合 {(tot_eq-1)*100:+.1f}% vs 沪深300 {(tot_b-1)*100:+.1f}%")
n = len(navs)
if n:
    ann = tot_eq**(1/n)-1; annb = tot_b**(1/n)-1
    print(f"年化: 组合 {ann*100:+.1f}% vs 基准 {annb*100:+.1f}% | 年均超额 {(ann-annb)*100:+.1f}pp")
    # 最大回撤 (年度nav)
    nav_series, peak, mdd = [1.0], 1.0, 0.0
    for _, pr, _ in navs:
        nav_series.append(nav_series[-1]*(1+pr))
    for v in nav_series:
        peak = max(peak, v); mdd = min(mdd, v/peak-1)
    print(f"组合最大回撤 (年度粒度): {mdd*100:.1f}%")
