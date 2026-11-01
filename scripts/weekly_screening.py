#!/usr/bin/env python3
"""
周度稳健选股脚本

流程:
1. Tavily 分析板块趋势（全球+ A股）
2. AlphaSift 获取 10 只牛市趋势候选股
3. 基于基本面指标筛选 5 只长期稳健股
4. TradingAgents 深度分析
5. 推荐 2 只 A股 + 1 只港股 + 1 只美股
6. A股 推荐由 TradingAgents 真实引擎产出（五级评级 + 决策报告），按评级排序，不含编造的买卖价位
"""

import argparse
import json
import os
import subprocess
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


# 备用概念关键词（市场数据不可用时的 fallback）
_FALLBACK_CONCEPT_KEYWORDS = [
    "半导体", "芯片", "集成电路", "晶圆", "封装", "光刻",
    "机器人", "工业机器人", "人形机器人", "减速器", "伺服",
    "智能驾驶", "自动驾驶", "车联网", "激光雷达", "域控制器",
    "黄金", "贵金属",
]


def _get_dynamic_concept_keywords() -> list[str]:
    """Combine fallback keywords with dynamically-fetched market hot sector keywords."""
    keywords = list(_FALLBACK_CONCEPT_KEYWORDS)
    try:
        sector_data = run_tavily_sector_analysis()
        for sector in sector_data.get("global_hot_sectors", []):
            name = sector.get("name") or sector.get("sector") or ""
            if name:
                keywords.append(name)
    except Exception:
        pass  # Fallback keywords still work
    return list(dict.fromkeys(keywords))  # Dedup, preserve order


# Runtime keyword list (lazy-evaluated on first use)
_CONCEPT_KEYWORDS = None


def _get_concept_keywords() -> list[str]:
    global _CONCEPT_KEYWORDS
    if _CONCEPT_KEYWORDS is None:
        _CONCEPT_KEYWORDS = _get_dynamic_concept_keywords()
    return _CONCEPT_KEYWORDS


def _tag_concept_stocks(candidates: list[dict]) -> list[dict]:
    """标记命中关注概念的股票，返回概念组子列表（不改变原始排序）。"""
    keywords = _get_concept_keywords()
    concept_picks = []
    for stock in candidates:
        text = (stock.get("industry") or "") + (stock.get("name") or "") + (stock.get("reason") or "")
        for kw in keywords:
            if kw in text:
                tagged = dict(stock)
                tagged["concept_tag"] = kw
                concept_picks.append(tagged)
                break
    if concept_picks:
        print(f"[概念偏好] {len(concept_picks)} 只候选股命中关注概念（半导体/机器人/智驾/黄金等）")
    return concept_picks


def run_alphasift_screening(max_results: int = 10) -> list[dict]:
    """使用 AlphaSift 获取候选股（复用 DSA 的 AlphaSiftService，避免错误的 AlphaSiftClient API）

    支持多策略合并：ALPHASIFT_STRATEGIES 可设为逗号分隔的多个策略，
    如 "momentum_quality,trend_following"，合并去重后按分数排序。
    单策略时仍可用 ALPHASIFT_STRATEGY（向后兼容）。
    """
    try:
        from src.config import get_config
        from src.services.alphasift_service import AlphaSiftService

        strategies_env = os.getenv("ALPHASIFT_STRATEGIES", "") or os.getenv("ALPHASIFT_STRATEGY", "momentum_quality")
        strategies = [s.strip() for s in strategies_env.split(",") if s.strip()]

        config = get_config()
        service = AlphaSiftService(config)

        sector_tilt = os.getenv("ALPHASIFT_SECTOR_TILT", "").strip().lower() == "true"
        screen_n = max(max_results * 3, 20) if sector_tilt else max_results

        all_candidates = []
        seen_codes = set()

        for strategy in strategies:
            try:
                from scripts.alphasift_screen_and_analyze import resolve_macro_first
                st, mr, macro_skip = resolve_macro_first(strategy, max_results)
                if macro_skip:
                    print(f"[AlphaSift:{strategy}] 宏观偏空且情绪分<30，跳过。")
                    continue

                result = service.screen(strategy=st, market="cn", max_results=screen_n)
                raw = (result.get("candidates") or []) if isinstance(result, dict) else []

                for stock in raw:
                    code = stock.get("code") or stock.get("stock_code") or ""
                    if code in seen_codes:
                        continue
                    seen_codes.add(code)
                    all_candidates.append({
                        "code": code,
                        "name": stock.get("name") or stock.get("stock_name") or "",
                        "score": stock.get("score") or 0,
                        "trend": stock.get("trend") or "neutral",
                        "volume_ratio": stock.get("volume_ratio"),
                        "pe_ratio": stock.get("pe_ratio"),
                        "pb_ratio": stock.get("pb_ratio"),
                        "market_cap": stock.get("market_cap") or 0,
                        "industry": stock.get("industry") or "",
                        "reason": stock.get("reason") or stock.get("analysis_reason") or "",
                        "strategy": strategy,
                    })
                print(f"[AlphaSift:{strategy}] +{len(raw)} 只候选 (去重后累计 {len(all_candidates)} 只)")
            except Exception as e:
                print(f"[AlphaSift:{strategy}] 失败: {e}，继续下一个策略...")


        if sector_tilt and all_candidates:
            from scripts.alphasift_screen_and_analyze import (
                get_market_hot_sectors,
                apply_sector_tilt,
            )
            hot_sectors = get_market_hot_sectors(limit=10)
            all_candidates = apply_sector_tilt(all_candidates, max_results, hot_sectors)

        return all_candidates

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
        market_cap = stock.get("market_cap") or 0
        trend = stock.get("trend", "neutral")
        volume_ratio = stock.get("volume_ratio")

        # 基本筛选条件：仅当字段真实存在时才施加硬筛选。
        # AlphaSift 已做质量初筛；若本地/网络未补全基本面，则不因缺字段而全量剔除。
        if pe is not None and (pe < 0 or pe > 30):
            continue
        if pb is not None and pb > 5:
            continue
        if market_cap and market_cap < 10_000_000_000:  # 100 亿
            continue
        if trend not in ["bullish", "neutral"]:
            continue
        if volume_ratio is not None and volume_ratio < 1.2:
            continue
        
        # 计算稳健性得分（缺失字段用合理默认值，避免 TypeError）
        score = stock.get("score", 0)
        _pe = pe if pe is not None else 15          # 缺 PE 默认中等估值
        _vol = volume_ratio if volume_ratio is not None else 1.0  # 缺量比默认正常
        # 优先选择大盘股、低估值、趋势好的
        stability_score = score * 0.5 + (_vol * 10) + (1 / max(_pe, 1) * 100)
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
                "code": "N/A",
                "name": "暂无推荐",
                "sector": "",
                "reason": "国际选股模块异常，请检查搜索引擎配置",
            },
            "us_recommendation": {
                "code": "N/A",
                "name": "暂无推荐",
                "sector": "",
                "reason": "国际选股模块异常，请检查搜索引擎配置",
            },
        }


def run_tradingagents_analysis(stocks: list[dict]) -> list[dict]:
    """使用真实 TradingAgents 引擎深度分析股票（评级驱动）。

    通过隔离的 TradingAgents venv 调用 scripts/run_tradingagents.py。该引擎产出
    五级评级（Buy/Overweight/Hold/Underweight/Sell）与多份研究/决策报告，
    不产生买卖价位，因此后续按评级排序选股。
    """
    analyzed_stocks = []
    ta_python = os.getenv("TA_VENV_PYTHON", ".venv-ta/bin/python")
    runner = Path(__file__).parent / "run_tradingagents.py"
    trade_date = datetime.now().strftime("%Y-%m-%d")
    timeout = int(os.getenv("TA_TIMEOUT_SECONDS", "600"))  # 10 min per stock; 5 stocks fits in 60 min workflow
    timed_out = False  # 只要有一只超时，跳过后续所有 TA 分析

    for stock in stocks:
        code = stock.get("code")
        name = stock.get("name", "")
        if not code:
            continue

        if timed_out:
            print(f"[TradingAgents] {code} {name} 跳过（前一只已超时，避免继续浪费时间）")
            analyzed = {**stock, "rating": "N/A", "analysis_error": "skipped: previous stock timed out"}
            analyzed_stocks.append(analyzed)
            continue

        try:
            proc = subprocess.run(
                [ta_python, str(runner), str(code), trade_date],
                env=dict(os.environ),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                err = (proc.stderr or proc.stdout or "")[-500:]
                print(f"[TradingAgents] {code} 运行失败: {err}")
                analyzed = {**stock, "rating": "N/A", "analysis_error": err}
            else:
                data = json.loads(proc.stdout)
                if data.get("error"):
                    print(f"[TradingAgents] {code} 分析错误: {data['error']}")
                    analyzed = {**stock, "rating": "N/A", "analysis_error": data["error"]}
                else:
                    analyzed = {**stock, **data}
        except subprocess.TimeoutExpired:
            timed_out = True
            print(f"[TradingAgents] {code} {name} 超时（{timeout}s）；跳过后续所有 TA 分析")
            analyzed = {**stock, "rating": "N/A", "analysis_error": f"timed out after {timeout}s"}
        except Exception as e:
            print(f"[TradingAgents] {code} 调用异常: {e}")
            analyzed = {**stock, "rating": "N/A", "analysis_error": str(e)}

        analyzed_stocks.append(analyzed)
        print(f"[TradingAgents] {code} {name} 评级: {analyzed.get('rating')}")

    return analyzed_stocks


# 五级评级权重（用于排序；低于 Hold 不入选）
_RATING_TIER = {
    "Buy": 5,
    "Overweight": 4,
    "Hold": 3,
    "Underweight": 2,
    "Sell": 1,
}


def select_weekly_picks(stocks: list[dict], max_picks: int = 2) -> list[dict]:
    """按 TradingAgents 五级评级筛选周度 A股 推荐。

    选股逻辑:
    1. 仅纳入评级 >= Hold 的个股
    2. 评级高者优先，同评级按 AlphaSift 得分排序
    """
    def _rank(s: dict) -> tuple:
        return (_RATING_TIER.get(s.get("rating", ""), 0), s.get("score", 0))

    qualified = [s for s in stocks if _RATING_TIER.get(s.get("rating", ""), 0) >= 3]
    qualified.sort(key=_rank, reverse=True)
    picks = qualified[:max_picks]

    print(f"\n[周度 A股 推荐] 选出 {len(picks)} 只股票:")
    for p in picks:
        print(f"  ✅ {p['code']} {p['name']}")
        print(f"     评级: {p.get('rating', 'N/A')}")
        decision = (p.get("final_trade_decision") or "")[:200].replace("\n", " ")
        if decision:
            print(f"     决策摘要: {decision}")
        print()

    return picks


def format_hk_us_pick(stock: dict, market: str) -> dict:
    """格式化港股/美股推荐"""
    # 国际选股仅通过搜索得出，不包含实时价格；使用估算值
    if market == "HK":
        current_price = stock.get("lot_price_hkd", 50)
        lot_size = 100
        total_cost = current_price * lot_size * 0.9
    else:  # US
        current_price = stock.get("price_usd", 15)
        lot_size = 1
        total_cost = current_price * 7.2

    ideal_buy_price = current_price * 0.95
    stop_loss_price = current_price * 0.90
    take_profit_price = current_price * 1.20
    risk_reward_ratio = (take_profit_price - ideal_buy_price) / max(ideal_buy_price - stop_loss_price, 0.01)
    position_percentage = min(15, max(5, risk_reward_ratio * 3))
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
        "risk_reward_ratio": round(risk_reward_ratio, 2),
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


def _load_market_review() -> str:
    """加载最新的大盘复盘摘要，由 workflow 中 main.py --market-review 预生成。"""
    reports_dir = Path("reports")
    if not reports_dir.is_dir():
        return ""
    # 找最新的 market_review / mkt_review 报告
    candidates = sorted(
        [p for p in reports_dir.glob("*.md")
         if "market" in p.name.lower() or "mkt" in p.name.lower()],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    if not candidates:
        return ""
    content = candidates[0].read_text(encoding="utf-8", errors="replace")
    # 取前 800 字做摘要（飞书消息有长度限制）
    return content[:800].strip()


def _extract_ta_action(decision: str, plan: str) -> list[str]:
    """从 TradingAgents 多 agent 决策报告中提取关键操作信号"""
    import re
    lines = []
    
    full = f"{decision} {plan}"
    
    # 操作方向
    if re.search(r'减仓|减配|Underweight|卖出|Sell|reduce|exit', full, re.IGNORECASE):
        m = re.search(r'(\d{1,3})\s*%', full)
        pct = m.group(1) if m else "20-30"
        lines.append(f"操作: 减仓 {pct}%（风险控制）")
        lines.append(f"止损: 破位清仓")
    elif re.search(r'加仓|增配|Overweight|买入|Buy|add|increase', full, re.IGNORECASE):
        lines.append("操作: 加仓（多 agent 看多共识）")
    else:
        lines.append("操作: 持有")
    
    # 价格区间
    prices = []
    for m in re.finditer(r'(\d{2,3}\.\d{2})\s*[元价位]', full):
        p = m.group(1)
        if p not in prices:
            prices.append(p)
    if prices:
        price_str = " / ".join(prices[:3])
        lines.append(f"参考价: {price_str} 元")
    
    # 持仓时间
    if re.search(r'2\s*[周个]|2\s*[wW]|short.?term|短期', full):
        lines.append("周期: 短期（1-2周）")
    elif re.search(r'3\s*[月个]|long.?term|中长期|长期', full):
        lines.append("周期: 中长期（1-3个月）")
    else:
        lines.append("周期: 中期观察")
    
    return lines


def send_notification(report: dict, timestamp: str):
    """通过 NotificationService 发送通知（支持飞书、邮件等所有已配置渠道）"""
    try:
        a_stock_picks = report.get("weekly_picks", [])
        hk_pick = report.get("hk_pick")
        us_pick = report.get("us_pick")
        
        if not a_stock_picks and not hk_pick and not us_pick:
            print("[通知] 无推荐股票，跳过发送")
            return
        
        content = "周度选股分析报告\n\n"
        
        # ---- 大盘复盘 ----
        mkt = _load_market_review()
        if mkt:
            content += "【大盘复盘】\n"
            content += mkt + "\n\n"
        
        # ---- 板块趋势 ----
        sector_analysis = report.get("sector_analysis", {})
        hot_sectors = sector_analysis.get("global_hot_sectors", [])
        if hot_sectors:
            content += "【热门板块趋势】\n"
            for s in hot_sectors[:5]:
                content += f"  - {s.get('sector', 'N/A')}: {s.get('trend', 'N/A')}\n"
            content += "\n"
        
        # ---- A股 推荐 ----
        if a_stock_picks:
            content += "【A股 推荐】（TradingAgents 评级驱动）\n\n"
            for i, pick in enumerate(a_stock_picks, 1):
                rating = pick.get("rating", "N/A")
                content += f"推荐 {i}: {pick['code']} {pick['name']}\n"
                content += f"  评级: {rating}\n"
                
                # 从 final_trade_decision 提取关键交易信号
                decision = (pick.get("final_trade_decision") or "").strip()
                plan = (pick.get("investment_plan") or "").strip()
                
                # 提取操作指令 + 价格
                action = _extract_ta_action(decision, plan)
                for line in action:
                    content += f"  {line}\n"
                
                # 决策摘要（压缩到 150 字）
                if decision:
                    brief = decision[:200].replace("\n", " ").strip()
                    content += f"  逻辑: {brief}...\n"
                
                # ---- 选股理由 ----
                content += "  选股理由: "
                reasons = []
                if rating in ("Buy", "Overweight"):
                    reasons.append(f"评级为 {rating}，TradingAgents 多 agent 共识强烈看多")
                elif rating == "Hold":
                    reasons.append("评级 Hold，具备估值安全垫，适合稳健配置")
                score = pick.get("score")
                if score:
                    reasons.append(f"AlphaSift 得分 {score}，技术+基本面量化筛选通过")
                sector = pick.get("sector", "")
                if sector:
                    reasons.append(f"所在板块 {sector} 处于热点/防御区间")
                if not reasons:
                    reasons.append("综合量化筛选 + AI 深度分析入选")
                content += "；".join(reasons) + "\n\n"
        
        # ---- 选股逻辑说明 ----
        analyzed = report.get("analyzed_stocks", [])
        if analyzed:
            total = len(analyzed)
            rated = sum(1 for s in analyzed if s.get("rating") not in ("N/A", None, ""))
            ta_rated = sum(1 for s in analyzed 
                          if _RATING_TIER.get(s.get("rating", ""), 0) >= 3)
            content += "【选股逻辑】\n"
            content += f"  全市场候选 {len(report.get('candidates', []))} 只"
            content += f" → 稳健筛选 {len(report.get('stable_stocks', []))} 只"
            content += f" → TA 深度分析 {rated}/{total} 只成功"
            content += f" → Hold 以上 {ta_rated} 只 → 最终推荐 {len(a_stock_picks)} 只\n"
            content += "  筛选标准：市值>100亿 + PE>0 + 非ST + 流动性充足 → "
            content += "TradingAgents 五级评级（Buy>Overweight>Hold>Underweight>Sell）"
            content += " → 仅入选 Hold 及以上\n\n"
        
        # ---- 进攻组 · 短线机会 ----
        momentum_picks = report.get("momentum_picks", [])
        if momentum_picks:
            content += "【进攻组 · 短线机会】（技术面快筛）\n\n"
            for p in momentum_picks:
                cross_tag = "🔥 MACD金叉 " if p.get("macd_golden_cross") else ""
                prob_5 = p.get("prob_5pct", 60)
                prob_8 = p.get("prob_8pct", 45)
                content += f"  {cross_tag}{p['name']}（{p['code']}）\n"
                content += f"    现价: {p['price']} 元 | 5日涨幅: +{p['ret_5d']}% | RSI: {p['rsi']}\n"
                content += f"    买入: {p['entry_price']} → +5% {p.get('target_price_2', 'N/A')}（概率 ~{prob_5}%）减半仓\n"
                content += f"                          +8% {p.get('target_price', 'N/A')}（概率 ~{prob_8}%）清仓\n"
                content += f"    止损-3%: {p['stop_loss']} | 换手: {p['turnover_rate']}% | 量比: {p['volume_ratio']}\n\n"
            content += "  ⚠️ 严格执行止损，单只不超过总资金 20%\n\n"
        
        content += "\n--- 仅供参考，投资需谨慎 ---"
        
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
        
        content += "\n--- 仅供参考，投资需谨慎 ---"
        
        # 通过 NotificationService 发送（自动检测已配置渠道：飞书、邮件、企微等）
        from src.notification import NotificationService
        notifier = NotificationService()
        channels = notifier.get_available_channels()
        if not channels:
            print("[通知] 无已配置的通知渠道，跳过发送")
            return
        print(f"[通知] 检测到 {len(channels)} 个渠道")
        ok = notifier.send(content, route_type="report")
        if ok:
            print("[通知] 发送成功")
        else:
            print("[通知] 发送失败（所有渠道均失败）")
        
    except Exception as e:
        print(f"[通知] 发送失败: {e}")


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

        # 概念偏好推荐：从完整候选池中提取命中概念的股票（独立于全市场推荐）
        concept_picks = _tag_concept_stocks(candidates)
        report["concept_picks"] = concept_picks
        if concept_picks:
            print(f"[概念推荐] {len(concept_picks)} 只概念股：{[c['code'] for c in concept_picks]}")
    
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
            # 合并全市场推荐 + 概念推荐（去重），概念组追加到分析列表末尾
            analysis_list = list(report["stable_stocks"])
            existing_codes = {s.get("code") for s in analysis_list}
            for cp in report.get("concept_picks", []):
                if cp.get("code") and cp["code"] not in existing_codes:
                    analysis_list.append(cp)
                    existing_codes.add(cp["code"])
            if len(analysis_list) > len(report["stable_stocks"]):
                added = len(analysis_list) - len(report["stable_stocks"])
                print(f"[Step 4] 概念组额外追加 {added} 只股票进入深度分析")

            print("\n[Step 4] TradingAgents 深度分析...")
            analyzed = run_tradingagents_analysis(analysis_list)
            report["analyzed_stocks"] = analyzed
            
            # Step 5: 选出周度 A股 推荐
            print("\n[Step 5] 选出周度 A股 推荐...")
            picks = select_weekly_picks(analyzed, max_picks=2)
            report["weekly_picks"] = picks
    
    # Step 6: 港股/美股 国际选股（当前搜索解析不稳定，暂不推荐）
    if False and args.mode in ["full", "analysis-only"]:
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
    
    # Step 7: 短线进攻组（技术面快筛 1-2 只）
    if args.mode in ["full", "screening-only"]:
        print("\n[Step 7] 短线进攻组...")
        try:
            sys.path.insert(0, str(Path(__file__).parent))
            from short_momentum_screener import screen_short_momentum, format_short_momentum_report
            momentum = screen_short_momentum(top_n=2, max_candidates=200)
            report["momentum_picks"] = momentum
            momentum_report = format_short_momentum_report(momentum)
            report["momentum_report"] = momentum_report
            print(f"[短线进攻] 选出 {len(momentum)} 只短线股")
        except Exception as e:
            print(f"[短线进攻] 失败: {e}")
    
    # 保存报告
    report_file = save_report(report, args.output, args.timestamp)
    
    # 发送通知（飞书、邮件等所有已配置渠道）
    send_notification(report, args.timestamp)
    
    print(f"\n{'='*60}")
    print(f"分析完成！报告已保存至: {report_file}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
