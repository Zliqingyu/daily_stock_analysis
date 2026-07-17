## Summary

Add LOF fund support to AkshareFetcher with precise code classification, empty-response fallback, and full-stack synchronization.

Standalone LOF PR from the three-way split per ZhuLinsen's review on #2016/#2027.

**Technical spec**: `docs/lof-fund-support.md`

## Classification

| Type | Exchange | Prefixes |
|------|----------|----------|
| LOF | SZ | 160-169 (all) |
| LOF | SH | 501, 502, 506 |
| ETF | SZ | 159 |
| ETF | SH | 510-518, 520, 526, 530, 560-563, 588-589 |

Cross-validated: akshare (390 LOF + 1549 ETF) + Brave (zhihu/tencent/wikipedia).
Excluded: 18xxxx (closed-end), 519xxx, 53x except 530.

## Full-Stack Sync

| Module | Change |
|--------|--------|
| `akshare_fetcher.py` | `_is_lof_code()` + `_fetch_lof_data()` + 3 dispatch points |
| `base.py` | `FUND_PREFIXES` with LOF/STAR ETF, removed `18` |
| `efinance_fetcher.py` | `_ETF_SH_PREFIXES` extended, removed `18` from SZ |
| `search_service.py` | `_A_ETF_PREFIXES` extended, removed `18` |
| `pipeline.py` | Auto-benefits via `SearchService.is_index_or_etf()` |

## Dispatch and Fallback

- LOF calls `fund_lof_hist_em()` first
- **Empty DataFrame or None -> fallback `_fetch_etf_data()`** (core fix)
- **Exception -> fallback `_fetch_etf_data()`**
- **Rate-limit -> raise RateLimitError** (no fallback)

## Verification

```
python -m pytest tests/test_lof_fund_support.py tests/test_etf_daily_routing.py -q -m "not network"
# 57 passed, 3 deselected

python -m flake8 --select=F,E9 <changed_files>  # CLEAN (F401 pre-existing)
```

57 tests: classification (14 LOF + 15 ETF + edge cases), dispatch, empty/None/exception fallback, rate-limit, realtime, full-stack semantics (base/search/efinance).

## Compatibility

See `docs/lof-fund-support.md` for full risk table. Key items:
- 18xxxx removed from all 4 classification sites (closed-end funds, delisted)
- 519xxx not matched (non-exchange)
- No .env / workflow / schema changes

## Rollback

```bash
git revert HEAD~8..HEAD  # revert all 9 commits
```

No DB migration, no config changes, no filesystem side effects.
