#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1-A 研究：拉取 ChangePCT<=30 的横截面（默认缓存被 9.5 截断，不能复用）。"""
import json, os, subprocess, sys, time

W = os.path.expanduser("~/.local/bin/westock")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "p1a_data")
DB = "/home/kevin/MyData/kline-select-stocks-approach/database"
EXPR = ("intersect([ChangePCT >= 2, ChangePCT <= 30, MainNetFlow > 0, "
        "MainNetFlow5D > 0, IsDelisted == 0, Ifsuspend == 0])")
os.makedirs(OUT, exist_ok=True)
days = json.load(open(os.path.join(DB, "trading_calendar.json")))["days"]
days = [d for d in days if "2026-06-01" <= d <= "2026-10-14"]

def md(txt):
    tbl = [l for l in txt.splitlines() if l.strip().startswith("|")]
    if len(tbl) < 2:
        return []
    hdr = [x.strip() for x in tbl[0].strip().strip("|").split("|")]
    out = []
    for ln in tbl[1:]:
        c = [x.strip() for x in ln.strip().strip("|").split("|")]
        if len(c) == len(hdr) and not all(set(v) <= set("-: ") for v in c):
            out.append(dict(zip(hdr, c)))
    return out

for i, d in enumerate(days, 1):
    path = os.path.join(OUT, f"cross30_{d}.json")
    if os.path.exists(path):
        print(f"{i}/{len(days)} {d} cached", flush=True)
        continue
    rows = None
    for attempt in range(2):
        p = subprocess.run([W, "screen", "condition", "--expression", EXPR,
                            "--date", d, "--limit", "6000"],
                           capture_output=True, timeout=300)
        rows = md(p.stdout.decode("utf-8", "replace"))
        if rows:
            break
        time.sleep(2)
    if not rows:
        print(f"{i}/{len(days)} {d} EMPTY/FAIL", flush=True)
        continue
    json.dump(rows, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"{i}/{len(days)} {d} {len(rows)}", flush=True)
    time.sleep(0.45)
print("done", flush=True)
