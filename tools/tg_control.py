#!/usr/bin/env python3
"""Telegram control for the market maker (the same bot as the alerts).

Commands (only from the configured chat, ALERTS_TELEGRAM_CHAT_ID):
  /status            markets, quotes, positions, P&L
  /pnl               P&L per market and total
  /capacity          what is free to send / receive per asset
  /pause [market]    pull all quotes (or one market's, e.g. /pause mm_btc_usdc)
  /resume [market]   quote again
  /deposit           where to send funds
  /id                the chat id (works from any chat, to finish the setup)
  /help

Runs next to the bot: ./trading-bot-venv/bin/python tools/tg_control.py
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv
load_dotenv(".env")
import requests

from lib import easy_ops as E

HELP = ("Hydra market maker\n"
        "/status — markets, quotes, positions, P&L\n/pnl — P&L\n"
        "/book [market] — order book, our quotes marked ★\n/markets — every DEX market: best bid/ask, our share, 24 h volume\n"
        "/orders — our open quotes\n/fills [n] — latest fills\n"
        "/capacity — free capacity per asset\n/leases — when leases end / how long the hub keeps liquidity\n"
        "/wallet — on-chain balances\n/deposit — where to send funds\n/config — current settings\n"
        "/pause [market] — pull quotes (all, or e.g. /pause mm_btc_usdc)\n/resume [market] — quote again\n"
        "/id — this chat's id")


def _num(x: float) -> str:
    return f"{x:,.2f}" if x >= 100 else f"{x:.6g}"


def _markets_of(config_path: str) -> dict:
    """{strategy name: pair} of the configured markets."""
    import yaml
    try:
        doc = yaml.safe_load(open(config_path)) or {}
    except FileNotFoundError:
        return {}
    return {e["name"]: e.get("pair") for e in doc.get("strategies") or [] if e.get("enabled", True)}


def _pick_pair(arg, config_path: str, known) -> str:
    """A market from 'mm_usdc_usdc', 'USDC.arb/USDC.eth', 'usdc' … (default: the first configured one)."""
    mine = _markets_of(config_path)
    if not arg:
        return next(iter(mine.values()), None) or next(iter(known), None)
    if arg in mine:
        return mine[arg]
    for p in known:
        if p.lower() == arg.lower():
            return p
    hits = [p for p in known if arg.lower() in p.lower()]
    return hits[0] if len(hits) == 1 else None


def fmt_book(pair: str, bk: dict, depth: int = 5) -> str:
    bids, asks = bk["bids"][:depth], bk["asks"][:depth]
    base = pair.split("/")[0]
    lines = [f"📖 {pair}  (amounts in {base}, ★ = ours)"]
    for px, amt, ours in reversed(asks):
        lines.append(f"  ask {px:.6g}  {_num(amt):>10}{' ★' if ours else ''}")
    if bids and asks:
        mid = (bids[0][0] + asks[0][0]) / 2
        lines.append(f"  ─ spread {(asks[0][0] - bids[0][0]) / mid * 100:.3f}% ─")
    elif not bids and not asks:
        lines.append("  (empty)")
    for px, amt, ours in bids:
        lines.append(f"  bid {px:.6g}  {_num(amt):>10}{' ★' if ours else ''}")
    return "\n".join(lines)


def fmt_status(ms) -> str:
    if not ms:
        return "No market status yet — is the bot running?"
    lines = []
    for s in ms:
        flag = "⏸" if s["paused"] else "▶️"
        lines.append(f"{flag} {s['pair']}: {s['bids']} bids / {s['asks']} asks, position {s['position']:+.6g} "
                     f"({s['pct']}), {s['fills']} fills, P&L {s['pnl']:+.4g}")
    return "\n".join(lines)


def fmt_pnl(ms) -> str:
    if not ms:
        return "No P&L yet."
    lines = [f"{s['pair']}: {s['pnl']:+.6g} {s['pair'].split('/')[1]}" for s in ms]
    usd = sum(s["pnl"] for s in ms if s["pair"].split("/")[1].startswith("USDC"))
    lines.append(f"USD markets together: {usd:+.2f}")
    return "\n".join(lines)


def handle(text: str, chat_id: str, allowed: str, config_path: str = "config/bot_config.yaml",
           node=None, env_path: str = ".env") -> str:
    """Reply for one message (pure enough to test: node calls go through `node`)."""
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        return ""
    cmd = parts[0].split("@")[0].lower()
    arg = parts[1] if len(parts) > 1 else None
    if cmd == "/id":
        return f"chat id: {chat_id}"
    if cmd == "/start" and arg:                    # pairing: `hydra-mm telegram` printed the code
        code = E.pairing_code()
        if code and arg.strip() == code:
            E.set_env(env_path, {"ALERTS_TELEGRAM_CHAT_ID": str(chat_id)})
            try:
                os.remove(E.PAIR_FILE)
            except OSError:
                pass
            return "✅ Paired — alerts and commands now come to this chat.\n\n" + HELP
        return "" if allowed else "That code is wrong or expired — run `hydra-mm telegram` for a new one."
    if str(chat_id) != str(allowed):
        return ""                                   # everything else only from the owner's chat
    if cmd in ("/help", "/start"):
        return HELP
    if cmd == "/status":
        return fmt_status(E.market_status())
    if cmd == "/pnl":
        return fmt_pnl(E.market_status())
    if cmd in ("/pause", "/resume"):
        names = E.strategy_names(config_path)
        if arg and arg not in names:
            return f"Unknown market {arg}. Markets: {', '.join(names)}"
        E.set_pause(cmd == "/pause", arg)
        return f"{'⏸ Paused' if cmd == '/pause' else '▶️ Resumed'} {arg or 'all markets'} (within seconds)."
    if cmd == "/book":
        node = node or E.connect()
        known = E.dex_markets(node)
        pair = _pick_pair(arg, config_path, known)
        if not pair or pair not in known:
            return f"Unknown market {arg}. Markets: {', '.join(known)}"
        return fmt_book(pair, E.book(node, pair, known[pair]))
    if cmd == "/markets":
        node = node or E.connect()
        lines = ["🏪 DEX markets (best bid / ask, our share of the quotes, 24 h volume)"]
        try:
            daily = E.market_daily_stats(node)
        except Exception:
            daily = {}
        for pair, cur in E.dex_markets(node).items():
            bk = E.book(node, pair, cur)
            if not bk["bids"] and not bk["asks"]:
                continue
            bb = bk["bids"][0][0] if bk["bids"] else None
            ba = bk["asks"][0][0] if bk["asks"] else None
            tot = sum(a for _, a, _ in bk["bids"] + bk["asks"])
            ours = sum(a for _, a, o in bk["bids"] + bk["asks"] if o)
            lines.append(f"{pair}: {bb:.6g} / {ba:.6g}" if bb and ba else f"{pair}: {bb or '-'} / {ba or '-'}")
            lines[-1] += f"  ({ours / tot * 100:.0f}% ours)" if tot else ""
            if daily.get(pair, {}).get("quote_volume"):
                lines[-1] += f", 24h vol {daily[pair]['quote_volume']:,.6g} {pair.split('/')[1]}"
        return "\n".join(lines)
    if cmd == "/orders":
        node = node or E.connect()
        lines = []
        for pair, cur in E.dex_markets(node).items():
            bk = E.book(node, pair, cur)
            b = [x for x in bk["bids"] if x[2]]
            a = [x for x in bk["asks"] if x[2]]
            if b or a:
                lines.append(f"{pair}: {len(b)} bids {b[0][0]:.6g}…{b[-1][0]:.6g}" if b else f"{pair}: 0 bids")
                lines[-1] += (f", {len(a)} asks {a[0][0]:.6g}…{a[-1][0]:.6g}" if a else ", 0 asks")
        return "\n".join(lines) or "No own quotes on the book."
    if cmd == "/fills":
        n = int(arg) if arg and arg.isdigit() else 5
        fs = E.recent_fills(min(n, 20))
        if not fs:
            return "No fills yet."
        return "\n".join(f"{f['at'][5:16]} {f['pair']}: {'bought' if f['side'] == 'buy' else 'sold'} "
                         f"{_num(f['amount'])} @ {f['price']:.6g}" +
                         (f" → position {f['position']:+.6g}" if f['position'] is not None else "") for f in fs)
    if cmd == "/leases":
        import datetime as _dt
        f = lambda t: _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%b %d %H:%M UTC")
        ls = E.leases()
        if not ls:
            return "No running leases."
        return "\n".join(f"{l['asset']} ({l['chain']}, {l['channel'][:10]}): lease ends {f(l['lease_end'])}"
                         + (f", hub keeps it until {f(l['liquidity_end'])}" if l['liquidity_end'] > l['lease_end'] else "")
                         for l in ls)
    if cmd == "/wallet":
        w = E.wallet_onchain(node or E.connect())
        return "On-chain (node wallet):\n" + "\n".join(f"{a}: {_num(v)}" for a, v in w.items() if v) \
            if any(w.values()) else "The node wallet holds nothing on-chain (all in channels)."
    if cmd == "/config":
        import yaml
        try:
            doc = yaml.safe_load(open(config_path)) or {}
        except FileNotFoundError:
            return "No config yet."
        out = []
        for e in doc.get("strategies") or []:
            q = e.get("params") or {}
            out.append(f"{e.get('name')} {e.get('pair')}{'' if e.get('enabled', True) else ' (off)'}: "
                       f"{q.get('levels')} levels × {q.get('bid_size', q.get('level_size'))}/"
                       f"{q.get('ask_size', q.get('level_size'))}, spread {q.get('half_spread_pct')}% "
                       f"+{q.get('level_step_pct')}%/level, max pos {q.get('max_position')}")
        return "\n".join(out) or "No markets configured."
    if cmd == "/capacity":
        cap = E.capacity(node or E.connect())
        return "\n".join(f"{a}: send {v['send_free']:.6g} free of {v['send']:.6g}, "
                         f"receive {v['recv_free']:.6g} free of {v['recv']:.6g}" for a, v in cap.items()) or "no channels"
    if cmd == "/deposit":
        addrs = E.deposit_addresses(node or E.connect())
        return "Send funds to the node (pick the matching network!):\n" + \
            "\n".join(f"{ch}: {ad}" for ch, ad in addrs.items())
    return "Unknown command. /help"


def creds():
    """Token and chat from .env as it is NOW (pairing / `hydra-mm telegram` change them live)."""
    from dotenv import dotenv_values
    env = dotenv_values(".env") if os.path.exists(".env") else {}
    return (env.get("ALERTS_TELEGRAM_BOT_TOKEN") or os.getenv("ALERTS_TELEGRAM_BOT_TOKEN") or "",
            env.get("ALERTS_TELEGRAM_CHAT_ID") or os.getenv("ALERTS_TELEGRAM_CHAT_ID") or "")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - tg - %(levelname)s - %(message)s",
                        handlers=[logging.FileHandler("tg_control.log"), logging.StreamHandler()])
    log = logging.getLogger("tg")
    token, offset = None, None
    seen = []                      # (chat, message_id) already answered: Telegram can deliver twice
    while True:
        new_token, chat = creds()
        if not new_token:
            if token is not False:
                log.info("no Telegram bot token yet — waiting (hydra-mm telegram sets one)")
                token = False
            time.sleep(20)
            continue
        if new_token != token:
            token, offset, api = new_token, None, f"https://api.telegram.org/bot{new_token}"
            code = E.pairing_code()
            log.info("telegram control started" + ("" if chat else
                     (f" — waiting for /start <code> (hydra-mm telegram)" if code else " — not paired yet")))
        try:
            r = requests.get(f"{api}/getUpdates", params={"timeout": 50, "offset": offset}, timeout=60).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("channel_post") or {}
                text, cid = msg.get("text") or "", str((msg.get("chat") or {}).get("id", ""))
                key = (cid, msg.get("message_id"))
                if key in seen:
                    log.info(f"duplicate update {u['update_id']} for message {key[1]} — ignored")
                    continue
                seen = (seen + [key])[-200:]
                try:
                    reply = handle(text, cid, chat)
                except Exception as e:
                    reply = f"Error: {type(e).__name__}: {str(e)[:150]}"
                if reply:
                    if os.getenv("ALERTS_PREFIX"):
                        reply = f"{os.environ['ALERTS_PREFIX']}\n{reply}"
                    log.info(f"{text.split()[0] if text else '?'} from {cid} (update {u['update_id']}, "
                             f"message {msg.get('message_id')}, {(msg.get('chat') or {}).get('type', '?')})")
                    try:
                        sent = requests.post(f"{api}/sendMessage", data={"chat_id": cid, "text": reply[:4000]},
                                             timeout=15).json()
                        if not sent.get("ok"):
                            log.warning(f"reply to {text.split()[0]} not delivered: {sent.get('description')}")
                    except Exception as e:
                        log.warning(f"reply to {text.split()[0]} not delivered: {type(e).__name__}: {e}")
        except Exception as e:
            log.warning(f"poll failed: {type(e).__name__}: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()
