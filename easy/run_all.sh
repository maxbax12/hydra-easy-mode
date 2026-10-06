#!/bin/bash
# Container entrypoint: links the persistent files from /app/data into place, then
# runs the web GUI, the market maker, the ops daemon (alerts, P&L, lease autopilot),
# the Telegram control and the volume daemon (pre-launch volume mode) side by side.
# SIGTERM is passed on so the bot cancels its orders and a volume run cleans up.
set -u
cd /app
mkdir -p data/state
for f in bot_config.yaml ops.yaml volume.yaml; do
  ln -sfn "/app/data/$f" "config/$f"      # written by `hydra-mm setup`
done
rm -rf state && ln -sfn /app/data/state state
for f in .env trading_bot.log ops.log tg_control.log volume.log gui.log; do
  touch "data/$f"; ln -sfn "/app/data/$f" "$f"
done

pids=()
sleeper=""
stop() {
  [ -n "$sleeper" ] && kill "$sleeper" 2>/dev/null
  # a volume run (own session) cancels its test orders and lifts its pause on SIGTERM
  pkill -TERM -f "hydra_mm.py volume run" 2>/dev/null && sleep 8
  for p in "${pids[@]}"; do kill -TERM "$p" 2>/dev/null; done
  for p in "${pids[@]}"; do wait "$p" 2>/dev/null; done     # the bot cancels its orders first
  exit 0
}
trap stop TERM INT

# the GUI first: its setup wizard has to work before anything is set up
python3 tools/gui.py --host 0.0.0.0 --port 8080 >>gui.log 2>&1 & pids+=($!)

until [ -s data/bot_config.yaml ]; do
  echo "Not set up yet — run:  ./hydra-mm setup   (waiting)"; sleep 30 & sleeper=$!
  wait $sleeper
done
sleeper=""
python3 tools/tg_control.py & pids+=($!)      # waits by itself until a bot token is set
# The bot connects to the node once at start: wait until the node answers
# (on a first start it syncs for minutes), or its markets would never load.
until python3 -c "import sys; from lib.easy_ops import booted; sys.exit(0 if booted() else 1)" 2>/dev/null; do
  echo "waiting for the node (starting, or waiting for a mainnet invite: ./hydra-mm invite <CODE>) …"
  sleep 10 & sleeper=$!; wait $sleeper
done
sleeper=""
python3 tools/ops_daemon.py & pids+=($!)
python3 tools/volume_daemon.py >>volume.log 2>&1 & pids+=($!)     # idle unless daily volume targets are on
python3 trading_bot_cli.py --config config/bot_config.yaml --daemon & pids+=($!)
wait -n
echo "a process exited — stopping the rest so docker restarts the container"
stop
