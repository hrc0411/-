#!/usr/bin/env python3
"""US-stock long-only paper account (rules: STOCK_RULES.md v0.1). Pure simulation.

python3 stocks.py   # idempotent: refreshes quotes; on the first run after 16:15 ET
                    # of a trading day, evaluates the completed daily bar.
Data: Yahoo Finance chart API (daily bars). Stdlib only.
"""
import csv, json, os, sys, time, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
ET, SGT = ZoneInfo("America/New_York"), ZoneInfo("Asia/Singapore")
RULES = "stock-v0.1"
START_CASH, SLOT, MAX_POS, FEE = 10_000.0, 2_000.0, 5, 2.0
STOP_PCT, MAX_DAYS, RSI_MAX = 0.08, 10, 10.0
UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "JPM", "V",
            "MA", "LLY", "UNH", "XOM", "COST", "WMT", "HD", "NFLX", "AMD", "ORCL"]
BENCH = "SPY"
DIR = os.path.join(ROOT, "accounts", "us")


def get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            err = e
            time.sleep(2 * (i + 1))
    print(f"[warn] {url}: {err}", file=sys.stderr)
    return None


def chart(sym, rng):
    host = "query1" if hash(sym) % 2 else "query2"
    j = get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d")
    try:
        r = j["chart"]["result"][0]
        meta, q = r["meta"], r["indicators"]["quote"][0]
        bars = []
        for i, t in enumerate(r.get("timestamp") or []):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c):
                continue
            bars.append({"d": datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d"), "o": o, "h": h, "l": l, "c": c})
        return meta, bars
    except Exception:
        return None, None


def completed(bars, now_et):
    """Drop today's bar unless the session is over (>=16:15 ET)."""
    today = now_et.strftime("%Y-%m-%d")
    done = now_et.hour * 60 + now_et.minute >= 16 * 60 + 15
    return [b for b in bars if b["d"] < today or (b["d"] == today and done)]


def sma(cs, n):
    return sum(cs[-n:]) / n if len(cs) >= n else None


def rsi2(cs):
    # Wilder RSI(2) over the full history
    if len(cs) < 30:
        return None
    ag = al = 0.0
    for i in range(1, len(cs)):
        d = cs[i] - cs[i - 1]
        g, l = max(d, 0), max(-d, 0)
        if i <= 2:
            ag += g / 2; al += l / 2
        else:
            ag = (ag * 1 + g) / 2; al = (al * 1 + l) / 2
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def indicators(bars):
    cs = [b["c"] for b in bars]
    mom = cs[-1] / cs[-127] - 1 if len(cs) >= 127 else None
    return {"close": cs[-1], "sma5": sma(cs, 5), "sma50": sma(cs, 50), "sma200": sma(cs, 200),
            "rsi2": rsi2(cs), "mom126": mom, "date": bars[-1]["d"]}


def load():
    p = os.path.join(DIR, "state.json")
    return json.load(open(p)) if os.path.exists(p) else None


def save(st):
    os.makedirs(DIR, exist_ok=True)
    json.dump(st, open(os.path.join(DIR, "state.json"), "w"), ensure_ascii=False, indent=1)


def log(text):
    os.makedirs(DIR, exist_ok=True)
    p = os.path.join(DIR, "log.md")
    new = not os.path.exists(p)
    with open(p, "a") as f:
        if new:
            f.write(f"# 美股模拟盘日志（规则 {RULES}，日期为美东交易日）\n\n")
        f.write(text + "\n")


def trade_row(row):
    p = os.path.join(DIR, "trades.csv")
    new = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["ticker", "entry_date", "entry", "shares", "exit_date", "exit", "reason", "net_pnl", "ret_pct"])
        w.writerow(row)


def f2(x):
    return "—" if x is None else f"{x:.2f}"


def evaluate(st, hist, now_et):
    """Process completed daily bars newer than st['last_date']."""
    last = st["last_date"]
    dates = sorted({b["d"] for bars in hist.values() if bars for b in bars if b["d"] > last})
    if not dates:
        return False
    lines = []
    for d in dates:
        is_latest = d == dates[-1]
        day = []
        # exits
        for t in list(st["positions"]):
            bars = [b for b in (hist.get(t) or []) if b["d"] <= d]
            if not bars or bars[-1]["d"] != d:
                day.append(f"- {t}：{d} 无数据（数据缺失），持仓不动")
                continue
            p = st["positions"][t]
            p["days"] += 1
            ind = indicators(bars)
            c = ind["close"]
            why = None
            if c <= p["entry"] * (1 - STOP_PCT):
                why = "止损（收盘 ≤ 入场 −8%）"
            elif ind["sma200"] and c < ind["sma200"]:
                why = "趋势破坏（收盘 < SMA200）"
            elif ind["sma5"] and c > ind["sma5"]:
                why = "反弹兑现（收盘 > SMA5）"
            elif p["days"] >= MAX_DAYS:
                why = f"时间止损（持有 {p['days']} 个交易日）"
            if why:
                net = p["shares"] * (c - p["entry"]) - FEE
                st["cash"] += p["shares"] * c - FEE / 2
                st["realized"] += net
                st["closed"] += 1
                st["wins"] += net > 0
                trade_row([t, p["entry_date"], round(p["entry"], 4), round(p["shares"], 6), d, round(c, 4), why,
                           round(net, 2), round(100 * (c / p["entry"] - 1), 2)])
                st["exited_today"] = {"d": d, "t": t}
                del st["positions"][t]
                day.append(f"- **卖出 {t}** @ {c:.2f}（{why}），净盈亏 {net:+.2f} USD")
            else:
                day.append(f"- 持有 {t}：收盘 {c:.2f}，SMA5 {f2(ind['sma5'])}，第 {p['days']} 天")
        # entries (latest completed day only)
        if is_latest:
            cands, notes = [], []
            for t in UNIVERSE:
                bars = [b for b in (hist.get(t) or []) if b["d"] <= d]
                if not bars or bars[-1]["d"] != d:
                    notes.append(f"{t} 数据缺失")
                    continue
                ind = indicators(bars)
                st["watch"][t] = ind
                if t in st["positions"] or st.get("exited_today", {}).get("t") == t and st["exited_today"]["d"] == d:
                    continue
                if None in (ind["sma50"], ind["sma200"], ind["rsi2"], ind["mom126"]):
                    continue
                if ind["close"] > ind["sma200"] and ind["sma50"] > ind["sma200"] and ind["rsi2"] < RSI_MAX:
                    cands.append((ind["mom126"], t, ind))
            cands.sort(reverse=True)
            free = MAX_POS - len(st["positions"])
            for mom, t, ind in cands[:max(free, 0)]:
                c = ind["close"]
                sh = SLOT / c
                st["cash"] -= SLOT + FEE / 2
                st["positions"][t] = {"entry": c, "entry_date": d, "shares": sh, "days": 0}
                day.append(f"- **买入 {t}** @ {c:.2f}（{sh:.4f} 股 ≈ 2000 USD）。理由：收盘 > SMA200、SMA50 > SMA200、"
                           f"RSI(2) {ind['rsi2']:.1f} < 10；126 日动量 {mom:+.1%}")
            for mom, t, ind in cands[max(free, 0):]:
                day.append(f"- {t} 满足条件但仓位已满（动量 {mom:+.1%} 排名靠后）")
            if not cands:
                day.append("- 无买入信号（没有处于上升趋势且 RSI(2) < 10 的股票）")
            if notes:
                day.append("- " + "，".join(notes))
        lines.append(f"### 交易日 {d}\n" + "\n".join(day))
        st["last_date"] = d
    bench = hist.get(BENCH)
    if bench:
        st["bench_last"] = bench[-1]["c"]
        st.setdefault("bench_start", bench[-1]["c"])
    eq = equity(st, {t: (hist[t][-1]["c"] if hist.get(t) else None) for t in st["positions"]})
    st["equity"].append({"d": st["last_date"], "equity": round(eq, 2),
                         "bench": round(START_CASH * st["bench_last"] / st["bench_start"], 2) if st.get("bench_start") else None})
    log(f"## {now_et:%Y-%m-%d %H:%M} ET 评估（权益 {eq:.2f}，已实现 {st['realized']:+.2f}，持仓 {len(st['positions'])}）\n\n"
        + "\n\n".join(lines) + "\n")
    return True


def equity(st, prices):
    v = st["cash"]
    for t, p in st["positions"].items():
        px = prices.get(t) or p["entry"]
        v += p["shares"] * px
    return v


def main():
    now = datetime.now(timezone.utc)
    now_et = now.astimezone(ET)
    st = load()
    first = st is None
    # live quotes (cheap) every run
    quotes = {}
    for t in UNIVERSE + [BENCH]:
        meta, _ = chart(t, "1d")
        if meta and meta.get("regularMarketPrice"):
            quotes[t] = {"price": meta["regularMarketPrice"], "prev": meta.get("chartPreviousClose"),
                         "time": meta.get("regularMarketTime")}
    changed = False
    # latest trading day that should be complete by now (weekday; holidays simply produce no new bar)
    from datetime import timedelta
    d = now_et if now_et.hour * 60 + now_et.minute >= 16 * 60 + 15 else now_et - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    expected = d.strftime("%Y-%m-%d")
    need_eval = first or (expected > st["last_date"] and expected != st.get("checked_for"))
    if need_eval:
        hist = {}
        for t in UNIVERSE + [BENCH]:
            _, bars = chart(t, "2y")
            hist[t] = completed(bars, now_et) if bars else None
        if first:
            last = max(b[-1]["d"] for b in hist.values() if b)
            st = {"rules": RULES, "start": now.astimezone(SGT).strftime("%Y-%m-%d %H:%M SGT"), "last_date": last,
                  "cash": START_CASH, "positions": {}, "realized": 0.0, "closed": 0, "wins": 0, "equity": [],
                  "watch": {t: indicators(b) for t, b in hist.items() if b and t != BENCH}}
            log(f"## {now_et:%Y-%m-%d %H:%M} ET 初始化（基线，不交易）\n\n最近已完成交易日 {last}。"
                f"首次决策在下一个交易日收盘后（16:15 ET 之后的第一次运行）。\n")
            changed = True
        else:
            changed = evaluate(st, hist, now_et)
        if changed or not first:
            st["checked_for"] = expected if st["last_date"] < expected else st.get("checked_for")
    if st is None:
        return
    if not changed and quotes == st.get("quotes"):
        print("[us] no change")
        return
    st["quotes"] = quotes
    st["quotes_updated"] = now.astimezone(SGT).strftime("%Y-%m-%d %H:%M SGT")
    save(st)
    p = os.path.join(DIR, "trades.csv")
    trades = list(csv.DictReader(open(p))) if os.path.exists(p) else []
    out = {"updated": st["quotes_updated"], "rules": RULES, "start_cash": START_CASH, "slot": SLOT,
           "universe": UNIVERSE, "bench": BENCH, "state": st, "trades": trades}
    os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
    json.dump(out, open(os.path.join(ROOT, "docs", "stocks.json"), "w"), ensure_ascii=False)
    print(f"[us] {'evaluated' if changed else 'quotes only'}; positions={list(st['positions'])} cash={st['cash']:.2f}")


if __name__ == "__main__":
    main()
