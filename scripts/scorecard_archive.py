#!/usr/bin/env python3
"""LLM 评分存档器 — 把每日报告的 (代码/评分/动作/价位) 追加到 scorecard.jsonl
目的: 30天后统计 "评分>=60 的票" vs 实际走势, 验证 LLM 评分体系是否有 alpha
cron: 每天 20:35 (晚报生成后) 运行一次
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
REPORTS = HOME / "reports"
OUT = HOME / "data" / "scorecard.jsonl"

# 摘要行格式: 🟢 **南网能源(003035)**: 买入 | 评分 62 | 看多 | 买入 回落买入位：5.84元（... 止盈 全部止盈位：6.45元（... 止损 清仓止损位：5.60元
LINE = re.compile(
    r"([\u4e00-\u9fa5A-Za-z0-9]{2,14})\((\d{6})\)\*{0,2}[:：]\s*"
    r"(买入|持有|观望|回避|减仓)\s*\|\s*评分\s*(\d+)")
PRICE = re.compile(r"(回落买入位|理想建仓位)[：:]?\s*([\d.]+)元")
STOP = re.compile(r"清仓止损位[：:]?\s*([\d.]+)元")
TP = re.compile(r"全部止盈位[：:]?\s*([\d.]+)元")


def parse_report(path: Path) -> list[dict]:
    text = path.read_text(errors="ignore")
    # 只取摘要段 (前 60 行, 避免正文重复)
    head = "\n".join(text.splitlines()[:60])
    recs = []
    for m in LINE.finditer(head):
        name, code, action, score = m.group(1), m.group(2), m.group(3), int(m.group(4))
        seg = head[m.start():m.start() + 400]
        entry = PRICE.search(seg)
        stop = STOP.search(seg)
        tp = TP.search(seg)
        recs.append({
            "date": path.stem.split("_")[-1],
            "code": code, "name": name, "action": action, "score": score,
            "entry": float(entry.group(2)) if entry else None,
            "stop": float(stop.group(1)) if stop else None,
            "tp": float(tp.group(1)) if tp else None,
        })
    return recs


def main():
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    targets = sorted(REPORTS.glob(f"report*{today}.md"))
    if not targets:
        print(f"no report for {today}")
        return
    recs = parse_report(targets[-1])
    if not recs:
        print("no scored rows parsed")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "a") as f:
        for r in recs:
            r["captured_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"archived {len(recs)} scores -> {OUT}")


if __name__ == "__main__":
    main()
