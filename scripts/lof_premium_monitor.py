#!/usr/bin/env python3
"""LOF 折溢价监控 — 每日收盘后跑
场内价 (腾讯实时) vs 最新净值 (天天基金)
|溢价| >= 3% → 飞书推送套利机会/回归风险
静默逻辑: 未触发不推送; 状态持久化防重复推"""
import json, re, sys, os, time, urllib.request, datetime

HOME = os.path.expanduser("~")
STATE = os.path.expanduser("~/.lof_premium_state.json")
THRESHOLD = 3.0  # %
# 持仓LOF必须监控; 其余遍历全市场场内LOF (每季度刷新列表)
ALWAYS = {"501018": "南方原油LOF", "160644": "港美互联网LOF", "161116": "黄金主题LOF"}
UNIVERSE_FILE = os.path.join(HOME, "dsa", "data", "lof_universe.json")
def load_lofs():
    lofs = dict(ALWAYS)
    try:
        for c, n in json.load(open(UNIVERSE_FILE)):
            if c not in lofs:
                lofs[c] = n
    except Exception:
        pass
    return lofs

def daily_vol(code):
    """近20日日收益率std → T+2持有期1σ波动% (真实风险项)"""
    try:
        sys.path.insert(0, os.path.join(HOME, "dsa"))
        from data_provider.tencent_fetcher import TencentFetcher
        f = TencentFetcher()
        df = f.get_daily_data(code, days=40)
        if df is None or len(df) < 22:
            return None
        ret = df["close"].pct_change().dropna().tail(20)
        return float(ret.std() * (2 ** 0.5) * 100)  # 2日持有期1σ
    except Exception:
        return None

def nav(code):
    req = urllib.request.Request(
        f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"})
    t = urllib.request.urlopen(req, timeout=15).read().decode('utf-8', 'ignore')
    m = re.search(r'Data_netWorthTrend\s*=\s*(\[.*?\]);', t)
    if not m: return None, None
    data = json.loads(m.group(1))
    last = data[-1]
    import datetime as dt
    d = dt.datetime.fromtimestamp(last['x']/1000)
    return float(last['y']), d.date().isoformat()

def price(codes):
    full = ",".join(("sh" if c[0] in "56" else "sz") + c for c in codes)
    req = urllib.request.Request(f"https://qt.gtimg.cn/q={full}", headers={"User-Agent": "Mozilla/5.0"})
    out = {}
    for line in urllib.request.urlopen(req, timeout=8).read().decode("gbk").strip().split(";"):
        if "=" not in line: continue
        p = line.split("~")
        if len(p) > 3:
            out[p[2]] = float(p[3])
    return out

def feishu(msg):
    import subprocess
    subprocess.run([f"{HOME}/dsa/.venv/bin/python", f"{HOME}/pi-feishu-bridge/feishu_send.py", msg],
                   timeout=30, capture_output=True)

def main():
    state = {}
    if os.path.exists(STATE):
        try: state = json.load(open(STATE))
        except Exception: state = {}
    LOFS = load_lofs()
    px = price(list(LOFS))
    alerts = []
    today = datetime.date.today().isoformat()
    for code, name in LOFS.items():
        p = px.get(code)
        if not p: continue  # 无场内价 = 纯场外份额, 跳过
        nv, nav_date = nav(code)
        if not nv: continue
        time.sleep(0.15)  # 限速保护
        prem = (p/nv - 1) * 100
        if abs(prem) > 25: continue  # 份额折算/拆分导致净值口径不一致的脏数据, 跳过
        key = f"{code}_{today}_{sys.argv[1] if len(sys.argv) > 1 else 'am'}"
        triggered = abs(prem) >= THRESHOLD
        if triggered and state.get(key) != prem.__round__(1):
            mkt = "sh" if code[0] in "56" else "sz"
            if prem > 0:
                # 风险调整: T+2转托管期间价格会动, 扣除1σ波动后才算真套利
                vol = daily_vol(code)
                buffer = vol if vol is not None else 99.0  # 拿不到波动率=不推
                net = prem - 0.5 - buffer                 # 0.5%固定成本
                if net <= 0:
                    state[key] = round(prem, 1)
                    continue  # 波动会吃掉利润 → 不是套利, 静默
                alerts.append(f"  {name}\n"
                              f"    场外申购代码: {code} (按净值申购)\n"
                              f"    场内卖出代码: {mkt}{code} (转托管后场内卖出)\n"
                              f"    场内现价 {p:.3f} vs 净值 {nv:.4f} ({nav_date}) → 溢价 +{prem:.1f}%\n"
                              f"    风险调整: 成本0.5% + T+2波动缓冲{buffer:.1f}%\n"
                              f"    → 锁定后净空间 ≈ +{net:.1f}% (1σ口径, 仍有尾部风险)\n"
                              f"    操作: 券商App场外申购 → T+2转托管到场内 → 溢价卖出\n"
                              f"    ⚠ 少数LOF场内外代码不同, 申购前在券商App搜名称核对")
            else:
                alerts.append(f"  {name}\n"
                              f"    场内买入代码: {mkt}{code} (现价 {p:.3f}, 折价 {prem:.1f}%)\n"
                              f"    净值 {nv:.4f} ({nav_date}) → 场内买入比场外申购便宜 {abs(prem):.1f}%\n"
                              f"    性质: 提示而非套利 — 折价回归无时间表, 回归前净值也会波动")
        state[key] = round(prem, 1)
    # 清理7天前的state
    cutoff = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    for k in list(state):
        if k.split("_")[-1] < cutoff: del state[k]
    json.dump(state, open(STATE, "w"))
    if alerts:
        feishu(f"💰 LOF 套利信号 ({today})\n" + "\n".join(alerts[:5]))

if __name__ == "__main__":
    main()
