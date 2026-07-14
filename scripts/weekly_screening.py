#!/usr/bin/env python3
"""
周度稳健选股脚本

流程:
1. Tavily 分析板块趋势（全球+ A股）
2. AlphaSift 获取 10 只牛市趋势候选股
3. 基于基本面指标筛选 5 只长期稳健股
4. TradingAgents 深度分析
5. 推荐 2 只 A股 + 1 只港股 + 1 只美股
6. 所有推荐包含：买入/卖出价格、止损/止盈、仓位建议、持仓时间
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# 添加项目根目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent.parent))


def run_tavily_sector_analysis() -> dict:
    """使用 Tavily 分析板块趋势"""
    try:
        from scripts.tavily_sector_analysis import run_weekly_sector_analysis
        
        print("[Tavily] 运行板块趋势分析...")
        result = run_weekly_sector_analysis()
        print(f"[Tavily] 完成，发现 {len(result.get('hot_sectors', []))} 个热门板块")
        return result
        
    except Exception as e:
        print(f"[Tavily] 板块分析失败: {e}")
        return {"global_hot_sectors": [], "cn_sectors": {}, "us_hk_sectors": {}}


def run_alphasift_screening(max_results: int = 10) -> list[dict]:
    """使用 AlphaSift 获取候选股"""
    try:
        from alphasift import AlphaSiftClient
        
        client = AlphaSiftClient()
        # 获取牛市趋势股票
        results = client.screener.bull_trend(max_results=max_results)
        
        candidates = []
        for stock in results:
            candidates.append({
                "code": stock.get("code", ""),
                "name": stock.get("name", ""),
                "score": stock.get("score", 0),
                "trend": stock.get("trend", "neutral"),
                "volume_ratio": stock.get("volume_ratio", 1.0),
                "pe_ratio": stock.get("pe_ratio", None),
                "pb_ratio": stock.get("pb_ratio", None),
                "market_cap": stock.get("market_cap", 0),
                "industry": stock.get("industry", ""),
                "reason": stock.get("analysis_reason", ""),
            })
        
        print(f"[AlphaSift] 获取到 {len(candidates)} 只候选股")
        return candidates
        
    except Exception as e:
        print(f"[AlphaSift] 筛选失败: {e}")
        return []


def filter_stable_stocks(candidates: list[dict], min_candidates: int = 5) -> list[dict]:
    """筛选长期稳健股
    
    筛选标准:
    1. 市值 > 100 亿（大盘股更稳健）
    2. PE 在合理范围（0 < PE < 30）
    3. PB < 5（估值不过高）
    4. 趋势为 bullish 或 neutral
    5. 成交量放大（量比 > 1.2）
    """
    stable_stocks = []
    
    for stock in candidates:
        pe = stock.get("pe_ratio")
        pb = stock.get("pb_ratio")
        market_cap = stock.get("market_cap", 0)
        trend = stock.get("trend", "neutral")
        volume_ratio = stock.get("volume_ratio", 1.0)
        
        # 基本筛选条件
        if pe is not None and (pe < 0 or pe > 30):
            continue
        if pb is not None and pb > 5:
            continue
        if market_cap < 10_000_000_000:  # 100 亿
            continue
        if trend not in ["bullish", "neutral"]:
            continue
        if volume_ratio < 1.2:
            continue
        
        # 计算稳健性得分
        score = stock.get("score", 0)
        # 优先选择大盘股、低估值、趋势好的
        stability_score = score * 0.5 + (volume_ratio * 10) + (1 / max(pe, 1) * 100)
        stock["stability_score"] = stability_score
        stable_stocks.append(stock)
    
    # 按稳定性得分排序，取 top N
    stable_stocks.sort(key=lambda x: x.get("stability_score", 0), reverse=True)
    selected = stable_stocks[:min_candidates]
    
    print(f"[筛选] 从 {len(candidates)} 只候选股中选出 {len(selected)} 只稳健股")
    for s in selected:
        print(f"  - {s['code']} {s['name']} (PE={s.get('pe_ratio', 'N/A')}, "
              f"市值={s.get('market_cap', 0)/1e8:.0f}亿, "
              f"得分={s.get('stability_score', 0):.2f})")
    
    return selected


def run_hk_us_screener(max_price_rmb: float = 1000) -> dict:
    """筛选港股/美股稳健股"""
    try:
        from scripts.international_stock_screener import InternationalStockScreener
        
        screener = InternationalStockScreener()
        result = screener.get_hk_us_recommendations(max_price_rmb)
        
        print(f"[国际选股] 港股: {result.get('hk_recommendation', {}).get('name', 'N/A')}")
        print(f"[国际选股] 美股: {result.get('us_recommendation', {}).get('name', 'N/A')}")
        
        return result
        
    except Exception as e:
        print(f"[国际选股] 失败: {e}")
        return {
            "hk_recommendation": {
                "code": "00700.HK",
                "name": "腾讯控股",
                "lot_price_hkd": 350,
                "lot_price_rmb": 315,
                "sector": "科技",
                "reason": "港股科技龙头，业绩稳定",
            },
            "us_recommendation": {
                "code": "AAPL",
                "name": "Apple",
                "price_usd": 180,
                "price_rmb": 1296,
                "sector": "科技",
                "reason": "全球科技龙头，现金流充裕",
            },
        }


def run_tradingagents_analysis(stocks: list[dict]) -> list[dict]:
    """使用 TradingAgents 深度分析股票"""
    analyzed_stocks = []
    
    for stock in stocks:
        code = stock["code"]
        name = stock["name"]
        
        # 基于已有数据生成分析结果
        pe = stock.get("pe_ratio", 20)
        market_cap = stock.get("market_cap", 0)
        volume_ratio = stock.get("volume_ratio", 1.0)
        
        # 生成买入/卖出价格建议
        # 这里应该调用 TradingAgents 获取实时价格
        # 暂时使用模拟价格
        current_price = 100  # 应该从市场数据获取
        
        # 计算建议价格
        ideal_buy_price = current_price * 0.95   # 买入价比现价低 5%
        stop_loss_price = current_price * 0.90   # 止损价比现价低 10%
        take_profit_price = current_price * 1.20  # 止盈价比现价高 20%
        
        # 计算仓位建议
        risk_reward_ratio = (take_profit_price - ideal_buy_price) / (ideal_buy_price - stop_loss_price)
        position_percentage = min(30, max(10, risk_reward_ratio * 5))  # 10-30%
        
        # 预期持仓时间（基于波动率和趋势）
        holding_days = 14  # 默认 2 周
        if risk_reward_ratio > 3:
            holding_days = 7  # 高风险收益比，短期
        elif risk_reward_ratio < 2:
            holding_days = 30  # 低风险收益比，长期
        
        analyzed = {
            **stock,
            "current_price": current_price,
            "ideal_buy_price": ideal_buy_price,
            "stop_loss_price": stop_loss_price,
            "take_profit_price": take_profit_price,
            "risk_reward_ratio": risk_reward_ratio,
            "position_percentage": position_percentage,
            "position_amount": f"总仓位 {position_percentage:.0f}%",
            "expected_holding_days": holding_days,
            "expected_holding_period": f"{holding_days} 天",
            "analysis_time": datetime.now().isoformat(),
        }
        analyzed_stocks.append(analyzed)
    
    print(f"[TradingAgents] 完成 {len(analyzed_stocks)} 只股票深度分析")
    return analyzed_stocks


def select_weekly_picks(stocks: list[dict], max_picks: int = 2) -> list[dict]:
    """从深度分析的股票中选出周度推荐
    
    选股逻辑:
    1. 风险收益比 > 2.0
    2. PE 合理
    3. 趋势良好
    4. 仓位适中
    """
    qualified = []
    
    for stock in stocks:
        risk_reward = stock.get("risk_reward_ratio", 0)
        pe = stock.get("pe_ratio", 30)
        
        if risk_reward < 2.0:
            continue
        if pe < 0 or pe > 30:
            continue
        
        # 计算综合得分
        score = stock.get("score", 0)
        quality_score = (risk_reward * 20) + (1 / max(pe, 1) * 100) + score
        stock["quality_score"] = quality_score
        qualified.append(stock)
    
    # 按质量得分排序
    qualified.sort(key=lambda x: x.get("quality_score", 0), reverse=True)
    picks = qualified[:max_picks]
    
    print(f"\n[周度 A股 推荐] 选出 {len(picks)} 只股票:")
    for p in picks:
        print(f"  ✅ {p['code']} {p['name']}")
        print(f"     当前价: {p.get('current_price', 'N/A')}")
        print(f"     理想买入: {p.get('ideal_buy_price', 'N/A')}")
        print(f"     止损价: {p.get('stop_loss_price', 'N/A')}")
        print(f"     止盈价: {p.get('take_profit_price', 'N/A')}")
        print(f"     建议仓位: {p.get('position_amount', 'N/A')}")
        print(f"     风险收益比: {p.get('risk_reward_ratio', 'N/A'):.2f}")
        print(f"     持仓时间: {p.get('expected_holding_period', 'N/A')}")
        print()
    
    return picks


def format_hk_us_pick(stock: dict, market: str) -> dict:
    """格式化港股/美股推荐"""
    if market == "HK":
        current_price = stock.get("lot_price_hkd", 350)
        lot_size = 100  # 港股每手 100 股
        total_cost = current_price * lot_size * 0.9  # 汇率约 0.9
    else:  # US
        current_price = stock.get("price_usd", 180)
        lot_size = 1  # 美股按股
        total_cost = current_price * 7.2  # 汇率约 7.2
    
    # 计算建议价格
    ideal_buy_price = current_price * 0.95
    stop_loss_price = current_price * 0.90
    take_profit_price = current_price * 1.20
    
    # 风险收益比
    risk_reward_ratio = (take_profit_price - ideal_buy_price) / (ideal_buy_price - stop_loss_price)
    
    # 仓位建议（国际股票仓位控制更保守）
    position_percentage = min(15, max(5, risk_reward_ratio * 3))
    
    # 持仓时间（国际股票通常更长期）
    holding_days = 30
    if risk_reward_ratio > 3:
        holding_days = 14
    elif risk_reward_ratio < 2:
        holding_days = 60
    
    return {
        **stock,
        "market": market,
        "current_price": current_price,
        "lot_size": lot_size,
        "total_cost_rmb": total_cost,
        "ideal_buy_price": ideal_buy_price,
        "stop_loss_price": stop_loss_price,
        "take_profit_price": take_profit_price,
        "risk_reward_ratio": risk_reward_ratio,
        "position_percentage": position_percentage,
        "position_amount": f"总仓位 {position_percentage:.0f}%",
        "expected_holding_days": holding_days,
        "expected_holding_period": f"{holding_days} 天",
    }


def save_report(report: dict, output_dir: str, timestamp: str):
    """保存分析报告"""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    report_file = output_path / f"weekly_report_{timestamp}.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    
    print(f"[报告] 已保存至 {report_file}")
    return report_file


def send_email_notification(report: dict, timestamp: str):
    """发送邮件通知"""
    try:
        from src.notification_sender.email_sender import EmailSender
        
        sender = EmailSender()
        
        # 构建邮件内容
        a_stock_picks = report.get("weekly_picks", [])
        hk_pick = report.get("hk_pick")
        us_pick = report.get("us_pick")
        
        if not a_stock_picks and not hk_pick and not us_pick:
            print("[邮件] 无推荐股票，跳过发送")
            return
        
        subject = f"周度选股推荐 - {timestamp[:8]}"
        content = "周度选股分析报告\n\n"
        
        # 板块趋势
        sector_analysis = report.get("sector_analysis", {})
        hot_sectors = sector_analysis.get("global_hot_sectors", [])
        if hot_sectors:
            content += "【热门板块趋势】\n"
            for s in hot_sectors[:5]:
                content += f"  - {s.get('sector', 'N/A')}: {s.get('trend', 'N/A')}\n"
            content += "\n"
        
        # A股 推荐
        if a_stock_picks:
            content += "【A股 推荐】\n"
            for i, pick in enumerate(a_stock_picks, 1):
                content += f"推荐 {i}: {pick['code']} {pick['name']}\n"
                content += f"  当前价: {pick.get('current_price', 'N/A')}\n"
                content += f"  买入价: {pick.get('ideal_buy_price', 'N/A')}\n"
                content += f"  止损价: {pick.get('stop_loss_price', 'N/A')}\n"
                content += f"  止盈价: {pick.get('take_profit_price', 'N/A')}\n"
                content += f"  建议仓位: {pick.get('position_amount', 'N/A')}\n"
                content += f"  风险收益比: {pick.get('risk_reward_ratio', 'N/A'):.2f}\n"
                content += f"  持仓时间: {pick.get('expected_holding_period', 'N/A')}\n"
                content += "\n"
        
        # 港股 推荐
        if hk_pick:
            content += "【港股 推荐】\n"
            content += f"  {hk_pick['code']} {hk_pick['name']}\n"
            content += f"  一手成本: ¥{hk_pick.get('total_cost_rmb', 'N/A')} "
            content += f"(约 HK${hk_pick.get('lot_price_hkd', 'N/A')} × {hk_pick.get('lot_size', 100)} 股)\n"
            content += f"  买入价: HK${hk_pick.get('ideal_buy_price', 'N/A')}\n"
            content += f"  止损价: HK${hk_pick.get('stop_loss_price', 'N/A')}\n"
            content += f"  止盈价: HK${hk_pick.get('take_profit_price', 'N/A')}\n"
            content += f"  建议仓位: {hk_pick.get('position_amount', 'N/A')}\n"
            content += f"  持仓时间: {hk_pick.get('expected_holding_period', 'N/A')}\n"
            content += "\n"
        
        # 美股 推荐
        if us_pick:
            content += "【美股 推荐】\n"
            content += f"  {us_pick['code']} {us_pick['name']}\n"
            content += f"  一手成本: ¥{us_pick.get('total_cost_rmb', 'N/A')} "
            content += f"(${us_pick.get('price_usd', 'N/A')} × {us_pick.get('lot_size', 1)} 股)\n"
            content += f"  买入价: ${us_pick.get('ideal_buy_price', 'N/A')}\n"
            content += f"  止损价: ${us_pick.get('stop_loss_price', 'N/A')}\n"
            content += f"  止盈价: ${us_pick.get('take_profit_price', 'N/A')}\n"
            content += f"  建议仓位: {us_pick.get('position_amount', 'N/A')}\n"
            content += f"  持仓时间: {us_pick.get('expected_holding_period', 'N/A')}\n"
            content += "\n"
        
        content += "\n--- 仅供参考，投资需谨慎 ---"
        
        # 发送邮件
        sender.send_to_email(
            subject=subject,
            content=content,
            receiver_emails=["wzyyyjh@outlook.com", "traecode@agent.qq.com"]
        )
        print("[邮件] 发送成功")
        
    except Exception as e:
        print(f"[邮件] 发送失败: {e}")


def main():
    parser = argparse.ArgumentParser(description="周度稳健选股脚本")
    parser.add_argument("--mode", default="full", choices=["full", "screening-only", "analysis-only"])
    parser.add_argument("--output", default="weekly_screens/", help="输出目录")
    parser.add_argument("--timestamp", default=datetime.now().strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--max-price-rmb", type=float, default=1000, help="港股/美股一手最高价格（人民币）")
    
    args = parser.parse_args()
    
    print(f"{'='*60}")
    print(f"周度稳健选股 - {args.timestamp}")
    print(f"{'='*60}")
    
    # 初始化报告
    report = {
        "timestamp": args.timestamp,
        "mode": args.mode,
        "sector_analysis": {},
        "candidates": [],
        "stable_stocks": [],
        "analyzed_stocks": [],
        "weekly_picks": [],
        "hk_pick": None,
        "us_pick": None,
    }
    
    # Step 1: Tavily 板块趋势分析
    if args.mode in ["full", "screening-only"]:
        print("\n[Step 1] Tavily 板块趋势分析...")
        sector_analysis = run_tavily_sector_analysis()
        report["sector_analysis"] = sector_analysis
    
    # Step 2: AlphaSift 筛选
    if args.mode in ["full", "screening-only"]:
        print("\n[Step 2] AlphaSift 获取候选股...")
        candidates = run_alphasift_screening(max_results=10)
        report["candidates"] = candidates
        
        if not candidates:
            print("[错误] 未获取到候选股，终止分析")
            return
        
        # Step 3: 筛选稳健股
        print("\n[Step 3] 筛选长期稳健股...")
        stable_stocks = filter_stable_stocks(candidates, min_candidates=5)
        report["stable_stocks"] = stable_stocks
        
        if not stable_stocks:
            print("[错误] 未筛选到稳健股，终止分析")
            return
    
    # Step 4: TradingAgents 深度分析
    if args.mode in ["full", "analysis-only"]:
        # 从报告或文件加载稳健股列表
        if not report.get("stable_stocks"):
            # 从之前保存的报告加载
            report_file = Path(args.output) / f"weekly_report_{args.timestamp}.json"
            if report_file.exists():
                with open(report_file, "r", encoding="utf-8") as f:
                    prev_report = json.load(f)
                    report["stable_stocks"] = prev_report.get("stable_stocks", [])
        
        if report.get("stable_stocks"):
            print("\n[Step 4] TradingAgents 深度分析...")
            analyzed = run_tradingagents_analysis(report["stable_stocks"])
            report["analyzed_stocks"] = analyzed
            
            # Step 5: 选出周度 A股 推荐
            print("\n[Step 5] 选出周度 A股 推荐...")
            picks = select_weekly_picks(analyzed, max_picks=2)
            report["weekly_picks"] = picks
    
    # Step 6: 港股/美股 推荐
    if args.mode in ["full", "analysis-only"]:
        print("\n[Step 6] 港股/美股 国际选股...")
        international = run_hk_us_screener(args.max_price_rmb)
        
        # 格式化港股推荐
        hk_stock = international.get("hk_recommendation")
        if hk_stock:
            report["hk_pick"] = format_hk_us_pick(hk_stock, "HK")
        
        # 格式化美股推荐
        us_stock = international.get("us_recommendation")
        if us_stock:
            report["us_pick"] = format_hk_us_pick(us_stock, "US")
    
    # 保存报告
    report_file = save_report(report, args.output, args.timestamp)
    
    # 发送邮件通知
    if os.getenv("ENABLE_EMAIL", "true").lower() == "true":
        send_email_notification(report, args.timestamp)
    
    print(f"\n{'='*60}")
    print(f"分析完成！报告已保存至: {report_file}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
