# -*- coding: utf-8 -*-
"""
===================================
Connectivity Check Script
===================================

Validates remote endpoints used by the analysis system:

1. LLM channels (LLM_CHANNELS / LITELLM_MODEL)
   -> Uses litellm with same params as runtime (model / api_key / api_base / extra_headers)
2. Search API keys (Tavily / Brave / SerpAPI / MiniMax / Bocha)
   -> Uses same endpoints and auth headers as src/search_service.py

Output: Console table + reports/connectivity_<date>.md + reports/connectivity_<date>.json

Usage:
    python scripts/check_connectivity.py
    python scripts/check_connectivity.py --llm-only
    python scripts/check_connectivity.py --search-only
    python scripts/check_connectivity.py --list-models
"""
import argparse
import json
import logging
import os
import requests
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Callable
from urllib.parse import urlparse

# Proxy config - controlled by USE_PROXY env var, off by default.
if os.getenv("GITHUB_ACTIONS") != "true" and os.getenv("USE_PROXY", "false").lower() == "true":
    proxy_host = os.getenv("PROXY_HOST", "127.0.0.1")
    proxy_port = os.getenv("PROXY_PORT", "10809")
    proxy_url = f"http://{proxy_host}:{proxy_port}"
    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url

os.environ.setdefault("LITELLM_LOG", "ERROR")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _reconfigure_output_stream(stream):
    """Avoid UnicodeEncodeError on legacy Windows console code pages."""
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    for kwargs in ({"encoding": "utf-8", "errors": "replace"}, {"errors": "replace"}):
        try:
            reconfigure(**kwargs)
            return
        except Exception:
            continue


def configure_console_encoding():
    for stream in (sys.stdout, sys.stderr):
        _reconfigure_output_stream(stream)


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

try:
    import litellm
    litellm.set_verbose = False
    logging.getLogger("litellm").setLevel(logging.ERROR)
except Exception:
    litellm = None


@dataclass
class CheckResult:
    category: str
    name: str
    target: str
    status: str  # PASS / FAIL / WARN / SKIP
    latency_s: Optional[float] = None
    detail: str = ""


TIMEOUT = int(os.getenv("CONNECTIVITY_TIMEOUT", "30"))


def _mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "（空）"
    if len(value) <= 8:
        return value[:1] + "***"
    return value[:4] + "…" + value[-2:]


def _netloc(url: Optional[str]) -> str:
    if not url:
        return "（默认端点）"
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


def _split_keys(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [k.strip() for k in raw.split(",") if k.strip()]


def _short_error(exc: Exception, secret: Optional[str] = None) -> str:
    msg = str(exc)
    if secret:
        msg = msg.replace(secret, "***KEY***")
    msg = msg.replace("\n", " ").strip()
    if len(msg) > 300:
        msg = msg[:300] + "…"
    etype = type(exc).__name__
    return f"{etype}: {msg}"


# ---------------------------------------------------------------------------
# 1) LLM Channel Connectivity
# ---------------------------------------------------------------------------
def check_llm_channels(config) -> List[CheckResult]:
    results: List[CheckResult] = []
    model_list = getattr(config, "llm_model_list", None) or []
    if not model_list:
        results.append(CheckResult(
            "LLM Channels", "No config", "-", "WARN",
            detail="No LLM model_list parsed (configure LLM_CHANNELS or LITELLM_MODEL).",
        ))
        return results

    seen = set()
    for entry in model_list:
        lp = entry.get("litellm_params") or {}
        model_name = entry.get("model_name") or lp.get("model") or "?"
        api_base = lp.get("api_base") or lp.get("base_url")
        dedup_key = (model_name, api_base or "")
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        api_key = lp.get("api_key")
        wire_model = lp.get("model")
        extra_headers = lp.get("extra_headers")

        host = urlparse(api_base).hostname if api_base else None
        if host in ("127.0.0.1", "localhost", "0.0.0.0"):
            results.append(CheckResult(
                "LLM Channels", model_name, _netloc(api_base), "SKIP",
                detail="Local loopback endpoint, skipped.",
            ))
            continue

        if not api_key:
            results.append(CheckResult(
                "LLM Channels", model_name, _netloc(api_base), "WARN",
                detail="No API key configured, skipped connectivity test.",
            ))
            continue

        if litellm is None:
            results.append(CheckResult(
                "LLM Channels", model_name, _netloc(api_base), "FAIL",
                detail="litellm not installed, cannot send test request.",
            ))
            continue

        start = time.time()
        try:
            resp = litellm.completion(
                model=wire_model,
                messages=[{"role": "user", "content": "ping"}],
                api_key=api_key,
                api_base=api_base,
                extra_headers=extra_headers,
                max_tokens=5,
                temperature=0,
                timeout=TIMEOUT,
                stream=False,
            )
            latency = time.time() - start
            used = None
            try:
                used = getattr(resp, "model", None)
            except Exception:
                pass
            results.append(CheckResult(
                "LLM Channels", model_name, _netloc(api_base), "PASS",
                latency_s=latency,
                detail=f"Response model: {used or wire_model}",
            ))
        except Exception as exc:
            latency = time.time() - start
            results.append(CheckResult(
                "LLM Channels", model_name, _netloc(api_base), "FAIL",
                latency_s=latency,
                detail=_short_error(exc, api_key),
            ))
    return results


# ---------------------------------------------------------------------------
# 2) Search API Key Connectivity
# ---------------------------------------------------------------------------
def _http_probe(name: str, method: str, url: str, *, headers=None, params=None, json_body=None,
                 secret: Optional[str] = None,
                 validate_body: Optional[Callable[[dict], tuple[bool, str]]] = None) -> CheckResult:
    import requests

    start = time.time()
    try:
        resp = requests.request(
            method, url, headers=headers or {}, params=params, json=json_body, timeout=TIMEOUT,
        )
        latency = time.time() - start
        if resp.status_code == 200:
            # Validate response body for providers that return HTTP 200 on app-level errors.
            # If validate_body is provided, body parse failure is a real problem — the
            # runtime (search_service.py) would also call response.json() and fail.
            if validate_body:
                try:
                    body = resp.json()
                except Exception:
                    return CheckResult(
                        "Search API", name, _netloc(url), "WARN",
                        latency_s=latency, detail="HTTP 200 but body is not valid JSON",
                    )
                ok, detail = validate_body(body)
                if not ok:
                    return CheckResult(
                        "Search API", name, _netloc(url), "WARN",
                        latency_s=latency, detail=f"HTTP 200 but {detail}",
                    )
            return CheckResult(
                "Search API", name, _netloc(url), "PASS",
                latency_s=latency, detail=f"HTTP {resp.status_code}",
            )
        detail = f"HTTP {resp.status_code}: {resp.text[:120]}"
        if secret:
            detail = detail.replace(secret, "***KEY***")
        return CheckResult(
            "Search API", name, _netloc(url), "FAIL",
            latency_s=latency, detail=detail,
        )
    except Exception as exc:
        latency = time.time() - start
        detail = _short_error(exc, secret)
        return CheckResult(
            "Search API", name, _netloc(url), "FAIL",
            latency_s=latency, detail=detail,
        )


def check_search_keys() -> List[CheckResult]:
    results: List[CheckResult] = []

    # Tavily
    tavily = _split_keys(os.getenv("TAVILY_API_KEYS"))
    if tavily:
        results.append(_http_probe(
            "Tavily", "POST", "https://api.tavily.com/search",
            json_body={"api_key": tavily[0], "query": "test", "max_results": 1, "topic": "general"},
            secret=tavily[0],
        ))
    else:
        results.append(CheckResult("Search API", "Tavily", "api.tavily.com", "SKIP", detail="TAVILY_API_KEYS not configured"))

    # Brave
    brave = _split_keys(os.getenv("BRAVE_API_KEYS"))
    brave_enabled_raw = (os.getenv("BRAVE_ENABLED") or "").strip().lower()
    brave_enabled = brave_enabled_raw not in ("false", "0", "no")
    if not brave_enabled:
        results.append(CheckResult("Search API", "Brave", "api.search.brave.com", "SKIP", detail="BRAVE_ENABLED=false"))
    elif brave:
        results.append(_http_probe(
            "Brave", "GET", "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": brave[0], "Accept": "application/json"},
            params={"q": "test", "count": 1},
            secret=brave[0],
        ))
    else:
        results.append(CheckResult("Search API", "Brave", "api.search.brave.com", "SKIP", detail="BRAVE_API_KEYS not configured"))

    # SerpAPI
    serpapi = _split_keys(os.getenv("SERPAPI_API_KEYS"))
    if serpapi:
        results.append(_http_probe(
            "SerpAPI", "GET", "https://serpapi.com/search.json",
            params={"q": "test", "num": 1, "api_key": serpapi[0]},
            secret=serpapi[0],
        ))
    else:
        results.append(CheckResult("Search API", "SerpAPI", "serpapi.com", "SKIP", detail="SERPAPI_API_KEYS not configured"))

    # MiniMax
    minimax = _split_keys(os.getenv("MINIMAX_API_KEYS"))
    if minimax:
        results.append(_http_probe(
            "MiniMax", "POST", "https://api.minimaxi.com/v1/coding_plan/search",
            headers={"Authorization": f"Bearer {minimax[0]}", "Content-Type": "application/json", "MM-API-Source": "Minimax-MCP"},
            json_body={"q": "test"},
            secret=minimax[0],
            validate_body=lambda b: (
                b.get("base_resp", {}).get("status_code", 0) == 0,
                f"base_resp.status_code={b.get('base_resp', {}).get('status_code')}",
            ),
        ))
    else:
        results.append(CheckResult("Search API", "MiniMax", "api.minimaxi.com", "SKIP", detail="MINIMAX_API_KEYS not configured"))

    # Bocha
    bocha = _split_keys(os.getenv("BOCHA_API_KEYS"))
    if bocha:
        results.append(_http_probe(
            "Bocha", "POST", "https://api.bocha.cn/v1/web-search",
            headers={"Authorization": f"Bearer {bocha[0]}", "Content-Type": "application/json"},
            json_body={"query": "test", "freshness": "oneWeek", "summary": True, "count": 1},
            secret=bocha[0],
            validate_body=lambda b: (
                b.get("code") == 200,
                f"code={b.get('code')!r} (expected 200)",
            ),
        ))
    else:
        results.append(CheckResult("Search API", "Bocha", "api.bocha.cn", "SKIP", detail="BOCHA_API_KEYS not configured"))

    # Anspire
    anspire = _split_keys(os.getenv("ANSPIRE_API_KEYS"))
    anspire_search_enabled = (os.getenv("ANSPIRE_SEARCH_ENABLED") or "").strip().lower() not in ("false", "0", "no")
    if anspire and anspire_search_enabled:
        results.append(_http_probe(
            "Anspire", "GET", "https://plugin.anspire.cn/api/ntsearch/search",
            headers={"Authorization": f"Bearer {anspire[0]}"},
            params={"query": "test", "top_k": 1},
            secret=anspire[0],
            validate_body=lambda b: (
                ("code" not in b or b.get("code") == 200) and "results" in b,
                f"code={b.get('code')!r}, has_results={'results' in b}",
            ),
        ))
    else:
        results.append(CheckResult("Search API", "Anspire", "plugin.anspire.cn", "SKIP", detail="ANSPIRE_API_KEYS not configured or ANSPIRE_SEARCH_ENABLED=false"))

    return results


# ---------------------------------------------------------------------------
# 3) Notification Channel Connectivity
# ---------------------------------------------------------------------------

def _probe_webhook(name: str, url: str, *, send_test: bool = False,
                   secret: Optional[str] = None, json_body: Optional[dict] = None) -> CheckResult:
    """Probe a webhook-based notification channel.

    Default mode: HEAD/GET request to verify URL is reachable (no message sent).
    --send-test mode: POST a minimal test message.
    """
    start = time.time()
    try:
        if send_test:
            body = json_body or {"text": "[Connectivity Test] DSA notification probe", "msgtype": "text"}
            resp = requests.post(url, json=body, timeout=TIMEOUT,
                                 headers={"Content-Type": "application/json"})
        else:
            resp = requests.head(url, timeout=TIMEOUT, allow_redirects=True)
            if resp.status_code == 405:  # Method not allowed, try GET
                resp = requests.get(url, timeout=TIMEOUT, allow_redirects=True)
        latency = time.time() - start
        if resp.status_code in (200, 201, 204, 302):
            return CheckResult("Notification", name, _netloc(url), "PASS",
                               latency_s=latency, detail=f"HTTP {resp.status_code}{' (+test sent)' if send_test else ''}")
        # Many webhook endpoints return 4xx for HEAD/GET but are still valid
        if not send_test and 400 <= resp.status_code < 500:
            return CheckResult("Notification", name, _netloc(url), "PASS",
                               latency_s=latency, detail=f"HTTP {resp.status_code} (endpoint reachable, use --send-test to verify)")
        detail = f"HTTP {resp.status_code}"
        if secret:
            detail = detail.replace(secret, "***")
        return CheckResult("Notification", name, _netloc(url), "FAIL",
                           latency_s=latency, detail=detail)
    except Exception as exc:
        latency = time.time() - start
        return CheckResult("Notification", name, _netloc(url), "FAIL",
                           latency_s=latency, detail=_short_error(exc, secret))


def _probe_email(sender: str, password: str, *, send_test: bool = False) -> CheckResult:
    """Probe email connectivity by attempting SMTP login.

    Default mode: connect + login only (no message sent).
    --send-test mode: send a minimal test email to self.
    """
    import smtplib
    from email.mime.text import MIMEText

    start = time.time()
    domain = sender.split("@")[-1] if "@" in sender else "unknown"
    smtp_hosts = {
        "gmail.com": ("smtp.gmail.com", 587),
        "outlook.com": ("smtp-mail.outlook.com", 587),
        "hotmail.com": ("smtp-mail.outlook.com", 587),
        "qq.com": ("smtp.qq.com", 587),
        "163.com": ("smtp.163.com", 587),
        "126.com": ("smtp.126.com", 587),
        "sina.com": ("smtp.sina.com", 587),
        "foxmail.com": ("smtp.qq.com", 587),
        "yeah.net": ("smtp.yeah.net", 587),
    }
    host, port = smtp_hosts.get(domain.lower(), (f"smtp.{domain}", 587))
    try:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
        server.starttls()
        server.login(sender, password)
        if send_test:
            msg = MIMEText("[Connectivity Test] DSA notification probe", "plain", "utf-8")
            msg["Subject"] = "DSA Connectivity Test"
            msg["From"] = sender
            msg["To"] = sender
            server.sendmail(sender, [sender], msg.as_string())
        server.quit()
        latency = time.time() - start
        return CheckResult("Notification", "Email", f"{host}:{port}", "PASS",
                           latency_s=latency, detail=f"SMTP login OK ({sender}){' (+test sent)' if send_test else ''}")
    except Exception as exc:
        latency = time.time() - start
        return CheckResult("Notification", "Email", f"{host}:{port}", "FAIL",
                           latency_s=latency, detail=_short_error(exc, password))


def _probe_telegram(token: str, chat_id: str, *, send_test: bool = False) -> CheckResult:
    """Probe Telegram bot connectivity."""
    base = f"https://api.telegram.org/bot{token}"
    start = time.time()
    try:
        method = "sendMessage" if send_test else "getMe"
        params = {} if not send_test else {"chat_id": chat_id, "text": "[Connectivity Test] DSA probe"}
        resp = requests.get(f"{base}/{method}", params=params, timeout=TIMEOUT)
        latency = time.time() - start
        d = resp.json()
        if d.get("ok"):
            bot_name = d.get("result", {}).get("username", "") if not send_test else ""
            return CheckResult("Notification", "Telegram", "api.telegram.org", "PASS",
                               latency_s=latency, detail=f"Bot OK{' (@'+bot_name+')' if bot_name else ''}{' (+test sent)' if send_test else ''}")
        return CheckResult("Notification", "Telegram", "api.telegram.org", "FAIL",
                           latency_s=latency, detail=f"API error: {d.get('description', 'unknown')}")
    except Exception as exc:
        latency = time.time() - start
        return CheckResult("Notification", "Telegram", "api.telegram.org", "FAIL",
                           latency_s=latency, detail=_short_error(exc, token))


def _probe_discord_slack(name: str, webhook_url: str, *, send_test: bool = False) -> CheckResult:
    """Probe Discord/Slack webhook."""
    start = time.time()
    try:
        if send_test:
            resp = requests.post(webhook_url, json={"content": "[Connectivity Test] DSA probe"},
                                 timeout=TIMEOUT, headers={"Content-Type": "application/json"})
        else:
            resp = requests.head(webhook_url, timeout=TIMEOUT, allow_redirects=True)
        latency = time.time() - start
        if resp.status_code in (200, 204, 302):
            return CheckResult("Notification", name, _netloc(webhook_url), "PASS",
                               latency_s=latency, detail=f"HTTP {resp.status_code}{' (+test sent)' if send_test else ''}")
        if not send_test and 400 <= resp.status_code < 500:
            return CheckResult("Notification", name, _netloc(webhook_url), "PASS",
                               latency_s=latency, detail=f"HTTP {resp.status_code} (reachable, use --send-test)")
        return CheckResult("Notification", name, _netloc(webhook_url), "FAIL",
                           latency_s=latency, detail=f"HTTP {resp.status_code}: {resp.text[:80]}")
    except Exception as exc:
        latency = time.time() - start
        return CheckResult("Notification", name, _netloc(webhook_url), "FAIL",
                           latency_s=latency, detail=_short_error(exc))


def check_notification_channels(config, *, send_test: bool = False) -> List[CheckResult]:
    """Check all configured notification channels.

    Channels that are not configured are skipped (SKIP status).
    Channels that are configured get probed:
    - Default: verify endpoint reachability without sending actual messages.
    - --send-test: send a minimal test message to confirm end-to-end delivery.
    """
    results: List[CheckResult] = []

    # WeChat (企业微信 webhook)
    wechat_url = getattr(config, "wechat_webhook_url", None)
    if wechat_url:
        results.append(_probe_webhook("WeChat", wechat_url, send_test=send_test))
    else:
        results.append(CheckResult("Notification", "WeChat", "—", "SKIP", detail="WECHAT_WEBHOOK_URL not configured"))

    # DingTalk
    dingtalk_url = getattr(config, "dingtalk_webhook_url", None)
    dingtalk_secret = getattr(config, "dingtalk_secret", None)
    if dingtalk_url:
        # In --send-test mode, append DingTalk sign if secret is configured
        probe_url = dingtalk_url
        if send_test and dingtalk_secret:
            import hashlib, hmac, base64, urllib.parse
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f'{timestamp}\n{dingtalk_secret}'
            hmac_code = hmac.new(dingtalk_secret.encode('utf-8'),
                                string_to_sign.encode('utf-8'), digestmod=hashlib.sha256).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            sep = "&" if "?" in probe_url else "?"
            probe_url = f"{probe_url}{sep}timestamp={timestamp}&sign={sign}"
        results.append(_probe_webhook("DingTalk", probe_url, send_test=send_test, secret=dingtalk_secret))
    else:
        results.append(CheckResult("Notification", "DingTalk", "—", "SKIP", detail="DINGTALK_WEBHOOK_URL not configured"))

    # Feishu
    try:
        from src.notification_contracts import is_feishu_static_configured
        if is_feishu_static_configured(config):
            feishu_url = getattr(config, "feishu_webhook_url", None)
            feishu_secret = getattr(config, "feishu_webhook_secret", None) or ""
            if feishu_url:
                # In --send-test mode, prepend keyword + add security signature if configured
                if send_test:
                    import hashlib, hmac as _hmac, base64 as _b64
                    keyword = getattr(config, "feishu_webhook_keyword", None) or ""
                    msg_text = f"{keyword}\n[Connectivity Test] DSA probe" if keyword else "[Connectivity Test] DSA probe"
                    body: dict = {"msg_type": "text", "content": {"text": msg_text}}
                    if feishu_secret:
                        timestamp = str(int(time.time()))
                        string_to_sign = f"{timestamp}\n{feishu_secret}"
                        sign = _b64.b64encode(_hmac.new(string_to_sign.encode("utf-8"),
                                                         digestmod=hashlib.sha256).digest()).decode("utf-8")
                        body["timestamp"] = timestamp
                        body["sign"] = sign
                    results.append(_probe_webhook("Feishu", feishu_url, send_test=send_test, json_body=body,
                                                 secret=feishu_secret or None))
                else:
                    results.append(_probe_webhook("Feishu", feishu_url, send_test=send_test))
            else:
                # App bot mode — check API endpoint reachability
                domain = getattr(config, "feishu_domain", "feishu")
                api_host = f"open.{domain}.cn" if domain == "feishu" else f"open.{domain}.com"
                results.append(_probe_webhook("Feishu", f"https://{api_host}/open-apis/bot/v2/hook/test",
                                            send_test=False))
        else:
            results.append(CheckResult("Notification", "Feishu", "—", "SKIP", detail="Feishu not configured"))
    except Exception:
        results.append(CheckResult("Notification", "Feishu", "—", "SKIP", detail="Feishu config unavailable"))

    # Telegram
    tg_token = getattr(config, "telegram_bot_token", None)
    tg_chat = getattr(config, "telegram_chat_id", None)
    if tg_token and tg_chat:
        results.append(_probe_telegram(tg_token, tg_chat, send_test=send_test))
    else:
        results.append(CheckResult("Notification", "Telegram", "—", "SKIP", detail="TELEGRAM_BOT_TOKEN/CHAT_ID not configured"))

    # Email
    email_sender = getattr(config, "email_sender", None)
    email_pass = getattr(config, "email_password", None)
    if email_sender and email_pass:
        results.append(_probe_email(email_sender, email_pass, send_test=send_test))
    else:
        results.append(CheckResult("Notification", "Email", "—", "SKIP", detail="EMAIL_SENDER/PASSWORD not configured"))

    # Pushover
    po_key = getattr(config, "pushover_user_key", None)
    po_token = getattr(config, "pushover_api_token", None)
    if po_key and po_token:
        start = time.time()
        try:
            params = {"user": po_key, "token": po_token}
            if send_test:
                params.update({"message": "[Connectivity Test] DSA probe", "title": "DSA Test"})
            resp = requests.post("https://api.pushover.net/1/messages.json", data=params, timeout=TIMEOUT)
            latency = time.time() - start
            d = resp.json()
            if d.get("status") == 1:
                results.append(CheckResult("Notification", "Pushover", "api.pushover.net", "PASS",
                                          latency_s=latency, detail=f"OK{' (+test sent)' if send_test else ''}"))
            else:
                results.append(CheckResult("Notification", "Pushover", "api.pushover.net", "FAIL",
                                          latency_s=latency, detail=str(d.get("errors", d))))
        except Exception as exc:
            results.append(CheckResult("Notification", "Pushover", "api.pushover.net", "FAIL",
                                      latency_s=time.time() - start, detail=_short_error(exc, po_key)))
    else:
        results.append(CheckResult("Notification", "Pushover", "—", "SKIP", detail="PUSHOVER_USER_KEY/API_TOKEN not configured"))

    # ntfy
    ntfy_url = getattr(config, "ntfy_url", None)
    if ntfy_url:
        try:
            # Parse ntfy URL: https://server/topic or https://server/topic?token=xxx
            from urllib.parse import urlparse
            parsed = urlparse(ntfy_url)
            ntfy_server = f"{parsed.scheme}://{parsed.netloc}"
            ntfy_topic = parsed.path.strip("/")
            if ntfy_topic:
                start = time.time()
                test_url = f"{ntfy_server}/{ntfy_topic}"
                ntfy_token = (getattr(config, "ntfy_token", None) or "").strip()
                headers = {}
                if ntfy_token:
                    headers["Authorization"] = f"Bearer {ntfy_token}"
                if send_test:
                    resp = requests.post(test_url, data="[Connectivity Test] DSA probe",
                                       headers=headers, timeout=TIMEOUT)
                else:
                    resp = requests.head(test_url, timeout=TIMEOUT, allow_redirects=True)
                latency = time.time() - start
                ok = resp.status_code in (200, 201, 204, 302, 404)
                results.append(CheckResult("Notification", "ntfy", _netloc(ntfy_server),
                                          "PASS" if ok else "FAIL",
                                          latency_s=latency, detail=f"HTTP {resp.status_code}{' (+test sent)' if send_test else ''}"))
            else:
                results.append(CheckResult("Notification", "ntfy", "—", "SKIP", detail="NTFY_URL has no topic path"))
        except Exception:
            results.append(CheckResult("Notification", "ntfy", "—", "SKIP", detail="ntfy config unavailable"))
    else:
        results.append(CheckResult("Notification", "ntfy", "—", "SKIP", detail="NTFY_URL not configured"))

    # Gotify
    gotify_url = getattr(config, "gotify_url", None)
    gotify_token = (getattr(config, "gotify_token", None) or "").strip()
    if gotify_url and gotify_token:
        try:
            # Gotify message endpoint: {base_url}/message?token={token}
            from urllib.parse import urlparse
            parsed = urlparse(gotify_url)
            gotify_ep = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}/message"
            start = time.time()
            headers = {"X-Gotify-Key": gotify_token}
            if send_test:
                resp = requests.post(gotify_ep, headers=headers,
                                    json={"title": "DSA", "message": "Connectivity test", "priority": 1},
                                    timeout=TIMEOUT)
            else:
                resp = requests.head(gotify_ep, headers=headers, timeout=TIMEOUT, allow_redirects=True)
            latency = time.time() - start
            ok = resp.status_code in (200, 201, 204, 302)
            results.append(CheckResult("Notification", "Gotify", _netloc(gotify_ep),
                                      "PASS" if ok else "FAIL",
                                      latency_s=latency, detail=f"HTTP {resp.status_code}{' (+test sent)' if send_test else ''}"))
        except Exception:
            results.append(CheckResult("Notification", "Gotify", "—", "SKIP", detail="gotify config unavailable"))
    else:
        results.append(CheckResult("Notification", "Gotify", "—", "SKIP", detail="GOTIFY_URL/TOKEN not configured"))

    # PushPlus
    pp_token = getattr(config, "pushplus_token", None)
    if pp_token:
        start = time.time()
        try:
            if send_test:
                resp = requests.post("https://www.pushplus.plus/send",
                                    json={"token": pp_token, "title": "DSA", "content": "Connectivity test"},
                                    timeout=TIMEOUT)
            else:
                resp = requests.head("https://www.pushplus.plus/send", timeout=TIMEOUT, allow_redirects=True)
            latency = time.time() - start
            ok = resp.status_code in (200, 201, 204, 302)
            results.append(CheckResult("Notification", "PushPlus", "pushplus.plus",
                                      "PASS" if ok else "FAIL",
                                      latency_s=latency, detail=f"HTTP {resp.status_code}{' (+test sent)' if send_test else ''}"))
        except Exception as exc:
            results.append(CheckResult("Notification", "PushPlus", "pushplus.plus", "FAIL",
                                      latency_s=time.time() - start, detail=_short_error(exc, pp_token)))
    else:
        results.append(CheckResult("Notification", "PushPlus", "—", "SKIP", detail="PUSHPLUS_TOKEN not configured"))

    # ServerChan3
    sc_key = getattr(config, "serverchan3_sendkey", None)
    if sc_key:
        start = time.time()
        try:
            url = f"https://sc3.ft07.com/send/{sc_key}.send"
            if send_test:
                resp = requests.post(url, json={"title": "DSA", "desp": "Connectivity test"}, timeout=TIMEOUT)
            else:
                resp = requests.head(url, timeout=TIMEOUT, allow_redirects=True)
            latency = time.time() - start
            ok = resp.status_code in (200, 204, 302)
            results.append(CheckResult("Notification", "ServerChan3", "sc3.ft07.com",
                                      "PASS" if ok else "FAIL",
                                      latency_s=latency, detail=f"HTTP {resp.status_code}{' (+test sent)' if send_test else ''}"))
        except Exception as exc:
            results.append(CheckResult("Notification", "ServerChan3", "sc3.ft07.com", "FAIL",
                                      latency_s=time.time() - start, detail=_short_error(exc, sc_key)))
    else:
        results.append(CheckResult("Notification", "ServerChan3", "—", "SKIP", detail="SERVERCHAN3_SENDKEY not configured"))

    # Custom webhook
    custom_urls = getattr(config, "custom_webhook_urls", None)
    custom_bearer = getattr(config, "custom_webhook_bearer_token", None)
    if custom_urls:
        for i, url in enumerate(custom_urls if isinstance(custom_urls, list) else [custom_urls]):
            # Add bearer token in --send-test mode if configured
            json_body = None
            if send_test and custom_bearer:
                json_body = {"content": "[Connectivity Test] DSA probe"}
                # _probe_webhook doesn't support custom headers, so we handle inline
                start = time.time()
                try:
                    resp = requests.post(url, json=json_body, timeout=TIMEOUT,
                                       headers={"Content-Type": "application/json",
                                               "Authorization": f"Bearer {custom_bearer}"})
                    latency = time.time() - start
                    if resp.status_code in (200, 201, 204):
                        results.append(CheckResult("Notification", f"Custom[{i}]", _netloc(url), "PASS",
                                                  latency_s=latency, detail="HTTP 200 (+test sent)"))
                    else:
                        results.append(CheckResult("Notification", f"Custom[{i}]", _netloc(url), "FAIL",
                                                  latency_s=latency, detail=f"HTTP {resp.status_code}"))
                except Exception as exc:
                    results.append(CheckResult("Notification", f"Custom[{i}]", _netloc(url), "FAIL",
                                              latency_s=time.time() - start, detail=_short_error(exc, custom_bearer)))
            else:
                results.append(_probe_webhook(f"Custom[{i}]", url, send_test=send_test))
    else:
        results.append(CheckResult("Notification", "Custom", "—", "SKIP", detail="CUSTOM_WEBHOOK_URLS not configured"))

    # Discord (webhook OR bot_token + channel_id)
    discord_url = getattr(config, "discord_webhook_url", None)
    discord_bot = getattr(config, "discord_bot_token", None)
    discord_chan = getattr(config, "discord_main_channel_id", None)
    if discord_url:
        results.append(_probe_discord_slack("Discord", discord_url, send_test=send_test))
    elif discord_bot and discord_chan:
        # Bot API mode — probe Discord API gateway
        start = time.time()
        try:
            resp = requests.get("https://discord.com/api/v10/users/@me",
                              headers={"Authorization": f"Bot {discord_bot}"}, timeout=TIMEOUT)
            latency = time.time() - start
            if resp.status_code == 200:
                results.append(CheckResult("Notification", "Discord", "discord.com", "PASS",
                                          latency_s=latency, detail="Bot API OK (+test sent)" if send_test else "Bot API OK"))
            else:
                results.append(CheckResult("Notification", "Discord", "discord.com", "FAIL",
                                          latency_s=latency, detail=f"HTTP {resp.status_code}"))
        except Exception as exc:
            results.append(CheckResult("Notification", "Discord", "discord.com", "FAIL",
                                      latency_s=time.time() - start, detail=_short_error(exc, discord_bot)))
    else:
        results.append(CheckResult("Notification", "Discord", "—", "SKIP", detail="DISCORD_WEBHOOK_URL or DISCORD_BOT_TOKEN+CHANNEL_ID not configured"))

    # Slack (webhook OR bot_token + channel_id)
    slack_url = getattr(config, "slack_webhook_url", None)
    slack_bot = getattr(config, "slack_bot_token", None)
    slack_chan = getattr(config, "slack_channel_id", None)
    if slack_url:
        results.append(_probe_discord_slack("Slack", slack_url, send_test=send_test))
    elif slack_bot and slack_chan:
        # Bot API mode — probe Slack auth.test
        start = time.time()
        try:
            resp = requests.post("https://slack.com/api/auth.test",
                               headers={"Authorization": f"Bearer {slack_bot}"}, timeout=TIMEOUT)
            latency = time.time() - start
            d = resp.json()
            if d.get("ok"):
                results.append(CheckResult("Notification", "Slack", "slack.com", "PASS",
                                          latency_s=latency, detail="Bot API OK" + (" (+test sent)" if send_test else "")))
            else:
                results.append(CheckResult("Notification", "Slack", "slack.com", "FAIL",
                                          latency_s=latency, detail=f"API error: {d.get('error', 'unknown')}"))
        except Exception as exc:
            results.append(CheckResult("Notification", "Slack", "slack.com", "FAIL",
                                      latency_s=time.time() - start, detail=_short_error(exc, slack_bot)))
    else:
        results.append(CheckResult("Notification", "Slack", "—", "SKIP", detail="SLACK_WEBHOOK_URL or SLACK_BOT_TOKEN+CHANNEL_ID not configured"))

    # AstrBot
    astr_url = getattr(config, "astrbot_url", None)
    astr_token = getattr(config, "astrbot_token", None)
    if astr_url:
        if send_test and astr_token:
            # AstrBot requires HMAC signature (X-Signature + X-Timestamp)
            # Replicate the signing logic from astrbot_sender.py
            import hashlib, hmac
            start = time.time()
            try:
                payload = {"content": "[Connectivity Test] DSA probe"}
                timestamp = str(int(time.time()))
                payload_json = json.dumps(payload, sort_keys=True)
                sign_data = f"{timestamp}.{payload_json}".encode('utf-8')
                signature = hmac.new(astr_token.encode('utf-8'), sign_data, hashlib.sha256).hexdigest()
                resp = requests.post(astr_url, json=payload, timeout=TIMEOUT,
                                   headers={"Content-Type": "application/json",
                                           "X-Signature": signature, "X-Timestamp": timestamp})
                latency = time.time() - start
                if resp.status_code in (200, 201, 204):
                    results.append(CheckResult("Notification", "AstrBot", _netloc(astr_url), "PASS",
                                              latency_s=latency, detail="Signed send OK (+test sent)"))
                else:
                    results.append(CheckResult("Notification", "AstrBot", _netloc(astr_url), "FAIL",
                                              latency_s=latency, detail=f"HTTP {resp.status_code}"))
            except Exception as exc:
                results.append(CheckResult("Notification", "AstrBot", _netloc(astr_url), "FAIL",
                                          latency_s=time.time() - start, detail=_short_error(exc, astr_token)))
        else:
            results.append(_probe_webhook("AstrBot", astr_url, send_test=send_test))
    else:
        results.append(CheckResult("Notification", "AstrBot", "—", "SKIP", detail="ASTRBOT_URL not configured"))

    return results


# ---------------------------------------------------------------------------
# 4) Model Catalog Fetch from base_url/models
# ---------------------------------------------------------------------------
def _fetch_openai_models(base_url: Optional[str], api_key: Optional[str], timeout: int):
    """Fetch model catalog from OpenAI-compatible endpoint {base_url}/models.

    Returns (ids, error): success when error=None with ids list; failure when ids=None.
    """
    if not base_url:
        return None, "No base_url (default endpoint, skipped)"
    base = str(base_url).rstrip("/")
    url = base + "/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        import requests
        resp = requests.get(url, headers=headers, timeout=timeout)
    except Exception as exc:
        return None, "Request error: " + _short_error(exc, api_key)
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: " + _short_error(Exception(resp.text[:300]), api_key)
    try:
        data = resp.json()
    except Exception as exc:
        return None, "Non-JSON response: " + _short_error(exc, api_key)
    ids: List[str] = []
    items = None
    if isinstance(data, dict):
        candidate = data.get("data") if isinstance(data.get("data"), list) else data.get("models")
        items = candidate
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
    if not ids:
        return [], "Empty model list or unknown structure: " + _short_error(Exception(resp.text[:300]), api_key)
    return ids, None


# ---------------------------------------------------------------------------
# 4) List and Validate Models (config-only, no network)
# ---------------------------------------------------------------------------
def list_and_validate_models(config) -> List[CheckResult]:
    results: List[CheckResult] = []
    channels = getattr(config, "llm_channels", None) or []
    model_list = getattr(config, "llm_model_list", None) or []
    raw_channels_str = (os.getenv("LLM_CHANNELS") or "").strip()
    raw_names = [c.strip() for c in raw_channels_str.split(",") if c.strip()]

    resolved_names = {str(ch.get("name") or "").lower() for ch in channels}

    if raw_names:
        for name in raw_names:
            if name.lower() in resolved_names:
                continue
            up = name.upper()
            has_models = bool((os.getenv(f"LLM_{up}_MODELS") or "").strip())
            has_key = bool(
                (os.getenv(f"LLM_{up}_API_KEYS") or os.getenv(f"LLM_{up}_API_KEY") or "").strip()
            )
            enabled_raw = os.getenv(f"LLM_{up}_ENABLED")
            disabled = enabled_raw is not None and enabled_raw.strip().lower() in (
                "false", "0", "no", "off",
            )
            if disabled:
                results.append(CheckResult(
                    "LLM Config", name, "-", "SKIP",
                    detail=f"Disabled (LLM_{up}_ENABLED=false), channel discarded",
                ))
            elif not has_models:
                results.append(CheckResult(
                    "LLM Config", name, "-", "FAIL",
                    detail=f"No model_name set (LLM_{up}_MODELS empty), channel discarded",
                ))
            elif not has_key:
                results.append(CheckResult(
                    "LLM Config", name, "-", "WARN",
                    detail=f"No API key (LLM_{up}_API_KEY(S) empty), channel discarded",
                ))
            else:
                results.append(CheckResult(
                    "LLM Config", name, "-", "WARN",
                    detail="Not parsed (unsupported protocol or invalid config), channel discarded",
                ))
    elif model_list:
        results.append(CheckResult(
            "LLM Config", "legacy (LITELLM_MODEL)", "-", "PASS",
            detail="No LLM_CHANNELS declared, using legacy LITELLM_MODEL path",
        ))
    else:
        results.append(CheckResult(
            "LLM Config", "No config", "-", "WARN",
            detail="No LLM channels parsed (configure LLM_CHANNELS or LITELLM_MODEL).",
        ))

    for ch in channels:
        name = ch.get("name") or "?"
        models = ch.get("models") or []
        base_url = ch.get("base_url")
        if not models:
            results.append(CheckResult(
                "LLM Config", name, _netloc(base_url), "FAIL",
                detail="Model list is empty",
            ))
        else:
            results.append(CheckResult(
                "LLM Config", name, _netloc(base_url), "PASS",
                detail="models: " + ", ".join(models),
            ))

    for e in model_list:
        if not e.get("model_name"):
            wire = (e.get("litellm_params") or {}).get("model") or "?"
            results.append(CheckResult(
                "Expanded Model", wire, "-", "WARN", detail="model_name is empty",
            ))
    return results


# ---------------------------------------------------------------------------
# Report Rendering
# ---------------------------------------------------------------------------
_STATUS_ICON = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️", "SKIP": "⏭️"}


def render_markdown(results: List[CheckResult], env_label: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    counts = {s: 0 for s in _STATUS_ICON}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    lines: List[str] = []
    lines.append("# 🔌 Connectivity Check Report")
    lines.append("")
    lines.append(f"- Time: {now}")
    lines.append(f"- Environment: {env_label}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(
        f"- ✅ Pass: **{counts.get('PASS', 0)}**  "
        f"- ❌ Fail: **{counts.get('FAIL', 0)}**  "
        f"- ⚠️ Warn: **{counts.get('WARN', 0)}**  "
        f"- ⏭️ Skip: **{counts.get('SKIP', 0)}**"
    )
    lines.append("")

    by_cat: Dict[str, List[CheckResult]] = {}
    for r in results:
        by_cat.setdefault(r.category, []).append(r)

    for cat, items in by_cat.items():
        lines.append(f"## {cat}")
        lines.append("")
        lines.append("| Name | Endpoint | Latency | Status | Detail |")
        lines.append("| --- | --- | --- | --- | --- |")
        for r in items:
            lat = f"{r.latency_s:.2f}s" if r.latency_s is not None else "-"
            lines.append(
                f"| {r.name} | {r.target} | {lat} | {_STATUS_ICON.get(r.status, r.status)} {r.status} | {r.detail} |"
            )
        lines.append("")

    fails = [r for r in results if r.status == "FAIL"]
    if fails:
        lines.append("## Failure Details")
        lines.append("")
        for r in fails:
            lines.append(f"- **{r.category} / {r.name}** ({r.target}): {r.detail}")
        lines.append("")
    return "\n".join(lines)


def write_reports(markdown: str, results: List[CheckResult]) -> Path:
    reports_dir = REPO_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")
    md_path = reports_dir / f"connectivity_{date_str}.md"
    json_path = reports_dir / f"connectivity_{date_str}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(
        [r.__dict__ for r in results], ensure_ascii=False, indent=2
    ), encoding="utf-8")
    return md_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    configure_console_encoding()

    parser = argparse.ArgumentParser(description="DSA - Connectivity Check")
    parser.add_argument("--llm-only", action="store_true", help="Check LLM channels only")
    parser.add_argument("--search-only", action="store_true", help="Check search API keys only")
    parser.add_argument("--notification-only", action="store_true", help="Check notification channels only")
    parser.add_argument("--send-test", action="store_true",
                        help="Send actual test messages to notification channels (default: probe endpoints only)")
    parser.add_argument("--list-models", action="store_true",
                        help="List LLM models and fetch catalog from base_url/models")
    args = parser.parse_args()

    env_label = "GitHub Actions (CI)" if os.getenv("GITHUB_ACTIONS") == "true" else "Local"

    print("\n" + "=" * 60)
    print("  🔌 Connectivity Check")
    print("=" * 60)
    print(f"  Environment: {env_label}")
    print(f"  Timeout: {TIMEOUT}s")
    print("=" * 60 + "\n")

    if args.list_models:
        try:
            from src.config import get_config
            config = get_config()
        except Exception as exc:
            logger.warning(f"Failed to load LLM config: {exc}")
            print("\n❌ Cannot load config, exit code 1.")
            return 1

        expanded = []
        try:
            from src.config import get_configured_llm_models
            expanded = get_configured_llm_models(config.llm_model_list)
        except Exception:
            pass

        print("📋 LLM_CHANNELS parsed models:")
        for m in expanded:
            print(f"   - {m}")
        print("")

        print("🔎 Channel endpoint model catalogs (GET base_url/models):")
        channels = getattr(config, "llm_channels", None) or []
        for ch in channels:
            name = ch.get("name") or "?"
            up = str(name).upper()
            base_url = ch.get("base_url")
            models = ch.get("models") or []
            key = (os.getenv(f"LLM_{up}_API_KEYS") or os.getenv(f"LLM_{up}_API_KEY") or "").strip()
            print(f"\n   • Channel: {name}")
            print(f"     base_url: {base_url or '(default endpoint)'}")
            ids, err = _fetch_openai_models(base_url, key, TIMEOUT)
            if err:
                print(f"     Fetch failed: {err}")
            elif ids:
                print(f"     Available models ({len(ids)}):")
                for mid in ids:
                    print(f"       - {mid}")
            else:
                print("     No models returned (endpoint may not support /models)")
            print(f"     Configured models: {', '.join(models) if models else '(empty)'}")
        print("")

        results = list_and_validate_models(config)
        markdown = render_markdown(results, env_label)
        print(markdown)
        try:
            md_path = write_reports(markdown, results)
            print(f"\n📄 Report written to: {md_path}")
        except Exception as exc:
            logger.warning(f"Failed to write report: {exc}")

        missing = [r for r in results if r.status == "FAIL"]
        if missing:
            print(f"\n❌ {len(missing)} channels missing model_name, exit code 1.")
            return 1
        print("\n✅ All channels have model_name configured.")
        return 0

    results: List[CheckResult] = []
    _llm_config_failed = False

    # Load config once for LLM + notification checks
    config = None
    if not args.search_only:
        try:
            from src.config import get_config
            config = get_config()
        except Exception as exc:
            logger.warning(f"Failed to load config: {exc}")
            _llm_config_failed = True

    if config and not args.search_only and not args.notification_only:
        results += check_llm_channels(config)

    if not args.llm_only and not args.notification_only:
        results += check_search_keys()

    # Notification channels (requires config)
    if not args.llm_only and not args.search_only:
        if config:
            try:
                results += check_notification_channels(config, send_test=args.send_test)
            except Exception as exc:
                logger.warning(f"Notification channel check failed: {exc}")
        elif not _llm_config_failed:
            logger.info("Notification check skipped: config not available")

    # Fail-closed: if LLM config failed and we have no results at all,
    # or if --llm-only was used and config load failed, exit 1.
    if _llm_config_failed and args.llm_only and not results:
        print("\n❌ LLM config load failed, no checks performed, exit code 1.")
        return 1
    if _llm_config_failed and not results:
        print("\n❌ All config load failed, no checks performed, exit code 1.")
        return 1

    markdown = render_markdown(results, env_label)
    print(markdown)

    try:
        md_path = write_reports(markdown, results)
        print(f"\n📄 Report written to: {md_path}")
    except Exception as exc:
        logger.warning(f"Failed to write report: {exc}")

    failed = sum(1 for r in results if r.status == "FAIL")
    if failed:
        print(f"\n❌ {failed} connectivity failures, exit code 1.")
        return 1
    print("\n✅ No connectivity failures.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
