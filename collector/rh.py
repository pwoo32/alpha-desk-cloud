#!/usr/bin/env python3
"""Read-only Robinhood client for the market-data collector.

SAFETY: every request goes through ReadOnlySession, which refuses anything but GET (the single exception is the OAuth
refresh-token POST). There is no order code in this repository, so it cannot place, change or cancel an order.
No password is ever used here: only OAuth tokens, restored from the encrypted runner-state branch (collector/state.py).

  python collector/rh.py selftest   # read-only: checks every endpoint the collector uses and prints which ones work
"""
import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
import requests

API = "https://api.robinhood.com"
TOKEN_URL = API + "/oauth2/token/"
CLIENT_ID = "c82SH0WZOsabOXGP2sxqcj34FxkvfnWRZBKlBjFS"   # Robinhood's public web/app client id (same one robin_stocks uses)
SPY_CHAIN = "c277b118-58d9-4060-8dc5-a3b5898955cb"
TOKEN_FILE = Path(os.environ.get("RH_TOKEN_FILE", Path.home() / ".tokens" / "rh_collector.json"))


class AuthError(RuntimeError):
    pass


class ReadOnlySession(requests.Session):
    """A requests.Session that can only read. Orders need POST/DELETE/PATCH/PUT, which raise here."""
    def request(self, method, url, *a, **k):
        m = method.upper()
        refresh = m == "POST" and url == TOKEN_URL and (k.get("data") or {}).get("grant_type") == "refresh_token"
        if m not in ("GET", "HEAD", "OPTIONS") and not refresh:
            raise PermissionError(f"collector is read-only: refused {m} {url}")
        return super().request(method, url, *a, **k)


class RH:
    def __init__(self, token_file=TOKEN_FILE):
        self.token_file = Path(token_file)
        if not self.token_file.exists():
            raise AuthError(f"no Robinhood token at {self.token_file}. Restore it with `python collector/state.py pull`.")
        self.tok = json.loads(self.token_file.read_text())
        self.s = ReadOnlySession()
        self.s.headers.update({"Accept": "application/json", "User-Agent": "0dte-collector/1.0",
                               "X-Robinhood-API-Version": "1.431.4"})
        self._auth()
        self.intervals = {}   # which bar interval each endpoint accepted (filled lazily)

    # ---------- auth ----------
    def _auth(self):
        self.s.headers["Authorization"] = f"{self.tok['token_type']} {self.tok['access_token']}"

    def refresh(self):
        data = dict(grant_type="refresh_token", refresh_token=self.tok["refresh_token"], client_id=CLIENT_ID,
                    scope="internal", expires_in=86400, device_token=self.tok.get("device_token", ""))
        r = self.s.post(TOKEN_URL, data=data, timeout=20)
        if r.status_code != 200 or "access_token" not in r.text:
            raise AuthError(f"token refresh failed (HTTP {r.status_code}): the Robinhood login must be renewed from the owner's PC.")
        j = r.json()
        self.tok.update(access_token=j["access_token"], token_type=j.get("token_type", "Bearer"),
                        refresh_token=j.get("refresh_token", self.tok["refresh_token"]), refreshed_utc=_utc())
        save_token(self.tok, self.token_file); self._auth()

    # ---------- transport ----------
    def get(self, path, params=None, ok404=False):
        url = path if path.startswith("http") else API + path
        for attempt in range(5):
            try:
                r = self.s.get(url, params=params, timeout=20)
            except requests.RequestException:
                time.sleep(2 ** attempt); continue
            if r.status_code == 401 and attempt == 0:
                self.refresh(); continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(30, 2 ** attempt)); continue
            if r.status_code == 404 and ok404:
                return None
            if r.status_code >= 400:
                raise requests.HTTPError(f"HTTP {r.status_code} for {url} {params or ''}: {r.text[:200]}", response=r)
            return r.json()
        raise requests.HTTPError(f"gave up after retries: {url}")

    def paginate(self, path, params=None):
        out, j = [], self.get(path, params)
        while j:
            out += j.get("results", [])
            j = self.get(j["next"]) if j.get("next") else None
        return out

    # ---------- equities / indexes ----------
    def quote(self, symbol):
        return self.get("/marketdata/quotes/", {"symbols": symbol})["results"][0]

    def stock_bars(self, symbol, intervals=("minute",), span="day", bounds="regular"):
        """[{ts, open, high, low, close, volume, interpolated, session}] for today's session; tries each interval in turn."""
        last = None
        for iv in intervals:
            try:
                j = self.get(f"/marketdata/historicals/{symbol}/", {"interval": iv, "span": span, "bounds": bounds})
            except requests.HTTPError as e:
                last = e; continue
            self.intervals[("stock", iv)] = True
            return iv, [_bar(b) for b in j.get("historicals", [])]
        raise last or RuntimeError("no interval accepted")

    def spy_1m(self, symbol="SPY"):
        """1-minute bars. Robinhood's public API historically offers 15second/5minute; 'minute' is tried first, then 15-second
        bars (last hour only) are rolled up into minutes. Returns (source, bars)."""
        try:
            return self.stock_bars(symbol, ("minute",))
        except requests.HTTPError:
            iv, b15 = self.stock_bars(symbol, ("15second",), span="hour")
            return "15second->1m", roll_up(b15, 60)

    def index_bars(self, symbol, day=None):
        """VIX / SPX 1-minute values for `day` (default today, ET). Endpoint from Robinhood's own Android app
        (IndexApi: GET marketdata/indexes/historicals/v1/{id}/?interval=&start=&end=); bars use open_value/high_value/...
        A few parameter spellings are tried; the one that answers is remembered."""
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York"); day = day or datetime.now(et).strftime("%Y-%m-%d")
        t0 = datetime.fromisoformat(day + "T09:30:00").replace(tzinfo=et).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        t1 = datetime.fromisoformat(day + "T16:00:00").replace(tzinfo=et).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = (self.get("/indexes/", {"symbol": symbol}, ok404=True) or {}).get("results") or []
        if not res:
            raise RuntimeError(f"index {symbol} not found")
        iid = res[0]["id"]; base = "/marketdata/indexes/historicals/v1/"
        tries = [(base + f"{iid}/", {"interval": "minute", "start": t0, "end": t1}),
                 (base, {"ids": iid, "interval": "minute", "start": t0, "end": t1}),
                 (base + f"{iid}/", {"interval": "minute", "start_time": t0, "end_time": t1}),
                 (base + f"{iid}/", {"interval": "minute", "span": "day", "bounds": "regular"})]
        if getattr(self, "_ix_try", None) is not None:
            tries = [tries[self._ix_try]] + tries
        errs = []
        for k, (path, params) in enumerate(tries):
            try:
                j = self.get(path, params)
            except requests.HTTPError as e:
                errs.append(str(e)[:80]); continue
            pts = _find_bars(j)
            if pts:
                self._ix_try = k % 4
                return "minute", [_bar(b) for b in pts]
            errs.append(f"{path} answered with no bars")
        raise RuntimeError(f"no index bars for {symbol}: " + " | ".join(errs[:4]))

    def instrument_id(self, symbol):
        return self.get("/instruments/", {"symbol": symbol})["results"][0]["id"]

    def price_book(self, symbol):
        return self.get(f"/marketdata/pricebook/snapshots/{self.instrument_id(symbol)}/")

    # ---------- options ----------
    def option_instruments(self, day, otype=None, chain=SPY_CHAIN):
        p = {"chain_id": chain, "expiration_dates": day, "state": "active"}
        if otype: p["type"] = otype
        return [dict(id=i["id"], strike=float(i["strike_price"]), type=i["type"], exp=i["expiration_date"]) for i in self.paginate("/options/instruments/", p)]

    def option_quotes(self, ids, batch=40):
        out = []
        for k in range(0, len(ids), batch):
            chunk = ids[k:k + batch]
            try:
                j = self.get("/marketdata/options/", {"ids": ",".join(chunk)})
            except requests.HTTPError:
                j = self.get("/marketdata/options/", {"instruments": ",".join(f"{API}/options/instruments/{i}/" for i in chunk)})
            out += [r for r in j.get("results", []) if r]
        return out

    def option_bars(self, oid, interval="minute", span="day"):
        j = self.get(f"/marketdata/options/historicals/{oid}/", {"interval": interval, "span": span})
        return [_bar(b) for b in j.get("data_points", [])]


# ---------- helpers ----------
def _utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

def _bar(b):
    g = lambda k: b.get(k + "_price", b.get(k + "_value"))   # equities/options: *_price; indexes: *_value
    return dict(ts=b["begins_at"], open=_f(g("open")), high=_f(g("high")), low=_f(g("low")),
                close=_f(g("close")), volume=_f(b.get("volume")), interpolated=bool(b.get("interpolated")),
                session=b.get("session", "reg"))

def _find_bars(j):
    """First list of dicts carrying 'begins_at' anywhere in a JSON response."""
    if isinstance(j, list):
        if j and isinstance(j[0], dict) and "begins_at" in j[0]: return j
        for x in j:
            r = _find_bars(x)
            if r: return r
    elif isinstance(j, dict):
        for v in j.values():
            r = _find_bars(v)
            if r: return r
    return None

def roll_up(bars, step):
    """Aggregate sub-minute bars into `step`-second bars keyed by their start."""
    out = {}
    for b in sorted(bars, key=lambda b: b["ts"]):
        if b["interpolated"] or b["session"] != "reg":
            continue
        t = datetime.fromisoformat(b["ts"].replace("Z", "+00:00")).timestamp(); k = int(t // step * step)
        if k not in out:
            out[k] = dict(b, ts=datetime.fromtimestamp(k, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        else:
            o = out[k]; o.update(high=max(o["high"], b["high"]), low=min(o["low"], b["low"]), close=b["close"], volume=o["volume"] + b["volume"])
    return [out[k] for k in sorted(out)]

def save_token(tok, path=TOKEN_FILE):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(tok)); os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    if os.environ.get("COLLECTOR_STATE"):   # Actions: Robinhood rotates the refresh token, so save the new one for the next job
        try:
            import state
            state.push(path)
        except Exception as e:
            print(f"WARNING: could not save the refreshed Robinhood token to the runner-state branch: {e}", file=sys.stderr, flush=True)


def selftest():
    from zoneinfo import ZoneInfo
    rh = RH(); day = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"); ok = True
    def step(name, fn):
        nonlocal ok
        try:
            print(f"OK   {name}: {fn()}")
        except Exception as e:
            print(f"FAIL {name}: {e}"); ok = False
    step("refresh token", lambda: (rh.refresh(), "fresh access token saved")[1])
    step("SPY quote", lambda: rh.quote("SPY")["last_trade_price"])
    step("SPY 1-min bars", lambda: (lambda s_b: f"{s_b[0]}: {len(s_b[1])} bars, last {s_b[1][-1]['ts'] if s_b[1] else '-'}")(rh.spy_1m()))
    step("SPY 5-min bars", lambda: f"{len(rh.stock_bars('SPY', ('5minute',))[1])} bars")
    inst = []
    step(f"0DTE instruments {day}", lambda: (inst.extend(rh.option_instruments(day)), f"{len(inst)} contracts")[1])
    if inst:
        step("option quotes (40 ids)", lambda: (lambda q: f"{len(q)} quotes, e.g. bid {q[0].get('bid_price')} ask {q[0].get('ask_price')}")(rh.option_quotes([i["id"] for i in inst[:40]])))
        mid = sorted(inst, key=lambda i: i["strike"])[len(inst) // 2]["id"]
        for iv in ("minute", "5minute"):
            step(f"option bars {iv}", lambda iv=iv: f"{len(rh.option_bars(mid, iv))} bars")
    for s in ("VIX", "SPX"):
        step(f"{s} index bars", lambda s=s: (lambda x: f"{x[0]}: {len(x[1])} bars")(rh.index_bars(s)))
    step("SPY price book", lambda: f"{len(json.dumps(rh.price_book('SPY')))} bytes")
    step("order guard", lambda: (_ for _ in ()).throw(AssertionError("POST was allowed!")) if _try_post(rh) else "POST refused as expected")
    print("ALL OK" if ok else "Some checks failed (index bars / price book are optional; everything else is needed).")

def _try_post(rh):
    try:
        rh.s.post(API + "/orders/", data={})
        return True
    except PermissionError:
        return False


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "selftest": selftest()
    else: print(__doc__)
