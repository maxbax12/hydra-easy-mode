#!/usr/bin/env python3
"""hydra-mm — the one command for easy mode.

  hydra-mm doctor             checks every step and prints the exact next command
  hydra-mm invite CODE        redeem a mainnet invite (mainnet is invite-gated)
  hydra-mm setup              budget, preset, markets, Telegram (asks, or all flags + --yes)
  hydra-mm plan [budget]      show what a budget would do (nothing changes)
  hydra-mm fund [--go]        what to send where, then open/fund the channels
  hydra-mm status             markets, positions, P&L, capacity
  hydra-mm pause [market]     pull all quotes (or one market's); `resume` undoes it
  hydra-mm resume [market]
  hydra-mm addresses          where to send funds (one address per chain)
  hydra-mm peers              connect to the Hydranet hub on every chain (fund does it too)
  hydra-mm telegram           connect a Telegram bot: token, then /start <code> in your chat
  hydra-mm identity           this node's identity key (for an invite / whitelisting)
  hydra-mm invite-create      mint an invite code for someone else (admitted nodes)
  hydra-mm new-seed --out F   write a fresh wallet seed to a node .env (install.sh uses it)
  hydra-mm volume …           pre-launch volume mode: the node trades with itself
                              run USD [--market M] [--size S] [--bg] · status · stop · target USD [--market M] · off
  hydra-mm gui                how to open the web GUI (SSH tunnel + link with the access token)

Everything runs against the local node (HYDRA_HOST/HYDRA_PORT).
Unattended (scripts, agents): every question has a flag; secrets come from the
environment (ALERTS_TELEGRAM_BOT_TOKEN, HYDRA_INVITE_CODE), never flags.
"""
import argparse
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv
load_dotenv(".env")

from lib import easy_ops as E
from lib.planner import MARKETS, PRESETS, CHAIN, plan, describe

BOT_CONFIG = "config/bot_config.yaml"
OPS_CONFIG = "config/ops.yaml"
PLAN_FILE = "state/easy_plan.json"


def ask(prompt: str, default: str = "", choices=None, secret=False) -> str:
    while True:
        shown = f" [{default}]" if default and not secret else ""
        raw = (getpass.getpass(f"{prompt}{shown}: ") if secret else input(f"{prompt}{shown}: ")).strip()
        val = raw or default
        if choices and val not in choices:
            print(f"  please answer one of: {', '.join(choices)}")
            continue
        return val


def yes(prompt: str, default: bool = False) -> bool:
    return ask(prompt + " (y/n)", "y" if default else "n", ["y", "n", "Y", "N"]).lower() == "y"


def node_or_exit(require_admitted: bool = False):
    ok, info = E.node_ready()
    if not ok:
        sys.exit("Can't reach the Hydra node. Is it running? (docker compose ps; it needs a minute to start)\n"
                 f"  details: {info}")
    return E.connect()


def save_plan(p, px):
    os.makedirs("state", exist_ok=True)
    json.dump({"budget": p.budget_usd, "preset": p.preset,
               "markets": [e["pair"] for e in p.entries], "prices": px}, open(PLAN_FILE, "w"))


def load_plan():
    try:
        d = json.load(open(PLAN_FILE))
    except FileNotFoundError:
        sys.exit("No plan yet — run `hydra-mm setup` first.")
    px = E.prices()
    return plan(d["budget"], d["preset"], d["markets"], px), px


# ------------------------------------------------------------------ commands
def cmd_setup(a):
    unattended = a.yes and a.budget is not None
    print("Hydra market maker — setup\n"
          "You provide liquidity on the Hydra DEX and earn the spread + maker rebates.\n"
          "It is real money: prices move, and a market maker can lose. Start small.\n")
    node_or_exit()
    px = E.prices()
    print(f"Prices now: BTC ${px['BTC']:,.0f}, ETH ${px['ETH']:,.0f}\n")
    if unattended:
        budget, preset = a.budget, a.preset or "balanced"
        markets = a.markets.split(",") if a.markets else list(MARKETS)
    else:
        budget = a.budget if a.budget is not None else float(ask("How much do you want to put in, in USD", "1000"))
        print("\nPresets:\n  conservative  wide spreads, fewer quotes, no arbitrage\n"
              "  balanced      the middle (recommended)\n  aggressive    tight spreads, more quotes, more fills and more risk")
        preset = a.preset or ask("Preset", "balanced", list(PRESETS))
        print("\nMarkets: " + ", ".join(MARKETS))
        markets = a.markets.split(",") if a.markets else (list(MARKETS) if yes("Use all markets?", True) else
                                                         [m for m in MARKETS if yes(f"  {m}?", True)])
    p = plan(budget, preset, markets, px)
    print("\n" + describe(p) + "\n")
    if not p.entries:
        sys.exit("Nothing to quote with this budget — raise it or pick fewer markets.")
    if not unattended and not yes("Save this setup?", True):
        sys.exit("Nothing saved.")

    env = {}
    tg_token = None
    if unattended:
        tg_token = os.getenv("ALERTS_TELEGRAM_BOT_TOKEN") or None
        if a.telegram_chat:
            env["ALERTS_TELEGRAM_CHAT_ID"] = a.telegram_chat
    elif yes("\nTelegram alerts and control from your phone? (recommended)", True):
        print("  In Telegram, open @BotFather, send /newbot, pick a name — it gives you a token.")
        tg_token = ask("Bot token (hidden)", secret=True) or None
    if env:
        E.set_env(".env", env)
        print("  saved to .env (readable only by you)")

    backup = E.write_bot_config(p, BOT_CONFIG)
    E.write_ops_config(p, OPS_CONFIG, lease_autorenew=True, max_fee_usd=max(10.0, p.lease_cost_week_usd * 2))
    save_plan(p, px)
    print(f"\nWrote {BOT_CONFIG}" + (f" (old one kept as {backup})" if backup else "") + f" and {OPS_CONFIG}.")
    if tg_token:
        # Re-pair only for a NEW bot, or when no chat is linked yet. (.env is loaded into the
        # environment at start, so an existing token looks "given" — re-pairing on it
        # unlinked the paired chat on every re-plan: easy wallet, 2026-09-30.)
        from dotenv import dotenv_values
        env = dotenv_values(".env") if os.path.exists(".env") else {}
        if tg_token != env.get("ALERTS_TELEGRAM_BOT_TOKEN") or not env.get("ALERTS_TELEGRAM_CHAT_ID"):
            print_pairing(E.set_telegram(tg_token))
        else:
            print("Telegram: already set up (bot and chat kept).")
    print("\nNext: `hydra-mm fund` shows where to send your funds and opens the channels.")


def cmd_plan(a):
    px = E.prices()
    p = plan(float(a.budget), a.preset, None, px)
    print(describe(p))


def cmd_addresses(a):
    c = node_or_exit()
    for chain, addr in E.deposit_addresses(c).items():
        what = ", ".join(x for x, ch in CHAIN.items() if ch == chain)
        print(f"  {chain:<9} ({what}): {addr}")


def cmd_fund(a):
    c = node_or_exit()
    p, px = load_plan()
    wallet, cap = E.wallet_onchain(c), E.capacity(c)
    gaps = E.funding_gaps(p, wallet, cap)
    if gaps:
        addrs = E.deposit_addresses(c)
        print("Send these to the node first (exact network matters):")
        for asset, amt in gaps.items():
            chain = CHAIN[asset]
            print(f"  {amt:.8g} {asset:<9} on {chain:<9} -> {addrs.get(chain)}")
        if "ETH" in p.assets:
            print(f"  + ~0.003 ETH on ethereum for gas (ETH deposits) -> {addrs.get('ethereum')}")
        if any(a.startswith("USDC") for a in p.assets):
            print("  (no ETH needed for USDC: without gas the hub deposits it with a permit;\n"
                  "   a little ETH on that chain makes it one fee instead of two)")
        print("\nRun `hydra-mm fund` again once they have arrived (a few confirmations).")
        if not a.anyway:
            return
    for chain, res in E.connect_peers(c).items():
        if res not in ("ok", "already"):
            print(f"  (hub on {chain} not reachable yet: {res})")
    reqs = E.channel_requests(p, px, cap, lease_hours=a.hours, gas=E.native_balances(c))
    if not reqs:
        print("Channels already hold what the plan needs. Nothing to do.")
        return
    total = 0.0
    for r in reqs:
        fee, err = E.estimate_channel(r)
        parts = ", ".join(f"deposit {v['own']:.6g} + lease {v['inbound']:.6g} {k}" for k, v in r["assets"].items())
        if err and not E.allowance_needed(err):
            print(f"  {r['chain']:<9} {parts}: can't do it yet — {err}")
            continue
        total += fee or 0.0
        mode, pay = E.fee_mode(r)
        how = {"dual": " (from the deposit)", "offchain": " (from the USDC.arb channel)",
               "sponsored": " for the deposit (no gas needed); the lease follows on the next run"}[mode]
        print(f"  {r['chain']:<9} {parts}, lease {r['hours']}h: fee "
              + (f"{fee:.6g} {pay}{how}" if fee is not None else "after the approval"))
    if not a.go:
        print("\nNothing done yet. Run `hydra-mm fund --go` to open/fund these channels.")
        return
    for r in reqs:
        print(f"\n{r['chain']}:")
        res = E.open_channel(c, r)
        if res["ok"]:
            print(f"  ✅ channel {res['channel'][:12]}…" + (f" (lease tx {res['txid']})" if res["txid"] else "")
                  + "".join(f" ({a} deposit tx {t})" for a, t in res.get("deposits", {}).items()))
            if res.get("lease_pending"):
                print("  deposit made without gas; the inbound lease is a second step —"
                      " run `hydra-mm fund --go` again once the channel is active (a few minutes)")
        else:
            print(f"  ❌ {res['why']}")
            if r["chain"] == "arbitrum":
                print("  Stopping: the other chains pay their fees from the Arbitrum channel.")
                break
    print("\nChannels activate after a few confirmations; the bot starts quoting on its own.")


def print_pairing(code: str):
    print("\nTelegram: add your bot to a group (or just open a chat with it) and send\n\n"
          f"      /start {code}\n\n"
          "That chat then gets the alerts and can control the bot (/help). The code works once, for an hour.")


def cmd_telegram(a):
    """Set (or replace) the Telegram bot and pair a chat with a one-time code."""
    token = os.getenv("ALERTS_TELEGRAM_BOT_TOKEN_NEW") or ask(
        "Bot token from @BotFather (hidden; Enter keeps the current bot)", secret=True)
    if token:
        code = E.set_telegram(token)
    else:
        from dotenv import dotenv_values
        if not dotenv_values(".env").get("ALERTS_TELEGRAM_BOT_TOKEN"):
            sys.exit("No bot yet — create one with @BotFather (/newbot) and run this again.")
        code = E.new_pairing_code()
    print_pairing(code)


def cmd_invite(a):
    import time as _t
    # `-` takes the code from HYDRA_INVITE_CODE: an invite is a bearer secret, so the web GUI
    # never puts it on a command line (visible in `ps`).
    code = (os.getenv("HYDRA_INVITE_CODE", "") if a.code == "-" else a.code).strip()
    if not code:
        sys.exit("No invite code: give it as the argument, or `-` with HYDRA_INVITE_CODE set.")
    node_or_exit(require_admitted=False)
    if E.booted():
        print("This node is already admitted — no invite needed.")
        return
    try:
        inviter = E.redeem_invite(code)
    except Exception as e:
        sys.exit(f"Invite not accepted: {E.explain_error(getattr(e, 'details', lambda: str(e))() or str(e))}")
    print(f"✅ Invite redeemed (invited by {inviter[:16]}…) — the node finishes starting now …")
    t0 = _t.time()
    while _t.time() - t0 < a.timeout:
        if E.booted():
            print("✅ Node admitted and running. Next: hydra-mm doctor")
            return
        _t.sleep(5)
    print("The node has not finished starting yet — check again in a few minutes: hydra-mm doctor")


def cmd_invite_create(a):
    node_or_exit()
    print(f"Invite code (a bearer secret — send it privately): {E.create_invite()}")


def cmd_identity(a):
    node_or_exit(require_admitted=False)
    print(E.identity_key())


def cmd_peers(a):
    c = node_or_exit()
    for chain, res in E.connect_peers(c).items():
        print(f"  {chain:<9} {'✅ ' + res if res in ('ok', 'already') else '❌ ' + res}")


def cmd_new_seed(a):
    seed = E.new_seed(24)
    E.write_node_env(a.out, seed)
    print(f"New wallet seed written to {a.out} (0600).")
    if a.show:
        print("\nRECOVERY PHRASE — write it down offline, it is the only way to recover the funds:\n\n  "
              + seed + "\n")
    else:
        print("Back it up: the MNEMONIC line in that file is the only way to recover the funds.")


def doctor_steps() -> list:
    """[(ok, what, next_command_or_hint)] — each step only if the previous one passed."""
    steps = []
    ok, info = E.node_ready()
    steps.append((ok, f"node answers ({info or 'starting'})" if ok else f"node not reachable ({info})",
                  "docker compose up -d   (then wait a minute: docker compose logs -f node)"))
    if not ok:
        return steps
    if not info:                                  # API up, no networks: waiting for an invite
        try:
            ident = E.identity_key()
        except Exception:
            ident = "(hydra-mm identity)"
        steps.append((False, "node is waiting for a mainnet invite (mainnet is invite-gated)",
                      f"hydra-mm invite <CODE>   (an existing user mints one; or ask the team to whitelist {ident})"))
        return steps
    c = E.connect()
    ok, info = E.admitted(c)
    ident = ""
    if not ok:
        try:
            ident = E.identity_key()
        except Exception:
            pass
    steps.append((ok, f"admitted to mainnet ({info})" if ok else f"not admitted to mainnet yet ({info})",
                  f"hydra-mm invite <CODE>   (ask an existing user for a code, or the team to whitelist "
                  f"identity {ident or '(hydra-mm identity)'})"))
    if not ok:
        return steps
    peers = E.connect_peers(c)
    bad = {k: v for k, v in peers.items() if v not in ("ok", "already")}
    steps.append((not bad, "connected to the Hydranet hub on every chain" if not bad else f"hub not reachable: {bad}",
                  "hydra-mm peers   (retry; the hub is always on port 443)"))
    configured = os.path.exists(BOT_CONFIG) and os.path.getsize(os.path.realpath(BOT_CONFIG)) > 0 \
        and os.path.exists(PLAN_FILE)
    steps.append((configured, "configured" if configured else "not configured yet",
                  "hydra-mm setup   (or unattended: hydra-mm setup --budget 500 --markets USDC.arb/USDC.eth --yes)"))
    if not configured:
        return steps
    p, px = load_plan()
    cap = E.capacity(c)
    # A market maker's value moves between its two assets as it trades: judge the funding by
    # the TOTAL, and only flag an asset as unfunded when (almost) nothing of it is there.
    funded, have_usd, plan_usd, empty = E.funding_state(p, cap, px)
    steps.append((funded, f"channels funded (${have_usd:,.0f} of ${plan_usd:,.0f} planned)" if funded else
                  (f"nothing in the channels yet for {', '.join(empty)}" if empty else
                   f"channels hold ${have_usd:,.0f} of ${plan_usd:,.0f} planned"),
                  "hydra-mm fund   (shows what to send where; then hydra-mm fund --go)"))
    if not funded:
        return steps
    # Count what is really on the book (the log's status line comes every 10 min only).
    try:
        live = len(c.get_all_own_orders() or {})
    except Exception:
        live = sum(m["bids"] + m["asks"] for m in E.market_status())
    paused = os.path.exists(os.path.join("state", "pause"))
    steps.append((live > 0, f"{live} own quotes on the book" if live else
                  ("paused (state/pause) — hydra-mm resume" if paused else "no quotes on the book yet"),
                  "hydra-mm resume" if paused else "wait a minute after a (re)start; else docker compose logs --tail 50 bot"))
    from dotenv import dotenv_values
    env = dotenv_values(".env") if os.path.exists(".env") else {}
    tok, chat = env.get("ALERTS_TELEGRAM_BOT_TOKEN"), env.get("ALERTS_TELEGRAM_CHAT_ID")
    code = E.pairing_code()
    steps.append((True, "Telegram alerts on" if tok and chat else
                  (f"Telegram: waiting for /start {code} in your chat" if tok and code else "Telegram off (optional)"),
                  ""))
    return steps


def cmd_wait_node(a):
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < a.timeout:
        ok, info = E.node_ready(timeout=5)
        if ok:
            print(f"node is up ({info})")
            return
        _t.sleep(5)
    sys.exit(f"node did not come up within {a.timeout}s — docker compose logs --tail 80 node")


def cmd_doctor(a):
    steps = doctor_steps()
    nxt = None
    for ok, what, hint in steps:
        print(f"  {'✅' if ok else '❌'} {what}")
        if not ok and nxt is None:
            nxt = hint
    print("\n" + (f"Next: {nxt}" if nxt else "All good — the market maker is running."))
    sys.exit(0 if nxt is None else 1)


def cmd_volume(a):
    """Pre-launch volume mode (lib/volume.py): self-matched rounds, fees only."""
    import asyncio
    import time as _t
    from lib import volume as V
    if a.action == "status":
        st = V.status()
        r, last, cfg = st["running"], st["last_run"], st["config"]
        if r:
            print(f"RUNNING on {r['market']} ({r['source']}): ${r['volume_usd']:,.2f} of ${r['target_usd']:,.2f}, "
                  f"{r['rounds']} rounds, cost ~${r['cost_usd']:.2f} — {r.get('message', '')}")
        else:
            print("No volume run active.")
        if last:
            print(f"Last run: {last['market']} ${last['volume_usd']:,.2f} in {last['rounds']} rounds, cost ${last['cost_usd']:.4f} "
                  f"— {last['result']} ({_t.strftime('%Y-%m-%d %H:%M', _t.localtime(last.get('ended_at') or 0))})")
        print("\nToday (UTC):" if st["today"] else "\nToday (UTC): nothing yet")
        for m, v in st["today"].items():
            print(f"  {m:<18} ${v['volume_usd']:>10,.2f}  cost ${v['cost_usd']:.2f}  {v['rounds']} rounds")
        print(f"\nDaily targets ({'ON' if cfg['enabled'] else 'off'}, bursts every {cfg['burst_every_min']} min, "
              f"{cfg['active_hours'][0]}-{cfg['active_hours'][1]} h UTC):")
        for m, v in cfg["markets"].items():
            print(f"  {m:<18} ${v['daily_usd']:>10,.2f}/day  (est. ${st['cost_per_1000'].get(m, 0):.2f} per $1,000)")
        return
    if a.action == "stop":
        print("Stopping after the current round." if V.stop_run() else "No volume run active.")
        return
    if a.action in ("target", "off"):
        cfg = V.load_config()
        if a.action == "off":
            cfg["enabled"] = False
        else:
            cfg["markets"][a.market]["daily_usd"] = float(a.usd)
            cfg["enabled"] = any(v["daily_usd"] > 0 for v in cfg["markets"].values())
        try:
            V.save_config(cfg)
        except ValueError as e:
            sys.exit(f"Not saved: {e}")
        print("Daily volume targets " + ("ON: " + ", ".join(f"{m} ${v['daily_usd']:,.0f}/day" for m, v in cfg["markets"].items()
                                                         if v["daily_usd"] > 0) if cfg["enabled"] else "off") + ".")
        return
    # run
    if a.bg:
        try:
            pid = V.start_run(a.market, a.usd, a.size)
        except (RuntimeError, ValueError) as e:
            sys.exit(str(e))
        print(f"Started in the background (pid {pid}). Progress: hydra-mm volume status · stop: hydra-mm volume stop")
        return
    rec = asyncio.run(V.run(a.market, a.usd, a.size, a.source, log=lambda m: print(m, flush=True)))
    sys.exit(0 if rec["result"] in ("done", "stopped") else 1)


def cmd_gui(a):
    try:
        from tools.gui import gui_token
    except Exception:                        # the GUI module is missing: same token file, same format
        import secrets
        def gui_token(path="state/gui_token"):
            if not os.path.exists(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
                os.write(fd, secrets.token_urlsafe(24).encode()); os.close(fd)
            return open(path).read().strip()
    port = os.getenv("HYDRA_GUI_PORT", "8080")
    print("The GUI only listens on this server (127.0.0.1). On your own computer run:\n\n"
          f"  ssh -L {port}:127.0.0.1:{port} <user>@<this server's address>\n\n"
          f"keep that open, then open this link in your browser (it carries the access token — don't share it):\n\n"
          f"  http://localhost:{port}/#t={gui_token()}\n")


def cmd_status(a):
    ms = E.market_status()
    if not ms:
        print("No market status in trading_bot.log yet — is the bot running?")
    for s in ms:
        flag = "⏸ paused" if s["paused"] else "▶ quoting"
        print(f"  {s['pair']:<18} {flag:<10} {s['bids']} bids / {s['asks']} asks   position {s['position']:+.6g} "
              f"({s['pct']})   {s['fills']} fills   P&L {s['pnl']:+.4g}   ({s['at']})")
    try:
        c = E.connect()
        cap = E.capacity(c)
        print("\nCapacity (free to send / to receive):")
        for asset, v in cap.items():
            print(f"  {asset:<9} send {v['send_free']:.6g} of {v['send']:.6g}   receive {v['recv_free']:.6g} of {v['recv']:.6g}")
    except Exception as e:
        print(f"\n(node not reachable: {type(e).__name__})")


def cmd_pause(a, on=True):
    path = E.set_pause(on, a.market)
    what = a.market or "all markets"
    print(f"{'Paused' if on else 'Resumed'} {what} — takes effect within seconds ({path}).")


def main():
    ap = argparse.ArgumentParser(prog="hydra-mm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("setup")
    sp.add_argument("--budget", type=float); sp.add_argument("--preset", choices=list(PRESETS))
    sp.add_argument("--markets", help="comma-separated, e.g. USDC.arb/USDC.eth,BTC/USDC.arb (default: all)")
    sp.add_argument("--telegram-chat", help="Telegram group chat id (token from ALERTS_TELEGRAM_BOT_TOKEN)")
    sp.add_argument("--yes", action="store_true", help="no questions (needs --budget)")
    sub.add_parser("telegram"); sub.add_parser("doctor"); sub.add_parser("identity"); sub.add_parser("peers"); sub.add_parser("invite-create")
    ip = sub.add_parser("invite"); ip.add_argument("code", help="the invite code, or - to read it from HYDRA_INVITE_CODE")
    ip.add_argument("--timeout", type=int, default=600)
    wn = sub.add_parser("wait-node"); wn.add_argument("--timeout", type=int, default=900)
    ns = sub.add_parser("new-seed"); ns.add_argument("--out", required=True); ns.add_argument("--show", action="store_true")
    pp = sub.add_parser("plan"); pp.add_argument("budget", type=float, nargs="?", default=1000)
    pp.add_argument("--preset", default="balanced", choices=list(PRESETS))
    fp = sub.add_parser("fund"); fp.add_argument("--go", action="store_true"); fp.add_argument("--hours", type=int, default=168)
    fp.add_argument("--anyway", action="store_true", help="try the channels even if the wallet looks short")
    sub.add_parser("status"); sub.add_parser("addresses")
    for n in ("pause", "resume"):
        x = sub.add_parser(n); x.add_argument("market", nargs="?", help="strategy name, e.g. mm_btc_usdc (default: all)")
    from lib.volume import MARKETS as VOL_MARKETS
    vp = sub.add_parser("volume", help="pre-launch volume mode (self-matched rounds)")
    vs = vp.add_subparsers(dest="action", required=True)
    vr = vs.add_parser("run", help="generate USD of volume now")
    vr.add_argument("usd", type=float); vr.add_argument("--market", default="USDC.arb/USDC.eth", choices=list(VOL_MARKETS))
    vr.add_argument("--size", type=float, help="max round size in base units (default: config)")
    vr.add_argument("--source", default="manual", choices=["manual", "daily"], help=argparse.SUPPRESS)
    vr.add_argument("--bg", action="store_true", help="run in the background")
    vs.add_parser("status"); vs.add_parser("stop"); vs.add_parser("off")
    vt = vs.add_parser("target", help="daily volume target (0 = none) — the volume daemon spreads it over the day")
    vt.add_argument("usd", type=float); vt.add_argument("--market", default="USDC.arb/USDC.eth", choices=list(VOL_MARKETS))
    sub.add_parser("gui")
    a = ap.parse_args()
    {"setup": cmd_setup, "plan": cmd_plan, "fund": cmd_fund, "status": cmd_status, "addresses": cmd_addresses,
     "doctor": cmd_doctor, "telegram": cmd_telegram, "invite": cmd_invite, "invite-create": cmd_invite_create, "identity": cmd_identity,
     "peers": cmd_peers, "new-seed": cmd_new_seed, "wait-node": cmd_wait_node, "volume": cmd_volume, "gui": cmd_gui,
     "pause": lambda a: cmd_pause(a, True), "resume": lambda a: cmd_pause(a, False)}[a.cmd](a)


if __name__ == "__main__":
    main()
