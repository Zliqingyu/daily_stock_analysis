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
- `ALPHASIFT_STRATEGY`：选股策略，默认 `dragon_head`（龙头）。可选 bull_trend / hot_theme /
  volume_breakout / ma_golden_cross / growth_quality 等（见 `strategies/*.yaml`）。
- `ALPHASIFT_MAX_RESULTS`：最多分析的候选数量，默认 5。
- 其余如 `ALPHASIFT_ENABLED`、`LLM_*`、`STOCK_LIST` 等沿用环境变量配置。
"""

import os
import subprocess
import sys


def _eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


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

    strategy = os.getenv("ALPHASIFT_STRATEGY", "dragon_head").strip()
    try:
        max_results = int(os.getenv("ALPHASIFT_MAX_RESULTS", "5").strip())
    except ValueError:
        max_results = 5
    if max_results <= 0:
        max_results = 5

    print(f"🔍 AlphaSift 选股：strategy={strategy}, max_results={max_results}")
    try:
        config = get_config()
        result = AlphaSiftService.screen(strategy=strategy, market="cn", max_results=max_results)
    except Exception as exc:
        _eprint(f"❌ AlphaSift 选股失败：{exc}")
        _eprint("   请确认已 `pip install -r requirements.txt`（含 alphasift 适配层）且 ALPHASIFT_ENABLED=true。")
        return 1

    candidates = (result.get("candidates") or []) if isinstance(result, dict) else []
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

    if not codes:
        _eprint("⚠️ 候选股缺少可用代码字段，跳过深度分析。")
        return 0

    stock_list = ",".join(codes)
    print(f"📊 对 {len(codes)} 只候选股执行 daily_stock_analysis 深度分析：{stock_list}")

    env = dict(os.environ)
    env["STOCK_LIST"] = stock_list
    # 选股产出的候选股不再做全市场大盘复盘，聚焦个股深度分析
    cmd = [sys.executable, "main.py", "--stocks", stock_list, "--no-market-review"]
    try:
        proc = subprocess.run(cmd, env=env, check=False)
    except Exception as exc:
        _eprint(f"❌ 深度分析执行失败：{exc}")
        return 1

    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
