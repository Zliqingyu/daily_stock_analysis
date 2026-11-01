#!/usr/bin/env python3
"""Standalone TradingAgents runner (runs inside the isolated TradingAgents venv).

Reads config from environment, runs ``TradingAgentsGraph.propagate(ticker, trade_date)``
and prints a single JSON object to stdout with the 5-tier rating and the analysis reports.

This script must be executed with the Python interpreter of the dedicated TradingAgents
virtualenv (``.venv-ta/bin/python``), because TradingAgents pins ``httpx==0.25.2`` via
mootdx, which conflicts with dsa-private's ``httpx>=0.28``. It only depends on
``tradingagents`` and the standard library, never on dsa-private modules.

Environment:
  # Two-model setup: complex tasks use TA_DEEP_MODEL, simple/fast tasks use TA_QUICK_MODEL.
  # Both models must come from the SAME provider (single llm_provider in TradingAgents).
  TA_LLM_PROVIDER           LLM provider: "openai" (default, OpenAI-compatible e.g. PackyAPI) or "google"
  # google
  GOOGLE_API_KEY / GEMINI_API_KEY / LLM_GEMINI_API_KEY / TAGENT_API_KEY   Gemini key
  TA_DEEP_MODEL / TA_QUICK_MODEL   default gemini-2.5-flash
  TA_GOOGLE_THINKING_LEVEL  optional ("high"/"minimal"/...)
  # openai-compatible (e.g. PackyAPI + DeepSeek)
  OPENAI_API_KEY / LLM_PRIMARY_API_KEY / PACKYAPI_API_KEY / TAGENT_API_KEY   API key
  TA_BASE_URL               (已废弃) base URL 取 LLM_PRIMARY_BASE_URL
  TA_DEEP_MODEL / TA_QUICK_MODEL   default deepseek-v4-flash (set TA_DEEP_MODEL to your "pro" model)
  # shared
  TA_RESULTS_DIR            default data/tradingagents/logs
  TA_CACHE_DIR              default data/tradingagents/cache
  TA_MAX_DEBATE_ROUNDS      default 1
  TA_OUTPUT_LANG            default Chinese
  USE_PROXY/PROXY_HOST/PROXY_PORT   applied only for google (Google is GFW-blocked in CN)
"""
import argparse
import json
import os
import sys
from pathlib import Path

try:
    from langchain_core.callbacks import BaseCallbackHandler

    class _Progress(BaseCallbackHandler):
        """Lightweight progress to stderr (stdout stays clean JSON for the parent)."""

        def _tick(self, tag):
            if os.getenv("TA_VERBOSE", "1") != "0":
                print(f"[TA {tag}]", file=sys.stderr, flush=True)

        def on_llm_start(self, *args, **kwargs):
            self._tick("llm→")

        def on_llm_end(self, *args, **kwargs):
            self._tick("llm←")

        def on_tool_start(self, *args, **kwargs):
            self._tick("tool→")

        def on_tool_end(self, *args, **kwargs):
            self._tick("tool←")
except Exception:  # pragma: no cover
    _Progress = None


try:
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.default_config import DEFAULT_CONFIG
except Exception as exc:  # pragma: no cover - import guard for clean error JSON
    print(json.dumps({"error": f"tradingagents import failed: {exc}"}))
    sys.exit(1)


def _install_reasoning_content_patch() -> None:
    """Monkey-patch ChatOpenAI for DeepSeek ``reasoning_content`` round-trip.

    DeepSeek returns ``reasoning_content`` in API responses.  langchain-openai
    0.1.x does **not** capture it into ``AIMessage.additional_kwargs`` during
    ``_create_chat_result``, so when the message is serialised back into the
    next request the field is lost and PackyAPI rejects with 400.

    Fix: ① hook ``_create_chat_result`` to store ``reasoning_content`` into
    ``additional_kwargs``, then ② hook ``_get_request_payload`` to inject it
    back into the serialised message dict.
    """
    try:
        from langchain_openai.chat_models import ChatOpenAI as _CO

        # ── step ①: capture reasoning_content from API response ──
        _orig_ccr = _CO._create_chat_result

        def _patched_ccr(self, response, *args, **kwargs):
            result = _orig_ccr(self, response, *args, **kwargs)
            for gen, choice in zip(
                result.generations,
                getattr(response, "choices", []) or [],
            ):
                rc = getattr(getattr(choice, "message", None), "reasoning_content", None)
                if rc:
                    gen.message.additional_kwargs["reasoning_content"] = rc
            return result

        _CO._create_chat_result = _patched_ccr

        # ── step ②: inject reasoning_content into outgoing payload ──
        _orig_rp = _CO._get_request_payload

        def _patched_rp(self, input_, *, stop=None, **kwargs):
            payload = _orig_rp(self, input_, stop=stop, **kwargs)
            msgs = input_ if isinstance(input_, list) else [input_]
            for msg, entry in zip(msgs, payload.get("messages", [])):
                rc = getattr(msg, "additional_kwargs", {}).get("reasoning_content")
                if rc and entry.get("role") == "assistant":
                    entry["reasoning_content"] = rc
            return payload

        _CO._get_request_payload = _patched_rp
    except Exception:
        pass  # openai provider not in use — non-fatal


def _disable_responses_api() -> None:
    """Monkey-patch ChatOpenAI to force-disable the Responses API.

    Third-party OpenAI-compatible gateways (PackyAPI, z.ai, etc.) typically
    only implement ``/chat/completions`` and return 404 for ``/responses``.
    TradingAgents' own OpenAIClient respects ``base_url`` to skip Responses,
    but langchain-openai may still auto-enable it in certain code paths.
    This patch forces ``use_responses_api=False`` at construction time.
    """
    try:
        from langchain_openai.chat_models import ChatOpenAI as _CO
        _orig_init = _CO.__init__

        def _patched_init(self, *args, **kwargs):
            kwargs["use_responses_api"] = False
            _orig_init(self, *args, **kwargs)

        _CO.__init__ = _patched_init
    except Exception as e:
        print(f"[TA] _disable_responses_api patch skipped: {e}", file=sys.stderr, flush=True)


def _load_dotenv() -> None:
    """Minimal stdlib .env loader so direct local runs pick up keys/proxy."""
    p = Path(".env")
    if not p.exists():
        return
    for ln in p.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def _apply_proxy_if_needed(provider: str) -> None:
    """Google is GFW-blocked in CN; route through the configured HTTP proxy."""
    if provider != "google":
        return
    if os.getenv("USE_PROXY", "").lower() != "true":
        return
    host = os.getenv("PROXY_HOST", "127.0.0.1")
    port = os.getenv("PROXY_PORT", "7890")
    proxy = f"http://{host}:{port}"
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[var] = proxy


def build_config() -> dict:
    # TradingAgentsGraph authenticates the LLM ONLY via an env var
    # (GOOGLE_API_KEY for google, OPENAI_API_KEY for openai-compatible);
    # it never forwards config["api_key"] to the client, so export it first.
    _load_dotenv()
    provider = os.getenv("TA_LLM_PROVIDER", "openai").lower()  # 默认 openai（PackyAPI），与 workflow/.env.example 一致

    if provider == "google":
        api_key = (
            os.getenv("GOOGLE_API_KEY")
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("LLM_GEMINI_API_KEY")
            or os.getenv("TAGENT_API_KEY")
        )
        if api_key:
            os.environ["GOOGLE_API_KEY"] = api_key
        deep = os.getenv("TA_DEEP_MODEL", "gemini-2.5-flash")
        quick = os.getenv("TA_QUICK_MODEL", "gemini-2.5-flash")
        _apply_proxy_if_needed(provider)
    else:  # openai-compatible (PackyAPI + DeepSeek, etc.)
        api_key = (
            os.getenv("OPENAI_API_KEY")
            or os.getenv("LLM_PRIMARY_API_KEY")
            or os.getenv("PACKYAPI_API_KEY")
            or os.getenv("TAGENT_API_KEY")
        )
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
        # default to deepseek-v4-flash; override TA_DEEP_MODEL with your "pro" model
        deep = os.getenv("TA_DEEP_MODEL", "deepseek-v4-flash")
        quick = os.getenv("TA_QUICK_MODEL", "deepseek-v4-flash")

    config = dict(DEFAULT_CONFIG)
    if api_key:
        config["api_key"] = api_key
    config["llm_provider"] = provider
    config["deep_think_llm"] = deep
    config["quick_think_llm"] = quick
    base_url = os.getenv("LLM_PRIMARY_BASE_URL")
    if base_url:
        config["backend_url"] = base_url
    # 强制使用 ChatCompletions API，禁用 Responses API。
    # 原因：第三方 OpenAI 兼容中转站（PackyAPI/z.ai 等）通常只实现
    # /chat/completions，不支持 /responses 端点。设置 base_url 后
    # TradingAgents 内部 OpenAIClient 理论上会跳过 Responses API，
    # 但 langchain-openai 某些版本仍可能自动启用，这里通过环境变量
    # 显式关闭。
    os.environ.setdefault("OPENAI_USE_RESPONSES_API", "false")
    config["results_dir"] = os.getenv("TA_RESULTS_DIR", "data/tradingagents/logs")
    config["data_cache_dir"] = os.getenv("TA_CACHE_DIR", "data/tradingagents/cache")
    config["max_debate_rounds"] = int(os.getenv("TA_MAX_DEBATE_ROUNDS", "1"))
    config["output_language"] = os.getenv("TA_OUTPUT_LANG", "Chinese")
    thinking = os.getenv("TA_GOOGLE_THINKING_LEVEL")
    if thinking and provider == "google":
        config["google_thinking_level"] = thinking
    return config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker")
    parser.add_argument("trade_date")
    args = parser.parse_args()

    try:
        _install_reasoning_content_patch()
        _disable_responses_api()
        cbs = [_Progress()] if _Progress is not None else None
        ta = TradingAgentsGraph(debug=False, config=build_config(), callbacks=cbs)
        final_state, decision = ta.propagate(args.ticker, args.trade_date)
    except Exception as exc:
        import traceback
        print(f"[TA ERROR] {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({
            "ticker": args.ticker,
            "trade_date": args.trade_date,
            "rating": "N/A",
            "error": str(exc),
        }, ensure_ascii=False))
        return

    out = {
        "ticker": args.ticker,
        "trade_date": args.trade_date,
        "rating": decision,
        "final_trade_decision": final_state.get("final_trade_decision", ""),
        "trader_investment_decision": final_state.get("trader_investment_decision", ""),
        "investment_plan": final_state.get("investment_plan", ""),
        "market_report": final_state.get("market_report", ""),
        "sentiment_report": final_state.get("sentiment_report", ""),
        "news_report": final_state.get("news_report", ""),
        "fundamentals_report": final_state.get("fundamentals_report", ""),
        "policy_report": final_state.get("policy_report", ""),
        "hot_money_report": final_state.get("hot_money_report", ""),
        "lockup_report": final_state.get("lockup_report", ""),
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
