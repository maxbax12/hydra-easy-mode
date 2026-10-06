#!/bin/bash
# Hydra market maker — easy mode, from a fresh server in one line:
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/maxbax12/hydra-easy-mode/main/easy/get.sh)
#   bash <(curl -fsSL …/get.sh) --invite CODE --budget 500 --markets USDC.arb/USDC.eth --yes
#
# (`bash <(curl …)`, not `curl … | bash`: the setup asks questions on the terminal.)
# Needs an x86-64 Debian or Ubuntu server. Installs Docker and git if missing, fetches
# the bot into ~/hydra-mm, then runs easy/install.sh with the options you pass.
#
#   HYDRA_MM_REPO / HYDRA_MM_BRANCH / HYDRA_MM_DIR   override where it comes from / goes
set -euo pipefail

REPO="${HYDRA_MM_REPO:-https://github.com/maxbax12/hydra-easy-mode.git}"
BRANCH="${HYDRA_MM_BRANCH:-main}"
DIR="${HYDRA_MM_DIR:-$HOME/hydra-mm}"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[ "$(uname -m)" = "x86_64" ] || die "This needs an x86-64 (amd64) machine — the Hydra node has no ARM build (e.g. Apple Silicon or Raspberry Pi won't work)."
[ -r /etc/os-release ] && . /etc/os-release || true
case "${ID:-}${ID_LIKE:-}" in
  *debian*|*ubuntu*) ;;
  *) die "Tested on Debian and Ubuntu. On ${PRETTY_NAME:-this system}: install Docker + git yourself, then clone $REPO and run easy/install.sh." ;;
esac
SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  say "Installing Docker (official script from get.docker.com)"
  curl -fsSL https://get.docker.com | $SUDO sh
  [ -z "$SUDO" ] || $SUDO usermod -aG docker "$USER" || true
fi
if ! command -v git >/dev/null; then
  say "Installing git"
  $SUDO apt-get update -qq && $SUDO apt-get install -y -qq git
fi
$SUDO systemctl enable --now docker >/dev/null 2>&1 || true

if [ -d "$DIR/.git" ]; then
  say "Updating $DIR"
  git -C "$DIR" pull -q --ff-only
else
  say "Fetching the bot into $DIR"
  git clone -q -b "$BRANCH" "$REPO" "$DIR" || die "Could not fetch $REPO ($BRANCH). If it is private, log in first (gh auth login) or set HYDRA_MM_REPO."
fi

cd "$DIR/easy"
if [ -n "$SUDO" ] && ! docker info >/dev/null 2>&1; then
  exec $SUDO ./install.sh "$@"       # first run: this shell is not in the docker group yet
fi
exec ./install.sh "$@"
