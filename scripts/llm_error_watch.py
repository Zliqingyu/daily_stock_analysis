#!/usr/bin/env python3
"""LLM 错误审查哨兵 — 引擎日志出现 ERROR 时自动请 GLM 审查
用法: cron 每 5 分钟 (有状态: offset + 去重, 无重复审查)
输出: ~/ats/llm_reviews.log + 邮件
"""
import json, os, re, smtplib, sys, urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.header import Header

HOME = os.path.expanduser("~")
LOG = f"{HOME}/ats/ats.log"
STATE = f"{HOME}/ats/.llm_watch_state.json"
OUT = f"{HOME}/ats/llm_reviews.log"
for p in (f"{HOME}/dus/.env", f"{HOME}/ats/.env"):
    if os.path.exists(p):
        for line in open(p):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)

GLM_KEY = os.getenv("GLM_KEY", "")
GLM_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"
GLM_MODEL = os.getenv("GLM_MODEL", "glm-5.3-flash")
DEDUP_MIN = 45          # 同类错误 45 分钟内只审一次
MAX_LOG_CTX = 60        # 附带日志上下文行数

def load_state():
    try: return json.load(open(STATE))
    except Exception: return {"offset": 0, "seen": {}}

def save_state(s):
    # seen 保留 2 小时
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    s["seen"] = {k: v for k, v in s.get("seen", {}).items() if v > cutoff}
    json.dump(s, open(STATE, "w"))

def read_new_errors():
    """读 offset 之后的新日志, 抽 ERROR/CRITICAL 行"""
    s = load_state()
    try:
        size = os.path.getsize(LOG)
    except FileNotFoundError:
        return s, []
    off = s.get("offset")
    if off is None or off > size:   # 首次运行 / 日志轮转 → 从末尾, 只看新错误
        s["offset"] = size
        save_state(s)
        return s, []
    off = off
    with open(LOG, errors="ignore") as f:
        f.seek(off)
        fresh = f.read()
    s["offset"] = off + len(fresh.encode("utf-8", "ignore"))
    # 免费数据源/网络噪音: 不审查不推送 (用户 2026-09-13 要求)
    _NOISE = ("Too Many Requests", "RemoteDisconnected", "Connection aborted",
              "ConnectionError", "Max retries", "请求超时", "熔断器",
              "Rate limit", "RATE LIMIT", "限流", "remote_disconnect",
              "Connection reset", "Event loop is closed", "CancelledError",
              "lifespan", "Parameter ordId error")
    errs = [l for l in fresh.splitlines()
            if ("[ERROR]" in l or "[CRITICAL]" in l)
            and not any(p in l for p in _NOISE)]
    return s, errs

def fingerprint(line):
    """同类错误归一: 去数字/id/价格"""
    return re.sub(r"[0-9]+\.[0-9]+|[0-9]{6,}|clOrdId[^,]*|'[^']{20,}'", "N", line)[:200]

def gather_context(err_lines):
    """错误行 + 前后日志上下文"""
    with open(LOG, errors="ignore") as f:
        lines = f.readlines()
    idxs = []
    for e in err_lines[-3:]:
        for i, l in enumerate(lines):
            if l.rstrip() == e.rstrip():
                idxs.append(i); break
    ctx = []
    for i in sorted(set(idxs)):
        ctx += lines[max(0, i-8):i+8]
    return "".join(ctx)[-4000:]

def ask_llm(err_lines, context):
    prompt = f"""你是交易系统值班审查员。加密货币交易引擎(OKX demo现货, FIXED_CAPITAL虚拟账本, 动量轮动策略)日志出现错误。

错误行:
{chr(10).join(err_lines[-5:])}

日志上下文:
{context}

审查后输出 JSON (不要输出其他内容, 不要 markdown 代码块):
{{"action": "NONE|RESTART|PATCH|ESCALATE",
 "reason": "中文一句话: 根因+为什么选这个动作",
 "patch": {{"file": "/home/kos/ats/完整路径.py", "old": "要替换的精确原文", "new": "修复后的文本"}}

决策规则:
- 良性错误(已自动重试成功/偶发网络抖动) → action=NONE
- 进程状态坏掉/引擎无响应, 重启能解决 → action=RESTART
- 明确的代码 bug 且你能给出精确的 old/new 文本修复 → action=PATCH
  (old 必须是从现有文件能唯一匹配的原文; 只改 /home/kos/ats/ 下的 .py)
- 需要人工判断/涉及资金逻辑/你不确定 → action=ESCALATE
不确定时一律 ESCALATE, 宁可打扰人不可乱修。"""

    body = json.dumps({
        "model": GLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
    }).encode()
    req = urllib.request.Request(GLM_URL, data=body, headers={
        "Authorization": f"Bearer {GLM_KEY}", "Content-Type": "application/json"})
    r = json.loads(urllib.request.urlopen(req, timeout=120).read())
    msg = r["choices"][0]["message"]
    text = (msg.get("content") or "").strip()
    if not text:
        return json.dumps({"action": "ESCALATE", "reason": "LLM 空回复"})
    # 提取 JSON
    import re as _re
    m = _re.search(r"\{.*\}", text, _re.S)
    if m:
        try:
            d = json.loads(m.group())
            if "action" in d:
                return json.dumps(d, ensure_ascii=False)
        except Exception:
            pass
    return json.dumps({"action": "ESCALATE",
                       "reason": f"LLM 输出非结构化: {text[:300]}"}, ensure_ascii=False)

def mail(review, errs):
    """飞书推送 (不再发邮件)"""
    import subprocess
    first = errs[0][:80] if errs else ""
    subprocess.run([f"{HOME}/dsa/.venv/bin/python",
                    f"{HOME}/pi-feishu-bridge/feishu_send.py",
                    f"⚠ ats 引擎错误审查\n{first}\n----------\n{review}"],
                   timeout=30, capture_output=True)

def main():
    s, errs = read_new_errors()
    if not errs:
        save_state(s); return
    now = datetime.now(timezone.utc).isoformat()
    to_review = []
    for e in errs:
        fp = fingerprint(e)
        if now > s["seen"].get(fp, ""):      # 未审过或已过冷却
            s["seen"][fp] = (datetime.now(timezone.utc) + timedelta(minutes=DEDUP_MIN)).isoformat()
            to_review.append(e)
    save_state(s)
    if not to_review:
        return
    ctx = gather_context(to_review)
    try:
        review = ask_llm(to_review, ctx)
    except Exception as e:
        review = f"[LLM 调用失败: {e}]"
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    entry = f"\n{'='*60}\n[{stamp}] {len(to_review)} 个新错误\n" + "\n".join(to_review[-5:]) + f"\n\n--- GLM 决策 ---\n{review}\n"
    open(OUT, "a").write(entry)
    # 自愈执行 (ats 主引擎; PATCH/RESTART 带回滚护栏)
    action = "ESCALATE"
    try:
        action = json.loads(review).get("action", "ESCALATE")
    except Exception:
        pass
    if action in ("RESTART", "PATCH"):
        sys.path.insert(0, f"{HOME}/ats/scripts")
        import self_heal
        result = self_heal.heal(json.loads(review))
        entry += f"\n--- 自愈结果 ---\n{result}\n"
        open(OUT, "a").write(entry)
        mail(f"动作: {action}\n{result}", to_review)
    else:
        mail(review, to_review)
    print(f"[{stamp}] 审查完成: {len(to_review)} 错误, 动作={action}")

if __name__ == "__main__":
    main()
