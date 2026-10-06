# Hydra easy mode — a market maker for the Hydra DEX, set up in one command

Easy mode runs a **market maker on the [Hydra DEX](https://hydranet.ai)** for you: a Hydra
node plus a bot that keeps buy and sell quotes on the order book and earns the spread and
maker rebates whenever someone trades against them. You answer a few questions (budget,
risk preset, markets), send funds to the addresses it gives you, and it does the rest:
channels, liquidity leases, quoting, alerts, lease renewals.

> **This is real money.** Prices move, and a market maker can lose — most of all on
> inventory it holds while the price runs. The DEX is still small. Start with an amount
> you are fine with, and read [Risks](#risks).

**Contents:** [What it does](#what-it-does) · [Is it really easy?](#is-it-really-easy) ·
[Requirements](#requirements) · [Install](#1--install) · [Setup](#2--setup) ·
[Funding](#3--funding) · [Daily use](#daily-use) · [Web GUI](#web-gui) ·
[Volume mode (pre-launch)](#volume-mode-pre-launch) · [Settings](#changing-settings) ·
[Updating](#updating) · [Backups & recovery](#backups-and-recovery) · [Security](#security) ·
[Troubleshooting](#troubleshooting) · [How it works](#how-it-works) ·
[For AI agents](#for-ai-agents) · [Development](#development) · [Risks](#risks)

---

## What it does

| | |
|---|---|
| **Markets** | `BTC/USDC.arb`, `ETH/USDC.arb`, `ETH/BTC`, `USDC.arb/USDC.eth` — all four or any subset |
| **Quoting** | a ladder of 4–6 bids and asks per market around a fair price (Pyth oracle with CoinGecko as fallback), post-only so a quote never takes by accident |
| **Inventory control** | quotes skew and shrink as a position builds up; hard limits per market |
| **Arbitrage takes** | on the stablecoin market, takes mispriced orders when the edge beats the taker fee (balanced/aggressive presets) |
| **Funding** | one command opens/funds the channels and leases "room to receive" (inbound liquidity) from the Hydra liquidity service |
| **Lease autopilot** | leases are renewed automatically before they end, within a fee cap |
| **Telegram** | alerts (fills, low capacity, leases, bot down, daily P&L) and control (`/status`, `/pause`, `/resume`, …) from your phone |
| **Live settings** | edit the config, the bot picks it up without a restart; a broken edit is rejected |
| **Web GUI** | dashboard, controls, setup & funding wizard and volume panel in your browser — private to your server, opened through an SSH tunnel |
| **Volume mode (pre-launch)** | the node trades with itself to generate volume for testing before launch — on demand or as a daily target; costs only the fees |
| **`hydra-mm doctor`** | checks every step and prints the exact next command |
| **Agent-friendly** | every question has a flag, so an AI agent (Claude Code etc.) can do the whole setup — see [easy/AGENTS.md](easy/AGENTS.md) |

## Is it really easy?

**The technical part is.** You never touch peers, channel ids, token allowances, lease
slots, config files or private keys. Installing is one command, the setup asks five
questions, funding is "send X to address Y, then run one command". After the install
everything can be done from the [web GUI](#web-gui) or Telegram instead of the terminal.

**What you still do yourself** (and roughly how long it takes):

| Step | Time | Why it can't be automated |
|---|---|---|
| Get an x86-64 Linux server (any small VPS) | 5 min | the node needs to run 24/7 |
| Get a **mainnet invite code** from an existing Hydra user | — | mainnet is invite-gated |
| Back up the wallet seed (`easy/node/.env`) offline | 2 min | only you should hold it |
| Buy/send the funds to the addresses `fund` prints (exact networks!) | 10–60 min | your money, your exchange |
| Optional: create a Telegram bot with @BotFather | 2 min | Telegram requires it |

Total: about **15 minutes of your time** plus waiting for deposits to confirm
(minutes on Arbitrum/Ethereum, 10–60 min on Bitcoin).

**Honest limitations**

- **x86-64 only.** No Apple Silicon Macs, no Raspberry Pi: the node image is x86-only, and
  under emulation its channel messages fail to decrypt (tested). A $5–10/month VPS works.
- **Invite-gated.** Without an invite code the node waits ("not admitted yet").
- **Leases cost money and expire.** A lease gives you room to *receive* on a chain for at
  most 7 days at a time; the autopilot renews it (fee cap), and the hub often keeps
  liquidity longer for active channels. Expect roughly $3 per $1,000 of budget per week.
- **Competition.** Every copy of this bot on the same market splits the same order flow.
- **One node, one wallet** per server.

## Requirements

- **x86-64 Linux** (Debian or Ubuntu tested), 2 CPU, 4 GB RAM, 40 GB disk, always on
- **Docker** with Compose v2 (the installer sets it up on Debian/Ubuntu)
- a **mainnet invite code**
- **funds** on the chains of the markets you pick: BTC (Bitcoin), ETH and USDC (Ethereum),
  USDC (Arbitrum One). Minimum budget **$300**.
- optional: a **Telegram** account (alerts and control from your phone)

---

## 1 — Install

**On a fresh x86-64 Debian/Ubuntu server, one line does everything** — installs Docker and
git if missing, fetches the bot into `~/hydra-mm` and runs the installer:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/maxbax12/hydra-easy-mode/main/easy/get.sh) --invite YOUR-INVITE-CODE
```

(`bash <(curl …)`, not `curl … | bash`: the setup asks questions on the terminal.)

**Or by hand** (Docker with Compose v2 already installed — on Debian/Ubuntu:
`curl -fsSL https://get.docker.com | sudo sh`):

```bash
git clone https://github.com/maxbax12/hydra-easy-mode.git ~/hydra-mm
cd ~/hydra-mm/easy
./install.sh --invite YOUR-INVITE-CODE
```

What `install.sh` does, step by step (it prints each one):

1. creates `easy/node/config.yaml` from the official mainnet template
2. pulls the Hydra node image and builds the bot image
3. **creates a new wallet** and saves its 24-word recovery phrase in `easy/node/.env`
   (readable only by you) — **back this file up offline now**. It is the only way to
   recover your funds. (`--restore` uses your existing seed instead.)
4. starts the node (first start takes a few minutes) and **redeems your invite on the node**
   — no web app needed, nobody else sees the seed
5. runs the setup (next section) and `hydra-mm doctor`

`install.sh` options:

| Option | Meaning |
|---|---|
| `--invite CODE` | redeem this mainnet invite code (without it the node waits; later: `./hydra-mm invite CODE`) |
| `--restore` | use your existing wallet: type its 12/24-word seed (hidden) instead of creating a new one |
| `--seed-file FILE` | the same, with the seed words read from a file (scripts, agents) |
| `--env-file FILE` | take over an existing node `.env` as it is (seed **and** password — same identity), e.g. when moving to a new server |
| `--show-seed` | print the new recovery phrase once on screen (default: only saved to `node/.env`) |
| `--budget N --preset P --markets A,B --telegram-chat ID --yes` | answers for the setup — with `--yes` nothing is asked (scripts, agents) |

Secrets are never flags: for an unattended setup put the Telegram bot token in the
environment (`ALERTS_TELEGRAM_BOT_TOKEN`).

Example, no questions at all:

```bash
./install.sh --invite CODE --budget 500 --markets USDC.arb/USDC.eth --yes
```

Re-running `install.sh` is safe: it keeps an existing wallet and config.

### Already have an admitted wallet?

Mainnet admits a **wallet identity**, which comes from the seed (and the node password). If
your wallet is already admitted — you redeemed an invite in the Hydra web app, or the team
whitelisted it — install with that wallet and **no invite is needed**:

```bash
./install.sh --restore                    # type the seed words (hidden)
./install.sh --seed-file ~/seed.txt       # or read them from a file
./install.sh --env-file ~/old/node/.env   # or take over another node's .env (seed + password)
```

The installer checks the words (a typo is caught by the checksum), starts the node and prints
**"This wallet is admitted to mainnet — no invite needed."** An invite code you passed anyway
is not used up.

- A public key alone is not enough: it only *names* the identity; the node needs the seed to
  act as it.
- **Run one wallet on one node only.** Stop the Hydra web app (or any other node with the same
  seed) first — two nodes with one wallet can lose funds when their channel states clash.
- Easy mode leaves the node password empty, like the web app, so a web-app seed keeps its
  identity. `--env-file` keeps whatever password the old node used.
- Already installed with a new wallet and want to switch? Only **before** funding it:
  `docker compose down`, move `node/.env` and `node/data/` away, then `./install.sh --restore`.

## 2 — Setup

`./hydra-mm setup` (the installer runs it for you) asks:

| You choose | The bot works out |
|---|---|
| **budget** in USD (min. $300) | quote sizes per market, how much goes on each chain, how much inbound to lease |
| **preset**: conservative / **balanced** / aggressive | spreads, number of quotes, arbitrage on/off |
| **markets** (default: all four) | inventory limits, skew, fees per market |
| **Telegram** (optional) | pairs your chat with a one-time code |

**Presets** (distance of the first quote from the fair price, extra distance per level):

| Preset | Quotes per side | First quote | Per level | Arbitrage | Budget used |
|---|---|---|---|---|---|
| conservative | 4 | 0.45% | +0.20% | off | 60% |
| **balanced** | 5 | 0.35% | +0.15% | edge > 0.10% | 80% |
| aggressive | 6 | 0.25% | +0.15% | edge > 0.05% | 90% |

The stablecoin market (`USDC.arb/USDC.eth`) uses much tighter values (0.08–0.15%), and it
is the only market where the bot takes mispriced orders (arbitrage). The bot carries the
price risk of what it holds, so on the other markets positions are capped at about a third
of one side's ladder.

**Markets** and how the budget is split:

| Market | Share of the budget |
|---|---|
| BTC/USDC.arb | 35% |
| ETH/USDC.arb | 25% |
| ETH/BTC | 20% |
| USDC.arb/USDC.eth | 20% (both sides are dollars: the calmest market) |

**See a plan without changing anything:** `./hydra-mm plan 1000` (add `--preset`):

```
Plan: $1,000, balanced

Markets:
  BTC/USDC.arb       5 quotes per side of 0.00033 BTC, first quote 0.35% from the price
  ETH/USDC.arb       5 quotes per side of 0.0074 ETH, first quote 0.35% from the price
  ETH/BTC            5 quotes per side of 0.0059 ETH, first quote 0.35% from the price
  USDC.arb/USDC.eth  4 quotes per side of 20 USDC.arb, first quote 0.1% from the price

What you deposit (your money) and what is leased (room to receive):
  BTC       deposit 0.00271 (~$231)   lease inbound 0.0031 (~$264)   on bitcoin
  ETH       deposit 0.0698 (~$188)    lease inbound 0.0798 (~$215)   on ethereum
  USDC.eth  deposit 84 (~$84)         lease inbound 96 (~$96)        on ethereum
  USDC.arb  deposit 336 (~$336)       lease inbound 384 (~$384)      on arbitrum
  lease cost ~$3.22 per week
```

Running setup again re-plans (the old config is kept as a backup); Telegram stays paired.

## 3 — Funding

```bash
./hydra-mm fund          # what to send where (nothing moves)
```

```
Send these to the node first (exact network matters):
  0.0055  BTC       on bitcoin  -> bc1p…
  0.1407  ETH       on ethereum -> 0x…
  168     USDC.eth  on ethereum -> 0x…
  672     USDC.arb  on arbitrum -> 0x…
```

**The network matters**: USDC on Arbitrum One is not USDC on Ethereum. Once the funds
have arrived (`./hydra-mm fund` shows nothing missing):

```bash
./hydra-mm fund --go     # opens / funds the channels and leases inbound; shows the fees
./hydra-mm doctor        # repeat until "All good — the market maker is running."
```

What `fund --go` does per chain, so you don't have to:

- **USDC (Arbitrum, Ethereum):** one request to the Hydra liquidity service that deposits
  your USDC *and* leases matching inbound in the same channel; the fee comes out of the deposit.
  **No ETH needed:** without gas on a chain it uses a permit-signed ("sponsored") deposit
  instead, and the lease follows on the next `fund --go`.
- **ETH:** the node deposits it with its own transaction (keep ~0.003 ETH extra for gas).
- **BTC:** Bitcoin has no dual funding — it leases inbound first, then the node deposits your BTC.
- Leases run `--hours` (default 168 = 7 days, the maximum). Existing channels are reused;
  it never opens a channel it doesn't need.

The first quotes appear within a minute after the channels are ready.

---

## Daily use

### From Telegram

Pairing: `setup` (or later `./hydra-mm telegram`) asks for a bot token from
[@BotFather](https://t.me/BotFather) (`/newbot`) and prints a code. Send `/start <code>` to
your bot in the chat you want (a private chat or a group). Only that chat is answered.
Switch bot or chat any time with `./hydra-mm telegram`.

| Command | What it does |
|---|---|
| `/status` | markets, quotes, positions, P&L |
| `/pnl` | P&L per market |
| `/book [market]` | the order book, our quotes marked ★ |
| `/markets` | every DEX market: best bid/ask, our share, 24 h volume |
| `/orders` · `/fills [n]` | our open quotes · the latest fills |
| `/capacity` | free capacity to send / receive per asset |
| `/leases` | when leases end, how long the hub keeps the liquidity |
| `/wallet` · `/deposit` | on-chain balances · where to send funds |
| `/config` | current settings |
| `/pause [market]` · `/resume [market]` | pull all quotes (or one market's, e.g. `/pause mm_btc_usdc`) within seconds · quote again |
| `/id` · `/help` | this chat's id · the list |

**Alerts** come on their own, bundled into at most one message per 5 minutes:

- 💱 fills (small fills under $5 summed per market) · 🎯 arbitrage takes
- 📉 a market running out of room to send or receive (after two checks in a row)
- ⏳ a lease ending within 24 h / 3 h · 🔄 renewed by the autopilot · ✅ kept by the hub · ⌛ ended
- 🛑 a swap failure (the market pauses itself if it was our fault) · 🚨 bot not running
- 📅 a **daily report** (20:00) with P&L per market, fee tier and DEX volume of the last 24 h

### From the server

All commands run from `~/hydra-mm/easy`:

| Command | What it does |
|---|---|
| `./hydra-mm doctor` | checks node, admission, hub connection, config, funding, quotes, Telegram — and prints the next step |
| `./hydra-mm status` | markets, positions, P&L, capacity |
| `./hydra-mm pause [market]` / `resume [market]` | pull / restore quotes |
| `./hydra-mm addresses` | the deposit address per chain |
| `./hydra-mm plan [budget]` | what a budget would do |
| `./hydra-mm fund [--go] [--hours H] [--anyway]` | funding plan / execute it |
| `./hydra-mm setup [flags]` | re-plan |
| `./hydra-mm telegram` | connect or switch the Telegram bot |
| `./hydra-mm invite CODE` | redeem a mainnet invite |
| `./hydra-mm invite-create` | mint an invite code for someone else |
| `./hydra-mm identity` | this node's identity key (for whitelisting) |
| `./hydra-mm peers` | connect to the Hydranet hub on every chain (`fund` does it too) |
| `./hydra-mm gui` | how to open the web GUI (SSH tunnel + link with the access token) |
| `./hydra-mm volume …` | pre-launch volume mode: `run USD`, `status`, `stop`, `target USD`, `off` (see below) |
| `docker compose logs --tail 100 bot` / `node` | logs |

## Web GUI

Everything above also works in the browser. Until your market maker is fully running, the GUI
opens on a guided **Get started** page; after that the dashboard is home. The GUI runs inside the
bot container and is reachable **only from the server itself** — you open it from your computer
through an SSH tunnel:

```bash
./hydra-mm gui          # on the server: prints the two steps below with your link
```

1. On your computer: `ssh -L 8080:127.0.0.1:8080 you@your-server` (keep it open)
2. Open the printed link, `http://localhost:8080/#t=…` — the part after `#t=` is your access
   token; the page keeps it for the browser session and removes it from the address bar.

**Get started** — six steps, each one checks itself (from the same checks as `hydra-mm doctor`),
so the page always shows what comes next:

1. **Node and access** — node status; paste your invite code here if the node is still waiting
   for one; your identity key with a copy button (for whitelisting).
2. **Back up your wallet** — where the recovery phrase is and how to back it up (the GUI never
   shows it); tick it off once done.
3. **Your plan** — budget (slider), style (conservative / balanced / aggressive) and markets,
   with a live preview of what you deposit on each network, what gets leased and the
   weekly lease cost. Nothing moves until you save.
4. **Telegram alerts** (optional) — paste a bot token, send the shown `/start` code to your bot.
5. **Fund your node** — what to send where, with copy buttons; each network ticks to "arrived"
   on its own (checked every 15 s). Then **Open channels**: shows the fees, you type `OPEN` to
   confirm, and you watch it progress per network.
6. **Start trading** — done once the first quotes are on the book.

After setup:

| Tab | What you can do |
|---|---|
| **Dashboard** | tiles for P&L, markets quoting, capacity and leases; markets (quotes, position, P&L), capacity per network, leases, wallet, recent trades, health checks — refreshes every 15 s. A banner at the top names the next thing to do whenever something needs attention |
| **Controls** | pause / resume everything or one market; change a market's sizes, levels, spreads and limits (checked before saving, applied live, old config kept as a backup); alert settings |
| **Setup** | the Get started page again — re-plan, add funds, reconnect Telegram |
| **Volume** | cost per $1,000 per market, today's volume and cost, daily targets, **Run now** (shows the estimated fees first), live progress with a Stop button, last result and history |

It works on a phone too (with an SSH app that does port forwarding).

**Security:** the port is published on the server's `127.0.0.1` only, never on the internet.
Every request needs the access token (`data/state/gui_token`, readable only by you);
requests coming from other websites are refused, and the page loads no outside code. Secrets
are never displayed — only "set / not set". Another port: `HYDRA_GUI_PORT=8090 docker compose up -d`.
New token (e.g. if a link leaked): delete `data/state/gui_token`, then `docker compose restart bot`.

## Volume mode (pre-launch)

Before launch the DEX needs trades to test with. Volume mode lets your node **trade with
itself**: each round places a maker order between the other participants' quotes and takes
it with an opposite order at the same price and size. Both sides are yours, so your
balances stay the same — the only cost is the fee (taker fee minus maker rebate):

| Market | Approx. cost per $1,000 of volume |
|---|---|
| USDC.arb/USDC.eth | $0.70 |
| ETH/USDC.arb | $1.25 |
| BTC/USDC.arb | $2.50 |
| ETH/BTC | $3.50 |

(List fee rates; after the first run the bot uses your actual fee tier. The exact cost of
every run is read from the node's payment records afterwards.)

```bash
./hydra-mm volume run 1000                                  # $1,000 on USDC/USDC now, progress on screen
./hydra-mm volume run 500 --market BTC/USDC.arb --bg        # in the background
./hydra-mm volume status                                    # running run, last result, today's volume and cost
./hydra-mm volume stop                                      # stop after the current round
./hydra-mm volume target 5000                               # $5,000 per day on USDC/USDC, spread over the day
./hydra-mm volume target 2000 --market ETH/USDC.arb         # per market; 0 removes a target
./hydra-mm volume off                                       # all daily targets off
```

The same from the GUI's **Volume** tab.

How it behaves:

- **The market maker of that market pauses during a run** and resumes afterwards, so its quotes
  free their capacity and it can't trade against the test orders. A pause you set yourself is
  never lifted by volume mode. Other markets keep running.
- **Round size** follows the free capacity on both sides (at most `max_size` per market), and
  the last round is sized to what is left of the target.
- **Safety first:** it only trades into a quiet book, at a price strictly between other people's
  quotes. If someone else requotes while our maker rests, it pulls the maker and tries again
  (at most 10 times in a row). Anything unexpected — our maker filled by someone else, the
  taker not matching — stops the run and cancels our test orders.
- **Daily targets** live in `data/volume.yaml`: `enabled`, `burst_every_min` (default 20),
  `active_hours` (UTC, default `[0, 24]`), and per market `daily_usd` and `max_size`. The volume
  daemon checks every minute and starts a short run ("burst") whenever the market is behind
  schedule; after a failed run it waits before trying again.
- Every test order's client id starts with **`vol-`**, so this volume can be told apart from
  real trading later.

> **Pre-launch only.** Self-matched trades aren't real trading. Switch daily targets off at
> launch (`./hydra-mm volume off`) so the volume people see is volume they can trade against.

## Changing settings

The configs live in `easy/data/`:

| File | What |
|---|---|
| `bot_config.yaml` | one `market_maker` entry per market: sizes, levels, spreads, limits |
| `ops.yaml` | alerts and the lease autopilot |
| `volume.yaml` | pre-launch volume mode: daily targets per market |
| `.env` | secrets: Telegram token and chat (readable only by you) |

Edit `bot_config.yaml`, then:

```bash
docker compose exec bot touch state/reload
```

Only the markets you changed move; nothing restarts. A broken edit is rejected, the bot
keeps the old settings and says so in Telegram. The most useful keys per market:
`bid_size` / `ask_size`, `levels`, `half_spread_pct` (first quote's distance from fair),
`level_step_pct`, `max_position`.

`ops.yaml` highlights:

| Key | Default | Meaning |
|---|---|---|
| `lease_autorenew.enabled` | `true` | renew leases automatically |
| `lease_autorenew.renew_below_hours` | `24` | renew when a lease has less than this left |
| `lease_autorenew.extend_hours` | `168` | extend by up to this (a lease may end at most 7 days out) |
| `lease_autorenew.max_fee_usd` | ≥ `10` | never pay more than this per renewal (alerts instead) |
| `lease_warn_hours` | `[24, 3]` | when to warn about a lease ending |
| `digest_every` | `300` | seconds between Telegram messages (0 = immediately) |
| `daily_report_hour` | `20` | hour of the daily report (server time) |
| `fill_alert_min_usd` | `5` | fills below this are summed in the digest |

`ops.yaml` changes apply when the container restarts (`docker compose restart bot`).

## Updating

```bash
cd ~/hydra-mm && git pull
cd easy && docker compose build bot && docker compose up -d bot      # the bot cancels its quotes, then restarts
docker compose pull node && docker compose up -d node                 # new node image (when Hydranet releases one)
```

The node gets 120 s to shut down cleanly (it must flush channel state); never kill it hard.

## Stopping

```bash
docker compose down        # the bot cancels all its quotes first; funds stay in your channels
docker compose up -d       # start again
```

To take the money out, pause, then close channels or withdraw from them with the Hydra
web app or the node API using the same seed.

## Backups and recovery

| What | Where | Why |
|---|---|---|
| **Wallet seed** | `easy/node/.env` (`MNEMONIC=`) | recovers the on-chain wallet — **back it up offline** |
| Channel state | `easy/node/data/` | the node's live state; also backed up server-side by Hydra (`backup_config` in the node template), so a lost data folder can be restored |
| Secrets | `easy/data/.env` | the Telegram bot — re-connect it if lost |
| Configs & stats | `easy/data/` | `bot_config.yaml`, `ops.yaml`, `state/` (positions, P&L) |

**Restore on a new server:** clone the repo, `./install.sh --restore` and type in the 24 words.

## Security

- **The node API has no authentication**, so it is never published: in the compose file only
  the bot container (same Docker network) can reach it. Don't add `ports:` to the node.
- The seed and secrets are files readable only by their owner (`0600`) and never printed
  (except `--show-seed`, once, on purpose).
- Telegram answers only the paired chat.
- Invite codes are bearer secrets: use once, don't share in public.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `This needs an x86-64 (amd64) machine` | use an x86-64 server; ARM is not supported |
| node "not admitted" / waiting for an invite | `./hydra-mm invite CODE` (or have the team whitelist `./hydra-mm identity`) |
| `node did not come up within 15 min` | `docker compose logs --tail 80 node`; the first start syncs for a few minutes |
| `fund` still shows missing funds | check the **network** you sent on; Bitcoin needs confirmations |
| no quotes on one side | `/capacity`: that side ran out of room — lease more inbound (`fund --go`) or rebalance |
| ⏳ lease alerts | the autopilot renews within its fee cap; if it can't, raise `max_fee_usd` or run `fund --go` |
| Telegram silent | `./hydra-mm telegram` and send `/start <code>` again |
| anything else | `./hydra-mm doctor`, then `docker compose logs --tail 200 bot` |

---

## How it works

```
 your server
 ┌───────────────────────── docker compose (project "easy") ─────────────────────────┐
 │  node  (ghcr.io/offchain-dex/hydra-app-public)        bot  (this repo)            │
 │  wallet, channels, orderbook client  <── gRPC :5003 ── market maker               │
 │  keys in node/.env, state in node/data                 ops daemon (alerts, leases)│
 │                                                        Telegram control           │
 └────────────┬──────────────────────────────────────────────────────────────────────┘
              │ channels (BTC / Ethereum / Arbitrum)
        Hydranet hub + orderbook + liquidity service
```

- **The node** is Hydranet's official mainnet image with the official config template
  (`easy/node/config.template.yaml`): daemon mode from `node/.env`, hub peers on port 443,
  server-side channel backups, watchtowers, invite-gated admission.
- **The bot container** runs five processes (`easy/run_all.sh`): the web GUI (`tools/gui.py`),
  the market maker (`trading_bot_cli.py` + `strategies/market_maker.py`), the ops daemon
  (`tools/ops_daemon.py`: alerts, digests, daily report, lease autopilot), the Telegram
  control (`tools/tg_control.py`) and the volume daemon (`tools/volume_daemon.py`, idle unless
  daily volume targets are on). If one exits, the container restarts.
- **Market maker loop:** fair price → ladder of post-only quotes → fills booked immediately
  (also partial and late ones) → skew and limits from the position.
  Swap failures are attributed (ours / counterparty / hub); only our own pause the market.
- **Liquidity:** a channel's "room to receive" is leased from the Hydra liquidity service
  (`OpenOrDeposit` into existing channels). Leases end on whole hours; the hub keeps its
  liquidity until `liquidity_expiry`, often longer than paid for active channels — alerts and
  the autopilot go by that date.

| File | Role |
|---|---|
| `easy/install.sh`, `easy/get.sh` | installer / one-line bootstrap |
| `easy/docker-compose.yml`, `easy/Dockerfile`, `easy/run_all.sh` | containers and entrypoint |
| `easy/hydra-mm` | runs `tools/hydra_mm.py` inside the bot container |
| `easy/node/config.template.yaml` | official mainnet node config |
| `easy/AGENTS.md` | runbook for an AI agent doing the setup |
| `tools/hydra_mm.py` | the `hydra-mm` command |
| `lib/planner.py` | budget + preset → market entries, deposits, inbound leases |
| `lib/easy_ops.py` | node access, wallet/capacity, funding, config writers, pause, status, Telegram pairing |
| `strategies/market_maker.py` | the market maker |
| `tools/ops_daemon.py`, `lib/alerts.py` | alerts, digests, daily report, lease autopilot |
| `tools/tg_control.py` | Telegram commands |
| `lib/volume.py`, `tools/volume_daemon.py` | pre-launch volume mode: the self-matching engine and the daily-target scheduler |
| `tools/gui.py`, `gui/` | the web GUI: API server (stdlib only) and the page (plain HTML/JS/CSS, no outside code) |
| `lib/grpc_client.py`, `lib/hydra_pb/` | Hydra node API (gRPC) |
| `trading_bot_cli.py`, `connectors/`, `order_tracker.py` | bot runtime, exchange connector, order tracking |

Facts learned against the mainnet node (built in): Bitcoin has no dual funding; the
liquidity service deposits tokens but never native coins (ETH/BTC go in with the node's own
`DepositChannel`); sponsored (permit) deposits are client-only (no inbound in the same
request); `target_node_pubkey` must stay unset; a fee estimate passes without an allowance
but the request needs an exact one (approved with 0.5% headroom, as exact decimal strings
from integer base units); the node's `PASSWORD` changes its identity, so easy mode leaves it
empty to match the web app.

## For AI agents

[easy/AGENTS.md](easy/AGENTS.md) is a runbook for Claude Code or similar agents setting this
up for a person: the rules (never print the seed, secrets only via environment, ask before
`fund --go`, keep the node API private) and the exact command sequence.

## Development

```bash
pip install -r requirements_bot.txt
python backtest/easy_dryrun.py        # planner, funding, setup, Telegram, alerts  (no node needed)
python backtest/mm_dryrun.py          # the market maker against a stub exchange
python backtest/volume_dryrun.py      # volume mode against a fake order book
python backtest/gui_dryrun.py         # web GUI: API, security (token, host, cross-site), jobs, settings
```

All four run offline against stubs. The bot also contains strategies easy mode doesn't use (grid, arbitrage, the
testnet volume maker) because the runtime imports them.

Tested end to end on mainnet (2026-09-30 → 10-01): fresh server → `install.sh` → invite
redeemed on the node → `setup` → `fund --go` with real funds (Arbitrum + Ethereum
channels) → quoting, alerts and Telegram control running.

## Risks

- **Market risk:** the bot holds inventory; if the price moves through your quotes you can
  lose more than the spread earns. The parity pair (USDC/USDC) carries the least of it.
- **Liquidity cost:** leases cost a fee whether or not anyone trades.
- **Software and counterparty risk:** this is young software on a young DEX, with payment
  channels, a hub and third-party services involved.
- **Keys:** whoever has the seed has the funds. Back it up offline; never share it.
