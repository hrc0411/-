#!/usr/bin/env python3
"""Claude Code paper-trading engine (v0.1). Pure simulation, no real orders.

Usage: python3 engine.py            # evaluate both accounts (1h and 4h)
       python3 engine.py --tf 4h    # evaluate one account

Idempotent: each run processes only Binance.US klines that closed since the
last processed bar, so it can be run as often as you like (cron runs hourly).
Only Python stdlib is used.
"""
import argparse, csv, json, os, sys, time, urllib.request
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
SGT = timezone(timedelta(hours=8))
RULES_VERSION = "v0.1"

MARGIN, LEVERAGE, FEE = 200.0, 10, 2.0
NOTIONAL = MARGIN * LEVERAGE              # 2000 USDT
STOP_PCT = 0.03
FEE_PCT = FEE / NOTIONAL                  # 0.1% of notional
MIN_RR = 1.0
MAX_SAME_SIDE = 4
MAX_DEVIATION = 0.005                     # Coinbase vs CoinGecko

COINS = {
    "LTC":  {"cb": "LTC-USD",  "cg": "litecoin", "bn": "LTCUSDT"},
    "ETH":  {"cb": "ETH-USD",  "cg": "ethereum", "bn": "ETHUSDT"},
    "SOL":  {"cb": "SOL-USD",  "cg": "solana",   "bn": "SOLUSDT"},
    "XRP":  {"cb": "XRP-USD",  "cg": "ripple",   "bn": "XRPUSDT"},
    "DOGE": {"cb": "DOGE-USD", "cg": "dogecoin", "bn": "DOGEUSDT"},
}
CLOSE_ONLY = {"DOGE"}

# Frozen level table (Muse 2026-11-06)
LEVELS = {
    "LTC":  dict(res_lo=53.46,  res_hi=55.00,  brk=50.63,   tp1=50.63,  tp2=48.92, inval=55.00,   long_trig=58.55,   long_tp=62.55),
    "ETH":  dict(res_lo=2315,   res_hi=2348,   brk=2205,    tp1=2107,   tp2=1972,  inval=2405,    long_trig=2405,    long_tp=2548),
    "SOL":  dict(res_lo=92.69,  res_hi=96.83,  brk=88.24,   tp1=83.98,  tp2=80.00, inval=98.08,   long_trig=98.08,   long_tp=102.71),
    "XRP":  dict(res_lo=1.257,  res_hi=1.28,   brk=1.16,    tp1=1.09,   tp2=1.00,  inval=1.32,    long_trig=1.32,    long_tp=1.3884),
    "DOGE": dict(res_lo=0.0786, res_hi=0.0806, brk=0.07203, tp1=0.0677, tp2=None,  inval=0.08196, long_trig=0.08196, long_tp=0.08821),
}

ACCOUNTS = {
    "4h": {"interval": "4h", "ms": 4 * 3600_000, "max_bars": 12},   # 12 x 4h = 48h
    "1h": {"interval": "1h", "ms": 3600_000,     "max_bars": 48},   # 48 x 1h = 48h
    # v0.2: same logic, levels derived from daily candles instead of the frozen table
    "4h-v2": {"interval": "4h", "ms": 4 * 3600_000, "max_bars": 12, "dyn": True},
    "1h-v2": {"interval": "1h", "ms": 3600_000,     "max_bars": 48, "dyn": True},
}
DAY_MS = 86_400_000
DYN_LOOKBACK, ATR_N = 10, 14
TP1_ATR, TP2_ATR, LONG_TP_ATR = 1.5, 3.0, 2.0


def dyn_levels(daily, until_ms):
    """v0.2 levels from Binance.US daily bars that closed by until_ms."""
    d = [b for b in (daily or []) if b["t"] + DAY_MS <= until_ms]
    if len(d) < ATR_N + 1:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(d[-ATR_N - 1:-1], d[-ATR_N:])]
    atr = sum(trs) / ATR_N
    w = d[-DYN_LOOKBACK:]
    hi, lo = max(b["h"] for b in w), min(b["l"] for b in w)
    return {"res_lo": hi - atr, "res_hi": hi, "brk": lo, "inval": hi, "long_trig": hi, "long_inval": hi - atr,
            "atr": atr, "tp1": None, "tp2": None, "long_tp": None}


def with_targets(lv, close):
    """v0.2 targets are measured from the entry (close) in ATR units."""
    lv = dict(lv)
    lv.update(tp1=close - TP1_ATR * lv["atr"], tp2=close - TP2_ATR * lv["atr"], long_tp=close + LONG_TP_ATR * lv["atr"])
    return lv


# ---------------------------------------------------------------- data
def get_json(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "paper-sim/0.1", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (i + 1))
    print(f"[warn] fetch failed {url}: {last}", file=sys.stderr)
    return None


def fetch_spot():
    out = {c: {"coinbase": None, "coingecko": None} for c in COINS}
    for c, m in COINS.items():
        j = get_json(f"https://api.coinbase.com/v2/prices/{m['cb']}/spot")
        try:
            out[c]["coinbase"] = float(j["data"]["amount"])
        except Exception:
            pass
    ids = ",".join(m["cg"] for m in COINS.values())
    j = get_json(f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd")
    for c, m in COINS.items():
        try:
            out[c]["coingecko"] = float(j[m["cg"]]["usd"])
        except Exception:
            pass
    for c, v in out.items():
        a, b = v["coinbase"], v["coingecko"]
        if a is None or b is None:
            v["ok"], v["dev"], v["why"] = False, None, "某一来源查不到"
        else:
            dev = abs(a - b) / ((a + b) / 2)
            v["dev"] = dev
            v["ok"] = dev <= MAX_DEVIATION
            v["why"] = "" if v["ok"] else f"偏差 {dev:.2%} > 0.5%"
    return out


def fetch_closed_klines(symbol, interval, now_ms):
    j = get_json(f"https://api.binance.us/api/v3/klines?symbol={symbol}&interval={interval}&limit=200")
    if not isinstance(j, list):
        return None
    bars = []
    for k in j:
        if int(k[6]) < now_ms:  # close time passed -> closed bar
            bars.append({"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4])})
    return bars


# ---------------------------------------------------------------- helpers
def fmt_t(ms):
    return datetime.fromtimestamp(ms / 1000, SGT).strftime("%Y-%m-%d %H:%M")


def pnl(side, entry, exit_):
    d = 1 if side == "long" else -1
    return NOTIONAL * d * (exit_ - entry) / entry - FEE


def acct_dir(tf):
    return os.path.join(ROOT, "accounts", tf)


def load_state(tf):
    p = os.path.join(acct_dir(tf), "state.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


def save_state(tf, st):
    os.makedirs(acct_dir(tf), exist_ok=True)
    with open(os.path.join(acct_dir(tf), "state.json"), "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def append_log(tf, text):
    version = "v0.2（动态价位）" if ACCOUNTS[tf].get("dyn") else RULES_VERSION
    os.makedirs(acct_dir(tf), exist_ok=True)
    p = os.path.join(acct_dir(tf), "log.md")
    new = not os.path.exists(p)
    with open(p, "a") as f:
        if new:
            f.write(f"# {tf} 模拟盘评估日志（规则 {version}，时间为新加坡时间）\n\n")
        f.write(text + "\n")


def append_trade(tf, row):
    p = os.path.join(acct_dir(tf), "trades.csv")
    new = not os.path.exists(p)
    with open(p, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["coin", "side", "entry_time", "entry", "exit_time", "exit", "reason", "net_pnl"])
        w.writerow(row)


def fp(x):
    if x is None:
        return "—"
    return f"{x:.6g}"


# ---------------------------------------------------------------- rules
def next_target(side, lv, price):
    """Next support below (short) / resistance above (long). Returns (target, stage)."""
    if side == "short":
        if lv["tp1"] < price:
            return lv["tp1"], 1
        if lv["tp2"] is not None and lv["tp2"] < price:
            return lv["tp2"], 2
        return None, None
    return (lv["long_tp"], 1) if lv["long_tp"] > price else (None, None)


def check_entry(coin, bar, prev_bar, lv):
    """Return (side, reason, target, stage) or (None, reason, None, None)."""
    C, H, L = bar["c"], bar["h"], bar["l"]
    if C > lv["long_trig"]:
        side, why = "long", f"收盘 {fp(C)} > 做多触发 {fp(lv['long_trig'])}"
    else:
        if coin in CLOSE_ONLY:
            touched = prev_bar is not None and prev_bar["c"] >= lv["res_lo"]
            a_desc = "前一根收盘在阻力区内/上方"
        else:
            touched = H >= lv["res_lo"]
            a_desc = f"最高 {fp(H)} 进入阻力区"
        A = touched and C < lv["res_lo"]
        rng = H - L
        B = C < lv["brk"] and rng > 0 and (C - L) <= 0.5 * rng
        if A:
            side, why = "short", f"A 反弹受阻：{a_desc}，收盘 {fp(C)} 回到下沿 {fp(lv['res_lo'])} 之下"
        elif B:
            side, why = "short", f"B 破位追空：收盘 {fp(C)} < 破位线 {fp(lv['brk'])}，且位于 K 线下半部"
        else:
            if C < lv["brk"]:
                return None, f"收盘 {fp(C)} 在破位线下但位于 K 线上半部（疑似假破位/长下影）", None, None
            if C < lv["res_lo"]:
                return None, "价格夹在破位线与阻力区之间，未出现拒绝形态", None, None
            return None, "未满足入场条件", None, None
    tgt, stage = next_target(side, lv, C)
    if tgt is None:
        return None, why + "；但入场价外侧已无目标位 → 不开", None, None
    rr = (abs(tgt - C) / C - FEE_PCT) / STOP_PCT
    if rr < MIN_RR:
        return None, why + f"；净盈亏比 {rr:.2f} < 1.0 → 不开", None, None
    return side, why + f"；目标 {fp(tgt)}，净盈亏比 {rr:.2f}", tgt, stage


def manage(coin, pos, bar, lv, max_bars):
    """Apply one closed bar to an open position. Returns (exit_price, reason) or (None, note)."""
    pos["bars_held"] += 1
    C, H, L = bar["c"], bar["h"], bar["l"]
    co = coin in CLOSE_ONLY
    hi, lo = (C, C) if co else (H, L)
    short = pos["side"] == "short"
    stop_hit = hi >= pos["stop"] if short else lo <= pos["stop"]
    if stop_hit:
        return (C if co else pos["stop"]), ("保本止损" if pos["breakeven"] else "止损")
    if short and pos["stage"] == 1 and lv["tp2"] is not None and C < lv["tp1"]:
        pos.update(breakeven=True, stop=pos["entry"], target=lv["tp2"], stage=2)
        note = f"收盘 {fp(C)} 跌破第一止盈位 → 止损移至入场价 {fp(pos['entry'])}，目标改为 {fp(lv['tp2'])}"
        if lo <= pos["target"]:
            return (C if co else pos["target"]), "第二止盈"
        return None, note
    tgt_hit = lo <= pos["target"] if short else hi >= pos["target"]
    if tgt_hit:
        name = "止盈" if not short else ("第一止盈" if pos["stage"] == 1 else "第二止盈")
        return (C if co else pos["target"]), name
    if short and C > lv["inval"]:
        return C, f"收盘站上失效线 {fp(lv['inval'])}"
    if not short and C < lv.get("long_inval", lv["inval"]):
        return C, f"收盘跌破失效线 {fp(lv.get('long_inval', lv['inval']))}"
    if pos["bars_held"] >= max_bars:
        return C, f"时间止损（持仓 {pos['bars_held']} 根）"
    return None, f"持有（已 {pos['bars_held']} 根）"


# ---------------------------------------------------------------- run
_KCACHE = {}


def klines_cached(symbol, interval, now_ms):
    k = (symbol, interval)
    if k not in _KCACHE:
        _KCACHE[k] = fetch_closed_klines(symbol, interval, now_ms)
    return _KCACHE[k]


def run_account(tf, spot, now_ms):
    cfg = ACCOUNTS[tf]
    dyn = cfg.get("dyn", False)
    st = load_state(tf)
    klines = {c: klines_cached(m["bn"], cfg["interval"], now_ms) for c, m in COINS.items()}
    daily = {c: klines_cached(m["bn"], "1d", now_ms) for c, m in COINS.items()} if dyn else {}
    first = st is None
    if first:
        st = {"tf": tf, "rules": "v0.2" if dyn else RULES_VERSION, "levels": {}, "start_ms": now_ms, "last_bar": {}, "positions": {},
              "no_entry_before": {}, "realized": 0.0, "closed": 0, "wins": 0, "equity": []}
        lines = [f"## {fmt_t(now_ms)} 初始化（基线，不交易）", ""]
        for c in COINS:
            bars = klines[c]
            if bars:
                st["last_bar"][c] = bars[-1]["t"]
                b = bars[-1]
                lines.append(f"- {c}: 最近已收盘 K {fmt_t(b['t'])}–{fmt_t(b['t'] + cfg['ms'])[11:]} O/H/L/C {fp(b['o'])}/{fp(b['h'])}/{fp(b['l'])}/{fp(b['c'])}；"
                             f"Coinbase {fp(spot[c]['coinbase'])} / CoinGecko {fp(spot[c]['coingecko'])}")
        lines.append("\n首次决策从下一根收盘 K 线开始。\n")
        append_log(tf, "\n".join(lines))
        save_state(tf, st)
        return st, True

    lines, changed = [], False
    for c in COINS:
        bars = klines[c]
        last = st["last_bar"].get(c, 0)
        new = [b for b in (bars or []) if b["t"] > last]
        if bars is not None and not new:
            continue  # no newly closed bar for this coin -> not an evaluation
        changed = True
        s = spot[c]
        src = f"Coinbase {fp(s['coinbase'])} / CoinGecko {fp(s['coingecko'])}"
        if bars is None or not s["ok"]:
            why = "Binance.US K 线查不到" if bars is None else s["why"]
            lines.append(f"- **{c}** 数据缺失（{why}；{src}）→ 本轮不操作")
            continue
        if dyn and daily.get(c) is None:
            lines.append(f"- **{c}** 数据缺失（Binance.US 日线查不到）→ 本轮不操作")
            continue
        if dyn and dyn_levels(daily[c], new[-1]["t"] + cfg["ms"]) is None:
            lines.append(f"- **{c}** 数据缺失（日线不足 15 根）→ 本轮不操作")
            continue
        for b in new:
            if dyn:
                lv = dyn_levels(daily[c], b["t"] + cfg["ms"])
                if lv is None:
                    continue
                lv = with_targets(lv, b["c"])
                st.setdefault("levels", {})[c] = {k: v for k, v in lv.items() if k not in ("tp1", "tp2", "long_tp")}
            else:
                lv = LEVELS[c]
            pos = st["positions"].get(c)
            ohlc = f"K {fmt_t(b['t'])}–{fmt_t(b['t'] + cfg['ms'])[11:]} O/H/L/C {fp(b['o'])}/{fp(b['h'])}/{fp(b['l'])}/{fp(b['c'])}"
            if pos:
                px, why = manage(c, pos, b, pos.get("lv") or lv, cfg["max_bars"])
                if px is not None:
                    net = pnl(pos["side"], pos["entry"], px)
                    exit_ms = b["t"] + cfg["ms"]
                    append_trade(tf, [c, pos["side"], fmt_t(pos["entry_ms"]), pos["entry"], fmt_t(exit_ms), px, why, round(net, 2)])
                    st["realized"] += net
                    st["closed"] += 1
                    st["wins"] += 1 if net > 0 else 0
                    del st["positions"][c]
                    if "止损" in why:  # stop-loss, breakeven stop and time stop all trigger 1-bar cooldown
                        st["no_entry_before"][c] = b["t"] + 2 * cfg["ms"]
                    lines.append(f"- **{c}** {ohlc} → 平{'空' if pos['side']=='short' else '多'} @ {fp(px)}（{why}）净盈亏 {net:+.2f} USDT")
                else:
                    lines.append(f"- **{c}** {ohlc} → {why}，止损 {fp(pos['stop'])} 目标 {fp(pos['target'])}")
            if b is not new[-1] or c in st["positions"]:
                continue
            # entry evaluation on the latest closed bar only
            prev = next((x for x in reversed(bars) if x["t"] < b["t"]), None)
            if b["t"] < st["no_entry_before"].get(c, 0):
                lines.append(f"- **{c}** {ohlc}；{src} → 止损后冷却期，不开")
                continue
            side, why, tgt, stage = check_entry(c, b, prev, lv)
            if side:
                same = sum(1 for p in st["positions"].values() if p["side"] == side)
                if same >= MAX_SAME_SIDE:
                    lines.append(f"- **{c}** {ohlc} → {why}；但同向已 {same} 笔达上限 → 不开")
                    continue
                stop = b["c"] * (1 + STOP_PCT if side == "short" else 1 - STOP_PCT)
                st["positions"][c] = {"side": side, "entry": b["c"], "entry_ms": b["t"] + cfg["ms"], "stop": stop,
                                      "target": tgt, "stage": stage, "breakeven": False, "bars_held": 0}
                if dyn:
                    st["positions"][c]["lv"] = lv
                lines.append(f"- **{c}** {ohlc}；{src} → **开{'空' if side=='short' else '多'}** @ {fp(b['c'])}，"
                             f"止损 {fp(stop)}。理由：{why}")
            else:
                dl = (f"；动态价位 阻力 {fp(lv['res_lo'])}–{fp(lv['res_hi'])} 破位 {fp(lv['brk'])} ATR {fp(lv['atr'])}" if dyn else "")
                lines.append(f"- **{c}** {ohlc}；{src}{dl} → 不开：{why}")
        st["last_bar"][c] = new[-1]["t"]

    if changed:
        unreal = 0.0
        for c, p in st["positions"].items():
            last_c = (klines[c] or [{}])[-1].get("c") if klines[c] else None
            if last_c:
                unreal += pnl(p["side"], p["entry"], last_c)
        st["equity"].append({"t": now_ms, "realized": round(st["realized"], 2), "unrealized": round(unreal, 2)})
        head = f"## {fmt_t(now_ms)} 评估（已实现 {st['realized']:+.2f}，浮动 {unreal:+.2f}，持仓 {len(st['positions'])}）"
        append_log(tf, head + "\n\n" + "\n".join(lines) + "\n")
        save_state(tf, st)
    return st, changed


def write_dashboard_data(states, spot, now_ms):
    trades = {}
    for tf in states:
        p = os.path.join(acct_dir(tf), "trades.csv")
        trades[tf] = list(csv.DictReader(open(p))) if os.path.exists(p) else []
    data = {"updated_ms": now_ms, "updated": fmt_t(now_ms) + " SGT", "rules": RULES_VERSION,
            "params": {"notional": NOTIONAL, "fee": FEE, "margin": MARGIN, "leverage": LEVERAGE},
            "levels": LEVELS, "coins": COINS, "spot": spot,
            "accounts": {tf: {"state": st, "trades": trades[tf]} for tf, st in states.items()}}
    os.makedirs(os.path.join(ROOT, "docs"), exist_ok=True)
    with open(os.path.join(ROOT, "docs", "data.json"), "w") as f:
        json.dump(data, f, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", choices=list(ACCOUNTS), action="append")
    a = ap.parse_args()
    now_ms = int(time.time() * 1000)
    spot = fetch_spot()
    states = {}
    for tf in ACCOUNTS:
        if a.tf and tf not in a.tf:
            states[tf] = load_state(tf)
            continue
        states[tf], changed = run_account(tf, spot, now_ms)
        print(f"[{tf}] {'evaluated' if changed else 'no new closed bar'}; positions={list(states[tf]['positions'])} realized={states[tf]['realized']:.2f}")
    write_dashboard_data({k: v for k, v in states.items() if v}, spot, now_ms)


if __name__ == "__main__":
    main()
