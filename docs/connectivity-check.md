# Connectivity Check

## 背景

系统依赖多个外部服务：LLM API、搜索引擎 API、通知渠道。
任何一个服务失效都可能导致分析失败或通知丢失，但之前没有统一的方法验证"服务是否可用"。

**之前的问题**：
- 没有连通性检查脚本，CI 和本地都无法快速定位"哪个服务挂了"
- `--llm-only` 配置加载失败时生成 0 项检查报告，打印 "No connectivity failures" 并以退出码 0 结束
- 搜索 API 连通性检查的 `validate_body` 对部分服务商（Bocha/Anspire）返回空响应 `{}` 仍判 PASS

## 设计定义

### "服务可用" 契约

| 状态 | 含义 | 退出码影响 |
|------|------|-----------|
| PASS | 端点可达 + 响应体通过 validate_body | 无 |
| WARN | 端点可达但响应体异常（如 HTTP 200 + 空响应） | 无（提示性问题） |
| FAIL | 端点不可达 / 超时 / 认证失败 | exit 1 |
| SKIP | 渠道未配置（环境变量未设置） | 无 |

**核心原则：fail-closed**
- 配置加载失败时，不能生成 0 项检查就 exit 0
- 空响应 `{}` 不能判 PASS
- 非 JSON body 不能判 PASS

### 与 runtime 的对齐

连通性检查的 endpoint/method/params/validate_body **必须**与 `src/search_service.py` 中的实际 runtime 调用一致。

| 服务商 | runtime URL | runtime method | validate_body 契约 |
|--------|-------------|---------------|-------------------|
| Bocha | `https://api.bocha.cn/v1/web-search` | POST | `data.get('code') == 200`（对齐 `search_service.py:986`） |
| Anspire | `https://plugin.anspire.cn/api/ntsearch/search` | GET | `("code" not in b or b["code"]==200) and "results" in b`（对齐 `search_service.py:1168,1179`） |
| MiniMax | `https://api.minimaxi.com/v1/coding_plan/search` | POST | `b.get("base_resp",{}).get("status_code",0)==0` |

## 检查范围

### 1. LLM 渠道

对所有配置的 LLM 渠道（`LLM_CHANNELS`）逐一探测：
- 解析 `llm_channels` 和 `llm_model_list`
- 对每个渠道发送最简 LLM 请求（`POST /chat/completions` 或 `/completions`）
- 验证 `model_name` 字段存在

参数：`--llm-only` 只检查 LLM，`--list-models` 列出可用模型目录

### 2. 搜索 API Key

对配置了 key 的搜索引擎逐一探测：

| 服务商 | 探测方式 | validate_body |
|--------|---------|--------------|
| Tavily | POST `api.tavily.com/search` | 无（HTTP 200 即可） |
| Brave | GET `api.search.brave.com/res/v1/web/search` | 无 |
| SerpAPI | GET `serpapi.com/search` | 无 |
| MiniMax | POST `api.minimaxi.com/v1/coding_plan/search` | `base_resp.status_code == 0` |
| Bocha | POST `api.bocha.cn/v1/web-search` | `code == 200` |
| Anspire | GET `plugin.anspire.cn/api/ntsearch/search` | `code == 200 or absent` + `results` 存在 |

参数：`--search-only` 只检查搜索 API

### 3. 通知渠道

对配置了凭据的 14 种通知渠道逐一探测：

| 渠道 | 配置字段 | 默认探测方式 | --send-test 探测方式 |
|------|---------|-------------|---------------------|
| WeChat 企业微信 | `wechat_webhook_url` | HEAD webhook URL | POST `{"text":"test","msgtype":"text"}` |
| DingTalk 钉钉 | `dingtalk_webhook_url` | HEAD webhook URL | POST 测试消息 |
| Feishu 飞书 | `feishu_webhook_url` 或 app bot | HEAD webhook / API 域名 | POST 测试消息 |
| Telegram | `telegram_bot_token` + `telegram_chat_id` | GET `getMe` | POST `sendMessage` |
| Email 邮件 | `email_sender` + `email_password` | SMTP 登录（无邮件） | 发测试邮件给自己 |
| Pushover | `pushover_user_key` + `pushover_api_token` | POST API（无消息） | POST 含测试消息 |
| ntfy | `ntfy_url` | HEAD topic URL | POST 测试消息 |
| Gotify | `gotify_url` + `gotify_token` | HEAD endpoint | POST 测试消息 |
| PushPlus | `pushplus_token` | HEAD API | POST 测试消息 |
| ServerChan3 | `serverchan3_sendkey` | HEAD API | POST 测试消息 |
| Custom webhook | `custom_webhook_urls` | HEAD URL | POST 测试消息 |
| Discord | `discord_webhook_url` | HEAD webhook | POST 测试消息 |
| Slack | `slack_webhook_url` | HEAD webhook | POST 测试消息 |
| AstrBot | `astrbot_url` | HEAD URL | POST 测试消息 |

**配置字段对齐**：所有字段名与 `src/notification.py:get_available_channels()` 中的 `getattr(config, ...)` 完全一致。

参数：`--notification-only` 只检查通知渠道，`--send-test` 发送实际测试消息

## Fail-Closed 语义

### Bug 1: HTTP 200 + 非 JSON body → WARN（不再 PASS）

**修复前**：`validate_body` 提供时，`response.json()` 解析失败 → `except Exception: pass` → 判 PASS
**修复后**：解析失败 → 判 WARN（detail: "HTTP 200 but body is not valid JSON"）
**原因**：runtime（search_service.py）也会调 `response.json()`，如果这里失败，runtime 也会失败

### Bug 2: Bocha `{}` → WARN（不再 PASS）

**修复前**：`b.get("code", 0) == 200 or b.get("code") is None` → 空 dict 的 code 是 None → True → PASS
**修复后**：`b.get("code") == 200` → 空 dict 的 code 是 None → False → WARN
**原因**：runtime `if data.get('code') != 200: return error`（search_service.py:986）

### Bug 3: Anspire `{}` → WARN（不再 PASS）

**修复前**：`b.get("code", 200) == 200` → 空 dict 默认 200 → True → PASS
**修复后**：`("code" not in b or b["code"]==200) and "results" in b` → 空 dict 无 results → False → WARN
**原因**：runtime `if 'results' not in data: return error`（search_service.py:1179）

### Bug 4: `--llm-only` 配置加载失败 → exit 1（不再 exit 0）

**修复前**：config 加载异常 → `logger.warning` → results 为空 → "No connectivity failures" → exit 0
**修复后**：`_llm_config_failed = True` → 检查到 0 项结果 + 配置失败 → exit 1

## 异常处理

| 场景 | 行为 | 测试 |
|------|------|------|
| HTTP 200 + 非 JSON body（有 validate_body） | WARN | `test_http_200_non_json_with_validate_body` |
| HTTP 200 + 空 dict `{}` | validate_body 返回 False → WARN | `test_http_200_empty_dict_bocha_contract` |
| HTTP 500 | FAIL | `test_http_500` |
| 网络错误 / 超时 | FAIL | `test_network_error` |
| 配置加载失败 + `--llm-only` | exit 1 | `test_llm_only_config_fail_exits_1` |
| 配置加载成功 + 无渠道 | exit 0 | `test_llm_only_config_ok_exits_0` |
| 通知渠道 SMTP 不可达 | FAIL + 密钥脱敏 | `test_email_unreachable_fails` |
| 通知渠道 token 无效 | FAIL + token 脱敏 | `test_telegram_invalid_token_fails` |
| 通知渠道 webhook 4xx（默认模式） | PASS（端点可达） | `test_webhook_4xx_without_sendtest_passes` |
| 通知渠道 webhook 4xx（--send-test） | FAIL（实际发送失败） | `test_webhook_4xx_with_sendtest_fails` |
| 通知渠道未配置 | SKIP | `test_all_skip_when_nothing_configured` |

**密钥脱敏**：所有 probe 函数的 error detail 都通过 `_short_error(exc, secret)` 脱敏，确保 API key/token/password 不出现在日志和报告中。

## 验证命令与结果

### Mock 测试（CI 默认运行）

```bash
python -m pytest tests/test_connectivity_fail_closed.py -q -m "not network"
# 23 passed
```

测试覆盖：
- HTTP probe fail-closed：非 JSON body、空 dict（Bocha/Anspire 合约）、正常响应、HTTP 500、网络错误
- LLM 配置失败 exit 1、配置成功 exit 0
- validate_body 与 runtime 合约一致性（Bocha/Anspire）
- 通知渠道：全部 SKIP、webhook 可达/4xx/网络错误、Email SMTP 成功/失败、Telegram getMe 成功/失败
- `--send-test` 标志：默认模式不 POST

### 脚本运行

```bash
# 全部检查（LLM + 搜索 + 通知）
python scripts/check_connectivity.py

# 只检查通知渠道
python scripts/check_connectivity.py --notification-only

# 发送实际测试消息
python scripts/check_connectivity.py --send-test

# 只检查 LLM
python scripts/check_connectivity.py --llm-only

# 只检查搜索 API
python scripts/check_connectivity.py --search-only
```

### pyflakes

```bash
python -m pyflakes scripts/check_connectivity.py
# 0 undefined names
```

## 兼容性风险

| 风险 | 影响范围 | 严重度 | 说明 |
|------|---------|--------|------|
| 新增通知渠道探测增加运行时间 | 每个配置的渠道 +1-2s 网络请求 | 低 | 总耗时取决于配置的渠道数量；默认只 HEAD/GET 比 --send-test 快 |
| SMTP 探测可能触发邮箱安全告警 | 配置了 Email 的用户 | 极低 | 只是登录不发邮件；部分邮箱可能提示"新设备登录" |
| `_probe_webhook` 对某些 endpoint HEAD 返回 4xx | 所有 webhook 类渠道 | 无 | 4xx 在默认模式下判 PASS（端点可达），--send-test 才会真正 POST |
| `--send-test` 会向真实渠道发消息 | 使用 --send-test 的用户 | 低 | 消息内容为 "[Connectivity Test] DSA probe"，简短无害 |
| 通知渠道 config 字段名必须与 notification.py 一步 | 如果 notification.py 加了新渠道 | 低 | 需要同步更新 check_notification_channels，同 LOF 全栈同步 |

## 回滚方案

此 PR 包含 4 个 commit：

1. `feat: connectivity check with fail-closed semantics` — 核心脚本 + fail-closed 修复
2. `feat: add notification channel connectivity checks` — 14 种通知渠道
3. `fix: add module-level requests import` — pyflakes 修复
4. `test: add 11 notification channel probe tests + update CHANGELOG` — 测试 + 文档

回滚步骤：

```bash
# 完全回滚
git revert HEAD~3..HEAD

# 或只删除通知渠道（保留 fail-closed 修复）
git revert <commit-2> <commit-3> <commit-4>
```

回滚后：
- 上游 main 无 connectivity 脚本（这是新文件，删除即可）
- 无数据库迁移
- 无配置变更
- 无文件系统副作用
