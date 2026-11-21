#!/usr/bin/env python3
"""LLM 多模型降级链 — 按 LLM_CHAIN 的顺序调用, 第一个有回复的胜出.

链格式(逗号分隔): <provider>/<model>, provider ∈ opencode-go | glm
默认: opencode-go/deepseek-v4.1-flash,glm/glm-5.3-flash,opencode-go/hy4-flash-free

key 从环境变量读:
  OPENCODE_KEY  opencode Go 订阅
  GLM_KEY       z.ai
opencode 的 OpenAI 兼容端点强制要 x-opencode-session 头(缺了直接 400
MissingSessionID), 这里每次调用生成一个. 全部失败时抛 RuntimeError,
错误信息里带每一家的失败原因, 调用方按原有逻辑兜底.

可选参数:
  validate  收到回复后先校验 (返回 False 则换下一次/下一家), 用于要求 JSON 的场景
  retries   每个模型最多试几次 (默认 1)
  extra     额外请求字段: {"*": {...} 对所有模型, "glm": {...} 只对 glm}
"""
import json
import os
import urllib.request
import uuid

DEFAULT_CHAIN = ("opencode-go/deepseek-v4.1-flash,"
                 "glm/glm-5.3-flash,"
                 "opencode-go/hy4-flash-free")

# provider -> (key 环境变量, base 环境变量, 默认 base)
PROVIDERS = {
    "opencode-go": ("OPENCODE_KEY", "OPENCODE_BASE",
                    "https://opencode.ai/inference/go/openai/v1"),
    "glm": ("GLM_KEY", "GLM_BASE",
            "https://api.z.ai/api/coding/paas/v4"),
}


def parse_chain(raw=None):
    """把 LLM_CHAIN 解析成 [(provider, model), ...]; 没写 provider 的当 glm."""
    text = (raw if raw is not None else os.getenv("LLM_CHAIN")) or DEFAULT_CHAIN
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        prov, sep, model = part.partition("/")
        if not sep:
            prov, model = "glm", part
        out.append((prov.strip().lower(), model.strip()))
    return out


def _extra_for(extra, prov):
    if not extra:
        return {}
    merged = dict(extra.get("*") or {})
    merged.update(extra.get(prov) or {})
    return merged


def call_chain(prompt, max_tokens=4096, timeout=120, validate=None,
               retries=1, extra=None):
    """按链顺序试, 返回第一个通过校验的非空回复; 全挂则抛 RuntimeError."""
    errors = []
    for prov, model in parse_chain():
        cfg = PROVIDERS.get(prov)
        if not cfg:
            errors.append(f"{prov}: 未知 provider")
            continue
        key_env, base_env, default_base = cfg
        key = os.getenv(key_env, "").strip()
        if not key:
            errors.append(f"{prov}: {key_env} 未配置")
            continue
        base = (os.getenv(base_env) or default_base).rstrip("/")
        headers = {"Authorization": f"Bearer {key}",
                   "Content-Type": "application/json"}
        if prov == "opencode-go":
            headers["x-opencode-session"] = f"strategy-{uuid.uuid4()}"
        body = {"model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                **_extra_for(extra, prov)}
        for attempt in range(1, max(1, retries) + 1):
            try:
                req = urllib.request.Request(base + "/chat/completions",
                                             data=json.dumps(body).encode(),
                                             headers=headers)
                resp = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
                text = (resp["choices"][0]["message"].get("content") or "").strip()
                if not text:
                    errors.append(f"{prov}/{model}: 空回复")
                elif validate and not validate(text):
                    errors.append(f"{prov}/{model}: 未通过校验")
                else:
                    return text
            except Exception as e:
                errors.append(f"{prov}/{model}: {e}")
    raise RuntimeError("所有模型都失败: " + "; ".join(errors))
