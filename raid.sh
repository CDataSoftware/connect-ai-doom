#!/bin/bash
# Revenue Raid: open Stripe invoices from Connect AI as Doom monsters.
#   ./raid.sh          live data (needs CONNECT_AI_USER + CONNECT_AI_PAT)
#   ./raid.sh --demo   made-up invoices, no network
# Kills are dry run (logged to state/kills.jsonl) unless SANDBOX=1.
set -euo pipefail
cd "$(dirname "$0")"

PSDOOM=vendor/src/psdoom-ng/trunk/src/psdoom
BRIDGE="$PWD/connect_doom.py"

mkdir -p state
if [[ "${1:-}" == "--demo" ]]; then
  shift
  python3 "$BRIDGE" poll --demo
else
  : "${CONNECT_AI_USER:?set CONNECT_AI_USER}" "${CONNECT_AI_PAT:?set CONNECT_AI_PAT}"
  python3 "$BRIDGE" poll --once
  python3 "$BRIDGE" poll 2>>state/poll.log &
  POLLER=$!
  trap 'kill $POLLER 2>/dev/null' EXIT
fi

export PSDOOMPSCMD="python3 $BRIDGE list"
export PSDOOMKILLCMD="python3 $BRIDGE kill"
export PSDOOMRENICECMD="python3 $BRIDGE hit"

# -nopslev: spawn monsters into the three arenas of wads/arena.wad
# -psallusers: don't filter monsters by Unix user
"$PSDOOM" -iwad wads/freedoom2.wad -file wads/arena.wad -nopslev -psallusers \
  -warp 1 -skill 3 -window -geometry 1280x800 "$@" \
  >state/game.log 2> >(tee state/psdoom.err | grep --line-buffered -E 'KILL|ERROR' >&2) || true

python3 "$BRIDGE" score
