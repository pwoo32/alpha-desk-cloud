#!/usr/bin/env python3
"""SPY 0DTE market-data collector (Robinhood, read-only). Writes everything for trading day D to data/D/ and pushes it to main.

During the session (one tick ~5 s after every minute):
  spy_1m.csv          SPY 1-minute bars (merged every tick)
  ticks.csv           bid/ask/size/mark of every 0DTE contract within +/-$15 of SPY, every minute
  intraday_quotes.csv full-chain snapshot (bid/ask/sizes/mark/IV/greeks/OI/volume) at 10:00, 11:00, 12:00, 13:00, 14:00, 15:00 ET
After the close (+12 min):
  option_bars.csv.gz  1-minute and 5-minute bars of every listed 0DTE contract
  quotes.csv          end-of-day quote snapshot of every 0DTE contract
  QQQ_1m.csv, IWM_1m.csv, VIX_1m.csv, SPX_1m.csv (index bars best effort)
  next_expiry_<date>_quotes.csv  next expiry, strikes within +/-$15 of spot
  SPY_quote.json, SPY_book.json
  collector.log       what happened

GitHub-hosted jobs may run at most 6 hours, so the day is two jobs (see .github/workflows/collect.yml):
  python collector/collect.py --part am   # waits for 09:29 ET, ticks until 12:45 ET
  python collector/collect.py --part pm   # ticks from 12:45 ET to the close, then the after-close capture
  python collector/collect.py --part capture [--day D]   # only the after-close capture, now
"""
import argparse, csv, gzip, json, os, subprocess, sys, time, traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent; ROOT = HERE.parent
sys.path.insert(0, str(HERE))
ET = ZoneInfo("America/New_York")
BAND = 15.0
SNAPS = ("10:00", "11:00", "12:00", "13:00", "14:00", "15:00")
HANDOFF = "12:45"
TICK_COLS = ["snap_utc", "id", "strike", "type", "bid", "ask", "bid_size", "ask_size", "mark"]
SNAP_COLS = ["snap_utc", "strike", "type", "bid", "ask", "bid_size", "ask_size", "mark", "iv", "delta", "gamma", "theta", "vega", "open_interest", "volume"]
BAR_COLS = ["ts", "open", "high", "low", "close", "volume"]


def f(x):
    try: return float(x)
    except (TypeError, ValueError): return 0.0
def utc(ts): return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def hm(ts): return datetime.fromtimestamp(ts, ET).strftime("%H:%M")


class Day:
    def __init__(self, day):
        self.day = day; self.dir = ROOT / "data" / day; self.dir.mkdir(parents=True, exist_ok=True)

    def at(self, hhmm, sec=0):
        return datetime.fromisoformat(f"{self.day}T{hhmm}:00").replace(tzinfo=ET).timestamp() + sec

    def log(self, msg):
        line = f"{datetime.now(ET).strftime('%H:%M:%S')} {msg}"; print(line, flush=True)
        with open(self.dir / "collector.log", "a", encoding="utf-8") as fh: fh.write(line + "\n")


# ---------- files ----------
def append_csv(path, cols, rows):
    new = not Path(path).exists()
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, cols, extrasaction="ignore")
        if new: w.writeheader()
        w.writerows(rows)


def write_csv(path, cols, rows):
    path = Path(path)
    with (gzip.open(path, "wt", newline="") if path.suffix == ".gz" else open(path, "w", newline="")) as fh:
        w = csv.DictWriter(fh, cols, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    return len(rows)


def on_day(bars, day):
    return [b for b in bars if datetime.fromisoformat(b["ts"].replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d") == day]


def merge_bars(path, bars):
    """Merge regular-session bars into a ts,open,high,low,close,volume CSV (keeps earlier rows)."""
    rows = {r["ts"]: r for r in csv.DictReader(open(path))} if Path(path).exists() else {}
    for b in bars:
        if b["interpolated"] or b["session"] != "reg" or b["volume"] <= 0 or min(b["open"], b["high"], b["low"], b["close"]) <= 0: continue
        rows[b["ts"]] = {k: b[k] for k in BAR_COLS}
    write_csv(path, BAR_COLS, [rows[k] for k in sorted(rows)])
    return len(rows)


def iid_of(q): return q.get("instrument_id") or q.get("instrument", "").rstrip("/").split("/")[-1]


# ---------- git ----------
def git(*a, check=True):
    r = subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode: raise RuntimeError(f"git {' '.join(a)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def push(D, msg):
    git("add", "--", str(D.dir.relative_to(ROOT)))
    if not git("diff", "--cached", "--name-only"): return
    ident = ["-c", "user.name=collector", "-c", "user.email=collector@users.noreply.github.com"]
    git(*ident, "commit", "-q", "-m", msg)
    for k in range(4):
        try:
            git("pull", "-q", "--rebase", "origin", "main"); git("push", "-q", "origin", "HEAD:main"); return
        except Exception as e:
            D.log(f"push attempt {k + 1} failed: {e}"); time.sleep(10 * (k + 1))
    D.log(f"PUSH FAILED for '{msg}' (kept locally; the next push carries it)")


def on_origin(path):
    git("fetch", "-q", "origin", "main")
    return bool(git("ls-tree", "origin/main", path, check=False))


# ---------- session ----------
def hours(rh, day):
    """(open?, close 'HH:MM' ET). Half days close at 13:00."""
    try:
        h = rh.get(f"/markets/XNYS/hours/{day}/")
        if not h.get("is_open"): return False, None
        c = h.get("closes_at")
        return True, datetime.fromisoformat(c.replace("Z", "+00:00")).astimezone(ET).strftime("%H:%M") if c else "16:00"
    except Exception:
        return True, "16:00"


def sleep_until(ts):
    while (d := ts - time.time()) > 0: time.sleep(min(d, 30))


def loop(rh, D, start, stop):
    """Tick every minute (5 s after the boundary) from max(start, now) to stop; push every 30 min and at the end."""
    insts = rh.option_instruments(D.day)
    if not insts: D.log("no 0DTE contracts listed today"); return
    key = {i["id"]: (i["type"], i["strike"]) for i in insts}
    D.log(f"{len(insts)} 0DTE contracts; ticking {start}-{stop} ET")
    snaps = {s for s in SNAPS if D.at(s) < time.time()}; t = max(D.at(start, 5), time.time()); last_push = time.time(); errors = 0
    while t <= D.at(stop, 5):
        sleep_until(t)
        try:
            _, bars = rh.spy_1m(); merge_bars(D.dir / "spy_1m.csv", on_day(bars, D.day))
            spy = f(rh.quote("SPY").get("last_trade_price")) or (bars[-1]["close"] if bars else 0)
            ids = [i for i, (_, k) in key.items() if abs(k - spy) <= BAND]; W = time.time(); rows = []
            for q in rh.option_quotes(ids):
                i = iid_of(q)
                if i not in key: continue
                tp, k = key[i]
                rows.append(dict(snap_utc=utc(W), id=i, strike=f"{k:g}", type=tp, bid=f(q.get("bid_price")), ask=f(q.get("ask_price")),
                                 bid_size=q.get("bid_size", ""), ask_size=q.get("ask_size", ""), mark=q.get("mark_price", "")))
            append_csv(D.dir / "ticks.csv", TICK_COLS, rows)
            for s in SNAPS:
                if s not in snaps and hm(time.time()) >= s:
                    snaps.add(s); D.log(f"snapshot {s}: {snapshot(rh, D, key)} contracts")
        except Exception as e:
            errors += 1; D.log(f"tick error {hm(time.time())}: {e}")
            if errors > 60: D.log("too many errors, stopping the loop early"); break
        if time.time() - last_push > 1800:
            push(D, f"SPY 0DTE data {D.day} {hm(time.time())}"); last_push = time.time()
        t = (int(time.time() // 60) + 1) * 60 + 5
    D.log(f"loop done at {hm(time.time())} ({errors} tick errors)")
    push(D, f"SPY 0DTE data {D.day} {hm(time.time())}")


def snapshot(rh, D, key):
    W = time.time(); rows = []
    for q in rh.option_quotes(list(key)):
        i = iid_of(q)
        if i not in key: continue
        tp, k = key[i]
        rows.append(dict(snap_utc=utc(W), strike=f"{k:g}", type=tp, bid=q.get("bid_price"), ask=q.get("ask_price"), bid_size=q.get("bid_size"),
                         ask_size=q.get("ask_size"), mark=q.get("mark_price"), iv=q.get("implied_volatility"), delta=q.get("delta"), gamma=q.get("gamma"),
                         theta=q.get("theta"), vega=q.get("vega"), open_interest=q.get("open_interest"), volume=q.get("volume")))
    append_csv(D.dir / "intraday_quotes.csv", SNAP_COLS, rows); return len(rows)


# ---------- after the close ----------
def capture(rh, D):
    from rh import SPY_CHAIN
    day, out = D.day, D.dir; res = {}

    def step(name, fn):
        try: res[name] = fn(); D.log(f"capture {name}: {res[name]}")
        except Exception as e: res[name] = f"FAILED: {e}"; D.log(f"capture {name} FAILED: {e}")

    insts = rh.option_instruments(day); D.log(f"capture: {len(insts)} 0DTE contracts listed for {day}")

    def option_bars():
        rows, used = {}, {}
        for i in insts:
            for iv in ("minute", "5minute"):
                try: bars = rh.option_bars(i["id"], iv)
                except Exception: used[iv + " errors"] = used.get(iv + " errors", 0) + 1; continue
                used[iv] = used.get(iv, 0) + 1
                for b in on_day(bars, day):
                    rows[(i["strike"], i["type"], iv, b["ts"])] = dict(day=day, strike=f"{i['strike']:g}", type=i["type"], interval=iv, ts=b["ts"],
                                                                       open=b["open"], high=b["high"], low=b["low"], close=b["close"], interpolated=int(b["interpolated"]))
        n = write_csv(out / "option_bars.csv.gz", ["day", "strike", "type", "interval", "ts", "open", "high", "low", "close", "interpolated"], [rows[k] for k in sorted(rows)])
        return f"{n} rows ({used})"
    step("option_bars", option_bars)

    def quotes():
        snap = utc(time.time()); key = {i["id"]: i for i in insts}; rows = []
        for q in rh.option_quotes(list(key)):
            i = key.get(iid_of(q))
            if not i: continue
            rows.append(dict(day=day, snap_utc=snap, strike=f"{i['strike']:g}", type=i["type"], bid=q.get("bid_price"), ask=q.get("ask_price"),
                             mark=q.get("mark_price"), iv=q.get("implied_volatility") or "", delta=q.get("delta") or "",
                             open_interest=q.get("open_interest"), volume=q.get("volume")))
        rows.sort(key=lambda r: (float(r["strike"]), r["type"]))
        return write_csv(out / "quotes.csv", ["day", "snap_utc", "strike", "type", "bid", "ask", "mark", "iv", "delta", "open_interest", "volume"], rows)
    step("quotes", quotes)

    def spy():
        src, bars = rh.spy_1m(); n = merge_bars(out / "spy_1m.csv", on_day(bars, day)); return f"{n} bars ({src})"
    step("spy_1m", spy)

    for sym in ("QQQ", "IWM"):
        step(f"{sym}_1m", lambda sym=sym: write_csv(out / f"{sym}_1m.csv", BAR_COLS, [b for b in on_day(rh.spy_1m(sym)[1], day) if b["session"] == "reg"]))
    for sym in ("VIX", "SPX"):
        def ix(sym=sym):
            iv, bars = rh.index_bars(sym, day)
            if iv != "minute": return f"only {iv} bars offered; skipped"
            return write_csv(out / f"{sym}_1m.csv", ["ts", "open", "high", "low", "close", "interpolated"],
                             [dict(b, interpolated=str(b["interpolated"]).lower()) for b in on_day(bars, day)])
        step(f"{sym}_1m", ix)

    def next_expiry():
        spot = f(rh.quote("SPY")["last_trade_price"]); chain = rh.get(f"/options/chains/{SPY_CHAIN}/")
        exps = sorted(e for e in chain.get("expiration_dates", []) if e > day)
        if not exps: return "no later expiry listed"
        nx = exps[0]; key = {i["id"]: i for i in rh.option_instruments(nx) if abs(i["strike"] - spot) <= 15}; rows = []
        for q in rh.option_quotes(list(key)):
            i = key.get(iid_of(q))
            if not i: continue
            rows.append(dict(strike=f"{i['strike']:g}", type=i["type"], bid=q.get("bid_price"), bid_size=q.get("bid_size"), ask=q.get("ask_price"),
                             ask_size=q.get("ask_size"), mark=q.get("mark_price"), iv=q.get("implied_volatility"), delta=q.get("delta"), gamma=q.get("gamma"),
                             theta=q.get("theta"), vega=q.get("vega"), rho=q.get("rho"), open_interest=q.get("open_interest"), volume=q.get("volume"),
                             prev_close=q.get("previous_close_price"), updated_at=q.get("updated_at")))
        rows.sort(key=lambda r: (float(r["strike"]), r["type"]))
        return write_csv(out / f"next_expiry_{nx}_quotes.csv", list(rows[0]) if rows else ["strike"], rows)
    step("next_expiry", next_expiry)

    def spy_quote():
        q = rh.quote("SPY")
        o = dict(symbol="SPY", day=day, previous_close=f(q.get("previous_close")), previous_close_date=q.get("previous_close_date"),
                 last_trade_price=f(q.get("last_trade_price")), last_extended_hours_trade_price=f(q.get("last_extended_hours_trade_price")),
                 bid=f(q.get("bid_price")), ask=f(q.get("ask_price")), snap_utc=utc(time.time()))
        rows = list(csv.DictReader(open(out / "spy_1m.csv"))) if (out / "spy_1m.csv").exists() else []
        if rows:
            o.update(open=f(rows[0]["open"]), high=max(f(r["high"]) for r in rows), low=min(f(r["low"]) for r in rows), close=f(rows[-1]["close"]),
                     volume=sum(f(r["volume"]) for r in rows), volume_note=f"sum of regular-session 1m bars ({len(rows)})")
        (out / "SPY_quote.json").write_text(json.dumps(o)); return "ok"
    step("spy_quote", spy_quote)
    step("spy_book", lambda: ((out / "SPY_book.json").write_text(json.dumps(rh.price_book("SPY"))), "ok")[1])
    (out / "capture_done.txt").write_text(json.dumps(res, indent=1) + "\n")
    return res


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--part", choices=["am", "pm", "capture"], required=True); ap.add_argument("--day"); a = ap.parse_args()
    now = datetime.now(ET); day = a.day or now.strftime("%Y-%m-%d")
    if now.weekday() >= 5 and not a.day: print("weekend"); return
    D = Day(day); marker = f"data/{day}/started_{a.part}.txt"
    if a.part != "capture" and on_origin(marker):
        print(f"{marker} is already on main: this part already ran today"); return
    from rh import RH, AuthError
    try:
        rh = RH(); rh.refresh()
    except AuthError as e:
        D.log(f"ROBINHOOD LOGIN NEEDED: {e}"); (D.dir / "auth_failed.txt").write_text(str(e) + "\n"); push(D, f"SPY 0DTE data {day}: login needed"); sys.exit(2)
    if a.part == "capture":
        capture(rh, D); push(D, f"SPY 0DTE data {day} capture"); return
    is_open, close = hours(rh, day)
    if not is_open: print("market closed today"); return
    (D.dir / f"started_{a.part}.txt").write_text(f"{datetime.now(ET).strftime('%H:%M:%S')} ET, close {close}\n"); push(D, f"SPY 0DTE data {day} {a.part} start")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh: fh.write("market_open=true\n")
    if a.part == "am":
        loop(rh, D, "09:30", min(HANDOFF, close)); return
    if close > HANDOFF: loop(rh, D, HANDOFF, close)
    cm = int(close[:2]) * 60 + int(close[3:]) + 12
    sleep_until(D.at(f"{cm // 60:02d}:{cm % 60:02d}"))
    capture(rh, D); push(D, f"SPY 0DTE data {day} capture")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"COLLECTOR CRASHED: {e}\n{traceback.format_exc()}", flush=True); raise
