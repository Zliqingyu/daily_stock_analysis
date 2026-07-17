# LOF Fund Support

## 背景

AkshareFetcher 原先只支持 ETF 基金（`fund_etf_hist_em`），不支持 LOF（上市型开放式基金）。
LOF 与 ETF 共用部分号段（如 16xxxx），但 akshare 的 API 端点不同：

- LOF 使用 `ak.fund_lof_hist_em()`
- ETF 使用 `ak.fund_etf_hist_em()`

## 权威分类契约

### LOF 代码段

| 交易所 | 号段 | 说明 |
|--------|------|------|
| 深交所 | 160xxx-169xxx | 全段，160/161/162/163/164/165/166/167/168/169 |
| 上交所 | 501xxx | 普通 LOF 基金 |
| 上交所 | 502xxx | 原分级基金转型的 LOF |
| 上交所 | 506xxx | 科创板相关 LOF |

### ETF 代码段

| 交易所 | 号段 |
|--------|------|
| 深交所 | 159xxx |
| 上交所 | 510xxx-518xxx, 520xxx, 526xxx, 530xxx, 560xxx-563xxx, 588xxx-589xxx |

### 互斥规则

`_is_lof_code()` 在 dispatch 中优先于 `_is_etf_code()` 调用，确保 16xxxx 不会被 ETF 吞掉。

### 数据来源

分类基于四重交叉验证：
1. **akshare 实查**：`fund_lof_spot_em()` 返回 390 只 LOF，`fund_etf_spot_em()` 返回 1549 只 ETF
2. **知乎** — 场内基金的代码体系（160-169 全段，501/502）
3. **腾讯新闻** — "深交所LOF通常以'160'开头，上交所LOF一般以'501'开头"
4. **维基百科** — 上海证券交易所上市交易基金列表（501/502/506 详细说明）

## 全栈同步

LOF 分类不止在 AkshareFetcher 中，以下所有模块同步更新：

| 模块 | 改动 | 验证 |
|------|------|------|
| `data_provider/akshare_fetcher.py` | `_is_lof_code()` + `_fetch_lof_data()` + dispatch | 49 mock tests |
| `data_provider/base.py` | `FUND_PREFIXES` 含 501/502/506/588/589 | `test_base_is_etf_code_includes_sh_lof` |
| `data_provider/efinance_fetcher.py` | `_ETF_SH_PREFIXES` 扩展，secid 路由覆盖 | `test_efinance_secid_for_sh_lof` |
| `src/search_service.py` | `_A_ETF_PREFIXES` 含 LOF 号段，`is_index_or_etf` 覆盖 | `test_search_service_recognizes_sh_lof_as_fund` |
| `src/core/pipeline.py` | 通过 `SearchService.is_index_or_etf()` 自动受益 | 间接覆盖 |

## Dispatch 行为

### 历史数据 `_fetch_raw_data()`

```
_is_us_code → US fetcher
_is_hk_code → HK fetcher
_is_lof_code → _fetch_lof_data()   ← 新增
_is_etf_code → _fetch_etf_data()
default → _fetch_stock_data()
```

### LOF Fallback 逻辑

```
_fetch_lof_data(code)
  → ak.fund_lof_hist_em(code)
  → 非空 DataFrame → return
  → 空 DataFrame → fallback _fetch_etf_data()     ← 核心修复
  → 普通异常 → fallback _fetch_etf_data()
  → RateLimitError → raise（不 fallback）
```

### 其他 dispatch 点

- **实时行情** `get_realtime_quote()`：LOF 走 ETF 实时接口
- **筹码分布**：LOF 跳过（与 ETF 一致，基金无筹码数据）

## 验证命令与结果

### Mock 测试（CI 默认运行）

```bash
python -m pytest tests/test_lof_fund_support.py tests/test_etf_daily_routing.py -q -m "not network"
# 55 passed, 3 deselected
```

### 网络测试（手动运行）

```bash
python -m pytest tests/test_lof_fund_support.py::TestLofNetworkValidation -v -m network
# 需要可访问东方财富 API
# fund_etf_hist_em 对 ETF 代码返回数据（验证 fallback 目标可用）
```

### 分类验证

```python
from data_provider.akshare_fetcher import _is_lof_code, _is_etf_code
assert _is_lof_code("501018")       # Shanghai LOF
assert _is_lof_code("164105")       # Shenzhen LOF
assert not _is_etf_code("164105")   # 164 is LOF, not ETF
assert not _is_etf_code("180003")   # Traditional closed-end, not ETF
assert not _is_etf_code("519001")   # 上证基金通, not exchange-traded
```

## 兼容性风险

| 风险 | 影响范围 | 严重度 |
|------|---------|--------|
| 18xxxx 从 ETF 移除 | 传统封闭式基金（已清盘/转型），fall through 到 `_fetch_stock_data` | 低 |
| 519xxx 不匹配任何基金前缀 | 上证基金通系统基金，走普通股票路径 | 低 |
| 501/502/506 在 base/search/efinance 中新增 | 可能影响已有 ETF 路由逻辑（但测试无回归） | 极低 |
| 588/589 在 base/search 中新增 | 科创板 ETF，之前可能未覆盖 | 极低 |

## 回滚方案

此 PR 包含两个 commit：

1. `feat: add LOF fund support` — 核心功能
2. `fix: sync LOF classification across full stack` — 全栈同步

回滚步骤：

```bash
# 完全回滚（两个 commit 都 revert）
git revert <commit-2> <commit-1>

# 或只回滚全栈同步（保留 AkshareFetcher 内的 LOF 支持）
git revert <commit-2>
```

回滚后：
- AkshareFetcher 恢复为仅 ETF 支持
- base.py / search_service.py / efinance_fetcher.py 恢复旧前缀集
- 无数据库迁移、无配置变更、无文件系统副作用
