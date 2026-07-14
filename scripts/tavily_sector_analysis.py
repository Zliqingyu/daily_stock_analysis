#!/usr/bin/env python3
"""
Tavily 板块趋势分析模块

使用 Tavily Search API 分析全球行业板块趋势
"""

import os
import json
import requests
from datetime import datetime
from typing import Optional


class TavilySectorAnalyzer:
    """Tavily 板块趋势分析器"""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("TAVILY_API_KEY")
        self.base_url = "https://api.tavily.com/search"
    
    def search_sector_trends(self, sector: str, max_results: int = 5) -> dict:
        """搜索板块趋势"""
        query = f"{sector} industry trend analysis 2026"
        
        payload = {
            "query": query,
            "max_results": max_results,
            "search_depth": "advanced",
            "include_answer": True,
            "include_raw_content": False,
        }
        
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        
        try:
            response = requests.post(self.base_url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"[Tavily] 搜索失败: {e}")
            return {"results": [], "answer": ""}
    
    def get_global_hot_sectors(self) -> list[dict]:
        """获取当前全球热门板块"""
        sectors = [
            "semiconductor",
            "artificial intelligence",
            "electric vehicle battery",
            "cloud computing",
            "biotech pharmaceutical",
            "renewable energy solar",
            "consumer electronics",
            "cybersecurity",
        ]
        
        results = []
        for sector in sectors:
            data = self.search_sector_trends(sector, max_results=3)
            if data.get("results"):
                results.append({
                    "sector": sector,
                    "trend": self._extract_trend(data),
                    "summary": data.get("answer", "")[:200],
                })
        
        return results
    
    def _extract_trend(self, data: dict) -> str:
        """从搜索结果提取趋势判断"""
        answer = data.get("answer", "").lower()
        
        bullish_keywords = ["bullish", "growth", "surge", "strong", "boom", "upward"]
        bearish_keywords = ["bearish", "decline", "downturn", "weak", "slump", "downward"]
        
        bullish_score = sum(1 for kw in bullish_keywords if kw in answer)
        bearish_score = sum(1 for kw in bearish_keywords if kw in answer)
        
        if bullish_score > bearish_score:
            return "bullish"
        elif bearish_score > bullish_score:
            return "bearish"
        return "neutral"
    
    def analyze_a_stock_sectors(self) -> dict:
        """分析 A股 相关板块（结合全球趋势）"""
        sectors_to_check = [
            "半导体芯片",
            "人工智能",
            "新能源电池",
            "消费电子",
            "医药生物",
        ]
        
        results = {}
        for sector in sectors_to_check:
            # 先搜索全球趋势
            global_data = self.search_sector_trends(sector, max_results=3)
            
            # 搜索 A股 相关信息
            cn_query = f"{sector} A股 板块 趋势"
            cn_data = self.search_sector_trends(cn_query, max_results=3)
            
            # 合并分析
            all_answers = []
            if global_data.get("answer"):
                all_answers.append(global_data["answer"])
            if cn_data.get("answer"):
                all_answers.append(cn_data["answer"])
            
            combined_answer = " ".join(all_answers)[:500]
            
            results[sector] = {
                "global_trend": self._extract_trend(global_data),
                "cn_trend": self._extract_trend(cn_data),
                "analysis": combined_answer,
            }
        
        return results


def run_daily_sector_analysis() -> dict:
    """每日板块分析（轻量级）"""
    analyzer = TavilySectorAnalyzer()
    
    print("[Tavily] 分析板块趋势...")
    sector_analysis = analyzer.analyze_a_stock_sectors()
    
    # 提取关键信息
    hot_sectors = []
    for sector, data in sector_analysis.items():
        if data["global_trend"] == "bullish" or data["cn_trend"] == "bullish":
            hot_sectors.append({
                "sector": sector,
                "trend": "看涨",
                "reason": data["analysis"][:200],
            })
    
    return {
        "timestamp": datetime.now().isoformat(),
        "sector_analysis": sector_analysis,
        "hot_sectors": hot_sectors,
    }


def run_weekly_sector_analysis() -> dict:
    """每周板块分析（详细版）"""
    analyzer = TavilySectorAnalyzer()
    
    print("[Tavily] 详细板块趋势分析...")
    
    # 全球热门板块
    global_hot = analyzer.get_global_hot_sectors()
    
    # A股 相关板块
    cn_sectors = analyzer.analyze_a_stock_sectors()
    
    # 美股/港股 板块趋势
    us_hk_sectors = {}
    for sector in ["tech", "healthcare", "finance", "energy"]:
        data = analyzer.search_sector_trends(f"{sector} stock market trend", max_results=3)
        us_hk_sectors[sector] = {
            "trend": analyzer._extract_trend(data),
            "analysis": data.get("answer", "")[:300],
        }
    
    return {
        "timestamp": datetime.now().isoformat(),
        "global_hot_sectors": global_hot,
        "cn_sectors": cn_sectors,
        "us_hk_sectors": us_hk_sectors,
    }


if __name__ == "__main__":
    # 测试
    result = run_daily_sector_analysis()
    print(json.dumps(result, ensure_ascii=False, indent=2))
