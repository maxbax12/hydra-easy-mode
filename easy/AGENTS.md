# Runbook for an AI agent (Claude Code or similar) setting up easy mode

You are setting up a Hydra DEX market maker **for a human, with their real money**.
Work from this directory (`easy/`). Every step is one command; `./hydra-mm doctor`
always tells you the next one.

## Rules

1. **Never print, log, copy or send the wallet seed.** It lives in `node/.env`
   (`MNEMONIC=`). Tell the human to back that file up offline themselves.
   Do not pass `--show-seed` unless the human asks for it while watching.
2. **Secrets never go into flags or chat.** The Telegram bot token comes from the human's
   environment (`ALERTS_TELEGRAM_BOT_TOKEN`) or they type it into `./hydra-mm setup` themselves.
3. **Ask before anything that moves money**: `./hydra-mm fund --go` (opens channels,
   pays lease fees). Show the human the plan and the fee first (`./hydra-mm fund`
   without `--go` prints both).
4. **Invite codes are bearer secrets**: use it once, don't echo it back. If the human's wallet
   is already admitted, install with it instead (`--seed-file` / `--env-file`, from a file THEY
   prepare — never ask them to paste the seed into the chat); no invite is needed then.
5. Keep the node API private: never publish port 5003 or change the compose file's
   networking. Never run `docker compose down -v` (it deletes the wallet).
6. If a step fails twice the same way, stop and show the human the output.

## Steps

Ask the human for: **budget in USD**, **which markets** (default all; for a first test
`USDC.arb/USDC.eth` only), and a **mainnet invite
code** (mainnet is invite-gated — an existing user mints one). The installer redeems it on the
node; without a code it stops at "waiting for an invite" — later: `./hydra-mm invite <CODE>`.

```bash
# fresh server, one line (installs Docker + git, fetches into ~/hydra-mm, runs the installer; ~5-15 min):
bash <(curl -fsSL https://raw.githubusercontent.com/maxbax12/hydra-easy-mode/main/easy/get.sh) \
     --invite <CODE> --budget <USD> --markets <A,B> --yes
# (already cloned / Docker present: cd ~/hydra-mm/easy && ./install.sh --invite <CODE> --budget <USD> --markets <A,B> --yes)
./hydra-mm doctor                                                    # what is left
./hydra-mm fund                                                      # what to send where + fees
```

`fund` lists addresses and amounts per network. The human sends the funds (you can't).
Poll `./hydra-mm fund` every few minutes until nothing is missing, show the fees, get a
yes, then:

```bash
./hydra-mm fund --go
./hydra-mm doctor          # repeat until "All good — the market maker is running."
```

## Useful

| Need | Command |
|---|---|
| state of everything | `./hydra-mm doctor` |
| markets, P&L, capacity | `./hydra-mm status` |
| stop quoting now | `./hydra-mm pause` (one market: `./hydra-mm pause mm_usdc_usdc`) |
| resume | `./hydra-mm resume` |
| addresses to fund | `./hydra-mm addresses` |
| node identity (for whitelisting) | `./hydra-mm identity` |
| logs | `docker compose logs --tail 100 bot` / `node` |
| Telegram | the human runs `./hydra-mm telegram` (token hidden), then sends `/start <code>` to their bot |
| change settings live | edit `data/bot_config.yaml`, then `docker compose exec bot touch state/reload` |

Facts that save time: the node needs a few minutes on first start (syncing); the hub is
always reached on port 443; Bitcoin has no dual funding (lease first, then the node
deposits BTC itself); native ETH/BTC deposits cost on-chain gas from the node wallet
(keep ~0.003 ETH on Ethereum for ETH deposits) — USDC needs none: without gas `fund`
uses sponsored (permit) deposits, and the lease follows on the next `fund --go`; leases end on whole hours and the hub may keep liquidity
longer for active channels (`liquidity_expiry`).
