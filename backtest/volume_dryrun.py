#!/usr/bin/env python3
"""Dry run of the pre-launch volume mode (lib/volume.py, tools/volume_daemon.py) against a
fake exchange: other participants' quotes + our self-matched rounds. No node, no network.

  ./trading-bot-venv/bin/python backtest/volume_dryrun.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tools"))
os.chdir(ROOT)
from connectors.base_exchange import OrderSide, OrderType
from lib import volume as V
import volume_daemon as D

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  {detail}")


class FakeEx:
    """Our resting orders + other participants' levels. An order of ours crossing our own
    resting order at the same price matches it at once (both disappear)."""

    def __init__(self, bids=((0.9985, 100.0),), asks=((1.0, 50.0),)):
        self.bids, self.asks = list(bids), list(asks)
        self.mine, self.placed, self.cancelled, self.n = {}, [], [], 0
        self.on_place = None          # hook(order) for scenarios
        self.flip = False             # book moves on every read

    async def get_orderbook(self, pair, limit=50):
        if self.flip:
            self.bids[0] = (self.bids[0][0], self.bids[0][1] + 1)
        b = self.bids + [(o.price, o.amount) for o in self.mine.values() if o.side == OrderSide.BUY]
        a = self.asks + [(o.price, o.amount) for o in self.mine.values() if o.side == OrderSide.SELL]
        return types.SimpleNamespace(bids=sorted(b, key=lambda x: -x[0]), asks=sorted(a, key=lambda x: x[0]))

    async def get_orders(self, pair):
        return list(self.mine.values())

    async def place_order(self, pair, side, type, amount, price, params=None):
        self.n += 1
        o = types.SimpleNamespace(id=f"o{self.n}", side=side, price=price, amount=amount, filled=0.0)
        self.placed.append((side, price, amount, dict(params or {})))
        opp = [m for m in self.mine.values() if m.side != side and abs(m.price - price) < 1e-12]
        if opp:
            del self.mine[opp[0].id]
        else:
            self.mine[o.id] = o
        if self.on_place:
            self.on_place(o)
        return o

    async def cancel_order(self, oid, pair):
        self.cancelled.append(oid)
        self.mine.pop(oid, None)
        return True


async def nosleep(s):
    await asyncio.sleep(0)


BIG = {"base": (10_000.0, 10_000.0), "quote": (10_000.0, 10_000.0)}


def matcher(ex, usd=300.0, max_size=70.0, cap=BIG, ref=(1.0, 1.0), stop=lambda: False, logs=None):
    return V.SelfMatcher(ex, "PAIR", "USDC.arb/USDC.eth", usd, max_size, base_dp=2, price_dp=5,
                         capacity=lambda: cap, reference=lambda: ref, stop=stop,
                         log=(logs.append if logs is not None else (lambda m: None)), sleep=nosleep, settle_s=0)


async def main():
    print("\n1. self-matched rounds")
    ex = FakeEx(); m = matcher(ex)
    res = await m.run()
    makers = [p for p in ex.placed if p[3]["client_order_id"].split("-")[-2] == "m"]
    check("target $300 in rounds of 70: done, 5 rounds, ~$300", res == "done" and m.rounds == 5 and 299 < m.volume < 301,
          f"{res} {m.rounds} {m.volume}")
    check("maker side alternates sell / buy", [p[0] for p in makers][:3] == [OrderSide.SELL, OrderSide.BUY, OrderSide.SELL])
    check("price inside the others' spread (0.9985 / 1.0), clear of both",
          all(0.9985 + 0.0003 < p[1] < 1.0 - 0.0003 for p in ex.placed), f"{set(p[1] for p in ex.placed)}")
    check("every order tagged vol-… (client_order_id)", all(p[3]["client_order_id"].startswith("vol-") for p in ex.placed))
    check("last round sized to what is left (20.1, not 70)", abs(ex.placed[-1][2] - 20.1) < 0.02, f"{ex.placed[-1][2]}")
    check("nothing of ours left on the book", not ex.mine)

    print("\n2. someone reprices around our maker")
    ex = FakeEx(); hit = {"n": 0}
    def once(o):
        if hit["n"] == 0 and o.side == OrderSide.SELL:
            hit["n"] += 1; ex.asks[0] = (1.0, 51.0)          # the other maker requotes
    ex.on_place = once
    logs = []; m = matcher(ex, usd=140, logs=logs)
    res = await m.run()
    check("one repricing: maker pulled, retried, run completes", res == "done" and m.rounds == 2
          and any("retrying (1/10)" in l for l in logs) and not ex.mine, f"{res} {m.rounds} {logs[-3:]}")
    ex = FakeEx()
    def always(o):
        if o.side == OrderSide.SELL:
            ex.asks[0] = (1.0, ex.asks[0][1] + 1)
    ex.on_place = always
    m = matcher(ex, usd=140)
    try:
        await m.run(); res = "no abort"
    except V.Abort as e:
        res = str(e)
    check("repricing every time: aborts after 10 retries, maker cancelled", "10 retries" in res and not ex.mine, res)

    print("\n3. aborts")
    ex = FakeEx()
    def touch(o):
        if len(ex.placed) == 1:
            o.filled = 1.0
    ex.on_place = touch
    try:
        await matcher(ex).run(); res = "no abort"
    except V.Abort as e:
        res = str(e)
    check("our maker filled by someone else first: abort, maker cancelled", "touched" in res and not ex.mine, res)
    try:
        await matcher(FakeEx(), cap={"base": (3.0, 3.0), "quote": (3.0, 3.0)}).run(); res = "no abort"
    except V.Abort as e:
        res = str(e)
    check("free capacity below one round: abort with a hint", "not enough free capacity" in res, res)
    try:
        await matcher(FakeEx(), ref=(1.01, 1.0)).run(); res = "no abort"
    except V.Abort as e:
        res = str(e)
    check("reference far from the book (1.01 vs 0.9985/1.0): abort, nothing placed", "too far" in res, res)
    ex = FakeEx(); ex.flip = True
    try:
        await matcher(ex).run(); res = "no abort"
    except V.Abort as e:
        res = str(e)
    check("book never quiet: abort, nothing placed", "never settled" in res and not ex.placed, res)
    ex = FakeEx(asks=((0.99851, 5.0),))
    try:
        await matcher(ex).run(); res = "no abort"
    except V.Abort as e:
        res = str(e)
    check("no room inside the spread: abort", "no room" in res, res)

    print("\n4. stop")
    ex = FakeEx(); cnt = {"n": 0}
    m = matcher(ex, usd=1000)
    m.stop = lambda: m.rounds >= 2
    res = await m.run()
    check("stop request: finishes the current round, result 'stopped'", res == "stopped" and m.rounds == 2 and not ex.mine)

    print("\n5. config, files, status")
    tmp = tempfile.mkdtemp(); V.ROOT = tmp
    os.makedirs(os.path.join(tmp, "config")); os.makedirs(os.path.join(tmp, "state"))
    cfg = V.load_config()
    check("defaults: off, all four markets, no targets", cfg["enabled"] is False and set(cfg["markets"]) == set(V.MARKETS)
          and all(v["daily_usd"] == 0 for v in cfg["markets"].values()))
    for bad, why in (({"markets": {"DOGE/USDC": {}}}, "unknown market"), ({"markets": {"ETH/BTC": {"daily_usd": -5}}}, "daily_usd"),
                     ({"active_hours": [20, 8]}, "active_hours"), ({"enabled": "yes"}, "true or false"),
                     ({"markets": {"USDC.arb/USDC.eth": {"max_size": 1}}}, "max_size")):
        try:
            V.validate_config(bad); msg = "accepted"
        except ValueError as e:
            msg = str(e)
        check(f"rejects {json.dumps(bad)[:50]}", why in msg, msg)
    cfg["enabled"] = True; cfg["markets"]["USDC.arb/USDC.eth"]["daily_usd"] = 5000
    V.save_config(cfg)
    check("save + load round trip", V.load_config()["markets"]["USDC.arb/USDC.eth"]["daily_usd"] == 5000.0
          and V.load_config()["enabled"] is True)
    V._add_stats("USDC.arb/USDC.eth", 1049.6, 0.73, 15); V._add_stats("USDC.arb/USDC.eth", 840.0, 0.59, 12)
    st = V.status()
    check("per-day stats add up", st["today"]["USDC.arb/USDC.eth"] == {"volume_usd": 1889.6, "cost_usd": 1.32, "rounds": 27},
          f"{st['today']}")
    check("cost per $1,000 (list rates until a run fetches ours): USDC/USDC $0.70", st["cost_per_1000"]["USDC.arb/USDC.eth"] == 0.7)
    pf = os.path.join(tmp, "state", "pause_mm_usdc_usdc")
    open(pf, "w").write(f"{V.PAUSE_MARK} pid=999999\n")
    V._write_json(V.RUN_FILE, {"market": "USDC.arb/USDC.eth", "target_usd": 500, "volume_usd": 70, "rounds": 1, "cost_usd": 0.05,
                               "started_at": time.time(), "pid": 999999, "source": "manual", "pause_file": pf})
    st = V.status()
    check("dead run process: recorded as aborted, its pause on the market maker lifted",
          st["running"] is None and st["last_run"]["result"].startswith("aborted") and not os.path.exists(pf), f"{st['last_run']}")
    check("the pause file path is not exposed in status", "pause_file" not in (st["last_run"] or {}))
    open(pf, "w").close()                                    # someone paused it by hand
    V._release_pause(pf)
    check("a pause someone else set is never removed", os.path.exists(pf))
    V._write_json(V.RUN_FILE, {"market": "ETH/BTC", "pid": os.getpid(), "volume_usd": 0, "target_usd": 1, "rounds": 0,
                               "cost_usd": 0, "started_at": time.time(), "source": "manual"})
    try:
        V.start_run("USDC.arb/USDC.eth", 100); msg = "started"
    except RuntimeError as e:
        msg = str(e)
    check("only one run at a time", "already active" in msg, msg)
    check("stop request reaches the active run", V.stop_run() and os.path.exists(os.path.join(tmp, V.STOP_FILE)))
    os.remove(os.path.join(tmp, V.RUN_FILE))
    check("no active run: stop says so", V.stop_run() is False)

    print("\n6. daily target scheduling (tools/volume_daemon.py)")
    cfg = {"enabled": True, "burst_every_min": 20, "active_hours": [0, 24],
           "markets": {"USDC.arb/USDC.eth": {"daily_usd": 1000}, "ETH/BTC": {"daily_usd": 0}}}
    noon = time.mktime((2026, 10, 6, 12, 0, 0, 0, 0, 0)) - time.timezone      # 12:00 UTC
    pick = D.plan_burst(cfg, {}, noon, {}, False)
    check("12:00 UTC, nothing done of $1,000/day: catch up to the $500 due", pick == ("USDC.arb/USDC.eth", 500.0), f"{pick}")
    pick = D.plan_burst(cfg, {"USDC.arb/USDC.eth": {"volume_usd": 495}}, noon, {}, False)
    check("nearly on schedule: a regular burst ($1,000 / 72 bursts ≈ $13.89)", pick == ("USDC.arb/USDC.eth", 13.89), f"{pick}")
    check("on schedule: nothing", D.plan_burst(cfg, {"USDC.arb/USDC.eth": {"volume_usd": 600}}, noon, {}, False) is None)
    check("burst 5 min ago: wait", D.plan_burst(cfg, {}, noon, {"USDC.arb/USDC.eth": noon - 300}, False) is None)
    check("a run is active: wait", D.plan_burst(cfg, {}, noon, {}, True) is None)
    check("disabled: nothing", D.plan_burst(dict(cfg, enabled=False), {}, noon, {}, False) is None)
    check("outside active hours (8–18 UTC at 20:00): nothing",
          D.plan_burst(dict(cfg, active_hours=[8, 18]), {}, noon + 8 * 3600, {}, False) is None)
    failed_run = {"market": "USDC.arb/USDC.eth", "result": "aborted: no room", "ended_at": noon - 600}
    check("its last run aborted 10 min ago: back off", D.plan_burst(cfg, {}, noon, {}, False, failed_run) is None)
    check("target reached: nothing", D.plan_burst(cfg, {"USDC.arb/USDC.eth": {"volume_usd": 1000}}, noon, {}, False) is None)

    print(f"\n{passed}/{passed + failed} checks passed")
    sys.exit(1 if failed else 0)


asyncio.run(main())
