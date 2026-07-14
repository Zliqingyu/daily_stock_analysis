#!/usr/bin/env python3
"""
港股/美股 国际选股模块

筛选长期稳健的港股和美股，要求一手交易价格控制在 1000 人民币左右
"""

import os
import json
import requests
from datetime import datetime
from typing import Optional


class InternationalStockScreener:
    """港股/美股 选股器"""
    
    def __init__(self, tavily_api_key: Optional[str] = None):
        self.tavily_api_key = tavily_api_key or os.getenv("TAVILY_API_KEY")
        self.tavily_url = "https://api.tavily.com/search"
    
    def _tavily_search(self, query: str, max_results: int = 5) -> dict:
        """Tavily 搜索"""
        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": "advanced",
            "include_answer": True,
        }
        
        headers = {
            "Authorization": f"Bearer {self.tavily_api_key}",
            "Content-Type": "application/json",
        }
        
        try:
            response = requests.post(self.tavily_url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"[Tavily] 搜索失败: {e}")
            return {"results": [], "answer": ""}
    
    def screen_hk_stocks(self, max_price_rmb: float = 1000) -> list[dict]:
        """筛选港股稳健股
        
        一手价格控制在 max_price_rmb 人民币左右（约 1100 港币）
        """
        # 搜索港股蓝筹股、高股息股
        queries = [
            "Hong Kong stock blue chip stable dividend 2026",
            "港股 蓝筹 高股息 稳健 2026",
            "HK stock low lot price stable performance",
        ]
        
        candidates = []
        for query in queries:
            data = self._tavily_search(query, max_results=5)
            for result in data.get("results", []):
                content = result.get("content", "")
                title = result.get("title", "")
                
                # 解析可能的股票信息
                stock_info = self._parse_hk_stock(title, content)
                if stock_info and stock_info.get("lot_price_hkd", 0) <= 1100:
                    candidates.append(stock_info)
        
        # 去重
        seen = set()
        unique = []
        for s in candidates:
            code = s.get("code", "")
            if code and code not in seen:
                seen.add(code)
                unique.append(s)
        
        print(f"[港股筛选] 找到 {len(unique)} 只候选股")
        return unique[:3]
    
    def screen_us_stocks(self, max_price_rmb: float = 1000) -> list[dict]:
        """筛选美股稳健股
        
        一手价格控制在 max_price_rmb 人民币左右（约 140 美元）
        """
        # 搜索美股优质股
        queries = [
            "US stock stable growth dividend aristocrat 2026",
            "美股 优质 长期 稳健 蓝筹 2026",
            "US stock low price stable performance long term",
        ]
        
        candidates = []
        for query in queries:
            data = self._tavily_search(query, max_results=5)
            for result in data.get("results", []):
                content = result.get("content", "")
                title = result.get("title", "")
                
                stock_info = self._parse_us_stock(title, content)
                if stock_info and stock_info.get("price_usd", 0) <= 140:
                    candidates.append(stock_info)
        
        # 去重
        seen = set()
        unique = []
        for s in candidates:
            code = s.get("code", "")
            if code and code not in seen:
                seen.add(code)
                unique.append(s)
        
        print(f"[美股筛选] 找到 {len(unique)} 只候选股")
        return unique[:3]
    
    def _parse_hk_stock(self, title: str, content: str) -> Optional[dict]:
        """解析港股信息"""
        import re
        
        # 尝试匹配港股代码（5位数字）
        code_match = re.search(r'(\d{5})\.HK', title + content)
        if not code_match:
            # 常见港股代码
            common_hk = {
                "00700": {"name": "腾讯控股", "lot_price_hkd": 350, "sector": "科技"},
                "09988": {"name": "阿里巴巴-SW", "lot_price_hkd": 80, "sector": "科技"},
                "00005": {"name": "汇丰控股", "lot_price_hkd": 600, "sector": "金融"},
                "00001": {"name": "长和", "lot_price_hkd": 400, "sector": "综合"},
                "00027": {"name": "银河娱乐", "lot_price_hkd": 450, "sector": "博彩"},
                "01211": {"name": "比亚迪股份", "lot_price_hkd": 250, "sector": "新能源汽车"},
                "02020": {"name": "安踏体育", "lot_price_hkd": 80, "sector": "消费"},
                "00941": {"name": "中国移动", "lot_price_hkd": 700, "sector": "电信"},
                "03690": {"name": "美团-W", "lot_price_hkd": 150, "sector": "科技"},
            }
            
            # 从内容中查找股票名称
            for code, info in common_hk.items():
                if info["name"] in title or info["name"] in content:
                    return {
                        "code": code,
                        "name": info["name"],
                        "lot_price_hkd": info["lot_price_hkd"],
                        "lot_price_rmb": info["lot_price_hkd"] * 0.9,
                        "sector": info["sector"],
                        "reason": f"蓝筹股，一手约 {info['lot_price_hkd']} 港币",
                    }
            return None
        
        code = code_match.group(1)
        
        # 提取价格
        price_match = re.search(r'HK\$[\s]*(\d+\.?\d*)', title + content)
        lot_price = float(price_match.group(1)) if price_match else 500
        
        return {
            "code": f"{code}.HK",
            "name": title.split("-")[0].strip()[:20],
            "lot_price_hkd": lot_price,
            "lot_price_rmb": lot_price * 0.9,
            "sector": "未分类",
            "reason": content[:100],
        }
    
    def _parse_us_stock(self, title: str, content: str) -> Optional[dict]:
        """解析美股信息"""
        import re
        
        # 尝试匹配美股代码
        common_us = {
            "AAPL": {"name": "Apple", "price_usd": 180, "sector": "科技"},
            "MSFT": {"name": "Microsoft", "price_usd": 350, "sector": "科技"},
            "GOOGL": {"name": "Alphabet", "price_usd": 140, "sector": "科技"},
            "AMZN": {"name": "Amazon", "price_usd": 170, "sector": "科技"},
            "TSLA": {"name": "Tesla", "price_usd": 180, "sector": "新能源汽车"},
            "JNJ": {"name": "Johnson & Johnson", "price_usd": 160, "sector": "医药"},
            "PG": {"name": "Procter & Gamble", "price_usd": 160, "sector": "消费"},
            "KO": {"name": "Coca-Cola", "price_usd": 60, "sector": "消费"},
            "PFE": {"name": "Pfizer", "price_usd": 30, "sector": "医药"},
            "BAC": {"name": "Bank of America", "price_usd": 35, "sector": "金融"},
            "V": {"name": "Visa", "price_usd": 280, "sector": "金融"},
            "DIS": {"name": "Walt Disney", "price_usd": 110, "sector": "媒体"},
        }
        
        # 从内容中查找股票名称
        for code, info in common_us.items():
            if info["name"].lower() in title.lower() or info["name"].lower() in content.lower():
                return {
                    "code": code,
                    "name": info["name"],
                    "price_usd": info["price_usd"],
                    "price_rmb": info["price_usd"] * 7.2,
                    "sector": info["sector"],
                    "reason": f"优质蓝筹股，一股约 ${info['price_usd']} 美元",
                }
        
        return None
    
    def get_hk_us_recommendations(self, max_price_rmb: float = 1000) -> dict:
        """获取港股/美股推荐"""
        hk_stocks = self.screen_hk_stocks(max_price_rmb)
        us_stocks = self.screen_us_stocks(max_price_rmb)
        
        # 选择最佳推荐
        hk_pick = hk_stocks[0] if hk_stocks else {
            "code": "00700.HK",
            "name": "腾讯控股",
            "lot_price_hkd": 350,
            "lot_price_rmb": 315,
            "sector": "科技",
            "reason": "港股科技龙头，业绩稳定",
        }
        
        us_pick = us_stocks[0] if us_stocks else {
            "code": "AAPL",
            "name": "Apple",
            "price_usd": 180,
            "price_rmb": 1296,
            "sector": "科技",
            "reason": "全球科技龙头，现金流充裕",
        }
        
        return {
            "hk_recommendation": hk_pick,
            "us_recommendation": us_pick,
        }


if __name__ == "__main__":
    screener = InternationalStockScreener()
    results = screener.get_hk_us_recommendations()
    print(json.dumps(results, ensure_ascii=False, indent=2))
