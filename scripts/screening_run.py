#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选股漏斗 CLI — 上游 screening 子系统入口。

用法: python scripts/screening_run.py [strategy] [max_output]
默认: momentum_quality / 5。策略见 src/services/screening/strategies/*.yaml:
  momentum_quality volume_breakout dual_low quality_value balanced_alpha
  blue_chip_income capital_heat low_volatility_quality oversold_reversal shrink_pullback

三级漏斗: 全市场快照(efinance→akshare→tushare) → 硬过滤+因子打分 → LLM 复排序
推送: pi-feishu-bridge/feishu_send.py
cron: 平日 08:50 (晨报+轮动卡之后)
"""

from __future__ import annotations

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


def format_card(result) -> str:
    lines = []
    header = f"🫙 选股漏斗 · {result.strategy}"
    if result.strategy_category:
        header += f" ({result.strategy_category})"
    header += f" · {datetime.now():%m-%d %H:%M}"
    lines.append(header)
    lines.append(
        f"漏斗: 全市场 {result.snapshot_count} → 硬过滤后 {result.after_filter_count}"
        f" → Top {len(result.picks)}  (快照源: {result.snapshot_source or 'N/A'})"
    )
    if result.llm_ranked:
        view = (result.llm_market_view or "").strip().split("\n")[0][:90]
        if view:
            lines.append(f"LLM市场观: {view}")
    lines.append("")
    for p in result.picks:
        head = f"{p.rank}. {p.name} ({p.code})  {p.change_pct:+.1f}%  ¥{p.price:.2f}"
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
    meta = f"评分: 综合={result.picks[0].final_score:.1f}" if result.picks else ""
    if result.llm_model_used:
        meta += f"  LLM={result.llm_model_used}"
    if meta:
        lines.append(meta.strip())
    if result.degradation:
        short = [d.split(":", 1)[0] for d in result.degradation[:3]]
        lines.append(f"降级: {', '.join(short)}")
    if result.source_errors:
        lines.append(f"源错误: {result.source_errors[0][:60]}")
    lines.append("⚠️ 模型信号仅供参考, 非投资建议")
    return "\n".join(lines)


def main() -> int:
    strategy = STRATEGY_ALIASES.get(sys.argv[1], sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_STRATEGY
    max_output = int(sys.argv[2]) if len(sys.argv) > 2 else 5

    from src.services.screening.pipeline import screen

    result = screen(
        strategy,
        market="cn",
        max_output=max_output,
        use_llm=True,
        explain_filters=True,
        daily_enrich=True,
        industry_provider="akshare",
    )
    card = format_card(result)
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
