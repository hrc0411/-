#!/usr/bin/env python3
"""US-stock SHORT-only paper account (rules: STOCK_SHORT_RULES.md, stock-short-v0.1).

Mirror image of the long-only account in stocks.py (which is left untouched):
short overbought bounces inside established downtrends, cover on the pullback.
Run after stocks.py; idempotent. Stdlib only. Pure simulation.
"""
import csv, json, os
from datetime import datetime, timezone, timedelta
import stocks as S

S.DIR = DIR = os.path.join(S.ROOT, "accounts", "us-short")   # redirect S.log / S.save / S.trade_row
RULES = "stock-short-v0.1"
RSI_MIN = 90.0


def equity(st, prices):
    v = st["cash"]
    for t, p in st["positions"].items():
        px = prices.get(t) or p["entry"]
        v += S.SLOT + p["shares"] * (p["entry"] - px)   # margin back + short P&L
    return v


def evaluate(st, hist, now_et):
    last = st["last_date"]
    dates = sorted({b["d"] for bars in hist.values() if bars for b in bars if b["d"] > last})
    if not dates:
        return False
    lines = []
    for d in dates:
        is_latest = d == dates[-1]
        day = []
        for t in list(st["positions"]):
            bars = [b for b in (hist.get(t) or []) if b["d"] <= d]
            if not bars or bars[-1]["d"] != d:
                day.append(f"- {t}：{d} 无数据（数据缺失），持仓不动")
                continue
            p = st["positions"][t]
            p["days"] += 1
            ind = S.indicators(bars)
            c = ind["close"]
            why = None
            if c >= p["entry"] * (1 + S.STOP_PCT):
                why = "止损（收盘 ≥ 入场 +8%）"
            elif ind["sma200"] and c > ind["sma200"]:
                why = "趋势破坏（收盘 > SMA200）"
            elif ind["sma5"] and c < ind["sma5"]:
                why = "回落兑现（收盘 < SMA5）"
            elif p["days"] >= S.MAX_DAYS:
                why = f"时间止损（持有 {p['days']} 个交易日）"
            if why:
                net = p["shares"] * (p["entry"] - c) - S.FEE
                st["cash"] += S.SLOT + p["shares"] * (p["entry"] - c) - S.FEE / 2
                st["realized"] += net
                st["closed"] += 1
                st["wins"] += net > 0
                S.trade_row([t, p["entry_date"], round(p["entry"], 4), round(p["shares"], 6), d, round(c, 4), why,
                             round(net, 2), round(100 * (1 - c / p["entry"]), 2)])
                st["exited_today"] = {"d": d, "t": t}
                del st["positions"][t]
                day.append(f"- **回补 {t}** @ {c:.2f}（{why}），净盈亏 {net:+.2f} USD")
            else:
                day.append(f"- 持有空单 {t}：收盘 {c:.2f}，SMA5 {S.f2(ind['sma5'])}，第 {p['days']} 天")
        if is_latest:
            cands, notes = [], []
            for t in S.UNIVERSE:
                bars = [b for b in (hist.get(t) or []) if b["d"] <= d]
                if not bars or bars[-1]["d"] != d:
                    notes.append(f"{t} 数据缺失")
                    continue
                ind = S.indicators(bars)
                st["watch"][t] = ind
                if t in st["positions"] or (st.get("exited_today", {}).get("t") == t and st["exited_today"]["d"] == d):
                    continue
                if None in (ind["sma50"], ind["sma200"], ind["rsi2"], ind["mom126"]):
                    continue
                if ind["close"] < ind["sma200"] and ind["sma50"] < ind["sma200"] and ind["rsi2"] > RSI_MIN:
                    cands.append((ind["mom126"], t, ind))
            cands.sort()  # weakest 126-day momentum first
            free = S.MAX_POS - len(st["positions"])
            for mom, t, ind in cands[:max(free, 0)]:
                c = ind["close"]
                sh = S.SLOT / c
                st["cash"] -= S.SLOT + S.FEE / 2
                st["positions"][t] = {"entry": c, "entry_date": d, "shares": sh, "days": 0, "side": "short"}
                day.append(f"- **做空 {t}** @ {c:.2f}（{sh:.4f} 股 ≈ 2000 USD）。理由：收盘 < SMA200、SMA50 < SMA200、"
                           f"RSI(2) {ind['rsi2']:.1f} > 90；126 日动量 {mom:+.1%}")
            for mom, t, ind in cands[max(free, 0):]:
                day.append(f"- {t} 满足做空条件但仓位已满（动量 {mom:+.1%}）")
            if not cands:
                day.append("- 无做空信号（没有处于下降趋势且 RSI(2) > 90 的股票）")
            if notes:
                day.append("- " + "，".join(notes))
        lines.append(f"### 交易日 {d}\n" + "\n".join(day))
        st["last_date"] = d
    bench = hist.get(S.BENCH)
    if bench:
        st["bench_last"] = bench[-1]["c"]
        st.setdefault("bench_start", bench[-1]["c"])
    eq = equity(st, {t: (hist[t][-1]["c"] if hist.get(t) else None) for t in st["positions"]})
    st["equity"].append({"d": st["last_date"], "equity": round(eq, 2),
                         "bench": round(S.START_CASH * st["bench_last"] / st["bench_start"], 2) if st.get("bench_start") else None})
    S.log(f"## {now_et:%Y-%m-%d %H:%M} ET 评估（权益 {eq:.2f}，已实现 {st['realized']:+.2f}，空单 {len(st['positions'])}）\n\n"
          + "\n\n".join(lines) + "\n")
    return True


def main():
    now = datetime.now(timezone.utc)
    now_et = now.astimezone(S.ET)
    p = os.path.join(DIR, "state.json")
    st = json.load(open(p)) if os.path.exists(p) else None
    first = st is None
    lp = os.path.join(S.ROOT, "accounts", "us", "state.json")
    quotes = json.load(open(lp)).get("quotes", {}) if os.path.exists(lp) else {}   # reuse long account's quotes
    d = now_et if now_et.hour * 60 + now_et.minute >= 16 * 60 + 15 else now_et - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    expected = d.strftime("%Y-%m-%d")
    changed = False
    if first or (expected > st["last_date"] and expected != st.get("checked_for")):
        hist = {}
        for t in S.UNIVERSE + [S.BENCH]:
            _, bars = S.chart(t, "2y")
            hist[t] = S.completed(bars, now_et) if bars else None
        if first:
            last = max(b[-1]["d"] for b in hist.values() if b)
            st = {"rules": RULES, "side": "short", "start": now.astimezone(S.SGT).strftime("%Y-%m-%d %H:%M SGT"),
                  "last_date": last, "cash": S.START_CASH, "positions": {}, "realized": 0.0, "closed": 0, "wins": 0,
                  "equity": [], "watch": {t: S.indicators(b) for t, b in hist.items() if b and t != S.BENCH}}
            os.makedirs(DIR, exist_ok=True)
            with open(os.path.join(DIR, "log.md"), "w") as f:
                f.write(f"# 美股做空盘日志（规则 {RULES}，日期为美东交易日）\n\n")
            S.log(f"## {now_et:%Y-%m-%d %H:%M} ET 初始化（基线，不交易）\n\n最近已完成交易日 {last}。"
                  f"首次决策在下一个交易日收盘后（16:15 ET 之后的第一次运行）。\n")
            changed = True
        else:
            changed = evaluate(st, hist, now_et)
            st["checked_for"] = expected if st["last_date"] < expected else st.get("checked_for")
    if not changed and quotes == st.get("quotes"):
        print("[us-short] no change")
        return
    st["quotes"] = quotes
    st["quotes_updated"] = now.astimezone(S.SGT).strftime("%Y-%m-%d %H:%M SGT")
    S.save(st)
    tp = os.path.join(DIR, "trades.csv")
    trades = list(csv.DictReader(open(tp))) if os.path.exists(tp) else []
    out = {"updated": st["quotes_updated"], "rules": RULES, "start_cash": S.START_CASH, "slot": S.SLOT,
           "universe": S.UNIVERSE, "bench": S.BENCH, "state": st, "trades": trades}
    json.dump(out, open(os.path.join(S.ROOT, "docs", "stocks_short.json"), "w"), ensure_ascii=False)
    print(f"[us-short] {'evaluated' if changed else 'quotes only'}; positions={list(st['positions'])} cash={st['cash']:.2f}")


if __name__ == "__main__":
    main()
