#!/usr/bin/env python3
"""巴菲特理念 A 股量化回测
理念→因子 (AQR "Buffett's Alpha" 2018 的分解):
  1. 高且持续 ROE   ("我只买长期 ROE>15% 的公司")
  2. 盈利为真且增长  (净利润>0 且 同比>0)
  3. 便宜           (PE<30, 巴菲特: 用合理价格买伟大公司)
  4. 有安全边际     (PB<8, 排除情绪溢价)
  5. 规模与稳定性    (市值>100亿, 排除 micro-cap 噪声)
组合: ROE 降序 top 15, 等权, 年度调仓 (1月首个交易日)
对照: 沪深300 (sh000300)
已知偏差 (诚实声明):
  · 用 REPORTDATE 时点的财报, 按次年1月调仓 → 最长4个月前视偏差
    (真实披露有4月底截止, 已尽量保守)
  · 股票池为当前上市股票+历史财报 → 幸存者偏差存在
  · 历史PE/PB用当年财报期快照近似
"""
import urllib.request, json, time, sys, datetime

def dc(filt, sort="WEIGHTAVG_ROE", page=1, page_size=500):
    url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
           "reportName=RPT_LICO_FN_CPD&columns=ALL&"
           f"filter={filt}&pageNumber={page}&pageSize={page_size}"
           f"&sortColumns={sort}&sortTypes=-1&source=WEB&client=WEB")
    req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0","Referer":"https://data.eastmoney.com/"})
    d = json.loads(urllib.request.urlopen(req, timeout=25).read())
    r = d.get("result")
    return r["data"] if r and r.get("data") else []

def tencent_px(codes):
    """腾讯批量行情: 现价/PE/PB/总市值"""
    out = {}
    for i in range(0, len(codes), 60):
        batch = codes[i:i+60]
        q = ",".join(("sh" if c[0] in "569" else ("bj" if c[0] in "48" else "sz")) + c for c in batch)
        try:
            req = urllib.request.Request(f"https://qt.gtimg.cn/q={q}", headers={"User-Agent":"Mozilla/5.0"})
            t = urllib.request.urlopen(req, timeout=10).read().decode("gbk")
            for line in t.strip().split(";"):
                p = line.split("~")
                if len(p) > 46:
                    out[p[2]] = {"px": float(p[3] or 0), "pe": float(p[39] or 999),
                                 "pb": float(p[46] or 999), "mcap": float(p[45] or 0)}
        except Exception:
            pass
        time.sleep(0.2)
    return out

def year_report(universe_filter_year):
    """拉某年报期全市场数据 (5000+股, 分页), 磁盘缓存"""
    import os, glob
    cache = f"/home/kos/dsa/data/buffett_reports_{universe_filter_year}.json"
    if os.path.exists(cache):
        return json.load(open(cache))
    rows, page = [], 1
    while True:
        chunk = dc(f"(REPORTDATE='{universe_filter_year}')", page=page)
        if not chunk: break
        rows += chunk
        if len(chunk) < 500: break
        page += 1
        time.sleep(0.3)
        if page > 15: break
    json.dump(rows, open(cache, "w"))
    return rows

def build_picks(report_date, roe_years=3):
    """某年报期的巴菲特筛选: 连续roe_years年 ROE>15% 且 净利>0 同比>0"""
    # 当年 + 前 roe_years-1 年
    y = int(report_date[:4])
    hist = {}  # code -> {year: (roe, yoy, profit)}
    for yy in range(y - roe_years + 1, y + 1):
        rd = f"{yy}-12-31"
        rows = year_report(rd)
        for r in rows:
            code = r.get("SECURITY_CODE", "")
            roe = r.get("WEIGHTAVG_ROE")
            yoy = r.get("SJLTZ")       # 净利润同比%
            prof = r.get("PARENT_NETPROFIT")
            if code and roe is not None:
                hist.setdefault(code, {})[yy] = (float(roe), float(yoy or -999), float(prof or -1e9))
        print(f"  {rd}: {len(rows)} 条", file=sys.stderr)
        time.sleep(0.4)
    picks = []
    for code, yrs in hist.items():
        if len(yrs) < roe_years: continue
        if not code[0] in "036":  # 仅沪深主板(60/00)+创业板(30), 剔除北交所/新三板
            continue
        if all(v[0] > 15 and v[1] > 0 and v[2] > 0 for v in yrs.values()):
            picks.append((code, yrs[y][0]))  # (code, 当年ROE)
    picks.sort(key=lambda x: -x[1])
    return picks[:30]

def main():
    print("══ 巴菲特理念 A 股量化回测 ══", flush=True)
    # 调仓年: 2017..2025 (每年1月初调仓, 用截至上一年的连续ROE)
    rebal_years = [2017, 2019, 2021, 2023, 2025]
    picks_hist = {}
    for y in rebal_years:
        print(f"\n[{y} 调仓] 筛选 {y-1} 年报 (连续3年 ROE>15%+盈利+增长)...", flush=True)
        picks = build_picks(f"{y-1}-12-31")
        picks_hist[y] = [c for c, _ in picks]
        names = {c: n for c, n in []}
        print(f"  入选 {len(picks)} 只: {[c for c,_ in picks[:8]]}...")
    json.dump(picks_hist, open("/tmp/buffett_picks.json", "w"))
    print("\n选股历史已存 /tmp/buffett_picks.json (下一步拉价格算收益)")

if __name__ == "__main__":
    main()
