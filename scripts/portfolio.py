#!/usr/bin/env python3
"""DSA 组合管理终端 — 用法: python3 portfolio.py"""
import json, sys, os, time, urllib.request
from pathlib import Path
from urllib.request import urlopen, Request

BASE = "http://127.0.0.1:9996/api/v1/portfolio"
_CACHE_FILE = os.path.expanduser("~/.dsa_name_cache.json")
_WATCH_FILE = Path(__file__).resolve().parent.parent / "data" / "watchlist.json"
_name_cache = {}
_RST = "\033[0m"; _BOLD = "\033[1m"; _DIM = "\033[2m"

def _load_cache():
    global _name_cache
    try:
        with open(_CACHE_FILE) as f: _name_cache = json.load(f)
    except: pass

def _save_cache():
    try:
        with open(_CACHE_FILE, "w") as f: json.dump(_name_cache, f, ensure_ascii=False)
    except: pass

def _mpf(sym):
    if sym.startswith(("sh","sz")): return sym
    return f"sh{sym}" if sym.startswith(("6","5")) else f"sz{sym}"

def fetch_names(syms):
    todo = [s for s in syms if s not in _name_cache]
    if not todo: return
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            f"https://qt.gtimg.cn/q={','.join(_mpf(s) for s in todo)}",
            headers={"User-Agent":"Mozilla/5.0"}), timeout=5)
        for line in r.read().decode("gbk").strip().split(";"):
            if "=" not in line: continue
            parts = line.split("~")
            if len(parts) > 1:
                k = parts[0].split("=")[0][-8:]
                _name_cache[k[2:] if k[:2] in ("sh","sz") else k] = parts[1]
        _save_cache()
    except: pass

def fetch_prices(syms):
    if not syms: return {}
    try:
        r = urllib.request.urlopen(urllib.request.Request(
            f"https://qt.gtimg.cn/q={','.join(_mpf(s) for s in syms)}",
            headers={"User-Agent":"Mozilla/5.0"}), timeout=5)
        out = {}
        for line in r.read().decode("gbk").strip().split(";"):
            if "=" not in line: continue
            parts = line.split("~")
            if len(parts) > 3:
                k = parts[0].split("=")[0][-8:]
                orig = k[2:] if k[:2] in ("sh","sz") else k
                try:
                    p = float(parts[3])
                    if p > 0: out[orig] = p; _name_cache[orig] = parts[1]
                except: pass
        _save_cache()
        return out
    except: return {}

def get_name(s): return _name_cache.get(s, "")
def fmt(v, d=2): return "--" if v is None else f"{v:,.{d}f}"
def pnl_c(v): return "" if v is None else ("\033[92m" if v >= 0 else "\033[91m")

import unicodedata
def _dw(s):
    """显示宽度: 全角=2 半角=1"""
    return sum(2 if unicodedata.east_asian_width(c) in "FW" else 1 for c in str(s))
def pad(s, n):
    """按显示宽度左对齐填充"""
    s = str(s)
    return s + " " * max(0, n - _dw(s))
def pad_r(s, n):
    """按显示宽度右对齐填充"""
    s = str(s)
    return " " * max(0, n - _dw(s)) + s
def trunc(s, n):
    """按显示宽度截断(超宽加…)"""
    s = str(s)
    if _dw(s) <= n: return s
    out, w = "", 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in "FW" else 1
        if w + cw > n - 1: break
        out += c; w += cw
    return out + "…"

def api(method, path, data=None):
    try:
        req = Request(f"{BASE}{path}", method=method,
                      headers={"Content-Type":"application/json"} if data else {})
        if data: req.data = json.dumps(data).encode()
        with urlopen(req, timeout=30) as r: return json.loads(r.read())
    except Exception as e: return {"error": str(e)}

def load_watch():
    try:
        with open(_WATCH_FILE) as f: return json.load(f)
    except: return []

def save_watch(codes):
    _WATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(_WATCH_FILE, "w") as f: json.dump(codes, f)

def show():
    snap = api("GET", "/snapshot?include_realtime=true")
    if "error" in snap:
        print(f"  \033[91m✗ {snap['error']}\033[0m"); return

    eq = snap.get("total_equity", 0)
    cash = snap.get("total_cash", 0)

    positions = [p for a in (snap.get("accounts") or []) for p in (a.get("positions") or [])]
    pos_syms = {p.get("symbol","") for p in positions}
    watch = load_watch()
    watch_only = [w for w in watch if w not in pos_syms]

    prices = fetch_prices(list(pos_syms) + watch_only)
    for p in positions:
        cp = prices.get(p.get("symbol",""), 0)
        if cp > 0:
            p["_p"] = cp
            p["_mv"] = round(cp * p.get("quantity",0), 2)
            p["_pnl"] = round((cp - p.get("avg_cost",0)) * p.get("quantity",0), 2)

    mv = sum(p.get("_mv",0) for p in positions)
    upnl = sum(p.get("_pnl",0) for p in positions)
    eq = cash + mv
    pct = (upnl / eq * 100) if eq else 0

    # 序号选择表: 持仓在前(带数量/现价), 自选在后
    _sel_map.clear()
    _n = 0
    for p in sorted(positions, key=lambda x: x.get("_mv",0), reverse=True):
        _n += 1
        _sel_map[str(_n)] = (p.get("symbol",""), p.get("quantity",0), p.get("_p",0) or p.get("last_price",0) or 0)
    for w in watch_only:
        _n += 1
        _sel_map[str(_n)] = (w, 0.0, prices.get(w, 0))

    print(f"  {_BOLD}权益 {fmt(eq)}{_RST}  |  现金 {fmt(cash)}  |  市值 {fmt(mv)}  |  浮盈 {pnl_c(upnl)}{upnl:+,.2f} ({pct:+.2f}%){_RST}")

    if positions:
        print(f"  {_BOLD}持仓{_RST}")
        print(f"  {pad('序',3)} {pad('代码',8)} {pad('名称',12)} {pad_r('数量',8)} {pad_r('成本',8)} {pad_r('现价',8)} {pad_r('市值',12)} {pad_r('浮盈',10)}")
        for i, p in enumerate(sorted(positions, key=lambda x: x.get("_mv",0), reverse=True), 1):
            s = p.get("symbol","")
            nm = trunc(get_name(s), 12)
            print(f"  {i:<3} {s:<8} {pad(nm, 12)} {p.get('quantity',0):>8.0f} {fmt(p.get('avg_cost')):>8} {fmt(p.get('_p')):>8} {fmt(p.get('_mv')):>12} {pnl_c(p.get('_pnl'))}{p.get('_pnl',0):>+10.2f}{_RST}")

    if watch_only:
        print(f"  {_BOLD}自选{_RST}")
        for j, w in enumerate(watch_only, len(positions)+1):
            cp = prices.get(w, 0)
            nm = trunc(get_name(w), 12)
            print(f"  {j:<3} {w:<8} {pad(nm, 12)} {fmt(cp):>8}")

    if not positions and not watch:
        print(f"  {_DIM}无持仓无自选{_RST}")

_sel_map = {}  # 序号 -> (代码, 数量, 现价), show() 时刷新

def resolve_code(inp):
    """输入序号或代码 → (代码, 持仓数量或0, 现价或0)"""
    inp = inp.strip()
    if inp in _sel_map:
        return _sel_map[inp]
    return (inp, 0.0, 0.0)

def ask_code_qty_price(default_qty=None, default_px=None, allow_index=True):
    """返回 (symbol, qty, px) 或 None"""
    inp = input("  代码/序号: ").strip()
    if not inp: return None
    sym, held, px0 = resolve_code(inp) if allow_index else (inp, 0.0, 0.0)
    q = input(f"  数量{f' [默认{held:.0f}]' if default_qty and held else ''}: ").strip()
    if not q:
        if default_qty and held: q = str(held)
        else: return None
    p = input(f"  价格{f' [默认{px0:g}]' if default_px and px0 else ''}: ").strip()
    if not p:
        if default_px and px0: p = str(px0)
        else: return None
    try: return sym, float(q), float(p)
    except: print("  \033[91m必须是数字\033[0m"); return None

def do_buy():
    print(f"\n  {_BOLD}── 买入 (可输序号加仓) ──{_RST}")
    r = ask_code_qty_price(default_px=True)
    if not r: return
    sym, qty, px = r
    r2 = api("POST", "/trades", {"account_id":1,"symbol":sym,"side":"buy",
        "quantity":qty,"price":px,"fee":0,"tax":0,"trade_date":time.strftime("%Y-%m-%d")})
    print(f"  \033[92m✓ 买入 {sym} {qty:g} 股 @ {px:g} 成功\033[0m" if "error" not in r2 else f"  \033[91m失败: {r2['error']}\033[0m")

def do_sell():
    print(f"\n  {_BOLD}── 卖出 (可输序号, 回车=全仓现价) ──{_RST}")
    r = ask_code_qty_price(default_qty=True, default_px=True)
    if not r: return
    sym, qty, px = r
    r2 = api("POST", "/trades", {"account_id":1,"symbol":sym,"side":"sell",
        "quantity":qty,"price":px,"fee":0,"tax":0,"trade_date":time.strftime("%Y-%m-%d")})
    print(f"  \033[92m✓ 卖出 {sym} {qty:g} 股 @ {px:g} 成功\033[0m" if "error" not in r2 else f"  \033[91m失败: {r2['error']}\033[0m")

def do_cash(d):
    label = "入金" if d == "in" else "出金"
    print(f"\n  {_BOLD}── {label} ──{_RST}")
    amt = input("  金额: ").strip()
    if not amt: return
    try: amt=float(amt)
    except: print("  \033[91m必须是数字\033[0m"); return
    r = api("POST", "/cash-ledger", {"account_id":1,"direction":d,"amount":amt,
        "event_date":time.strftime("%Y-%m-%d")})
    print(f"  \033[92m✓ {label} 成功\033[0m" if "error" not in r else f"  \033[91m失败: {r['error']}\033[0m")

def do_add_watch():
    print(f"\n  {_BOLD}── 添加自选 ──{_RST}")
    codes = input("  代码 (逗号分隔): ").strip()
    if not codes: return
    wl = load_watch()
    added = []
    for c in codes.split(","):
        c = c.strip()
        if c and c not in wl:
            wl.append(c); added.append(c)
    if added:
        save_watch(wl); fetch_names(added)
        print(f"  \033[92m✓ 已添加: {', '.join(f'{c} {get_name(c)}' for c in added)}\033[0m")
    else:
        print(f"  无新增")

def do_del_watch():
    wl = load_watch()
    if not wl: print(f"  {_DIM}无自选{_RST}"); return
    print(f"\n  {_BOLD}── 删除自选 ──{_RST}")
    for i, c in enumerate(wl, 1): print(f"  {i}. {c} {get_name(c)}")
    ch = input("  序号: ").strip()
    if not ch: return
    try:
        idx = int(ch) - 1
        if 0 <= idx < len(wl):
            rm = wl.pop(idx); save_watch(wl)
            print(f"  \033[92m✓ 已删除 {rm}\033[0m")
    except: pass

def do_del_trade():
    print(f"\n  {_BOLD}── 删除错误交易 (最近30条) ──{_RST}")
    r = api("GET", "/trades?page_size=30")
    if "error" in r:
        print(f"  \033[91m✗ {r['error']}\033[0m"); return
    items = r.get("items", [])
    if not items:
        print(f"  {_DIM}无交易记录{_RST}"); return
    fetch_names([t.get("symbol","") for t in items])
    for i, t in enumerate(items, 1):
        side = "买" if t.get("side") == "buy" else "卖"
        print(f"  {i:>2}. id={t['id']:<4} {t.get('trade_date','')} {t.get('symbol','')} {get_name(t.get('symbol',''))} {side} {t.get('quantity',0):g}股 @ {t.get('price',0):g}")
    ch = input("  输入要删除的序号 (多选用逗号, 回车取消): ").strip()
    if not ch: return
    idxs = []
    try:
        for x in ch.split(","):
            i = int(x.strip()) - 1
            if 0 <= i < len(items): idxs.append(items[i]["id"])
    except: print("  \033[91m序号无效\033[0m"); return
    if not idxs: print("  无有效选择"); return
    ok = input(f"  \033[93m确认删除 {len(idxs)} 笔? 持仓将按 FIFO 自动重算 (y/N): \033[0m").strip().lower()
    if ok != "y": print("  已取消"); return
    for tid in idxs:
        r2 = api("DELETE", f"/trades/{tid}")
        if "error" in r2:
            print(f"  \033[91m✗ 删除 id={tid} 失败: {r2['error']}\033[0m")
        else:
            print(f"  \033[92m✓ 已删除 id={tid}, 持仓已重算\033[0m")

def do_history():
    print(f"\n  {_BOLD}── 交易记录 ──{_RST}")
    r = api("GET", "/trades?page_size=50")
    if "error" in r:
        print(f"  \033[91m✗ {r['error']}\033[0m"); return
    items = r.get("items", [])
    if not items:
        print(f"  {_DIM}无交易记录{_RST}"); return
    fetch_names([t.get("symbol","") for t in items])
    print(f"  {pad('日期',10)} {pad('代码',8)} {pad('名称',12)} {pad('方向',5)} {pad_r('数量',8)} {pad_r('价格',8)} {pad_r('金额',12)} {'备注'}")
    print(f"  {'-'*88}")
    for t in items:
        sym = t.get("symbol","")
        side = "买入" if t.get("side") == "buy" else "卖出"
        qty = t.get("quantity", 0)
        px = t.get("price", 0)
        amt = qty * px
        note = t.get("note", "") or ""
        date = t.get("trade_date", "")
        print(f"  {date:<10} {sym:<8} {pad(trunc(get_name(sym), 12), 12)} {pad(side, 5)} {qty:>8.0f} {fmt(px):>8} {fmt(amt):>12} {note[:18]}")
    print(f"  共 {len(items)} 条记录")

MENU = """
  ══════════ DSA 组合管理 ══════════
  1) 买入    2) 卖出
  3) 入金    4) 出金
  5) 加自选  6) 删自选
  7) 交易记录  8) 删错误交易
  0) 退出"""

def main():
    while True:
        os.system("clear" if os.name != "nt" else "cls")
        show(); print(MENU)
        try: ch = input("  > ").strip()
        except: print(); break
        if ch == "1": do_buy()
        elif ch == "2": do_sell()
        elif ch == "3": do_cash("in")
        elif ch == "4": do_cash("out")
        elif ch == "5": do_add_watch()
        elif ch == "6": do_del_watch()
        elif ch == "7": do_history()
        elif ch == "8": do_del_trade()
        elif ch == "0": break
        if ch != "0": input("\n  按回车继续...")

if __name__ == "__main__":
    main()
