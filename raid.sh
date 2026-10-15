#!/bin/bash
# Revenue Raid: open Salesforce deals from Connect AI as Doom monsters.
#   ./raid.sh          live data (CONNECT_AI_USER + CONNECT_AI_PAT, or the 1Password item)
#   ./raid.sh --demo   made-up deals, no network
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
  # Read the 1Password item once (it may prompt) instead of once per poll process.
  OP_ITEM="${CONNECT_AI_OP_ITEM:-op://Personal/Connect AI Dev Edition PAT}"
  : "${CONNECT_AI_USER:=$(op --account my.1password.com read "$OP_ITEM/username")}"
  : "${CONNECT_AI_PAT:=$(op --account my.1password.com read "$OP_ITEM/credential")}"
  export CONNECT_AI_USER CONNECT_AI_PAT
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
