#!/usr/bin/env python3
"""Weekly report: python3 report.py  -> weekly/YYYY-MM-DD.md (SGT date).

Muse comparison: drop Muse's closed trades into muse/trades_4h.csv (same columns
as accounts/4h/trades.csv: coin,side,entry_time,entry,exit_time,exit,reason,net_pnl).
"""
import csv, json, os, time
from datetime import datetime, timedelta
import engine as E

ROOT = E.ROOT


def load_trades(path):
    return list(csv.DictReader(open(path))) if os.path.exists(path) else []


def stats(rows, since=None):
    if since:
        rows = [r for r in rows if r["exit_time"] >= since]
    n = len(rows)
    net = sum(float(r["net_pnl"]) for r in rows)
    wins = sum(1 for r in rows if float(r["net_pnl"]) > 0)
    return n, wins, net


def line(name, rows, since):
    n, w, net = stats(rows, since)
    N, W, NET = stats(rows)
    wr = f"{w/n:.0%}" if n else "—"
    WR = f"{W/N:.0%}" if N else "—"
    return f"| {name} | {n} | {wr} | {net:+.2f} | {N} | {WR} | {NET:+.2f} |"


def main():
    now = datetime.now(E.SGT)
    since = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
    data = json.load(open(os.path.join(ROOT, "docs", "data.json")))
    out = [f"# 周报 {now:%Y-%m-%d}（新加坡时间，规则 {E.RULES_VERSION}）", "",
           f"统计窗口：{since} → {now:%Y-%m-%d %H:%M}；起始：{E.fmt_t(data['accounts']['4h']['state']['start_ms'])}", "",
           "| 盘 | 本周平仓 | 本周胜率 | 本周净盈亏 | 累计平仓 | 累计胜率 | 累计净盈亏 |",
           "|---|---|---|---|---|---|---|"]
    trades = {}
    for tf in ("4h", "1h"):
        trades[tf] = load_trades(os.path.join(ROOT, "accounts", tf, "trades.csv"))
        out.append(line(f"Claude {tf}", trades[tf], since))
    muse = load_trades(os.path.join(ROOT, "muse", "trades_4h.csv"))
    out.append(line("Muse 4h", muse, since) if muse else "| Muse 4h | 未提供数据（放入 muse/trades_4h.csv） | | | | | |")
    out += ["", "## 当前持仓（按最近数据快照）", ""]
    for tf, acc in data["accounts"].items():
        pos = acc["state"]["positions"]
        if not pos:
            out.append(f"- {tf}：空仓")
        for c, p in pos.items():
            px = data["spot"][c]["coinbase"] or data["spot"][c]["coingecko"]
            fl = E.pnl(p["side"], p["entry"], px) if px else 0
            out.append(f"- {tf} {c} {'空' if p['side']=='short' else '多'} @ {p['entry']:.6g}，止损 {p['stop']:.6g}，"
                       f"目标 {p['target']:.6g}，现价 {px}，浮动 {fl:+.2f}（不计入战绩）")
    out += ["", "## 本周平仓明细", ""]
    for tf in ("4h", "1h"):
        rows = [r for r in trades[tf] if r["exit_time"] >= since]
        out.append(f"**{tf}**：" + ("无" if not rows else ""))
        for r in rows:
            out.append(f"- {r['coin']} {r['side']} {r['entry_time']} @ {r['entry']} → {r['exit_time']} @ {float(r['exit']):.6g}，{r['reason']}，{float(r['net_pnl']):+.2f}")
        out.append("")
    out.append("样本很小，结果只说明这段时间这套规则的表现。")
    os.makedirs(os.path.join(ROOT, "weekly"), exist_ok=True)
    p = os.path.join(ROOT, "weekly", f"{now:%Y-%m-%d}.md")
    open(p, "w").write("\n".join(out) + "\n")
    print(p)


if __name__ == "__main__":
    main()
