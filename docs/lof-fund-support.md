# LOF Fund Support

## 背景

AkshareFetcher 原先只支持 ETF 基金（`fund_etf_hist_em`），不支持 LOF（上市型开放式基金）。
LOF 与 ETF 共用部分号段（如 16xxxx），但 akshare 的 API 端点不同：

- LOF 使用 `ak.fund_lof_hist_em()`
- ETF 使用 `ak.fund_etf_hist_em()`

**之前的问题**：LOF 代码（如 161116）落到 `_fetch_etf_data()` 路径，
`fund_etf_hist_em()` 对 LOF 代码返回空 DataFrame，导致分析失败。

## 权威分类契约

### LOF 代码段

| 交易所 | 号段 | 说明 | 实查数量 |
|--------|------|------|---------|
| 深交所 | 160xxx-169xxx | 全段 160/161/162/163/164/165/166/167/168/169 | 322 只 |
| 上交所 | 501xxx | 普通 LOF 基金 | 92 只 |
| 上交所 | 502xxx | 原分级基金转型的 LOF | 9 只 |
| 上交所 | 506xxx | 科创板相关 LOF（涨跌幅 ±20%） | 7 只 |

### ETF 代码段

| 交易所 | 号段 | 实查数量 |
|--------|------|---------|
| 深交所 | 159xxx | 679 只 |
| 上交所 | 510xxx-518xxx, 520xxx, 526xxx, 530xxx, 560xxx-563xxx, 588xxx-589xxx | 870 只 |

### 互斥规则

`_is_lof_code()` 在 dispatch 中优先于 `_is_etf_code()` 调用，确保 16xxxx 不会被 ETF 吞掉。

注意：`base.py._is_etf_code()` 和 `base.py.FUND_PREFIXES` 回答的是"是否为基金"（含 LOF+ETF），
不区分 ETF 和 LOF。精确分类只在 `akshare_fetcher` 层面需要（选择 API 端点）。

### 不属于基金的前缀（明确排除）

| 前缀 | 实际类型 | 原先处理 | 现在处理 |
|------|---------|---------|---------|
| 18xxxx | 传统封闭式基金（已清盘/转型） | 被当 ETF | 走普通股票路径 |
| 519xxx | 上证基金通（非竞价系统） | 不匹配 | 不匹配（正确） |
| 500xxx | 契约型封闭式（已清算） | 不匹配 | 不匹配（正确） |
| 53x（除 530） | 非基金 | 旧版 `startswith('53')` 误匹配 | 精确匹配 530 |

### 数据来源（四重交叉验证）

1. **akshare 实查**：`fund_lof_spot_em()` 返回 390 只 LOF + `fund_etf_spot_em()` 返回 1549 只 ETF
2. **知乎** — 场内基金的代码体系："深交所LOF，都是16开头，160到169都有"
3. **腾讯新闻** — "深交所LOF通常以'160'开头，上交所LOF一般以'501'开头"
4. **维基百科** — 上海证券交易所上市交易基金列表（501/502/506 详细说明）

## 全栈同步

分类逻辑在 4 处独立定义（历史架构，不做合并重构），必须保持一致：

| 模块 | 常量/函数 | 用途 | 改动 | 测试 |
|------|----------|------|------|------|
| `akshare_fetcher.py` | `_is_lof_code()` / `_is_etf_code()` | 选择 API 端点（LOF vs ETF vs stock） | 新增 `_is_lof_code` + 重写 `_is_etf_code`（精确前缀） | 14 LOF + 15 ETF + 4 edge |
| `akshare_fetcher.py` | `_fetch_lof_data()` | LOF 数据获取 + fallback | 新增方法 | dispatch + empty + exception + rate limit |
| `base.py` | `FUND_PREFIXES` / `_is_etf_code()` | DataFetcherManager 基金路由 + fundamental pipeline ETF 降级 | 加 501/502/506/588/589，删 18 | `test_base_is_etf_code_includes_sh_lof` |
| `efinance_fetcher.py` | `_ETF_SH_PREFIXES` / `_ETF_SZ_PREFIXES` | 东方财富 secid 构建（`1.xxx` vs `0.xxx`） | SH 加 501/502/506/588/589，SZ 删 18 | `test_efinance_secid_for_sh_lof` |
| `search_service.py` | `_A_ETF_PREFIXES` | `is_index_or_etf()` → 新闻搜索语义（ETF 用指数搜索，不用公司搜索） | 加 501/502/506/588/589，删 18 | `test_search_service_recognizes_sh_lof_as_fund` |
| `pipeline.py` | — | 通过 `SearchService.is_index_or_etf()` 自动受益 | 无代码改动（间接覆盖） | 间接 |

**一致性验证**：4 处前缀集合对 `18` 全部排除，对 `501/502/506` 全部包含。

## Dispatch 行为

### 历史数据 `_fetch_raw_data()` 调用顺序

```
_is_us_code  → US fetcher (YfinanceFetcher)
_is_hk_code  → HK fetcher
_is_lof_code → _fetch_lof_data()     ← 新增
_is_etf_code → _fetch_etf_data()
default      → _fetch_stock_data()   (普通 A 股)
```

### LOF Fallback 逻辑（核心修复）

```
_fetch_lof_data(code, start, end)
  │
  ├─ ak.fund_lof_hist_em(code) 成功 + 非空 DataFrame
  │   → return df                           ✅ 正常路径
  │
  ├─ 返回空 DataFrame（该代码可能是 ETF）
  │   → logger.warning("回退 ETF 接口")
  │   → return self._fetch_etf_data()       ✅ 空响应 fallback
  │
  ├─ 普通异常（网络/解析错误）
  │   → logger.warning("LOF API 异常, 回退 ETF")
  │   → return self._fetch_etf_data()       ✅ 异常 fallback
  │
  └─ 限流异常（banned/blocked/频率/rate/限制）
      → raise RateLimitError                ✅ 不 fallback，直接抛出
```

**为什么空响应要 fallback**：16xxxx 号段 LOF 和 ETF 共用。
`fund_lof_hist_em()` 对实际是 ETF 的 16xxxx 代码返回空 DataFrame（不报错）。
如果不 fallback，ETF 代码被误判为 LOF 后将返回空数据。

**为什么限流不 fallback**：如果东方财富在限流 AkshareFetcher，
继续调 `_fetch_etf_data()`（也走东方财富）只会加剧限流。
直接抛 RateLimitError 让 DataFetcherManager 切换到 EfinanceFetcher/Baostock 等其他源。

**fallback 时 rate limit 说明**：`_fetch_lof_data` 和 `_fetch_etf_data` 各调一次
`_enforce_rate_limit()`。这不是 bug——两次 API 调用之间确实需要安全间隔。

### 其他 dispatch 点

| 方法 | LOF 行为 | 理由 |
|------|---------|------|
| `get_realtime_quote()` | 走 ETF 实时接口 | LOF 和 ETF 共用东方财富实时行情端点 |
| 筹码分布 | 跳过（return None） | 基金无筹码分布数据 |
| `fundamental_context` | ETF 降级（capital_flow/dragon_tiger/boards = not_supported） | 基金无公司基本面数据 |

## 异常处理

| 场景 | 行为 | 测试 |
|------|------|------|
| `fund_lof_hist_em` 返回空 DataFrame | fallback 到 `_fetch_etf_data()` | `test_empty_lof_response_falls_back_to_etf` |
| `fund_lof_hist_em` 返回 None | fallback（`df is not None` 检查） | 覆盖在空响应分支 |
| `fund_lof_hist_em` 抛普通异常（含非 JSON 响应） | akshare 内部 `response.json()` 失败时抛 ValueError，被 `except Exception` 捕获 → fallback | `test_lof_exception_falls_back_to_etf` |
| 东方财富限流（banned/blocked/频率/rate/限制） | 关键词匹配 → `raise RateLimitError`，不 fallback | `test_lof_rate_limit_does_not_fallback` |
| fallback 到 ETF 也失败 | DataFetcherManager 继续尝试 EfinanceFetcher → Baostock → YFinance | 上游现有 fallback chain |
| 所有数据源都失败 | `DataFetchError` 抛出，pipeline 记录错误并跳过该股票 | 上游现有行为 |

## 验证命令与结果

### Mock 测试（CI 默认运行）

```bash
python -m pytest tests/test_lof_fund_support.py tests/test_etf_daily_routing.py -q -m "not network"
# 56 passed, 3 deselected
```

测试覆盖：
- 分类：14 LOF codes + 15 ETF codes + 4 edge cases + prefixed codes + invalid codes
- Dispatch：LOF 走 `fund_lof_hist_em`，不走 `fund_etf_hist_em`
- 空响应 fallback：直接调用 + 通过 dispatch 间接调用
- 异常 fallback：普通异常 → ETF，限流异常 → raise
- 全栈语义：base.py / search_service.py / efinance_fetcher.py 一致性
- 一致性：18xxxx 不在任何前缀集合中

### 网络测试（手动运行，需东方财富可访问）

```bash
python -m pytest tests/test_lof_fund_support.py::TestLofNetworkValidation -v -m network
# 需要可访问东方财富 API
# fund_etf_hist_em 对 ETF 代码返回数据（验证 fallback 目标可用）
# 注：fund_lof_hist_em 的网络测试可能因东方财富限流而超时
```

### 分类验证

```python
from data_provider.akshare_fetcher import _is_lof_code, _is_etf_code
from data_provider.base import _is_etf_code as base_etf

# LOF
assert _is_lof_code("501018")       # Shanghai LOF
assert _is_lof_code("164105")       # Shenzhen LOF
assert not _is_etf_code("164105")   # 164 is LOF, not ETF

# NOT fund
assert not _is_etf_code("180003")   # Traditional closed-end
assert not _is_etf_code("519001")   # 上证基金通
assert not _is_etf_code("531999")   # Not a fund prefix

# Consistency across all 4 sites
assert not base_etf("180003")       # base.py also excludes 18
```

### 一致性验证脚本

```bash
# Verify all 4 classification sites agree
python -c "
import sys; sys.path.insert(0, '.')
from data_provider.akshare_fetcher import _is_lof_code, _is_etf_code
from data_provider.base import _is_etf_code as base_etf
from data_provider.efinance_fetcher import _ETF_SZ_PREFIXES, _is_etf_code as efin_etf
from src.search_service import SearchService

# 18 excluded everywhere
for f in [lambda: not _is_etf_code('180003'), lambda: not base_etf('180003'),
          lambda: not efin_etf('180003'), lambda: not SearchService.is_index_or_etf('180003', 'x')]:
    assert f()

# 501/502/506 = fund everywhere
for c in ['501018', '502000', '506000']:
    for f in [lambda c=c: _is_lof_code(c), lambda c=c: base_etf(c),
              lambda c=c: efin_etf(c), lambda c=c: SearchService.is_index_or_etf(c, 'x')]:
        assert f()

print('ALL CONSISTENT')
"
```

### ci_gate.sh

```bash
./scripts/ci_gate.sh
# backend-gate: Python syntax check — PASS
# backend-gate: flake8 critical checks — 0 (F401 pre-existing, not from this PR)
# backend-gate: local deterministic checks — PASS
```

## 兼容性风险

| 风险 | 影响范围 | 严重度 | 说明 |
|------|---------|--------|------|
| 18xxxx 从所有前缀集合移除 | 传统封闭式基金（已全部清盘/转型），走普通股票路径 | 极低 | 4 处一致移除：akshare_fetcher + base + efinance + search_service |
| 519xxx 不匹配任何前缀 | 上证基金通系统基金 | 极低 | 非竞价系统交易，不应进入分析链路 |
| 500xxx 不匹配 | 契约型封闭式（已清算解散） | 极低 | 无活跃交易代码 |
| 501/502/506 新增到 base/search/efinance | 可能影响已有 ETF 路由 | 极低 | 7 个现有 ETF 测试全部通过，无回归 |
| 588/589 新增到 base/search | 科创板 ETF，之前 base/search 未覆盖 | 极低 | akshare_fetcher 之前已覆盖 |
| efinance secid `1.501xxx` 可能返回空 | AkshareFetcher 失败时 efinance fallback 可能取不到 LOF 数据 | 低 | 实测东方财富 push2his 对 LOF 返回空；DataFetcherManager 会继续 fallback 到 Baostock/YFinance |
| LOF 被 fundamental_context 当基金跳过龙虎榜/资金流 | LOF 不获取公司级数据 | 无 | 正确行为——基金不需要公司基本面 |
| fallback 路径双重 `_enforce_rate_limit` | 两次 rate limit 间隔 | 无 | 不是 bug——两次 API 调用需要安全间隔 |

## 回滚方案

此 PR 包含 5 个 commit（按时间顺序）：

1. `feat: add LOF fund support with precise classification and fallback` — 核心功能
2. `fix: sync LOF classification across full stack` — base/search/efinance 同步
3. `docs: add LOF fund support technical specification` — 专题文档
4. `fix: consistency audit — remove 18xxxx from FUND_PREFIXES, clarify semantics` — 一致性审计
5. `fix: remove stale '18' prefix from efinance and search_service` — 残留清理

回滚步骤：

```bash
# 完全回滚（revert 全部 5 个 commit）
git revert HEAD~4..HEAD

# 或只回滚到核心功能（保留 LOF 支持，去掉后续清理）
git revert <commit-5> <commit-4>  # 回滚一致性审计

# 或 reset 回上游 main（最彻底）
git checkout main
git branch -D feat/lof-fund-support
```

回滚后状态：
- AkshareFetcher 恢复为仅 ETF 支持
- base.py / search_service.py / efinance_fetcher.py 恢复旧前缀集（含 18）
- 无数据库迁移
- 无配置变更（.env.example / workflow YAML 未改动）
- 无文件系统副作用（无缓存/持久化文件）
