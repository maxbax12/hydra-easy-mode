#!/usr/bin/env python3
"""Ops daemon: runs next to the trading bot and alerts (Telegram) when the
book is at risk of going dark. Every `interval` seconds it checks:

  bot        trading bot process alive and logging status lines
  leases     a liquidity lease ends within 24h / 3h
  capacity   per asset, free+reserved send/receive vs what the full ladders need
  log        swap failures (🛑), tracebacks

  ./trading-bot-venv/bin/python tools/ops_daemon.py [--once]
Config: config/ops.yaml.
"""
import datetime as dt
import logging
import os
import re
import subprocess
import sys
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.join(ROOT, "tools")); os.chdir(ROOT)
from dotenv import load_dotenv
load_dotenv(".env")
import grpc
import yaml
from lib.alerts import Alerter
from lib.grpc_client import HydraGRPCClient
from lib.hydra_pb import liquidity_pb2 as L, liquidity_pb2_grpc as LG, primitives_pb2 as P

NETS = {"BTC": P.Network(protocol=1, id="f9beb4d9"), "ETH": P.Network(protocol=2, id="1"),
        "ARB": P.Network(protocol=2, id="42161")}
ASSETS = {("f9beb4d9", "0x" + "0" * 64): "BTC", ("1", "0x" + "0" * 40): "ETH",
          ("42161", "erc20:0xaf88d065e77c8cc2239327c5edb3a432268e5831"): "USDC.arb",
          ("1", "erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"): "USDC.eth"}
LOG_PATTERNS = [("swapfail", re.compile(r"🛑 ([A-Z/.a-z]+): swap failed")),
                ("traceback", re.compile(r"^Traceback"))]


def bot_alive(root: str) -> bool:
    out = subprocess.run(["pgrep", "-f", "trading_bot_cli.py --config"], capture_output=True, text=True).stdout.split()
    for p in out:
        try:
            if os.path.realpath(f"/proc/{p}/cwd") == os.path.realpath(root) and \
                    open(f"/proc/{p}/cmdline", "rb").read().split(b"\0")[0].endswith(b"python3"):
                return True
        except Exception:
            pass
    return False


def check_bot(al: Alerter, cfg, log_path: str):
    alive = bot_alive(ROOT)
    age = time.time() - os.path.getmtime(log_path) if os.path.exists(log_path) else 1e9
    last_status = None
    try:
        tail = subprocess.run(["tail", "-n", "3000", log_path], capture_output=True, text=True).stdout
        m = [l for l in tail.splitlines() if "📊 " in l and "strategy.mm_" in l]
        if m:
            last_status = dt.datetime.strptime(m[-1][:19], "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        pass
    quiet = time.time() - last_status if last_status else 1e9
    if not alive:
        al.raise_("bot", "🚨 Trading bot is NOT running — no quotes on any market. "
                         + cfg.get("bot_start_hint", "The container restarts it by itself; if it stays down: "
                                                     "docker compose restart bot (in easy/)."))
    elif quiet > cfg.get("status_max_age", 900):
        al.raise_("bot", f"⚠️ Trading bot runs but logged no status for {quiet / 60:.0f} min (log idle {age / 60:.0f} min)")
    else:
        al.clear("bot", "✅ Trading bot running again")


# Lease fees are paid in HDN where possible (off-chain from an HDN channel), USDC.arb otherwise.
FEE_ASSETS = [("HDN", "erc20:0xb0f66bdb39acbb043308eb9dbe78f5bb47ea5430"),
              ("USDC.arb", "erc20:0xaf88d065e77c8cc2239327c5edb3a432268e5831")]


def hdn_usd() -> float:
    """HDN in USD (CoinGecko via the price oracle); 0 if unknown."""
    try:
        from lib.price_oracle import PriceOracle
        return float(PriceOracle(logging.getLogger("oracle")).get_market_pair_price("HDN/USDC") or 0.0)
    except Exception:
        return 0.0


def renew_lease(al: Alerter, stub, net, l, left_h: float, auto: dict, asset: str) -> bool:
    """Lease autopilot (config lease_autorenew): extend a lease ending within
    renew_below_hours by up to extend_hours (a lease may end at most 7 days out),
    paid off-chain — in HDN if the service takes it, else USDC.arb — unless the fee is
    above max_fee_usd. True = extended."""
    from lib.easy_ops import lease_extension_hours
    hours = lease_extension_hours(left_h * 3600, int(auto.get("extend_hours", 168)))
    if hours < 1:
        return False
    key = f"renew:{l.channel_id[:10]}:{asset}"
    req, fee, pay, fee_usd, why = None, 0.0, "", 0.0, ""
    for pay, pay_id in FEE_ASSETS:
        r = L.RequestChannelLeaseExtensionRequest(
            network=net, channel_id=l.channel_id, asset_id=l.asset_id, lease_extension_seconds=hours * 3600,
            payment_network=NETS["ARB"], payment_asset_id=pay_id, offchain_fee_payment=L.OffchainFeePayment())
        try:
            fee = float(stub.EstimateRequestChannelLeaseExtensionFee(r).fee.value)
        except grpc.RpcError as e:
            why = (e.details() or "").strip().splitlines()[-1][:150]
            continue
        px = hdn_usd() if pay == "HDN" else 1.0
        if px <= 0:
            why = "no HDN price to check the fee cap"
            continue
        req, fee_usd = r, fee * px
        break
    if req is None:
        al.raise_(key, f"⚠️ Lease autopilot: can't quote the {asset} extension ({l.channel_id[:10]}): {why}")
        return False
    cap = float(auto.get("max_fee_usd", 10))
    if fee_usd > cap:
        al.raise_(key, f"⚠️ Lease autopilot: extending {asset} by {hours}h costs {fee:.2f} {pay} (${fee_usd:.2f}), "
                       f"above the ${cap:g} cap — not extended. Raise lease_autorenew.max_fee_usd or extend by hand.")
        return False
    try:
        r = stub.RequestChannelLeaseExtension(req, timeout=600)
    except grpc.RpcError as e:
        al.raise_(key, f"⚠️ Lease autopilot: extending {asset} failed: "
                       f"{(e.details() or '').strip().splitlines()[-1][:150]}")
        return False
    when = dt.datetime.fromtimestamp(r.expiry_timestamp_seconds, dt.timezone.utc).strftime("%b %d %H:%M UTC")
    al.clear(key)
    al.send(f"🔄 Lease {asset} inbound (channel {l.channel_id[:10]}) extended by {hours}h for "
            f"{fee:.2f} {pay}" + (f" (${fee_usd:.2f})" if pay != "USDC.arb" else "") + f" — now ends {when}")
    return True


def check_leases(al: Alerter, stub, cfg):
    now = time.time()
    warn_h = cfg.get("lease_warn_hours", [24, 3])
    auto = cfg.get("lease_autorenew") or {}
    seen = set()
    for n, net in NETS.items():
        try:
            leases = stub.GetLeases(L.GetLeasesRequest(network=net)).leases
        except grpc.RpcError:
            continue
        for l in leases:
            if not l.HasField("expiry"):
                continue
            # The hub keeps its liquidity until `liquidity_expiry`: the later of the paid
            # lease and what the channel's activity buys it. That is when the book
            # actually shrinks, so alerts and the autopilot go by it (older nodes: lease).
            end = l.expiry.seconds
            if l.HasField("liquidity_expiry") and l.liquidity_expiry.seconds > end:
                end = l.liquidity_expiry.seconds
            left_h = (end - now) / 3600
            if left_h < 0:
                continue                      # already expired (old leases stay listed)
            asset = ASSETS.get((net.id, l.asset_id.lower()), l.asset_id[:12])
            key = f"lease:{l.channel_id[:10]}:{asset}"
            seen.add(key)
            if auto.get("enabled") and left_h <= float(auto.get("renew_below_hours", 24)) and \
                    renew_lease(al, stub, net, l, left_h, auto, asset):
                al.clear(key)                 # its warning (if any) is moot; the 🔄 message said it all
                continue
            stage = next((h for h in sorted(warn_h) if left_h <= h), None)
            when = dt.datetime.fromtimestamp(end, dt.timezone.utc).strftime("%b %d %H:%M UTC")
            if stage is not None:
                # The text must not change between checks (the alerter re-sends on a new
                # text): only the stage (<24h, <3h) or a moved expiry makes a new message.
                al.raise_(key, f"⏳ Lease {asset} inbound (channel {l.channel_id[:10]}) ends {when} "
                               f"(<{stage}h left) and the hub plans to take its liquidity back then. "
                               f"Extend or re-lease, or that side of the book shrinks.")
            elif key in al.active:
                how = "by the hub (channel activity)" if end > l.expiry.seconds else "(lease extended)"
                al.clear(key, f"✅ Lease {asset} inbound (channel {l.channel_id[:10]}) kept {how} — "
                              f"liquidity now stays until {when}")
    for key in seen:
        _LEASE_MISSING.pop(key, None)
    for key in [k for k in al.active if k.startswith("lease:") and k not in seen]:
        # GetLeases now and then leaves a live lease out (2026-10-01: ⏳ → ⌛ → ⏳ within
        # 5 min for one BTC lease), and a failed call lists nothing: "ended" only once the
        # lease is missing at two checks in a row AND the end its warning named has passed.
        _LEASE_MISSING[key] = _LEASE_MISSING.get(key, 0) + 1
        end = _lease_end(al.active[key])
        if _LEASE_MISSING[key] >= 2 and (end is None or end <= now):
            del _LEASE_MISSING[key]
            al.clear(key, f"⌛ Lease {key.split(':', 2)[2]} inbound (channel {key.split(':')[1]}) has ended")


_LEASE_MISSING: dict = {}   # lease alert key -> checks in a row the lease was not listed


def _lease_end(text: str):
    """Epoch seconds of the end an ⏳ lease warning names ("ends Oct 02 08:00 UTC"), or None."""
    m = re.search(r"ends (\w{3} \d{2} \d{2}:\d{2}) UTC", text or "")
    if not m:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    try:
        t = dt.datetime.strptime(f"{now.year} {m.group(1)}", "%Y %b %d %H:%M").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    if (t - now).days > 180:                 # the text has no year: written last December
        t = t.replace(year=t.year - 1)
    elif (now - t).days > 180:               # ... or ends next January
        t = t.replace(year=t.year + 1)
    return t.timestamp()


_CAP_LOW: set = set()       # capacity keys that were low at the previous check


def check_capacity(al: Alerter, client, cfg):
    """Alert when a side's capacity is low at TWO checks in a row: a node that is starting
    reports 0 for chains whose channels are not loaded yet (2026-10-01: 📉/✅ pairs on
    every restart)."""
    need = cfg.get("capacity_need", {})
    frac = cfg.get("capacity_alert_frac", 0.5)
    v = lambda d: float(d.value) if getattr(d, "value", "") else 0.0
    low_now = set()
    for b in client.get_orderbook_balances():
        name = ASSETS.get((b.currency.network_id, b.currency.asset_id.lower()))
        if not name or name not in need:
            continue
        x = b.balance
        have = {"send": v(x.sending) + v(x.in_use_sending), "recv": v(x.receiving) + v(x.in_use_receiving)}
        for side in ("send", "recv"):
            want = need[name].get(side)
            key = f"cap:{name}:{side}"
            if want and have[side] < frac * want:
                low_now.add(key)
                if key not in _CAP_LOW:
                    continue                  # first low reading: wait for the next check
                what = "send (sell)" if side == "send" else "receive (inbound)"
                al.raise_(key, f"📉 {name} capacity to {what}: {have[side]:.6g} of {want:g} needed "
                               f"({have[side] / want:.0%}) — quotes on that side are shrinking. "
                               + ("Move funds in / rebalance." if side == "send" else "Lease inbound or rebalance."),
                          once=True)      # the amount moves every check: tell once, until it recovers
            elif have[side] >= 0.7 * (want or 0):
                al.clear(key, f"✅ {name} {side} capacity back to {have[side]:.6g}")
    _CAP_LOW.clear()
    _CAP_LOW.update(low_now)


def check_log(al: Alerter, log_path: str, window: int):
    cutoff = dt.datetime.now() - dt.timedelta(seconds=window)
    try:
        tail = subprocess.run(["tail", "-n", "4000", log_path], capture_output=True, text=True).stdout.splitlines()
    except Exception:
        return
    hits = {}
    for i, line in enumerate(tail):
        try:
            t = dt.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            t = None
        if t and t < cutoff:
            continue
        for kind, rx in LOG_PATTERNS:
            m = rx.search(line)
            if m and (t or kind == "traceback"):
                hits[kind] = (line[:19], m.groups(), line)
    for kind in ("swapfail", "traceback"):
        key = f"log:{kind}"
        if kind in hits:
            ts, g, line = hits[kind]
            text = {"swapfail": f"🛑 Swap failure on {g[0] if g else '?'} ({ts}) — market paused",
                    "traceback": "💥 Python traceback in trading_bot.log — check the bot"}[kind]
            al.raise_(key, text)
        else:
            al.clear(key)


FILL_RX = re.compile(r"strategy\.(mm_\w+) - INFO - 💱 ([^:]+): (buy|sell) ([0-9.e-]+) @ ([0-9.e-]+) \(([^)]*)\) — "
                     r"()position ([-+0-9.e]+) \(([-+0-9]+%)")
TAKE_RX = re.compile(r"🎯 ([^:]+): (buy|sell) ([0-9.e-]+) @ ([0-9.e-]+) — mispriced ([0-9.]+%)")
# hot reloads and rebalance bookings (and their rejections), forwarded as logged
RELOAD_RX = re.compile(r" - (?:INFO|WARNING|ERROR) - ((?:🔄 reload|🔄 \S+: config reloaded|🔁 |⚠️\s+reload rejected|"
                       r"⚠️\s+rebalance booking rejected|❌ reload).*)$")




FAIRS: dict = {}            # pair -> latest fair from the bot's 📊 lines (USD value of BTC-quoted fills)
FAIR_RX = re.compile(r"strategy\.mm_\w+ - INFO - 📊 ([^:]+): fair ([0-9.]+)")
FILL_LINE_RX = re.compile(r"^💱 ([^:]+): (BOUGHT|SOLD) ([0-9.,]+) @ ([0-9.,]+) \(≈ ([0-9.,]+) (\S+), [^)]*\) — ((?:net )?position .*)$")


def usd_value(amount: float, quote: str):
    if quote.startswith("USD"):
        return amount
    px = FAIRS.get(f"{quote}/USDC.arb")
    return amount * px if px else None


def compact_digest(items, min_usd: float = 5.0):
    """Digest body with the small stuff summed: fills worth less than `min_usd` become
    one line per market and side (2026-10-01 12:47–12:59: 15 lines of ~$1.20 partial
    fills from one taker). Bigger fills keep their own line; the header still counts
    every fill."""
    num = lambda x: float(x.replace(",", ""))
    out, groups = [], {}
    for line in (l for block in items for l in block.splitlines()):
        key = rec = None
        m = FILL_LINE_RX.match(line)
        if m:
            pair, verb, amt, _, val, quote, pos = m.groups()
            usd = usd_value(num(val), quote)
            if usd is not None and usd < min_usd:
                key, rec = ("fill", pair, verb), (num(amt), num(val), quote, pos)
        if key is None:
            out.append(line)
            continue
        if key not in groups:
            groups[key] = {"at": len(out), "n": 0, "amt": 0.0, "val": 0.0, "line": line}
            out.append(line)
        g = groups[key]
        g["n"] += 1; g["amt"] += rec[0]; g["val"] += rec[1]; g["rec"] = rec
    for key, g in groups.items():
        if g["n"] < 2:
            continue
        avg = fmt_amt(g["val"] / g["amt"]) if g["amt"] else "-"
        if key[0] == "fill":
            out[g["at"]] = (f"💱 {key[1]}: {key[2]} {fmt_amt(g['amt'])} in {g['n']} small fills "
                            f"(≈ {fmt_amt(g['val'])} {g['rec'][2]}, avg {avg}) — {g['rec'][3]}")
    return out


class LogFollower:
    """New lines appended to a log since the last call (starts at the end; survives rotation)."""

    def __init__(self, path: str):
        self.path, self.pos, self.ino = path, None, None

    def new_lines(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return []
        if self.pos is None or st.st_ino != self.ino or st.st_size < self.pos:
            first = self.pos is None
            self.ino, self.pos = st.st_ino, (st.st_size if first else 0)
            if first:
                return []
        with open(self.path, "r", errors="replace") as f:
            f.seek(self.pos)
            data = f.read()
            self.pos = f.tell()
        return data.splitlines()


def fmt_amt(x: float) -> str:
    """Human numbers, never scientific notation (2.83e-05 -> 0.0000283)."""
    if x >= 100:
        return f"{x:,.2f}"
    s = f"{x:.10f}".rstrip("0").rstrip(".")
    lead = len(s.split(".")[1]) - len(s.split(".")[1].lstrip("0")) if "." in s else 0
    return f"{x:.{lead + 6}f}".rstrip("0").rstrip(".") if x < 1 else f"{x:.6g}"


def fill_messages(lines):
    """One Telegram message for a batch of fills and takes (None if nothing)."""
    out = []
    for line in lines:
        m = FAIR_RX.search(line)
        if m:
            FAIRS[m.group(1)] = float(m.group(2))
            continue
        m = FILL_RX.search(line)
        if m:
            _, pair, side, amt, px, how, net, pos, pct = m.groups()
            a, p = float(amt), float(px)
            q = float(pos)
            q_s = f"{q:+.6g}"
            where = f"position {q_s} ({pct})"
            quote = pair.split("/")[1]
            kind = ("fill" if how == "filled" else "late-booked fill" if how == "filled after cancel"
                    else "partial fill" if how.startswith("partial") else how)
            out.append(f"💱 {pair}: {'BOUGHT' if side == 'buy' else 'SOLD'} {fmt_amt(a)} @ {fmt_amt(p)} "
                       f"(≈ {fmt_amt(a * p)} {quote}, {kind}) — {where}")
            continue
        m = TAKE_RX.search(line)
        if m:
            pair, side, amt, px, edge = m.groups()
            out.append(f"🎯 {pair}: taking — {side} {fmt_amt(float(amt))} @ {fmt_amt(float(px))} ({edge} vs fair)")
            continue
        m = RELOAD_RX.search(line)
        if m:
            out.append(re.sub(r"^(⚠️)\s+", r"\1 ", m.group(1)))
    return "\n".join(out) if out else None


ALERT_MARKS = set("🚨⚠📉⏳🔻🛑💥⚖✅❌")


def digest_header(items):
    """Overview line(s) for a digest: fills per market with notional, takes, alerts."""
    fills, notional, takes, alerts = {}, {}, 0, 0
    for block in items:
        for line in block.splitlines():
            m = re.match(r"💱 ([^:]+): \w+ ([0-9.,e-]+) @ ([0-9.,e-]+) \(≈ ([0-9.,e-]+) (\S+),", line)
            if m:
                pair, _, _, val, q = m.groups()
                fills[pair] = fills.get(pair, 0) + 1
                notional[q] = notional.get(q, 0.0) + float(val.replace(",", ""))
            elif line.startswith("🎯"):
                takes += 1
            elif line.strip() and line.lstrip()[:1] in ALERT_MARKS:
                alerts += 1
    parts = []
    if fills:
        n = sum(fills.values())
        vol = ", ".join(f"{v:,.2f} {q}" if v >= 1 else f"{v:.6g} {q}" for q, v in notional.items())
        parts.append(f"💱 {n} fill{'s' if n > 1 else ''} ({vol}) — " + ", ".join(f"{k} {v}" for k, v in fills.items()))
    if takes:
        parts.append(f"🎯 {takes} arbitrage take{'s' if takes > 1 else ''}")
    if alerts:
        parts.append(f"⚠️ {alerts} alert{'s' if alerts > 1 else ''}")
    return ("\n".join(parts) + "\n—\n") if parts else ""


STATUS_RX = re.compile(r"^(\S+ \S+),\d+ - strategy\.mm_\w+ - INFO - 📊 ([^:]+): fair ([0-9.]+) .*?\| ([0-9]+) fills.*est\. P&L ([-+0-9.e]+)")
MARKETS = ["BTC/USDC.arb", "ETH/BTC", "ETH/USDC.arb", "HDN/USDC.arb", "USDC.arb/USDC.eth"]


def pnl_snapshot(log_path="trading_bot.log", lines=20000):
    """Latest estimated P&L per market in USD (ETH/BTC converted at the BTC/USDC fair)."""
    tail = subprocess.run(["tail", "-n", str(lines), log_path], capture_output=True, text=True).stdout.splitlines()
    last, btc = {}, None
    for line in tail:
        m = STATUS_RX.match(line)
        if m:
            ts, pair, fair, fills, pnl = m.groups()
            if pair == "BTC/USDC.arb":
                btc = float(fair)
            last[pair] = (float(pnl), int(fills), ts)
    out = {}
    for pair, (pnl, fills, ts) in last.items():
        out[pair] = (pnl * (btc or 0) if pair == "ETH/BTC" else pnl, fills)
    return out


def fee_standing(host="127.0.0.1", port="5008"):
    """Weighted 30d volume and tier from orderbook_getFeeDiscount (None if unavailable)."""
    import json, urllib.request
    try:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "orderbook_getFeeDiscount", "params": [{}]}).encode()
        with urllib.request.urlopen(urllib.request.Request(f"http://{host}:{port}", body, {"Content-Type": "application/json"}), timeout=10) as r:
            act = ((json.load(r).get("result") or {}).get("standing") or {}).get("active") or {}
        vol = act.get("volume") or {}
        g = lambda d, k: float((d.get(k) or {}).get("value") or 0)
        nxt = vol.get("nextTier") or {}
        return dict(volume=g(vol, "settledVolumeUsd"), tier=int(vol.get("tier") or 0), maker=g(act, "makerDiscount"),
                    next_from=g(nxt, "fromVolumeUsd") if nxt else None, next_maker=g(nxt, "makerDiscount") if nxt else None)
    except Exception:
        return None


class PnlTracker:
    """P&L line for digests (change since the last message) and a daily report."""

    def __init__(self, state_path="state/ops_pnl.json"):
        import json
        self.path, self.json = state_path, json
        try:
            self.state = json.load(open(state_path))
        except Exception:
            self.state = {}

    def _save(self):
        try:
            self.json.dump(self.state, open(self.path, "w"))
        except Exception:
            pass

    def digest_line(self):
        snap = pnl_snapshot()
        if not snap:
            return ""
        total = sum(v for v, _ in snap.values())
        prev = self.state.get("last_digest_total")
        self.state["last_digest_total"] = total
        self._save()
        delta = f" ({total - prev:+.2f} since last msg)" if prev is not None else ""
        return f"💰 P&L ${total:+.2f}{delta}\n"

    def daily(self, hour: int):
        now = time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        if now.tm_hour < hour or self.state.get("daily_sent") == today:
            return None
        snap = pnl_snapshot()
        if not snap:
            return None
        total = sum(v for v, _ in snap.values()); fills = sum(n for _, n in snap.values())
        prev = self.state.get("daily_total"); prev_fills = self.state.get("daily_fills")
        lines = [f"📅 Daily report {today}"]
        for pair in MARKETS:
            if pair in snap:
                lines.append(f"  {pair:<18} ${snap[pair][0]:+8.2f}   {snap[pair][1]} fills")
        lines.append(f"  {'TOTAL':<18} ${total:+8.2f}   {fills} fills"
                     + (f"  (24h: {total - prev:+.2f} $, {fills - prev_fills} fills)" if prev is not None else ""))
        fs = fee_standing()
        if fs:
            tier = f"tier {fs['tier']} (maker discount {fs['maker']:.0%})"
            nxt = (f", next ${fs['next_from']:,.0f} → maker {fs['next_maker']:.0%} (${fs['next_from'] - fs['volume']:,.0f} to go)"
                   if fs.get("next_from") else ", top tier")
            lines.append(f"  30d weighted volume ${fs['volume']:,.0f} — {tier}{nxt}")
        note = "  (est. P&L, marked to fair; leases not included)"
        lines.append(note)
        try:
            from lib.easy_ops import connect, market_daily_stats, daily_stats_lines
            dl = daily_stats_lines(market_daily_stats(connect()))
            if dl:
                lines += ["", "📈 DEX last 24 h (all traders):"] + dl
        except Exception:
            pass
        self.state.update(daily_sent=today, daily_total=total, daily_fills=fills)
        self._save()
        return "\n".join(lines)


def main():
    cfg = yaml.safe_load(open("config/ops.yaml")) or {}
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - ops - %(levelname)s - %(message)s",
                        handlers=[logging.FileHandler("ops.log"), logging.StreamHandler()])
    log = logging.getLogger("ops")
    al = Alerter(log)
    al.digest_every = float(cfg.get("digest_every", 300)) or None   # one Telegram message per 5 min at most
    client = HydraGRPCClient(host=os.getenv("HYDRA_HOST", "localhost"), port=int(os.getenv("HYDRA_PORT", "5008")))
    client.connect(); client._log = lambda *a, **k: None
    liq = LG.LiquidityServiceStub(grpc.insecure_channel(f"{os.getenv('HYDRA_HOST', 'localhost')}:{os.getenv('HYDRA_PORT', '5008')}"))
    rb = None
    interval = float(cfg.get("interval", 300))
    fill_poll = float(cfg.get("fill_poll", 20))
    follower = LogFollower("trading_bot.log")
    min_usd = float(cfg.get("fill_alert_min_usd", 5))
    log.info(f"ops daemon started (every {interval:.0f}s, telegram {'on' if al.token else 'off'}, "
             f"rebalancer {'on' if rb else 'off'})")
    if "--test-alert" in sys.argv:
        al._post("✅ Ops daemon started — alerts are working.")
    last_checks = 0.0
    pnl = PnlTracker()
    daily_hour = int(cfg.get("daily_report_hour", 20))
    while True:
        try:
            lines = follower.new_lines()
            msg = fill_messages(lines)
            if msg and cfg.get("fill_alerts", True):
                al.send(msg)
        except Exception as e:
            log.warning(f"fill alerts failed: {type(e).__name__}: {e}")
        try:
            rep_msg = pnl.daily(daily_hour)
            if rep_msg:
                al.send(rep_msg)
        except Exception as e:
            log.warning(f"daily report failed: {type(e).__name__}: {e}")
        al.flush(lambda items: pnl.digest_line() + digest_header(items), force="--once" in sys.argv,
                 body_fn=lambda items: compact_digest(items, min_usd))
        if time.time() - last_checks < interval and "--once" not in sys.argv:
            time.sleep(fill_poll)
            continue
        last_checks = time.time()
        for name, fn in (("bot", lambda: check_bot(al, cfg, "trading_bot.log")),
                         ("leases", lambda: check_leases(al, liq, cfg)),
                         ("capacity", lambda: check_capacity(al, client, cfg)),
                         ("log", lambda: check_log(al, "trading_bot.log", int(interval) + 60)),
                         ("rebalance", lambda: rb.step() if rb else None)):
            try:
                fn()
            except Exception as e:
                log.warning(f"{name} check failed: {type(e).__name__}: {e}")
        if "--once" in sys.argv:
            break
        time.sleep(fill_poll)


if __name__ == "__main__":
    main()
