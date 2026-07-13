# 双引擎工作流方案（AlphaSift 选股 + DSA 深度分析）

> 状态：已基于上游 v3.26.0（内置 AlphaSift）落地骨架，并在 `integration` 分支重铺自定义功能。
> 本文件为方案说明，非运行入口。

## 目标

1. **全市场选股**：用 AlphaSift 做 A 股全市场初筛 + LLM 重排，产出候选股。
2. **深度分析 + 推送**：把候选股交给 daily_stock_analysis（DSA）做深度分析、买/卖观点与推送。
3. **持仓股复盘**：DSA 继续分析你持有的股票（含 LOF 基金），给出具体价位、仓位与**到达概率**。
4. **统一模型源**：全部 LLM 调用走 PackyAPI（OpenAI 兼容，仅 grok-4.5），部署在 GitHub Actions。

## 为什么选 AlphaSift 做选股、DSA 做深度分析

- AlphaSift 是独立的选股引擎（全市场快照、因子评分、策略库 `strategies/*.yaml`），
  只负责“找出候选”，不负责个股深度解读。
- DSA 负责“把候选股讲清楚”：决策仪表盘（具体买卖/止损价、仓位、检查清单）+
  到达概率 + 多渠道推送（企微/飞书/Telegram/邮件/PushPlus 等）。
- 上游已内置 AlphaSift 适配层（`alphasift.dsa_adapter`），DSA 调用 AlphaSift 时自动把
  已解析的 `LITELLM_MODEL` / `LLM_CHANNELS` / `LLM_*` 桥接过去，所以 PackyAPI 一套配置两家共用。

## 已落地的实现

### 1. PackyAPI 渠道（LiteLLM）

在 `.env` / `.env.example` 配置自定义 OpenAI 兼容渠道：

```ini
LLM_CHANNELS=packyapi
LLM_PACKYAPI_PROTOCOL=openai
LLM_PACKYAPI_BASE_URL=https://www.packyapi.com/v1
LLM_PACKYAPI_API_KEY=sk-xxxx
LLM_PACKYAPI_MODELS=grok-4.5
LITELLM_MODEL=openai/grok-4.5
```

GitHub Actions 通过 `secrets.LLM_PACKYAPI_API_KEY` 等注入（见 `.github/workflows/00-daily-analysis.yml`）。

### 2. AlphaSift 选股 -> DSA 深度分析 串联

- 选股开关：`ALPHASIFT_ENABLED=true`（上游默认 false）。
- 串联脚本：`scripts/alphasift_screen_and_analyze.py`
  - 调用 `AlphaSiftService.screen(strategy, market="cn", max_results)` 得到候选股；
  - 把候选代码作为 `STOCK_LIST` 调用 `python main.py --stocks <codes> --no-market-review`；
  - 复用 DSA 的 LLM 与通知配置，无需额外配置。
- 触发：GitHub Actions 设 `ALPHASIFT_SCREEN_ENABLED=true` 时，工作流在分析步骤后运行该脚本
  （默认策略 `dragon_head`，可用 `ALPHASIFT_STRATEGY` / `ALPHASIFT_MAX_RESULTS` 调整）。

### 3. 到达概率（买/卖价位被触及的主观估计）

上游 v3.26.0 已有 `dashboard.battle_plan`（具体买卖/止损价 + 仓位建议）。
在此之上新增（不重复、仅增强）：

- `sniper_points.buy_reach_probability` / `buy_time_horizon`：建仓/目标位被触及的概率与时间周期；
- `sniper_points.stop_reach_probability` / `stop_time_horizon`：止损位被触及的概率与时间周期；
- 均为模型**主观概率估计（0-100）**，prompt 明确“不是保证，仅作仓位与风控参考”；
- 报告表格与推送摘要渲染这两个概率。

### 4. 自定义数据增强（待重铺）

- **a-stock-data**：龙虎榜 / 融资融券 / 大宗交易 / 股东变化 / 资金流 / 概念板块，注入分析上下文。
- **LOF 基金**：161116 / 160644 / 501018 通过 `ak.fund_lof_hist_em()` 获取历史（ETF 兜底）。

## 运行方式

### 本地

```bash
pip install -r requirements.txt
# 配置 .env（PackyAPI + ALPHASIFT_ENABLED=true + 通知渠道）
python main.py --stocks 600519            # 分析单只/持仓
# 或跑选股->深度分析：
ALPHASIFT_SCREEN_ENABLED=true python scripts/alphasift_screen_and_analyze.py
```

### GitHub Actions

- Settings -> Secrets and variables -> Actions 配置：`LLM_PACKYAPI_API_KEY`、通知渠道密钥、
  `ALPHASIFT_SCREEN_ENABLED=true`、`ALPHASIFT_STRATEGY` 等。
- 手动 `workflow_dispatch` 或每个交易日 18:00（UTC 10:00）自动运行。

## 回滚

- 关闭选股：`ALPHASIFT_ENABLED=false`（不影响原有 STOCK_LIST 深度分析）。
- 关闭串联脚本：`ALPHASIFT_SCREEN_ENABLED=false`。
- 模型/渠道问题：改 `LLM_CHANNELS` / `LITELLM_MODEL` 即可，AlphaSift 自动跟随。
