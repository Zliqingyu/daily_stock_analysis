#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
panel.py — DSA/ATS 实时终端面板 (纯标准库, 全数字键操作)

默认进入实时面板: 每 3 秒自动刷新
  DSA 持仓(事件重放) · 自选行情(并发拉取) · ATS A/B 权益盈亏
  [1]买入 [2]卖出 [3]入金 [4]出金 [5]加自选 [6]移除自选 [7]刷新 [0]退出
  选股/确认均用数字键完成

一次性命令 (非交互/脚本):
  panel.py overview|dsa|ats
  panel.py buy 600021 100 8.50 [费] [税] | sell ... | cashin 5000 | cashout 3000
  panel.py watch add 601328 | watch rm 601328
"""
import os
import sys
import re
import json
import hmac
import hashlib
import base64
import sqlite3
import urllib.request
import datetime
import threading
import select
import tty
import termios
import time
from concurrent.futures import ThreadPoolExecutor

HOME = os.path.expanduser("~")
DB = os.path.join(HOME, "dsa", "data", "stock_analysis.db")
DSA_ENV = os.path.join(HOME, "dsa", ".env")
INDEX_JSON = os.path.join(HOME, "dsa", "data", "cache", "stocks.index.json")
QUOTE_API = "http://127.0.0.1:9996/api/v1/stocks/{code}/quote"
ATS_ENGINES = [("DEMO-A", os.path.join(HOME, "ats", ".env"), os.path.join(HOME, "ats", "ats.log")),
               ("DEMO-B", os.path.join(HOME, "ats-b", ".env"), os.path.join(HOME, "ats-b", "ats.log"))]
NAME_CACHE = os.path.join(HOME, ".panel_names.json")
ACC = 1
REFRESH_SEC = 3

B, DIM, R = "\033[1m", "\033[2m", "\033[0m"
GREEN, RED, CYAN, YELLOW, INV = "\033[32m", "\033[31m", "\033[36m", "\033[33m", "\033[7m"

# ══════════════════════════════════════════════════════════════════
# 数据层
# ══════════════════════════════════════════════════════════════════
def env_parse(path):
    out = {}
    try:
        for line in open(path, encoding="utf-8"):
            m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
            if m:
                out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def unified_list(snap, prices, quotes):
    """与面板一致的统一编号顺序: 持仓(按市值降序) + 待买自选"""
    pos = sorted(snap["positions"].items(),
                 key=lambda kv: -(kv[1]["qty"] * (quotes.get(kv[0], {}).get("price")
                                                  or prices.get(kv[0]) or kv[1]["avg_cost"])))
    return [s for s, _ in pos], [c for c in watch_list() if c not in snap["positions"]]


def replay(con):
    """事件重放: 现金流水+交易 → FIFO 持仓/现金/已实现"""
    events = []
    for r in con.execute("SELECT id,event_date,direction,amount FROM portfolio_cash_ledger "
                         "WHERE account_id=?", (ACC,)):
        events.append((r[1], 0, r[0], "cash", r))
    for r in con.execute("SELECT id,trade_date,symbol,side,quantity,price,fee,tax "
                         "FROM portfolio_trades WHERE account_id=?", (ACC,)):
        events.append((r[1], 1, r[0], "trade", r))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    lots, cash, realized, fees = {}, 0.0, 0.0, 0.0
    for _, _, _, kind, r in events:
        if kind == "cash":
            cash += r[3] if r[2] == "in" else -r[3]
            continue
        _, _, symbol, side, qty, price, fee, tax = r
        q, fee = float(qty), float(fee or 0) + float(tax or 0)
        fees += fee
        book = lots.setdefault(symbol, [])
        if side == "buy":
            book.append([q, float(price)])
            cash -= q * float(price) + fee
        else:
            cash += q * float(price) - fee
            left = q
            while left > 1e-9 and book:
                take = min(left, book[0][0])
                realized += take * (float(price) - book[0][1])
                book[0][0] -= take
                left -= take
                if book[0][0] <= 1e-9:
                    book.pop(0)
    positions = {}
    for sym, book in lots.items():
        if book:
            tq = sum(l[0] for l in book)
            positions[sym] = {"qty": tq, "avg_cost": sum(l[0] * l[1] for l in book) / tq}
    return {"positions": positions, "cash": cash, "realized": realized, "fees": fees}


def cached_prices(con):
    return {r[0]: r[1] for r in con.execute(
        "SELECT symbol,last_price FROM portfolio_positions WHERE account_id=?", (ACC,))}


def invalidate_cache(con):
    con.execute("DELETE FROM portfolio_position_lots WHERE account_id=?", (ACC,))
    con.execute("DELETE FROM portfolio_positions WHERE account_id=?", (ACC,))
    con.commit()


def load_name_cache():
    try:
        return json.load(open(NAME_CACHE, encoding="utf-8"))
    except Exception:
        return {}


def db_names():
    """从 DSA 分析历史取每个代码的最新名称 (含基金, 数据库真实数据)"""
    try:
        con = sqlite3.connect(DB)
        rows = con.execute(
            "SELECT code, name FROM analysis_history t "
            "WHERE id = (SELECT MAX(id) FROM analysis_history WHERE code = t.code) "
            "AND name IS NOT NULL").fetchall()
        con.close()
        return {c: n.strip() for c, n in rows if n and n.strip()}
    except Exception:
        return {}


def watch_list():
    try:
        m = re.search(r"^STOCK_LIST=(.*)$", open(DSA_ENV).read(), re.M)
        return [s.strip() for s in m.group(1).split(",") if s.strip()]
    except OSError:
        return []


def watch_save(lst):
    txt = open(DSA_ENV).read()
    open(DSA_ENV, "w").write(re.sub(r"^STOCK_LIST=.*$", "STOCK_LIST=" + ",".join(lst),
                                    txt, flags=re.M))


def parse_engine_metrics(log_path):
    """从引擎日志最后一条 [METRICS] 行提取盈亏 (引擎自算, 含成本口径)"""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 12000))
            tail = f.read().decode("utf-8", errors="replace")
        line = [l for l in tail.splitlines() if "[METRICS]" in l][-1]
        out = {}
        for k in ("EQUITY", "REALIZED_PNL", "UNREALIZED_PNL", "DD", "TRADES", "WINS"):
            m = re.search(rf"{k}=([\d.eE+-]+)", line)
            if m:
                out[k] = float(m.group(1))
        out["time"] = line.split("METRICS]")[-1].split("|")[0].strip() if "METRICS]" in line else ""
        return out
    except Exception:
        return {}


def okx_get(env_path, path):
    env = env_parse(env_path)
    key, sec, pp = env.get("OKX_API_KEY"), env.get("OKX_SECRET_KEY"), env.get("OKX_PASSPHRASE")
    if not all((key, sec, pp)):
        raise RuntimeError("OKX 密钥未配置")
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    sig = base64.b64encode(
        hmac.new(sec.encode(), (ts + "GET" + path).encode(), hashlib.sha256).digest()).decode()
    req = urllib.request.Request("https://www.okx.com" + path, headers={
        "OK-ACCESS-KEY": key, "OK-ACCESS-SIGN": sig, "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": pp, "x-simulated-trading": "1",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) panel.py"})
    d = json.load(urllib.request.urlopen(req, timeout=15))
    if d.get("code") != "0":
        raise RuntimeError(f"OKX {d.get('code')}")
    return d


import unicodedata


def dwidth(s):
    """终端显示宽度: 中文等全角字符占 2 格"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in str(s))


def pad(s, width, right=False):
    """按显示宽度补空格 (先对齐, 不含颜色码)"""
    s = str(s)
    gap = width - dwidth(s)
    if gap <= 0:
        return s
    return " " * gap + s if right else s + " " * gap


def cut(s, width):
    """按显示宽度截断"""
    out, w = "", 0
    for c in str(s):
        cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if w + cw > width:
            break
        out += c
        w += cw
    return out


def cell_money(val, width):
    """数字: 先定宽再上色 (颜色码不占宽)"""
    if val is None:
        return pad("·", width, True)
    f = float(val)
    s = pad(f"{f:,.2f}", width, True)
    return (GREEN if f > 0 else RED if f < 0 else "") + s + (R if f else "")


def cell_pct(pct, width=8):
    if pct is None:
        return DIM + pad("···", width, True) + R
    s = pad(f"{float(pct):+.2f}%", width, True)
    return (GREEN if float(pct) >= 0 else RED) + s + R


def money(val):
    try:
        f = float(val)
    except (TypeError, ValueError):
        return "-"
    return f"{GREEN if f > 0 else RED if f < 0 else ''}{f:,.2f}{R}"


# 后台轮询: 自选行情 + ATS
# ══════════════════════════════════════════════════════════════════
class Feeder:
    def __init__(self):
        self.lock = threading.Lock()
        self.quotes = {}          # code -> dict(name,price,pct,ts)
        self.names = load_name_cache()   # 启动秒显: 持久化名称缓存
        self.ats = {}             # engine -> dict(ok,eq,det,pos,err)
        self.ats_ok_at = 0.0
        self.stop = threading.Event()
        threading.Thread(target=self._name_loader, daemon=True).start()
        threading.Thread(target=self._quote_loop, daemon=True).start()
        threading.Thread(target=self._ats_loop, daemon=True).start()

    def save_names(self):
        """只持久化关心的代码 (自选+持仓) 的名称"""
        try:
            con = sqlite3.connect(DB)
            held = {r[0] for r in con.execute(
                "SELECT DISTINCT symbol FROM portfolio_trades WHERE account_id=?", (ACC,))}
            con.close()
            keys = set(watch_list()) | held
            json.dump({k: v for k, v in self.names.items() if k in keys},
                      open(NAME_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _name_loader(self):
        try:
            data = json.load(open(INDEX_JSON, encoding="utf-8"))
            self.names.update({row[1]: row[2] for row in data if len(row) > 2})
        except Exception:
            pass
        self.names.update(db_names())   # 数据库里的基金/股票名优先
        self.save_names()

    def _fetch_quote(self, code):
        try:
            with urllib.request.urlopen(QUOTE_API.format(code=code), timeout=12) as r:
                d = json.load(r)
            with self.lock:
                self.quotes[code] = {"name": d.get("stock_name") or "",
                                     "price": d.get("current_price"),
                                     "pct": d.get("change_percent"),
                                     "ts": time.time()}
            nm = d.get("stock_name")
            if nm and self.names.get(code) != nm:
                self.names[code] = nm
                self.save_names()
        except Exception:
            pass

    def _quote_loop(self):
        ex = ThreadPoolExecutor(max_workers=10)
        while not self.stop.wait(2.0):
            for code in watch_list():
                if self.stop.is_set():
                    return
                try:
                    ex.submit(self._fetch_quote, code)
                except RuntimeError:  # 解释器退出中
                    return

    def _ats_once(self, name, env_path):
        try:
            b = okx_get(env_path, "/api/v5/account/balance")["data"][0]
            det = " | ".join(f"{x['ccy']} {float(x.get('eq') or 0):,.2f}"
                             for x in (b.get("details") or [])[:3])
            pos = okx_get(env_path, "/api/v5/account/positions")["data"]
            with self.lock:
                self.ats[name] = {"ok": True, "eq": float(b.get("totalEq") or 0),
                                  "det": det, "pos": pos, "err": ""}
        except Exception as e:
            with self.lock:
                self.ats.setdefault(name, {"ok": False, "eq": 0, "det": "", "pos": [], "err": str(e)[:60]})

    def _ats_loop(self):
        while not self.stop.is_set():
            for name, p, lg in ATS_ENGINES:
                self._ats_once(name, p)
            self.ats_ok_at = time.time()
            self.stop.wait(30.0)


# ══════════════════════════════════════════════════════════════════
# 写操作 (DSA)
# ══════════════════════════════════════════════════════════════════
def dsa_write_trade(symbol, side, qty, price, fee=0.0, tax=0.0,
                    trade_date=None, confirm=True):
    con = sqlite3.connect(DB)
    try:
        snap = replay(con)
        held = snap["positions"].get(symbol, {}).get("qty", 0.0)
        if side == "sell" and qty > held + 1e-9:
            return f"{RED}✗ 超卖: 持有 {held:,.0f}, 卖出 {qty:,.0f}{R}"
        if confirm:
            cn = "买入" if side == "buy" else "卖出"
            print(f"\n确认: {B}{cn} {symbol}{R} {qty:,.0f} × {price} = {qty * price:,.2f} CNY")
            if input("确认录入? [Y/n]: ").strip().lower() in ("n", "no"):
                return "已取消"
        con.execute(
            "INSERT INTO portfolio_trades (account_id,trade_uid,symbol,market,currency,"
            "trade_date,side,quantity,price,fee,tax,note,dedup_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
            (ACC, None, symbol, "cn", "CNY",
             trade_date or datetime.date.today().isoformat(),
             side, qty, price, fee, tax, "panel.py 终端录入", None))
        invalidate_cache(con)
        return f"{GREEN}✓ 已记录{R}"
    finally:
        con.close()


def dsa_write_cash(direction, amount, note=None):
    con = sqlite3.connect(DB)
    try:
        con.execute("INSERT INTO portfolio_cash_ledger "
                    "(account_id,event_date,direction,amount,currency,note,created_at) "
                    "VALUES (?,?,?,?,?,?,datetime('now','localtime'))",
                    (ACC, datetime.date.today().isoformat(), direction, amount, "CNY", note))
        invalidate_cache(con)
        return f"{GREEN}✓ 已记录 {direction} {amount:,.2f} CNY{R}"
    finally:
        con.close()


# ══════════════════════════════════════════════════════════════════
# 实时面板
# ══════════════════════════════════════════════════════════════════
KEYS = "1234567890"


class Panel:
    def __init__(self):
        self.feeder = Feeder()
        self.fd = sys.stdin.fileno()
        self.old_term = None

    # ---- 终端模式 ----
    def cbreak_on(self):
        self.old_term = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)

    def cbreak_off(self):
        if self.old_term:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_term)
            self.old_term = None

    def ask(self, prompt, default=""):
        """行模式交互输入 (临时恢复终端)"""
        self.cbreak_off()
        try:
            s = input(f"{prompt}" + (f"{DIM}[{default}]{R}: " if default != "" else ": ")).strip()
            return s or default
        finally:
            self.cbreak_on()

    def ask_key(self, prompt, valid):
        """单键数字选择 (cbreak 模式)"""
        sys.stdout.write(f"\n{prompt} {DIM}[{'/'.join(valid)}]{R}: ")
        sys.stdout.flush()
        while True:
            ch = sys.stdin.read(1)
            if ch in valid:
                sys.stdout.write(ch + "\n")
                return ch
            if ch in ("\x03", "\x1b", "q", "Q", "\r", "\n"):
                sys.stdout.write("取消\n")
                return None

    # ---- 渲染 ----
    def render(self):
        con = sqlite3.connect(DB)
        try:
            snap, prices = replay(con), cached_prices(con)
        finally:
            con.close()
        wl = watch_list()
        q = dict(self.feeder.quotes)
        L = ["\033[H\033[J"]
        now = datetime.datetime.now().strftime("%H:%M:%S")

        # ── DSA: 汇总置顶 + 股票连续列表 (持仓+待买自选统一编号) ──
        pos_syms, wl = unified_list(snap, prices, q)
        mv = upl = 0.0
        px_of = {}
        for sym in pos_syms:
            px = (q.get(sym) or {}).get("price") or prices.get(sym)
            if px:
                mv += snap["positions"][sym]["qty"] * px
                upl += (px - snap["positions"][sym]["avg_cost"]) * snap["positions"][sym]["qty"]
            px_of[sym] = px
        L.append(f"现金 {money(snap['cash'])}   市值 {money(mv)}   "
                 f"{B}总权益 {money(snap['cash'] + mv)}{R}   {DIM}已实现 {money(snap['realized'])}{R}")
        L.append("─" * 82)
        hdr = " ".join([pad("No", 2, True), pad("代码", 7), pad("名称", 16),
                        pad("现价", 7, True), pad("涨跌%", 7, True),
                        pad("数量", 7, True), pad("市值", 10, True), pad("浮动盈亏", 10, True)])
        L.append(hdr)
        for i, sym in enumerate(pos_syms + wl):
            no = i + 1
            qt = q.get(sym) or {}
            name = qt.get("name") or self.feeder.names.get(sym, "")
            if sym in px_of:
                px, pct = px_of[sym], qt.get("pct")
                p = snap["positions"][sym]
                u = (px - p["avg_cost"]) * p["qty"] if px else None
                qty_c = pad(f"{p['qty']:,.0f}", 7, True)
                mv_c = pad(f"{(p['qty'] * px if px else 0):,.1f}", 10, True)
            else:
                px = qt.get("price") or prices.get(sym)
                pct = qt.get("pct")
                qty_c, mv_c = pad("·", 7, True), pad("·", 10, True)
                u = None
            row = " ".join([pad(no, 2, True), pad(sym, 7), pad(cut(name, 14), 16),
                             pad(f"{(px or 0):.3f}", 7, True), cell_pct(pct, 7),
                             qty_c, mv_c, cell_money(u, 10)])
            L.append(row)
        L.append("─" * 82)

        # ── ATS ──
        L.append("")
        L.append(f"{B}🤖 ATS (OKX 模拟盘){R}")
        with self.feeder.lock:
            ats = {k: dict(v) for k, v in self.feeder.ats.items()}
        for name, _, log in ATS_ENGINES:
            a = ats.get(name)
            met = parse_engine_metrics(log)
            if not a:
                L.append(f" {name}: {DIM}加载中…{R}")
            elif a["ok"]:
                L.append(f" {name} OKX {B}{a['eq']:,.2f} USDT{R}  {DIM}{a['det']}{R}")
                if met:
                    L.append(f"   {DIM}引擎{R} 权益 {met.get('EQUITY', 0):,.2f}  "
                             f"浮盈 {money(met.get('UNREALIZED_PNL'))}  "
                             f"已实现 {money(met.get('REALIZED_PNL'))}  "
                             f"DD {met.get('DD', 0):.2f}%  交易 {int(met.get('TRADES', 0))}次")
                for p in a["pos"]:
                    u, rt = float(p.get("upl") or 0), float(p.get("uplRatio") or 0) * 100
                    L.append(f"   {p['instId']:<13}{p.get('posSide') or ''} "
                             f"{float(p['pos']):,.0f}张 均价{float(p.get('avgPx') or 0):.4g} "
                             f"浮盈{money(u)} ({rt:+.2f}%)")
            else:
                L.append(f" {name}: {RED}✗ {a['err']}{R}")

        # ── 操作栏 ──
        L.append("")
        L.append(f"{CYAN}[1]{R}买入 {CYAN}[2]{R}卖出 {CYAN}[3]{R}入金 {CYAN}[4]{R}出金 "
                 f"{CYAN}[5]{R}加自选 {CYAN}[6]{R}移除自选 {CYAN}[7]{R}刷新 "
                 f"{CYAN}[8]{R}刷新ATS {CYAN}[0]{R}退出  {DIM}{now}{R}")
        sys.stdout.write("\n".join(L) + "\n")
        sys.stdout.flush()

    # ---- 交互 ----
    def pick_stock(self, side="buy"):
        """直接输面板编号选股 (不再重复列清单)"""
        con = sqlite3.connect(DB)
        try:
            snap = replay(con)
            prices = cached_prices(con)
        finally:
            con.close()
        pos_syms, wl = unified_list(snap, prices, dict(self.feeder.quotes))
        cands = pos_syms + wl
        title = "买入(含加仓)" if side == "buy" else "卖出"
        while True:
            s = self.ask(f"{title} 编号 (0=手动, 回车取消)").strip()
            if s == "":
                return None
            if s == "0":
                code = self.ask("代码")
                return code if re.fullmatch(r"\d{6}", code or "") else None
            if s.isdigit() and 1 <= int(s) <= len(cands):
                return cands[int(s) - 1]
            print(f"✗ 编号无效 (1-{len(cands)})")

    def trade_flow(self, side):
        symbol = self.pick_stock(side)
        if not symbol:
            return
        try:
            qty = float(self.ask("数量"))
            px = float(self.ask("价格"))
            fee = float(self.ask("手续费", "0") or 0)
            tax = float(self.ask("印花税", "0") or 0)
        except ValueError:
            print("✗ 数字无效")
            return
        cn = "买入" if side == "buy" else "卖出"
        print(f"{B}{cn} {symbol}{R} {qty:,.0f} × {px} = {qty * px:,.2f} CNY")
        if self.ask_key("确认? (1确认 0取消)", "01") == "1":
            print(dsa_write_trade(symbol, side, qty, px, fee, tax, confirm=False))
        else:
            print("已取消")
        time.sleep(1.2)

    def cash_flow(self, direction):
        try:
            amt = float(self.ask("金额 (CNY)"))
        except ValueError:
            return
        note = self.ask("备注", "") or None
        cn = "入金" if direction == "in" else "出金"
        print(f"{cn} {amt:,.2f} CNY")
        if self.ask_key("确认? (1确认 0取消)", "01") == "1":
            print(dsa_write_cash(direction, amt, note))
        time.sleep(1.2)

    def watch_add(self):
        code = self.ask("加自选 代码")
        if re.fullmatch(r"\d{6}", code or ""):
            wl = watch_list()
            if code not in wl:
                wl.append(code)
                watch_save(wl)
            if code not in self.feeder.names:
                nm = db_names().get(code)      # 新代码: 从数据库拉名称入缓存
                if nm:
                    self.feeder.names[code] = nm
            self.feeder.save_names()
            print(f"✓ {code} {self.feeder.names.get(code, '')} 在自选 (共 {len(wl)})")
        time.sleep(1)

    def watch_rm(self):
        code = self.pick_stock("buy")
        if code and code in watch_list():
            wl = watch_list()
            wl.remove(code)
            watch_save(wl)
            self.feeder.names.pop(code, None)  # 删自选 → 删名称缓存
            self.feeder.save_names()
            print(f"✓ {code} 已移除")
        time.sleep(1)

    def run(self):
        self.cbreak_on()
        try:
            while True:
                self.render()
                r, _, _ = select.select([sys.stdin], [], [], REFRESH_SEC)
                if not r:
                    continue
                k = sys.stdin.read(1)
                if k in ("0", "\x03") or (k == "\x1b" and sys.stdin.read(2) == ""):
                    return
                elif k == "\x1b":        # 忽略方向键等转义序列
                    sys.stdin.read(2)
                elif k == "1":
                    self.trade_flow("buy")
                elif k == "2":
                    self.trade_flow("sell")
                elif k == "3":
                    self.cash_flow("in")
                elif k == "4":
                    self.cash_flow("out")
                elif k == "5":
                    self.watch_add()
                elif k == "6":
                    self.watch_rm()
                elif k == "7":
                    pass            # 立即重绘
                elif k == "8":
                    for name, p, lg in ATS_ENGINES:
                        self.feeder._ats_once(name, p)
        finally:
            self.cbreak_off()


# ══════════════════════════════════════════════════════════════════
# 一次性命令 / 入口
# ══════════════════════════════════════════════════════════════════
def one_shot_dsa():
    con = sqlite3.connect(DB)
    try:
        snap, prices = replay(con), cached_prices(con)
    finally:
        con.close()
    print(f"\n{B}💼 DSA · 手动主账户{R} {DIM}(CNY · FIFO){R}")
    for sym, p in sorted(snap["positions"].items()):
        px = prices.get(sym)
        u = (px - p["avg_cost"]) * p["qty"] if px else None
        print(f"{sym:<8}{p['qty']:>9,.0f}{p['avg_cost']:>8.3f}{(px or 0):>8.3f}"
              f"{money(u):>12}")
    print(f"现金 {money(snap['cash'])}  已实现 {money(snap['realized'])}")


def one_shot_ats():
    for name, p, lg in ATS_ENGINES:
        try:
            b = okx_get(p, "/api/v5/account/balance")["data"][0]
            det = " | ".join(f"{x['ccy']} {float(x.get('eq') or 0):,.2f}"
                             for x in (b.get("details") or [])[:4])
            print(f"{B}{name}{R}: {float(b.get('totalEq') or 0):,.2f} USDT  {DIM}{det}{R}")
            for x in okx_get(p, "/api/v5/account/positions")["data"]:
                u = float(x.get("upl") or 0)
                print(f"  {x['instId']} {x.get('posSide')} {float(x['pos']):,.0f}张 "
                      f"浮盈{money(u)} ({float(x.get('uplRatio') or 0)*100:+.2f}%)")
        except Exception as e:
            print(f"{name}: {RED}✗ {e}{R}")


def main():
    argv = sys.argv[1:]
    if not argv:
        if sys.stdin.isatty():
            Panel().run()          # 实时面板
        else:
            one_shot_dsa(); one_shot_ats()
        return
    cmd, args = argv[0], argv[1:]
    if cmd in ("overview", "o"):
        one_shot_dsa(); one_shot_ats()
    elif cmd in ("buy", "b"):
        print(dsa_write_trade(args[0], "buy", float(args[1]), float(args[2]),
                              float(args[3]) if len(args) > 3 else 0,
                              float(args[4]) if len(args) > 4 else 0))
    elif cmd in ("sell", "s"):
        print(dsa_write_trade(args[0], "sell", float(args[1]), float(args[2]),
                              float(args[3]) if len(args) > 3 else 0,
                              float(args[4]) if len(args) > 4 else 0))
    elif cmd in ("cashin", "ci"):
        print(dsa_write_cash("in", float(args[0]), args[1] if len(args) > 1 else None))
    elif cmd in ("cashout", "co"):
        print(dsa_write_cash("out", float(args[0]), args[1] if len(args) > 1 else None))
    elif cmd == "watch":
        wl = watch_list()
        if len(args) >= 2 and args[0] == "add" and re.fullmatch(r"\d{6}", args[1]) \
                and args[1] not in wl:
            wl.append(args[1]); watch_save(wl); print(f"✓ (共 {len(wl)})")
        elif len(args) >= 2 and args[0] == "rm" and args[1] in wl:
            wl.remove(args[1]); watch_save(wl); print(f"✓ (共 {len(wl)})")
        else:
            print(" ".join(wl))
    elif cmd == "ats":
        one_shot_ats()
    elif cmd == "dsa":
        one_shot_dsa()
    else:
        print(__doc__)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    try:
        main()
    except KeyboardInterrupt:
        print()
