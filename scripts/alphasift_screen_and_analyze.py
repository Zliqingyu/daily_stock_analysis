#!/usr/bin/env python3
"""AlphaSift 全市场选股 -> daily_stock_analysis 深度分析 + 推送。

设计目标（双引擎工作流）：
- AlphaSift 负责全市场初筛与 LLM 重排，产出候选股；
- daily_stock_analysis 负责候选股的深度分析、买/卖观点与推送（复用已配置的 LLM 与通知渠道）。

运行机制：
- 复用 dsa-private 的 LLM 配置（PackyAPI / LiteLLM）与通知配置，无需额外配置；
- AlphaSift 的 LLM 运行环境由 `AlphaSiftService.screen` 自动桥接 DSA 已解析的
  `LITELLM_MODEL` / `LLM_CHANNELS` / `LLM_*` 等配置；
- 候选股会作为 `STOCK_LIST` 传给 `main.py` 做深度分析与推送。

开关：
- `ALPHASIFT_SCREEN_ENABLED=true` 才真正执行；否则直接退出（便于在 workflow 中条件触发）。
- `ALPHASIFT_STRATEGY`：选股策略，默认 `momentum_quality`（动量质量）。已钉版本策略：balanced_alpha /
  blue_chip_income / capital_heat / dual_low / low_volatility_quality / momentum_quality /
  oversold_reversal / quality_value / shrink_pullback / volume_breakout。
- `ALPHASIFT_MAX_RESULTS`：最多分析的候选数量，默认 5。
- 其余如 `ALPHASIFT_ENABLED`、`LLM_*`、`STOCK_LIST` 等沿用环境变量配置。
"""

import os
import subprocess
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from pathlib import Path

# 将仓库根目录加入 sys.path，确保 `scripts/` 下运行时能 import `src`
if str(Path(__file__).parent.parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent.parent))


def _eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def _save_picks_to_github(codes: list) -> None:
    """将 AlphaSift 候选股代码写入 data/alpha_latest_picks.txt。

    供次日早盘工作流读取（checkout 后自动获取）。
    GitHub Actions 与本地运行均写入（本地迁移后早盘计划任务依赖此文件）。
    """
    # GITHUB_ACTIONS 旧门卫已移除：本地周度筛选也需要产出 picks 供次日早盘合并

    picks = ",".join(codes)
    picks_path = Path(__file__).parent.parent / "data" / "alpha_latest_picks.txt"
    try:
        picks_path.parent.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        picks_path.write_text(f"{picks}\n# updated: {ts}\n", encoding="utf-8")
        print(f"✅ AlphaSift 候选股已写入 {picks_path}: {picks}")
    except Exception as exc:
        _eprint(f"⚠️ 写入 alpha_latest_picks.txt 失败（不影响分析流程）：{exc}")


def _sector_hit(cand: dict, hot_sectors: list) -> bool:
    """候选股是否落在领涨板块/概念内（行业名/概念名双向子串匹配）。"""
    texts = []
    for key in ("industry", "concept", "name", "stock_name", "reason"):
        value = cand.get(key)
        if isinstance(value, str) and value:
            texts.append(value)
    blob = " ".join(texts)
    for hot in hot_sectors:
        hot = str(hot).strip()
        if not hot:
            continue
        if hot in blob or blob in hot:
            return True
    return False


def get_market_hot_sectors(limit: int = 10) -> list:
    """从市场数据（行业/概念涨幅榜）取领涨板块名，用于板块->个股约束。

    来源是数据源的板块/概念排名，不是新闻，符合"热门与否看市场"。
    获取失败则降级返回空列表（调用方退化为不倾斜）。
    """
    try:
        from src.services.market_hotspot_service import MarketHotspotService

        service = MarketHotspotService()
        payload = service.get_hotspots(market="cn", limit=limit)
        names = []
        for key in ("leading_industries", "leading_concepts"):
            for item in (payload.get(key) or []):
                if isinstance(item, dict):
                    name = item.get("name")
                    if name:
                        names.append(str(name))
        return names
    except Exception as exc:  # 市场板块数据不可用时，板块倾斜降级为不使用
        _eprint(f"⚠️ 获取市场领涨板块失败（板块倾斜降级）：{exc}")
        return []


def apply_sector_tilt(candidates: list, top_n: int, hot_sectors: list) -> list:
    """板块优先排序：领涨板块内候选排前，整体按 score 兜底。"""
    if not candidates:
        return []
    if not hot_sectors:
        return candidates[:top_n]
    enriched = []
    for cand in candidates:
        cand = dict(cand)
        cand["_in_hot_sector"] = _sector_hit(cand, hot_sectors)
        enriched.append(cand)
    enriched.sort(
        key=lambda x: (not x.get("_in_hot_sector", False), -(x.get("score") or 0))
    )
    hot_count = sum(1 for x in enriched if x.get("_in_hot_sector"))
    print(
        f"板块倾斜（sector tilt）：{hot_count}/{len(enriched)} 只位于领涨板块"
        f"（{', '.join(str(h) for h in hot_sectors[:5])}…）"
    )
    return enriched[:top_n]


# 宏观定调（L2）：用市场复盘记录定调，覆盖 AlphaSift 选股策略/数量；极端偏空时跳过选股。
_REGIME_STRATEGY = {
    "bullish": ("momentum_quality", None),
    "neutral": ("balanced_alpha", None),
    "cautious": ("quality_value", 3),
    "cooling": ("quality_value", 3),
    "bearish": ("low_volatility_quality", 2),
}


def _load_market_regime(days: int = 7) -> dict:
    """读取最新市场复盘（code=MARKET）定调：情绪分、趋势、风险标签。"""
    try:
        from src.storage import get_db

        db = get_db()
        records = db.get_analysis_history(code="MARKET", days=days, limit=1)
        if not records:
            return None
        rec = records[0]
        summary = rec.analysis_summary or ""
        trend = rec.trend_prediction or ""
        from src.services.daily_market_context import _extract_risk_tags

        risk_tags = _extract_risk_tags(f"{summary} {trend}")
        return {
            "sentiment_score": rec.sentiment_score,
            "trend_prediction": trend,
            "analysis_summary": summary,
            "risk_tags": risk_tags,
        }
    except Exception as exc:  # 读不到定调时，宏观定调降级为不使用
        _eprint(f"⚠️ 读取市场定调失败（宏观定调降级为不使用）：{exc}")
        return None


def _classify_regime(regime: dict):
    """把市场定调映射为 (label, strategy, max_override, skip)。"""
    score = regime.get("sentiment_score")
    tags = set(regime.get("risk_tags") or [])
    trend = (regime.get("trend_prediction") or "").lower()
    bearish = any(k in trend for k in ("bearish", "下跌", "看空", "退潮", "risk-off", "risk off"))
    bullish = any(k in trend for k in ("bullish", "上涨", "看多"))

    if "high_risk" in tags or "market_cooling" in tags or bearish:
        label = "bearish"
    elif "low_position_cap" in tags or "conservative" in tags:
        label = "cautious"
    elif bullish:
        label = "bullish"
    else:
        label = "neutral"

    strategy, max_override = _REGIME_STRATEGY[label]
    skip = label == "bearish" and isinstance(score, (int, float)) and score < 30
    return label, strategy, max_override, skip


def resolve_macro_first(strategy: str, max_results: int):
    """宏观定调入口：开启 ALPHASIFT_MACRO_FIRST 时覆盖策略/数量，极端偏空返回 skip。"""
    if os.getenv("ALPHASIFT_MACRO_FIRST", "").strip().lower() != "true":
        return strategy, max_results, False
    regime = _load_market_regime()
    if not regime:
        print("⚠️ 未读到市场定调记录，宏观定调降级为不使用（沿用配置策略）。")
        return strategy, max_results, False
    label, reg_strategy, max_override, skip = _classify_regime(regime)
    score = regime.get("sentiment_score")
    print(
        f"宏观定调：{label}（sentiment={score}, tags={regime.get('risk_tags')}）"
        f"-> strategy={reg_strategy}"
        + ("；情绪分<30 跳过选股" if skip else "")
    )
    if skip:
        return strategy, max_results, True
    strategy = reg_strategy
    if max_override:
        max_results = max_override
    return strategy, max_results, False


def main() -> int:
    if os.getenv("ALPHASIFT_SCREEN_ENABLED", "").strip().lower() != "true":
        print("ℹ️ ALPHASIFT_SCREEN_ENABLED 未开启，跳过 AlphaSift 选股->深度分析流程。")
        return 0

    try:
        from src.config import get_config
        from src.services.alphasift_service import AlphaSiftService
    except Exception as exc:  # pragma: no cover - 依赖缺失时给出可读错误
        _eprint(f"❌ 无法导入 DSA/AlphaSift 模块：{exc}")
        return 1

    strategy = os.getenv("ALPHASIFT_STRATEGY", "momentum_quality").strip()
    try:
        max_results = int(os.getenv("ALPHASIFT_MAX_RESULTS", "5").strip())
    except ValueError:
        max_results = 5
    if max_results <= 0:
        max_results = 5

    strategy, max_results, macro_skip = resolve_macro_first(strategy, max_results)
    if macro_skip:
        print("ℹ️ 宏观偏空且情绪分<30，跳过 AlphaSift 选股。")
        return 0

    sector_tilt = os.getenv("ALPHASIFT_SECTOR_TILT", "").strip().lower() == "true"
    # 板块倾斜时放大盲扫池子，保证领涨板块内有足够候选可挑
    screen_n = max(max_results * 3, 20) if sector_tilt else max_results

    macro_on = os.getenv("ALPHASIFT_MACRO_FIRST", "").strip().lower() == "true"
    print(
        f"🔍 AlphaSift 选股：strategy={strategy}, max_results={max_results}"
        f"{', 板块倾斜=on' if sector_tilt else ''}"
        f"{', 宏观定调=on' if macro_on else ''}"
    )
    try:
        config = get_config()
        service = AlphaSiftService(config)
        result = service.screen(strategy=strategy, market="cn", max_results=screen_n)
    except Exception as exc:
        _eprint(f"❌ AlphaSift 选股失败：{exc}")
        _eprint("   请确认已 `pip install -r requirements.txt`（含 alphasift 适配层）且 ALPHASIFT_ENABLED=true。")
        return 1

    candidates = (result.get("candidates") or []) if isinstance(result, dict) else []
    # 概念偏好推荐：从完整候选池中提取命中概念的股票（独立于全市场推荐）
    from scripts.weekly_screening import _tag_concept_stocks
    concept_picks = _tag_concept_stocks(candidates)
    if concept_picks:
        print(f"[概念推荐] {len(concept_picks)} 只概念股：{[c.get('code') for c in concept_picks]}")
    if sector_tilt:
        hot_sectors = get_market_hot_sectors(limit=10)
        candidates = apply_sector_tilt(candidates, max_results, hot_sectors)
    if not candidates:
        _eprint("⚠️ AlphaSift 未返回候选股，跳过深度分析。")
        return 0

    codes = []
    for cand in candidates:
        code = cand.get("code") or cand.get("stock_code")
        name = cand.get("name") or cand.get("stock_name") or code
        if code:
            codes.append(str(code))
            print(f"   - {code} {name}  score={cand.get('score')}")

    # 追加概念组中不在全市场列表的股票
    for cp in concept_picks:
        cp_code = str(cp.get("code") or "")
        if cp_code and cp_code not in codes:
            codes.append(cp_code)
            print(f"   + {cp_code} {cp.get('name', '')}  [概念: {cp.get('concept_tag', '')}]")

    if not codes:
        _eprint("⚠️ 候选股缺少可用代码字段，跳过深度分析。")
        return 0

    stock_list = ",".join(codes)
    print(f"📊 对 {len(codes)} 只候选股执行 daily_stock_analysis 深度分析：{stock_list}")

    # 将候选股写入 GitHub Variable，供次日早盘工作流读取（需要 GITHUB_TOKEN + gh CLI）
    _save_picks_to_github(codes)

    env = dict(os.environ)
    env["STOCK_LIST"] = stock_list
    # 双引擎选股产出的深度分析邮件，用独立主题前缀与每日自选股分析区分
    env["EMAIL_SUBJECT_PREFIX"] = "【AlphaSift选股】"
    # 选股产出的候选股默认假设用户空仓——分析视角侧重"是否值得新建仓"
    env["DEFAULT_POSITION_STATE"] = "no_position"
    # 选股产出的候选股不再做全市场大盘复盘，聚焦个股深度分析
    # 选股深度分析报告与每日主报告文件名区分，避免互相覆盖
    env["DSA_REPORT_FILE_SUFFIX"] = "alphasift"
    # 深度分析子进程继承运行时长上限：到点停止新候选并发送已完成结果
    max_runtime = int(os.getenv("ALPHASIFT_MAX_RUNTIME_SECONDS", "3240").strip() or "3240")
    cmd = [
        sys.executable, "main.py",
        "--stocks", stock_list, "--no-market-review",
        "--max-runtime", str(max_runtime),
    ]
    try:
        # timeout=None：由子进程自身的 --max-runtime 在 job 超时前收尾发送；
        # job 级 timeout-minutes（默认60）作为最终兜底。
        proc = subprocess.run(cmd, env=env, check=False, timeout=None)
    except Exception as exc:
        _eprint(f"❌ 深度分析执行失败：{exc}")
        return 1

    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
