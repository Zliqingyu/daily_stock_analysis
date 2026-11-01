#!/usr/bin/env python3
"""
港股/美股 国际选股模块

全市场搜索，不依赖预设股票库，搜索低价潜力股。
使用 DSA 主系统的 SearchService（多引擎：Tavily / Brave / Anspire / Bocha）。
"""
import json
import re
from typing import Optional


# 常见美股的名称/行业对照（用于给搜到的代码补全名称，非筛选条件）
_COMMON_HK = {
    "00700": "腾讯控股", "09988": "阿里巴巴-SW", "03690": "美团-W",
    "00005": "汇丰控股", "01299": "友邦保险", "00941": "中国移动",
    "01211": "比亚迪股份", "02318": "中国平安", "03968": "招商银行",
    "01810": "小米集团-W", "09633": "农夫山泉", "02020": "安踏体育",
}
_COMMON_US = {
    "AAPL": "Apple", "MSFT": "Microsoft", "GOOGL": "Alphabet",
    "AMZN": "Amazon", "META": "Meta", "NVDA": "NVIDIA", "TSLA": "Tesla",
    "JNJ": "Johnson & Johnson", "PG": "Procter & Gamble", "KO": "Coca-Cola",
    "WMT": "Walmart", "V": "Visa", "JPM": "JPMorgan", "BAC": "Bank of America",
    "PFE": "Pfizer", "DIS": "Disney", "MCD": "McDonald's", "NKE": "Nike",
}

# 英文中不太可能是美股代码的常见词（大写时也不像 ticker）
_US_STOP_WORDS = {
    "THE", "AND", "FOR", "ARE", "NOT", "BUT", "ALL", "ANY", "CAN", "HAS",
    "HAD", "HOW", "ITS", "MAY", "NEW", "NOW", "ONE", "OUT", "PER", "SEE",
    "VIA", "WAS", "WILL", "YET", "YOU", "INC", "LTD", "LLC", "CORP", "CO",
    "ETF", "IPO", "CEO", "CFO", "CTO", "USD", "HKD", "CNY", "NYSE", "HKEX",
    "NASDAQ", "S&P", "DJI", "SPX", "EPS", "PE", "PB", "ROE", "ROI",
    "BUY", "SELL", "HOLD", "LONG", "SHORT", "BULL", "BEAR", "HIGH", "LOW",
    "OPEN", "CLOSE", "THIS", "THAT", "EACH", "SOME", "MORE", "MOST",
    "FUND", "BOND", "STOCK", "SHARE", "HOLD", "DEBT", "CASH",
    "YEAR", "MONTH", "WEEK", "DAYS", "TODAY", "LAST",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
    "ABOVE", "AFTER", "AGAIN", "ALSO", "BACK", "BEEN",
    "BEST", "BOTH", "COME", "DOES", "DOWN", "EVEN",
    "FIRST", "FROM", "GOOD", "HAVE", "INTO", "JUST",
    "KNOW", "LIKE", "LOOK", "MADE", "MAKE", "MUCH",
    "NEED", "NEXT", "ONCE", "ONLY", "OVER", "PAID",
    "PART", "PAST", "PLAN", "RATE", "SAID", "SAME",
    "SALE", "SAVE", "SHOW", "TAKE", "THAN", "THEN",
    "TIME", "UNDER", "UPON", "VERY", "WHEN", "WITH",
    "WORK", "YOUR", "ZONE",
}


def _resolve_hk_name(code: str) -> str:
    """通过搜索解析港股名称"""
    try:
        import requests
        resp = requests.get(f"https://finance.yahoo.com/quote/{code}.HK", timeout=5)
        if resp.status_code == 200:
            m = re.search(r'<h1[^>]*data-test="qsp-header"[^>]*>(.*?)</h1>', resp.text, re.DOTALL)
            if m:
                name = re.sub(r'\s*\(.*?\)\s*$', '', m.group(1)).strip()
                if name:
                    return name
    except Exception:
        pass
    return _COMMON_HK.get(code, f"港股{code}")


def _resolve_us_name(code: str) -> str:
    """通过搜索解析美股名称"""
    try:
        import requests
        resp = requests.get(f"https://finance.yahoo.com/quote/{code}", timeout=5)
        if resp.status_code == 200:
            m = re.search(r'<h1[^>]*data-test="qsp-header"[^>]*>(.*?)</h1>', resp.text, re.DOTALL)
            if m:
                name = re.sub(r'\s*\(.*?\)\s*$', '', m.group(1)).strip()
                if name:
                    return name
    except Exception:
        pass
    return _COMMON_US.get(code, code)


class InternationalStockScreener:
    """全市场港股/美股低价潜力股搜索器"""

    def __init__(self):
        self._search_fn = None
        try:
            from src.search_service import get_search_service
            svc = get_search_service()
            if svc.is_available:
                self._search_fn = svc
                print("[国际选股] 搜索引擎就绪")
            else:
                print("[国际选股] ⚠️ 无可用搜索引擎")
        except Exception as e:
            print(f"[国际选股] ⚠️ 初始化搜索服务失败: {e}")

    def _search(self, query: str, max_results: int = 5) -> list[str]:
        """多引擎搜索，返回文本片段列表"""
        if not self._search_fn:
            return []
        try:
            resp = self._search_fn.search(query, max_results=max_results)
            if resp.success:
                return [r.snippet for r in resp.results]
        except Exception as e:
            print(f"[国际选股] 搜索失败: {e}")
        return []

    def _extract_hk_codes(self, text: str) -> list[str]:
        """从文本中提取所有港股代码"""
        codes = set()
        for m in re.finditer(r'(?:\$)?(?:HK)?(\d{4,5})\.?HK?\b', text, re.IGNORECASE):
            code = m.group(1)
            if code.isdigit():
                n = int(code)
                if 1 <= n <= 99999 and code not in {"HSI", "16500", "16600", "800000", "800100"}:
                    codes.add(code.zfill(5))
        return sorted(codes)

    def _extract_us_codes(self, text: str) -> list[str]:
        """从文本中提取美股代码，过滤常见英文单词"""
        codes = set()
        # 模式1：$代码（如 $AAPL）
        for m in re.finditer(r'\$([A-Z]{1,5})\b', text):
            c = m.group(1)
            if c not in _US_STOP_WORDS:
                codes.add(c)
        # 模式2：带交易所前缀（NASDAQ:AAPL, NYSE:JPM）
        for m in re.finditer(r'(?:NASDAQ|NYSE|AMEX):([A-Z]{1,5})\b', text, re.IGNORECASE):
            codes.add(m.group(1).upper())
        # 模式3：3-5位大写字母 + 附近有股票上下文关键词
        for m in re.finditer(r'\b([A-Z]{2,5})\b', text):
            c = m.group(1)
            if c in _US_STOP_WORDS or c in _COMMON_HK:
                continue
            if len(c) <= 2 and c not in {"V", "GOOGL"}:
                continue
            # 检查附近是否有股票上下文
            start = max(0, m.start() - 40)
            end = min(len(text), m.end() + 40)
            context = text[start:end].lower()
            stock_signals = ["stock", "share", "price", "ticker", "nasdaq", "nyse",
                             "eps", "pe ", "buoy", "bear", "bull", "dividend",
                             "earnings", "revenue", "up ", "down "]
            if any(sig in context for sig in stock_signals):
                codes.add(c)
        return sorted(codes)

    def screen_hk_stocks(self) -> list[dict]:
        """全市场搜索港股低价潜力股"""
        queries = [
            "港股 5元以下 高成长 小市值 2026 买入推荐",
            "Hong Kong small cap stocks 2026 undervalued under 10 HKD",
            "港股 低价潜力股 2026 推荐投资",
            "港股 推荐 小盘 2026 投资目标价",
        ]
        return self._search_and_parse(queries, self._extract_hk_codes, _resolve_hk_name, max_results=2, label="港股")

    def screen_us_stocks(self) -> list[dict]:
        """全市场搜索美股低价潜力股"""
        queries = [
            "US undervalued small cap growth stocks 2026 under $20 buy",
            "美股 低价 成长股 潜力 小盘 2026 买入",
            "best cheap stocks to buy now 2026 under $10 high growth",
            "small cap stocks 2026 analysts recommend target price",
        ]
        return self._search_and_parse(queries, self._extract_us_codes, _resolve_us_name, max_results=2, label="美股")

    def _search_and_parse(self, queries: list[str], extract_fn, name_fn,
                          max_results: int = 3, label: str = "") -> list[dict]:
        """通用搜索+解析 pipeline"""
        candidates: dict[str, int] = {}  # code -> hit count
        for query in queries:
            snippets = self._search(query, max_results=8)  # 多搜点，提高命中
            for text in snippets:
                codes = extract_fn(text)
                for code in codes:
                    candidates[code] = candidates.get(code, 0) + 1

        # 按出现频率排序
        ranked = sorted(candidates.items(), key=lambda x: -x[1])
        results = []
        for code, hits in ranked:
            results.append({
                "code": code,
                "name": name_fn(code),
                "reason": f"搜索匹配 {hits} 次，来自 {label} 低价/潜力股搜索",
            })
            if len(results) >= max_results:
                break

        print(f"[{label}筛选] 找到 {len(results)} 只候选股")
        return results

    def get_hk_us_recommendations(self, max_price_rmb: float = 1000) -> dict:
        """获取港股/美股推荐"""
        hk_stocks = self.screen_hk_stocks()
        us_stocks = self.screen_us_stocks()

        hk_pick = hk_stocks[0] if hk_stocks else {
            "code": "N/A", "name": "暂无推荐", "sector": "",
            "reason": "搜索无结果，建议稍后重试或检查搜索引擎配置",
        }
        us_pick = us_stocks[0] if us_stocks else {
            "code": "N/A", "name": "暂无推荐", "sector": "",
            "reason": "搜索无结果，建议稍后重试或检查搜索引擎配置",
        }

        if not hk_stocks:
            print("[国际选股] ⚠️ 港股筛选无结果")
        if not us_stocks:
            print("[国际选股] ⚠️ 美股筛选无结果")

        return {"hk_recommendation": hk_pick, "us_recommendation": us_pick}


if __name__ == "__main__":
    screener = InternationalStockScreener()
    results = screener.get_hk_us_recommendations()
    print(json.dumps(results, ensure_ascii=False, indent=2))
