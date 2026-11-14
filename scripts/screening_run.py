#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选股漏斗 CLI — 统一入口 (原生 screening 引擎, 已合并 alpha 法工作流)。

用法: python scripts/screening_run.py [strategy] [max_output]
默认: momentum_quality / 5。策略见 src/services/screening/strategies/*.yaml:
  momentum_quality volume_breakout dual_low quality_value balanced_alpha
  blue_chip_income capital_heat low_volatility_quality oversold_reversal shrink_pullback

三级漏斗: 全市场快照(sina→efinance→akshare→em_datacenter→缓存) → 硬过滤+因子打分 → LLM 复排序
合并自 alpha 法 (scripts/alphasift_screen_and_analyze.py, alphasift pip 包与原生引擎90.6%同码, 已淘汰双轨):
  ① 宏观定调 SCREENING_MACRO_FIRST=true — 市场复盘定调覆盖策略/数量, 极端偏空跳过
  ② 板块倾斜 SCREENING_SECTOR_TILT=true — 领涨板块内候选优先
  ③ 不可交易板块过滤 — 默认剔除创业板/科创板/北交所 (SCREENING_ALLOW_BOARDS=chinext,star,bj 放行)
  ④ 写 data/alpha_latest_picks.txt 供次日早盘工作流
推送: pi-feishu-bridge/feishu_send.py
cron: 平日 08:50 (晨报+轮动卡之后)
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

STRATEGY_ALIASES = {
    "momentum": "momentum_quality",
    "volume": "volume_breakout",
    "value": "quality_value",
}
DEFAULT_STRATEGY = "momentum_quality"
FEISHU_SENDER = Path.home() / "pi-feishu-bridge" / "feishu_send.py"
PICKS_PATH = ROOT / "data" / "alpha_latest_picks.txt"


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() == "true"


def filter_untradeable_boards(picks: list) -> tuple[list, list[str]]:
    """剔除创业板(300/301)/科创板(688/689)/北交所(4,8开头)。

    返回 (保留, 被剔除的代码列表)。SCREENING_ALLOW_BOARDS=chinext,star,bj 放行。
    """
    allow = {b.strip().lower() for b in os.getenv("SCREENING_ALLOW_BOARDS", "").split(",") if b.strip()}

    def board_of(code: str):
        digits = "".join(ch for ch in code if ch.isdigit())[:6]
        if len(digits) < 6:
            return None
        if digits.startswith(("300", "301")):
            return "chinext"
        if digits.startswith(("688", "689")):
            return "star"
        if digits.startswith(("4", "8")):
            return "bj"
        return None

    kept, dropped = [], []
    for p in picks:
        b = board_of(getattr(p, "code", ""))
        if b and b not in allow:
            dropped.append(f"{p.code}({b})")
            continue
        kept.append(p)
    return kept, dropped


def save_picks(codes: list) -> None:
    """候选写入 data/alpha_latest_picks.txt（次日早盘工作流依赖此文件）。"""
    try:
        PICKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PICKS_PATH.write_text(f"{','.join(codes)}\n# updated: {datetime.now():%Y-%m-%d %H:%M}\n", encoding="utf-8")
        print(f"✅ picks 已写入 {PICKS_PATH}: {codes}")
    except Exception as exc:
        print(f"⚠️ 写 alpha_latest_picks.txt 失败: {exc}")


def format_card(result, picks: list, *, macro: str = "", tilt_info: str = "", dropped_boards: list[str] | None = None) -> str:
    lines = []
    header = f"🫙 选股漏斗 · {result.strategy}"
    if result.strategy_category:
        header += f" ({result.strategy_category})"
    header += f" · {datetime.now():%m-%d %H:%M}"
    lines.append(header)
    if macro:
        lines.append(f"宏观定调: {macro}")
    lines.append(
        f"漏斗: 全市场 {result.snapshot_count} → 硬过滤后 {result.after_filter_count}"
        f" → Top {len(picks)}  (快照源: {result.snapshot_source or 'N/A'})"
    )
    if result.llm_ranked:
        view = (result.llm_market_view or "").strip().split("\n")[0][:90]
        if view:
            lines.append(f"LLM市场观: {view}")
    if tilt_info:
        lines.append(tilt_info)
    lines.append("")
    for p in picks:
        head = f"{i_rank(p)}. {p.name} ({p.code})  {p.change_pct:+.1f}%  ¥{p.price:.2f}"
        if p.industry:
            head += f"  [{p.industry}]"
        lines.append(head)
        reason = (p.ranking_reason or "").strip().replace("\n", " ")
        if reason:
            lines.append(f"   ✦ {reason[:110]}")
        risk = (p.risk_summary or "").strip().replace("\n", " ")
        if risk:
            lines.append(f"   ⚠ {risk[:80]}")
    lines.append("")
    meta = f"评分: 综合={picks[0].final_score:.1f}" if picks else ""
    if result.llm_model_used:
        meta += f"  LLM={result.llm_model_used}"
    if meta:
        lines.append(meta.strip())
    if dropped_boards:
        lines.append(f"剔除无权限板块: {', '.join(dropped_boards[:5])}")
    if result.degradation:
        short = [d.split(":", 1)[0] for d in result.degradation[:3]]
        lines.append(f"降级: {', '.join(short)}")
    if result.source_errors:
        lines.append(f"源错误: {result.source_errors[0][:60]}")
    lines.append("⚠️ 模型信号仅供参考, 非投资建议")
    return "\n".join(lines)


def i_rank(p):
    """picks 重排后 rank 字段可能过期, 用列表序号。"""
    return getattr(p, "_display_rank", None) or p.rank


def main() -> int:
    strategy = STRATEGY_ALIASES.get(sys.argv[1], sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_STRATEGY
    max_output = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    # ① 宏观定调 (alpha 法移植)
    macro_note = ""
    if _env_flag("SCREENING_MACRO_FIRST"):
        from scripts.alphasift_screen_and_analyze import _load_market_regime, _classify_regime

        regime = _load_market_regime()
        if regime:
            label, reg_strategy, max_override, skip = _classify_regime(regime)
            score = regime.get("sentiment_score")
            if skip:
                print(f"ℹ️ 宏观偏空且情绪分<30 ({label}, {score}), 跳过选股。")
                return 0
            if reg_strategy:
                strategy = reg_strategy
            if max_override:
                max_output = max_override
            macro_note = f"{label} (情绪分={score}) → {strategy}"
            print(f"宏观定调: {macro_note}")
        else:
            print("⚠️ 未读到市场定调, 沿用配置策略。")

    # ② 板块倾斜: 放大池子
    sector_tilt = _env_flag("SCREENING_SECTOR_TILT")
    screen_n = max(max_output * 3, 20) if sector_tilt else max_output

    from src.services.screening.pipeline import screen

    result = screen(
        strategy,
        market="cn",
        max_output=screen_n,
        use_llm=True,
        explain_filters=True,
        daily_enrich=True,
        industry_provider="akshare",
    )

    picks = list(result.picks)

    # ③ 不可交易板块过滤
    picks, dropped = filter_untradeable_boards(picks)

    # ②' 板块倾斜重排 + 裁剪
    tilt_info = ""
    if sector_tilt and picks:
        from scripts.alphasift_screen_and_analyze import apply_sector_tilt, get_market_hot_sectors

        hot = get_market_hot_sectors(limit=10)
        cand_dicts = []
        for idx, p in enumerate(picks):
            d = {"code": p.code, "name": p.name, "industry": p.industry or "", "score": p.final_score}
            p_dict = d
            cand_dicts.append(d)
        tilted = apply_sector_tilt(cand_dicts, max_output, hot)
        by_code = {p.code: p for p in picks}
        picks = []
        for d in tilted:
            p = by_code.get(d["code"])
            if p is not None:
                p._display_rank = len(picks) + 1
                picks.append(p)
        if hot:
            tilt_info = f"板块倾斜: 领涨 {', '.join(hot[:4])}"
    else:
        picks = picks[:max_output]
        for idx, p in enumerate(picks):
            p._display_rank = idx + 1

    # ④ picks 文件 (次日早盘)
    save_picks([p.code for p in picks])

    card = format_card(result, picks, macro=macro_note, tilt_info=tilt_info, dropped_boards=dropped)
    print(card)
    if FEISHU_SENDER.exists():
        import subprocess

        try:
            subprocess.run(
                [sys.executable, str(FEISHU_SENDER), card],
                timeout=30, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
