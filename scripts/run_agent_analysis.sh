#!/bin/bash
# 全本地：拉数据 → 丢给 agent 分析
# 用法: bash scripts/run_agent_analysis.sh [股票代码]
# 默认分析 600036,601318

set -e
cd "$(dirname "$0")/.."

CODES="${1:-600036,601318}"
OUTPUT="data/stock_data.json"
PROMPT_FILE="prompts/analysis_prompt.md"

echo "=== 📊 拉取数据: $CODES ==="
python scripts/fetch_for_agent.py --codes "$CODES" --output "$OUTPUT"

echo ""
echo "=== 🤖 准备交给 agent 分析 ==="
echo "数据已保存到: $OUTPUT"
echo "提示模板: $PROMPT_FILE"
echo ""
echo "------------------------------------------------------------"
echo "  使用方法（任选一个 agent）："
echo ""
echo "  【OpenCode】"
echo "    opencode -f \"读 $OUTPUT，按 $PROMPT_FILE 分析输出\""
echo ""
echo "  【Codex】"
echo "    codex \"分析 $OUTPUT，按 $PROMPT_FILE 格式\""
echo ""
echo "  【Claude Code】"
echo "    claude -p \"分析 data/stock_data.json\""
echo ""
echo "  【手动】"
echo "    cat $OUTPUT | pbcopy  # 粘贴给 ChatGPT 等"
echo "------------------------------------------------------------"
echo ""
echo "=== ✅ 数据就绪 ==="
