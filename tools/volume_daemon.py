#!/usr/bin/env python3
"""Volume daemon (pre-launch): spreads the daily volume targets of config/volume.yaml over
the day. Every minute it checks each market: if today's volume (UTC day) is behind the
share of the target due by now (within active_hours, UTC), it starts a burst — one
background run via lib/volume.start_run — at most every `burst_every_min` per market and
one run at a time. Idle while `enabled: false`. Runs next to the bot (easy/run_all.sh).

  python3 tools/volume_daemon.py [--once]
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from lib import volume as V

MIN_BURST_USD = {"USDC.arb/USDC.eth": 10.0}     # tiny bursts are pointless (rounds have a minimum size)
DEFAULT_MIN_BURST = 25.0


def plan_burst(cfg: dict, today: dict, now: float, last_burst: dict, running: bool, last_run=None):
    """(market, usd) to run now, or None. Pure, for tests.
    today: {market: {"volume_usd": …}}; last_burst: {market: epoch}; last_run: status()["last_run"]."""
    if not cfg.get("enabled") or running:
        return None
    start, end = cfg.get("active_hours", [0, 24])
    t = time.gmtime(now)
    hour = t.tm_hour + t.tm_min / 60
    if not start <= hour < end:
        return None
    frac = (hour - start) / (end - start)
    every = cfg.get("burst_every_min", 20) * 60
    bursts_per_day = max(1.0, (end - start) * 3600 / every)
    for market, m in cfg.get("markets", {}).items():
        daily = float(m.get("daily_usd") or 0)
        if daily <= 0:
            continue
        done = float((today.get(market) or {}).get("volume_usd", 0))
        due = daily * frac
        if done >= daily or done >= due - 1e-6 or now - last_burst.get(market, 0) < every:
            continue
        if last_run and last_run.get("market") == market and str(last_run.get("result", "")).startswith("aborted") \
                and now - float(last_run.get("ended_at") or 0) < 3 * every:
            continue                                  # it just failed: back off before trying again
        usd = min(daily - done, max(due - done, daily / bursts_per_day))
        usd = max(usd, MIN_BURST_USD.get(market, DEFAULT_MIN_BURST))
        return market, round(usd, 2)
    return None


def main():
    last_burst = {}
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} volume daemon started", flush=True)
    while True:
        try:
            st = V.status()
            pick = plan_burst(st["config"], st["today"], time.time(), last_burst, bool(st["running"]), st["last_run"])
            if pick:
                market, usd = pick
                pid = V.start_run(market, usd, source="daily")
                last_burst[market] = time.time()
                print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} burst: ${usd:,.2f} on {market} (pid {pid})", flush=True)
        except Exception as e:
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} volume daemon: {type(e).__name__}: {e}", flush=True)
        if "--once" in sys.argv:
            break
        time.sleep(60)


if __name__ == "__main__":
    main()
