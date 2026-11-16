#!/usr/bin/env python3
"""按代码解析证券名称 — 多源回退 + 永久缓存 (2026-11-16)

源顺序(首个成功即停, 后续源自动跳过):
  1. 本地缓存 ~/.dsa_name_cache.json (与 scripts/portfolio.py 共用同一文件,
     任何工具找对一次, 所有工具永久记住)
  2. 腾讯实时行情 qt.gtimg.cn (场内股票/LOF/ETF, 批量免费)
  3. 天天基金全市场名单 fundcode_search.js (含场外 LOF/C 份额, 进程内缓存)

后续新源(如同花顺 iFinD)在 _from_* 系列与 resolve_names 的回退序列中注册即可。"""
import json
import os
import re
import urllib.request

CACHE_FILE = os.path.expanduser("~/.dsa_name_cache.json")
_UA = {"User-Agent": "Mozilla/5.0"}
_fund_list_cache = None


def _load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


def _prefix(code):
    return "sh" if code and code[0] in "569" else "sz"


def _from_tencent(codes):
    out = {}
    full = ",".join(_prefix(c) + c for c in codes)
    req = urllib.request.Request(f"https://qt.gtimg.cn/q={full}", headers=_UA)
    for line in urllib.request.urlopen(req, timeout=8).read().decode("gbk").strip().split(";"):
        if "~" not in line:
            continue
        p = line.split("~")
        if len(p) > 2 and p[1]:
            out[p[2]] = p[1]
    return out


def fund_list():
    """全市场基金名单 [(code, pinyin, name, type, ...), ...], 进程内缓存"""
    global _fund_list_cache
    if _fund_list_cache is None:
        req = urllib.request.Request(
            "https://fund.eastmoney.com/js/fundcode_search.js", headers=_UA)
        t = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
        _fund_list_cache = json.loads(re.search(r"\[.*\]", t, re.S).group(0))
    return _fund_list_cache


def _from_eastmoney(codes):
    table = {x[0]: x[2] for x in fund_list() if len(x) > 2 and x[2]}
    return {c: table[c] for c in codes if c in table and table[c]}


def resolve_names(codes):
    """批量解析名称; 返回 {code: name}。成功的写进永久缓存, 以后不再查询。"""
    codes = [c for c in dict.fromkeys(codes) if c]
    cache = _load_cache()
    out = {c: cache[c] for c in codes if cache.get(c)}
    todo = [c for c in codes if c not in out]
    for source in (_from_tencent, _from_eastmoney):
        if not todo:
            break
        try:
            got = source(todo)
        except Exception:
            continue
        for c, n in got.items():
            if n and c not in out:
                out[c] = n
                cache[c] = n
        todo = [c for c in codes if c not in out]
    _save_cache(cache)
    return out
