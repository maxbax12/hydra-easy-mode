"""
Volume mode (pre-launch): the node trades with itself to generate volume.

Each round rests a maker order strictly inside the OTHER participants' spread, waits
until it is the only order at that price, then takes it with an opposite limit order at
the same price and size. Both sides are ours, so inventory stays flat; the only cost is
the fee (taker fee minus maker rebate, ~0.07% on USDC/USDC). Direction alternates.
Anything unexpected aborts; someone else repricing around our maker only retries.

While a run is active the market maker of that market is paused (state/pause_<strategy>,
marked as ours, removed afterwards; a pause someone else set is never touched), so its
quotes free their capacity and it can't take our maker. Orders carry a client_order_id
starting with "vol-" so this volume can be told apart from real trading.

  start_run(market, usd)   background run (what the GUI and the daily target use)
  run(market, usd)         the engine itself (`hydra-mm volume run` calls it in the foreground)
  status()                 running run, last result, per-day volume/cost, config
  config/volume.yaml       daily targets per market (tools/volume_daemon.py spreads them over the day)
"""
import asyncio
import json
import math
import os
import random
import signal
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Optional

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CONFIG = "config/volume.yaml"
STATE = "state"
RUN_FILE = os.path.join(STATE, "volume_run.json")
LAST_FILE = os.path.join(STATE, "volume_last.json")
STATS_FILE = os.path.join(STATE, "volume_stats.json")
STOP_FILE = os.path.join(STATE, "volume_stop")
FEES_FILE = os.path.join(STATE, "volume_fees.json")
LOG_FILE = "volume.log"
PAUSE_MARK = "volume-mode"

# reference price, band around it, default/min round size (base units), list fee rates (%, fallback)
MARKETS: Dict[str, Dict[str, Any]] = {
    "USDC.arb/USDC.eth": dict(band=0.0015, max_size=70.0, min_size=5.0, taker=0.10, maker=-0.03),
    "BTC/USDC.arb": dict(band=0.01, max_size=0.002, min_size=0.0001, taker=0.30, maker=-0.05),
    "ETH/USDC.arb": dict(band=0.01, max_size=0.04, min_size=0.002, taker=0.20, maker=-0.075),
    "ETH/BTC": dict(band=0.01, max_size=0.04, min_size=0.002, taker=0.40, maker=-0.05),
}
DEFAULT_CONFIG = {"enabled": False, "pause_market_maker": True, "burst_every_min": 20, "active_hours": [0, 24],
                  "markets": {m: {"daily_usd": 0.0, "max_size": v["max_size"]} for m, v in MARKETS.items()}}
MAX_ROUNDS = 500                # per run, a safety stop
MAX_RETRIES = 10                # consecutive "someone repriced around our maker"


class Abort(Exception):
    """Something unexpected: stop the run, clean up, report the reason."""


# ------------------------------------------------------------------ files / config
def _path(p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def _read_json(p: str, default):
    try:
        with open(_path(p)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(p: str, data):
    p = _path(p)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(p + ".tmp", p)


def load_config(path: str = CONFIG) -> dict:
    import yaml
    try:
        doc = yaml.safe_load(open(_path(path))) or {}
    except FileNotFoundError:
        doc = {}
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.update({k: v for k, v in doc.items() if k != "markets"})
    for m, v in (doc.get("markets") or {}).items():
        if m in cfg["markets"]:
            cfg["markets"][m].update(v or {})
    return cfg


def validate_config(cfg: dict) -> dict:
    """A clean copy of cfg, or ValueError with a message a person can act on."""
    out = json.loads(json.dumps(DEFAULT_CONFIG))
    if not isinstance(cfg, dict):
        raise ValueError("config must be an object")
    for k in ("enabled", "pause_market_maker"):
        if k in cfg:
            if not isinstance(cfg[k], bool):
                raise ValueError(f"{k} must be true or false")
            out[k] = cfg[k]
    if "burst_every_min" in cfg:
        b = cfg["burst_every_min"]
        if not isinstance(b, (int, float)) or not 5 <= b <= 240:
            raise ValueError("burst_every_min must be between 5 and 240 minutes")
        out["burst_every_min"] = int(b)
    if "active_hours" in cfg:
        h = cfg["active_hours"]
        if (not isinstance(h, (list, tuple)) or len(h) != 2 or not all(isinstance(x, (int, float)) for x in h)
                or not 0 <= h[0] < h[1] <= 24):
            raise ValueError("active_hours must be [start, end] with 0 <= start < end <= 24")
        out["active_hours"] = [int(h[0]), int(h[1])]
    for m, v in (cfg.get("markets") or {}).items():
        if m not in MARKETS:
            raise ValueError(f"unknown market {m} (known: {', '.join(MARKETS)})")
        v = v or {}
        d = v.get("daily_usd", out["markets"][m]["daily_usd"])
        s = v.get("max_size", out["markets"][m]["max_size"])
        if not isinstance(d, (int, float)) or not 0 <= d <= 1_000_000:
            raise ValueError(f"{m}: daily_usd must be between 0 and 1,000,000")
        if not isinstance(s, (int, float)) or not s >= MARKETS[m]["min_size"]:
            raise ValueError(f"{m}: max_size must be at least {MARKETS[m]['min_size']:g}")
        out["markets"][m] = {"daily_usd": float(d), "max_size": float(s)}
    return out


def save_config(cfg: dict, path: str = CONFIG) -> None:
    import yaml
    clean = validate_config(cfg)
    p = os.path.realpath(_path(path))            # in the container config/ links into the data volume
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as f:
        yaml.safe_dump(clean, f, sort_keys=False)
    os.replace(p + ".tmp", p)


# ------------------------------------------------------------------ status / control
def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _strategy_of(market: str, config_path: str = "config/bot_config.yaml") -> Optional[str]:
    import yaml
    try:
        doc = yaml.safe_load(open(_path(config_path))) or {}
    except FileNotFoundError:
        return None
    for e in doc.get("strategies") or []:
        if e.get("pair") == market and e.get("type", "market_maker") == "market_maker":
            return e.get("name")
    return None


def _pause_path(strategy: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in strategy)
    return _path(os.path.join(STATE, f"pause_{clean}"))


def _release_pause(path: Optional[str]):
    """Remove a pause file only if volume mode wrote it."""
    try:
        if path and open(path).read().startswith(PAUSE_MARK):
            os.remove(path)
    except OSError:
        pass


def _finish(run: dict, result: str):
    run = dict(run, ended_at=time.time(), result=result)
    _write_json(LAST_FILE, run)
    try:
        os.remove(_path(RUN_FILE))
    except OSError:
        pass
    _release_pause(run.get("pause_file"))


def cleanup_stale() -> Optional[dict]:
    """A run whose process is gone (killed, container restart): record it as aborted and
    give its market back to the market maker. Returns the finished run, if any."""
    run = _read_json(RUN_FILE, None)
    if run and not _alive(run.get("pid")):
        _finish(run, "aborted: the process stopped (killed or restarted)")
        return run
    return None


def cost_per_1000() -> Dict[str, float]:
    """Estimated USD fee cost per $1,000 of volume: taker fee minus maker rebate (our fee
    tier from the node when a run has fetched it, else the list rates)."""
    node = _read_json(FEES_FILE, {})
    out = {}
    for m, v in MARKETS.items():
        taker, maker = node.get(m, (v["taker"], v["maker"]))
        out[m] = round(10.0 * (taker + maker), 4)
    return out


def status() -> dict:
    cleanup_stale()
    run = _read_json(RUN_FILE, None)
    stats = _read_json(STATS_FILE, {})
    days = sorted(stats)[-14:]
    history = [dict(date=d, market=m, **v) for d in days for m, v in sorted(stats[d].items())]
    public = lambda r: {k: v for k, v in r.items() if k != "pause_file"} if r else None
    return {"running": public(run), "last_run": public(_read_json(LAST_FILE, None)),
            "today": stats.get(_today(), {}), "history": history,
            "cost_per_1000": cost_per_1000(), "config": load_config()}


def start_run(market: str, usd: float, size: Optional[float] = None, source: str = "manual") -> int:
    """Start a run in the background; returns its pid. RuntimeError if one is active."""
    if market not in MARKETS:
        raise ValueError(f"unknown market {market} (known: {', '.join(MARKETS)})")
    if not isinstance(usd, (int, float)) or not 0 < usd <= 1_000_000:
        raise ValueError("usd must be between 0 and 1,000,000")
    cleanup_stale()
    run = _read_json(RUN_FILE, None)
    if run and _alive(run.get("pid")):
        raise RuntimeError(f"a volume run is already active on {run.get('market')} (pid {run.get('pid')})")
    try:
        os.remove(_path(STOP_FILE))
    except OSError:
        pass
    cmd = [sys.executable, os.path.join(ROOT, "tools", "hydra_mm.py"), "volume", "run", f"{float(usd):g}",
           "--market", market, "--source", source]
    if size:
        cmd += ["--size", f"{float(size):g}"]
    log = open(_path(LOG_FILE), "a")
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         start_new_session=True)
    return p.pid


def stop_run() -> bool:
    """Ask the active run to stop after the current round. False if none is active."""
    cleanup_stale()
    run = _read_json(RUN_FILE, None)
    if not (run and _alive(run.get("pid"))):
        return False
    os.makedirs(_path(STATE), exist_ok=True)
    open(_path(STOP_FILE), "w").close()
    return True


def _add_stats(market: str, volume: float, cost: float, rounds: int, day: Optional[str] = None):
    stats = _read_json(STATS_FILE, {})
    d = stats.setdefault(day or _today(), {}).setdefault(market, {"volume_usd": 0.0, "cost_usd": 0.0, "rounds": 0})
    d["volume_usd"] = round(d["volume_usd"] + volume, 2)
    d["cost_usd"] = round(d["cost_usd"] + cost, 4)
    d["rounds"] += rounds
    for old in sorted(stats)[:-30]:                 # keep 30 days
        del stats[old]
    _write_json(STATS_FILE, stats)


# ------------------------------------------------------------------ the engine
class SelfMatcher:
    """One run. `ex` is an exchange connector (HydraExchange or a stub with the same async
    get_orderbook / get_orders / place_order / cancel_order). Injected callables keep the
    logic testable: capacity() -> {"base": (send_free, recv_free), "quote": (...)},
    reference() -> (outside price, USD per 1 quote unit), stop() -> bool."""

    def __init__(self, ex, pair, market: str, usd: float, max_size: float, base_dp: int, price_dp: int,
                 capacity: Callable[[], dict], reference: Callable[[], tuple], stop: Callable[[], bool],
                 progress: Callable[[dict], None] = lambda r: None, log: Callable[[str], None] = print,
                 sleep=asyncio.sleep, settle_s: float = 2.0, rate: float = 0.0007):
        from connectors.base_exchange import OrderSide, OrderType
        self.Side, self.Type = OrderSide, OrderType
        self.ex, self.pair, self.market = ex, pair, market
        self.target, self.max_size = float(usd), float(max_size)
        self.min_size = MARKETS[market]["min_size"]
        self.band = MARKETS[market]["band"]
        self.base_dp, self.price_dp = base_dp, price_dp
        self.tick = 10 ** -price_dp
        self.capacity, self.reference, self.stop = capacity, reference, stop
        self.progress, self.log, self.sleep, self.settle_s, self.rate = progress, log, sleep, settle_s, rate
        self.volume = self.cost_est = 0.0
        self.rounds = self.retries = 0
        self.min_round_usd = 0.0                         # known after the first price
        self.ours = set()
        self.tag = f"vol-{''.join(c for c in market if c.isalnum())[:10].lower()}-{random.randrange(16**6):06x}"

    async def _book(self):
        ob = await self.ex.get_orderbook(self.pair, 50)
        return list(ob.bids), list(ob.asks)

    @staticmethod
    def _norm(levels):
        # the node returns the book as a map: same-price orders can come back in another order
        return sorted((round(q, 10), round(a, 8)) for q, a in levels)

    async def _live(self):
        return {o.id: o for o in await self.ex.get_orders(self.pair)}

    async def _cancel_wait(self, *oids):
        for oid in oids:
            try:
                await self.ex.cancel_order(oid, self.pair)
            except Exception as e:
                self.log(f"  cancel {oid[:8]} failed: {e}")
        for _ in range(30):
            if not (set(oids) & set(await self._live())):
                return True
            await self.sleep(0.2)
        return False

    async def _retry(self, why: str, mk_id: str):
        self.retries += 1
        if not await self._cancel_wait(mk_id):
            raise Abort(f"{why}; and our maker {mk_id[:8]} is still live after cancelling")
        if self.retries > MAX_RETRIES:
            raise Abort(f"{why} ({MAX_RETRIES} retries in a row)")
        self.log(f"  {why} — maker pulled, retrying ({self.retries}/{MAX_RETRIES})")

    def _size(self, p: float, quote_usd: float) -> float:
        cap = self.capacity()
        room = 0.9 * min(cap["base"][0], cap["base"][1], cap["quote"][0] / p, cap["quote"][1] / p)
        left = (self.target - self.volume) / (p * quote_usd)
        s = min(self.max_size, room, max(left, self.min_size))
        s = math.floor(s * 10 ** self.base_dp + 1e-9) / 10 ** self.base_dp
        if s < self.min_size:
            raise Abort(f"not enough free capacity for a round (room {room:.6g}, minimum {self.min_size:g}) — "
                        f"pause the market maker of this market or fund more")
        return s

    async def round(self) -> bool:
        """One self-matched round. True = counted, False = retried (someone repriced)."""
        live = set(await self._live())
        if self.ours & live:
            raise Abort(f"our previous test orders still resting: {sorted(self.ours & live)}")
        for attempt in range(60):                       # only trade into a QUIET book
            snap = await self._book()
            await self.sleep(self.settle_s)
            bids, asks = await self._book()
            if (self._norm(bids), self._norm(asks)) == (self._norm(snap[0]), self._norm(snap[1])):
                break
            if attempt % 5 == 0:
                self.log(f"  book moving ({len(bids)} bids / {len(asks)} asks) — waiting")
        else:
            raise Abort("the book never settled for 2 minutes")
        ext, quote_usd = self.reference()
        lo = bids[0][0] if bids else ext * 0.99          # an empty side counts as 1% away
        hi = asks[0][0] if asks else ext * 1.01
        gap = max(2 * self.tick, 0.25 * (hi - lo))       # stay clear of where others reprice
        p = round(min(max(ext, lo + gap), hi - gap), self.price_dp)
        if not lo + self.tick < p < hi - self.tick:
            raise Abort(f"no room inside the spread ({lo} / {hi})")
        if abs(p / ext - 1) >= self.band:
            raise Abort(f"price {p} too far from the reference {ext:.8g} (band {self.band:.2%})")
        self.min_round_usd = self.min_size * p * quote_usd
        size = self._size(p, quote_usd)
        maker = self.Side.SELL if self.rounds % 2 == 0 else self.Side.BUY
        taker = self.Side.BUY if maker == self.Side.SELL else self.Side.SELL
        cid = lambda r: {"client_order_id": f"{self.tag}-{self.rounds}-{r}-{random.randrange(16**4):04x}"}

        mk = await self.ex.place_order(pair=self.pair, side=maker, type=self.Type.LIMIT, amount=size, price=p,
                                       params=cid("m"))
        if not mk:
            raise Abort("maker order rejected (capacity? try a smaller size)")
        self.ours.add(mk.id)
        for _ in range(30):                              # rests, untouched, alone at the top
            await self.sleep(0.2)
            bids2, asks2 = await self._book()
            side = asks2 if maker == self.Side.SELL else bids2
            if side and abs(side[0][0] - p) < self.tick / 2:
                break
        else:
            await self._cancel_wait(mk.id)
            raise Abort("our maker never showed at the top of the book")
        mine = await self._live()
        if mk.id not in mine or getattr(mine[mk.id], "filled", 0) > 0:
            await self._cancel_wait(mk.id)
            raise Abort(f"maker {mk.id[:8]} was touched before our taker (an outside fill?)")
        better = [q for q, _ in side if (q < p - self.tick / 2 if maker == self.Side.SELL else q > p + self.tick / 2)]
        level = [a for q, a in side if abs(q - p) < self.tick / 2]
        if better or len(level) != 1 or abs(level[0] - size) > 10 ** -self.base_dp:
            await self._retry(f"someone else is at/inside {p}", mk.id)
            return False
        others = lambda lv: [x for x in lv if abs(x[0] - p) >= self.tick / 2]
        if (self._norm(others(bids2)), self._norm(others(asks2))) != (self._norm(others(bids)), self._norm(others(asks))):
            await self._retry("others changed their orders while our maker rested", mk.id)
            return False

        tk = await self.ex.place_order(pair=self.pair, side=taker, type=self.Type.LIMIT, amount=size, price=p,
                                       params=cid("t"))
        if not tk:
            await self._cancel_wait(mk.id)
            raise Abort("the taker order was rejected (capacity? try a smaller size)")
        self.ours.add(tk.id)
        t0 = time.time()
        while True:                                      # a match settles in ~1.5 s
            await self.sleep(0.2)
            live = set(await self._live())
            if mk.id not in live and tk.id not in live:
                break
            if time.time() - t0 > 3:
                left = live & {mk.id, tk.id}
                await self._cancel_wait(*left)
                raise Abort(f"orders still live 3 s after the taker ({', '.join(o[:8] for o in left)})")
        self.rounds += 1
        self.retries = 0
        usd = size * p * quote_usd
        self.volume += usd
        self.cost_est += usd * self.rate
        self.log(f"  round {self.rounds:>3}: maker {maker.value:<4} / taker {taker.value:<4} {size:g} @ {p}   "
                 f"volume ${self.volume:,.2f}")
        self.progress({"volume_usd": round(self.volume, 2), "rounds": self.rounds,
                       "cost_usd": round(self.cost_est, 4), "message": f"round {self.rounds} at {p}"})
        return True

    async def cancel_leftovers(self):
        """After an unexpected error: no test order of ours may stay on the book."""
        try:
            left = self.ours & set(await self._live())
            if left:
                self.log(f"  cancelling {len(left)} test order(s) left on the book")
                await self._cancel_wait(*left)
        except Exception as e:
            self.log(f"  could not check for leftover test orders: {e}")

    async def run(self) -> str:
        """Rounds until the target, a stop request, or MAX_ROUNDS. Returns the result text."""
        # a remainder below half a minimum round counts as done (no 5-USDC round for $0.05)
        while self.target - self.volume > max(0.01, 0.5 * self.min_round_usd) and self.rounds < MAX_ROUNDS:
            if self.stop():
                return "stopped"
            await self.round()
        return "done"


def selfmatch_cost(client, pcfg: dict, since: float):
    """Exact result from the node's payment records: a self-matched swap is a payment hash
    where we both SENT and RECEIVED the base asset (a fill with someone else in the same
    window has one side only). Returns (base net, quote net, swaps)."""
    from lib.hydra_pb import watch_only_node_pb2 as W, primitives_pb2 as P
    legs = {}
    for side in ("base", "quote"):
        x = pcfg[side]
        aid, net = x["asset_id"].lower(), P.Network(protocol=int(x["protocol"]), id=str(x["network_id"]))
        cursor = ""
        for _ in range(30):
            r = client.watch_only_node_stub.GetPayments(W.GetPaymentsRequest(
                network=net, pagination=P.PaginationRequest(limit=100, cursor=cursor)))
            for pay in r.payments:
                if pay.timestamp.seconds < since:
                    continue
                d = legs.setdefault(pay.hash, {"base": [0.0, 0.0], "quote": [0.0, 0.0]})
                d[side][0] += sum(float(v.value) for k, v in pay.spent.items() if k.lower() == aid)
                d[side][1] += sum(float(v.value) for k, v in pay.received.items() if k.lower() == aid)
            cursor = r.pagination.next_cursor
            if not r.pagination.has_more or not cursor:
                break
    mine = [d for d in legs.values() if d["base"][0] > 0 and d["base"][1] > 0]
    return (sum(d["base"][1] - d["base"][0] for d in mine),
            sum(d["quote"][1] - d["quote"][0] for d in mine), len(mine))


def _fee_rates(client, pcfg: dict):
    """(taker %, maker %) for us on this market from the node, or None."""
    import urllib.request
    cur = lambda c: {"protocol": int(c["protocol"]), "networkId": str(c["network_id"]), "assetId": c["asset_id"].lower()}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "orderbook_getMarketFeeRates",
                       "params": [{"firstCurrency": cur(pcfg["base"]), "otherCurrency": cur(pcfg["quote"])}]})
    try:
        with urllib.request.urlopen(urllib.request.Request(f"http://{client.host}:{client.port}", body.encode(),
                                                           {"Content-Type": "application/json"}), timeout=5) as r:
            fr = (json.load(r).get("result") or {}).get("feeRates") or {}
        return float(fr["takerBaseFee"]["value"]) * 100, float(fr["makerBaseFee"]["value"]) * 100
    except Exception:
        return None


async def run(market: str, usd: float, size: Optional[float] = None, source: str = "manual",
              log: Callable[[str], None] = print) -> dict:
    """The full run against the local node: pause the market maker, self-match until `usd`
    of volume, exact cost from the payment records, stats, clean up. Returns the run record."""
    import yaml
    from pathlib import Path
    from connectors.hydra_exchange import HydraExchange
    from lib.grpc_client import HydraGRPCClient
    from lib import easy_ops as E
    if market not in MARKETS:
        raise Abort(f"unknown market {market} (known: {', '.join(MARKETS)})")
    os.chdir(ROOT)
    cleanup_stale()
    cur = _read_json(RUN_FILE, None)
    if cur and _alive(cur.get("pid")) and int(cur["pid"]) != os.getpid():
        raise Abort(f"a volume run is already active on {cur.get('market')} (pid {cur.get('pid')})")
    cfg = load_config()
    max_size = size or cfg["markets"][market]["max_size"]
    rec = {"market": market, "target_usd": float(usd), "volume_usd": 0.0, "rounds": 0, "cost_usd": 0.0,
           "started_at": time.time(), "pid": os.getpid(), "source": source, "message": "starting"}
    _write_json(RUN_FILE, rec)

    stop_flag = {"v": False}
    def on_signal(*_):
        stop_flag["v"] = True
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(s, on_signal)
        except (ValueError, OSError):
            pass                                        # not the main thread (tests)
    stop = lambda: stop_flag["v"] or os.path.exists(_path(STOP_FILE))

    result, matcher = "aborted: did not start", None
    try:
        strategy = _strategy_of(market) if cfg["pause_market_maker"] else None
        if strategy:
            pf = _pause_path(strategy)
            if not os.path.exists(pf):
                os.makedirs(os.path.dirname(pf), exist_ok=True)
                with open(pf, "w") as f:
                    f.write(f"{PAUSE_MARK} pid={os.getpid()}\n")
                rec["pause_file"] = pf
                _write_json(RUN_FILE, rec)
                log(f"paused the market maker {strategy} for this run")
        host, port = os.getenv("HYDRA_HOST", "localhost"), int(os.getenv("HYDRA_PORT", "5008"))
        raw = Path(_path("config/exchanges/hydra.yaml")).read_text() \
            .replace("${HYDRA_HOST}", host).replace("${HYDRA_PORT}", str(port))
        xcfg = yaml.safe_load(raw); xcfg["host"], xcfg["port"] = host, port
        ex = HydraExchange(xcfg); await ex.connect()
        client = HydraGRPCClient(host=host, port=port); client.connect(); client._log = lambda *a, **k: None
        pair = next(p for p in await ex.get_trading_pairs() if p.symbol == market)
        await ex.ensure_market_initialized(pair)
        mc = ex._market_cache.get(pair.symbol, {})
        base_dp, price_dp = int(mc.get("base_precision", 6)), int(mc.get("quote_precision", 6))
        pcfg = next(p for p in xcfg["trading_pairs"] if f'{p["base_symbol"]}/{p["quote_symbol"]}' == market)
        rates = _fee_rates(client, pcfg)
        if rates:
            fees = _read_json(FEES_FILE, {}); fees[market] = rates; _write_json(FEES_FILE, fees)
        rate = (sum(rates) if rates else MARKETS[market]["taker"] + MARKETS[market]["maker"]) / 100
        if strategy and rec.get("pause_file"):          # let the market maker pull its quotes
            for _ in range(45):
                if not await ex.get_orders(pair):
                    break
                await asyncio.sleep(1)

        base, quote = market.split("/")
        def capacity():
            out = {}
            for b in client.get_orderbook_balances():
                c, x = b.currency, b.balance
                for name, key in ((base, "base"), (quote, "quote")):
                    want = pcfg[key]
                    if c.network_id == str(want["network_id"]) and c.asset_id.lower() == want["asset_id"].lower():
                        f = lambda d: float(d.value) if d.value else 0.0
                        out[key] = (f(x.sending), f(x.receiving))
            if set(out) != {"base", "quote"}:
                raise Abort(f"no channel balance found for {market}")
            return out
        px = {"t": 0.0, "v": None}
        def reference():
            if market.startswith("USDC"):
                return 1.0, 1.0
            if time.time() - px["t"] > 30 or px["v"] is None:
                px["v"], px["t"] = E.prices(), time.time()
            p = px["v"]
            if market == "ETH/BTC":
                return p["ETH"] / p["BTC"], p["BTC"]
            return p[base], 1.0
        def progress(d):
            rec.update(d); _write_json(RUN_FILE, rec)

        log(f"volume run on {market}: target ${usd:,.2f}, rounds up to {max_size:g} {base}, "
            f"est. cost {rate * 100:.3f}% of volume")
        matcher = SelfMatcher(ex, pair, market, usd, max_size, base_dp, price_dp, capacity, reference, stop,
                              progress=progress, log=log, rate=rate)
        t0 = time.time()
        try:
            result = await matcher.run()
        except Abort as e:
            result = f"aborted: {e}"
            log(f"ABORT: {e}")
        except Exception as e:                          # e.g. a gRPC error mid-round
            result = f"aborted: {type(e).__name__}: {e}"
            log(f"ABORT: {type(e).__name__}: {e}")
            await matcher.cancel_leftovers()
        if matcher.rounds:
            await asyncio.sleep(5)                      # the last swap's payment records land a moment later
            try:
                d_b, d_q, n = selfmatch_cost(client, pcfg, t0 - 5)
                _, quote_usd = reference()
                ext, _ = reference()
                rec["cost_usd"] = round(-(d_q + d_b * ext) * quote_usd, 4)
                rec["swaps_counted"] = n
            except Exception as e:
                log(f"cost from payment records unavailable ({type(e).__name__}: {e}); using the fee estimate")
            _add_stats(market, matcher.volume, rec["cost_usd"], matcher.rounds)
        rec.update(volume_usd=round(matcher.volume, 2), rounds=matcher.rounds, message=result)
        log(f"{result}: {matcher.rounds} rounds, ${matcher.volume:,.2f} volume, cost ${rec['cost_usd']:.4f}"
            + (f" ({rec['cost_usd'] / matcher.volume * 100:.3f}% of volume)" if matcher.volume else ""))
    except Abort as e:
        result = f"aborted: {e}"
        log(f"ABORT: {e}")
    except Exception as e:
        result = f"aborted: {type(e).__name__}: {e}"
        log(f"ABORT: {type(e).__name__}: {e}")
    finally:
        _finish(rec, result)
        try:
            os.remove(_path(STOP_FILE))
        except OSError:
            pass
    return dict(rec, result=result)
