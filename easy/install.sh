#!/bin/bash
# Easy mode, one command. Needs Docker (https://docs.docker.com/engine/install/).
#
#   ./install.sh                                  asks what it needs
#   ./install.sh --invite CODE --budget 300 --markets USDC.arb/USDC.eth --yes
#                                                 no questions (scripts, agents)
# Options:
#   --invite CODE     mainnet invite code (mainnet is invite-gated; ask an existing user)
#   --restore         use your existing wallet: type its 12/24-word seed (hidden)
#   --seed-file FILE  the same, with the seed words read from FILE (scripts, agents)
#   --env-file FILE   take over an existing node .env (MNEMONIC + PASSWORD) as it is,
#                     e.g. from another server — keeps the same identity
#   (an already admitted wallet needs no invite; never run one wallet on two nodes at once)
#   --show-seed       print the new recovery phrase once (default: only saved to node/.env)
#   --budget N --preset P --markets A,B --telegram-chat ID --yes
#                     passed to `hydra-mm setup` (the Telegram bot token via env:
#                     ALERTS_TELEGRAM_BOT_TOKEN)
set -euo pipefail
cd "$(dirname "$0")"

INVITE=""; RESTORE=0; SEED_FILE=""; ENV_FILE=""; SHOW=""; SETUP=()
while [ $# -gt 0 ]; do
  case "$1" in
    --invite) INVITE="$2"; shift 2 ;;
    --restore) RESTORE=1; shift ;;
    --seed-file) SEED_FILE="$2"; RESTORE=1; shift 2 ;;
    --env-file) ENV_FILE="$2"; RESTORE=1; shift 2 ;;
    --show-seed) SHOW="--show"; shift ;;
    --budget|--preset|--markets|--telegram-chat) SETUP+=("$1" "$2"); shift 2 ;;
    --yes) SETUP+=("$1"); shift ;;
    -h|--help) sed -n 2,20p "$0"; exit 0 ;;
    *) echo "unknown option $1 (see --help)"; exit 2 ;;
  esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
command -v docker >/dev/null || { echo "Docker is not installed: https://docs.docker.com/engine/install/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Docker Compose v2 is missing ('docker compose ...')."; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker is installed but not running — start Docker (Desktop) and run this again."; exit 1; }
mkdir -p node/data data/state
# compose reads node/.env for every command (even a build): make sure it exists, private
[ -e node/.env ] || { (umask 077; : > node/.env); }

say "1/5  Node config"
if [ ! -f node/config.yaml ]; then cp node/config.template.yaml node/config.yaml; echo "created node/config.yaml (official mainnet template)"; else echo "node/config.yaml exists — kept"; fi

say "2/5  Getting the node image and building the bot"
docker compose pull -q node
docker compose build -q bot

say "3/5  Wallet"
if [ -s node/.env ] && grep -q '^MNEMONIC=.' node/.env; then
  echo "node/.env already holds a wallet — kept"
  [ "$RESTORE" = 1 ] && echo "  (not replaced by the wallet you passed — to switch wallets see the README: 'Already have an admitted wallet?')"
elif [ "$RESTORE" = 1 ]; then
  echo "Using your existing wallet. Run it on THIS node only: stop the Hydra web app or any other node"
  echo "with the same seed first — two nodes with one wallet can lose funds (their channel states clash)."
  if [ -n "$ENV_FILE" ]; then
    [ -r "$ENV_FILE" ] || { echo "Can't read $ENV_FILE"; exit 1; }
    SRC="$(cd "$(dirname "$ENV_FILE")" && pwd)/$(basename "$ENV_FILE")"
    docker compose run --rm --no-deps -T -v "$PWD/node:/node" -v "$SRC:/import.env:ro" bot python3 -c "
import sys
from lib.easy_ops import read_node_env, write_node_env
try:
    words, password = read_node_env('/import.env')
    write_node_env('/node/.env', words, password)
except (ValueError, FileExistsError) as e:
    sys.exit(f'Not imported: {e}')
print('imported the wallet (seed + password) into node/.env')" || exit 1
  else
    if [ -n "$SEED_FILE" ]; then
      [ -r "$SEED_FILE" ] || { echo "Can't read $SEED_FILE"; exit 1; }
      WORDS="$(tr '\n' ' ' < "$SEED_FILE")"
    else
      read -rsp "Your 12 or 24 seed words (hidden): " WORDS; echo   # no password: see write_node_env
    fi
    docker compose run --rm --no-deps -T -e WORDS="$WORDS" -v "$PWD/node:/node" bot python3 -c "
import os, sys
from lib.easy_ops import write_node_env
try:
    write_node_env('/node/.env', os.environ['WORDS'])
except (ValueError, FileExistsError) as e:
    sys.exit(f'Not saved: {e}')
print('saved node/.env')" || { unset WORDS; exit 1; }
    unset WORDS
  fi
else
  docker compose run --rm --no-deps -T -v "$PWD/node:/node" bot python3 tools/hydra_mm.py new-seed --out /node/.env $SHOW
  echo "IMPORTANT: back up node/.env (the MNEMONIC line) somewhere offline."
fi
chmod 600 node/.env 2>/dev/null || true

say "4/5  Starting (the node needs a few minutes on first start)"
docker compose up -d
# Wait for the node's API. A new identity that is not admitted yet keeps the node waiting
# (API up, no networks) until an invite is redeemed — that is done here, from the node.
for i in $(seq 1 90); do
  ./hydra-mm wait-node --timeout 10 >/dev/null 2>&1 && break
  if docker compose logs node 2>&1 | grep -q ERROR_NOT_WHITELISTED && ! docker compose ps node | grep -q " Up "; then
    docker compose stop node >/dev/null 2>&1
    echo "This node image exits when the wallet is not admitted — get the current one: docker compose pull node, then run ./install.sh again."
    exit 3
  fi
  [ "$i" = 90 ] && { echo "node did not come up within 15 min — docker compose logs --tail 80 node"; exit 1; }
done
echo "node is up"
if docker compose exec -T bot python3 -c "import sys; from lib.easy_ops import waiting_for_invite; sys.exit(0 if waiting_for_invite() else 1)" 2>/dev/null; then
  if [ -n "$INVITE" ]; then
    ./hydra-mm invite "$INVITE" --timeout 900 || exit 3
  else
    ID=$(docker compose exec -T bot python3 -c "from lib.easy_ops import identity_key; print(identity_key())" 2>/dev/null || true)
    say "This wallet is not admitted to mainnet yet (mainnet is invite-gated)."
    echo "Identity key: ${ID:-?}"
    echo "The node is waiting. Redeem an invite code (an existing user mints one):  ./hydra-mm invite <CODE>"
    echo "or ask the Hydranet team to whitelist the identity key above. Then: ./hydra-mm doctor"
    echo "Already admitted with another wallet? See the README: 'Already have an admitted wallet?'"
    exit 3
  fi
else
  echo "✅ This wallet is admitted to mainnet — no invite needed."
  [ -z "$INVITE" ] || echo "   (the invite code you passed was not used — keep it for someone else)"
fi

say "5/5  Setup"
if [ ${#SETUP[@]} -gt 0 ]; then
  ./hydra-mm setup "${SETUP[@]}" || true
elif [ -t 0 ]; then
  ./hydra-mm setup || true                  # a person at the keyboard: ask the questions
fi
./hydra-mm doctor || true
