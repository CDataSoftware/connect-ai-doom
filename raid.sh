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
  # Token lookup: env, then the macOS Keychain (no prompt), then 1Password once.
  # The 1Password CLI re-asks per terminal window and after 10 idle minutes, so
  # the first 1Password read is copied into the Keychain for every later launch.
  : "${CONNECT_AI_USER:=te_dev_edition@clouddataos.com}"
  if [[ -z "${CONNECT_AI_PAT:-}" ]]; then
    CONNECT_AI_PAT=$(security find-generic-password -s connect-doom -a "$CONNECT_AI_USER" -w 2>/dev/null || true)
  fi
  if [[ -z "$CONNECT_AI_PAT" ]]; then
    OP_ITEM="${CONNECT_AI_OP_ITEM:-op://Personal/Connect AI Dev Edition PAT}"
    CONNECT_AI_PAT=$(op --account my.1password.com read "$OP_ITEM/credential")
    security add-generic-password -U -s connect-doom -a "$CONNECT_AI_USER" \
      -l "Connect AI PAT (connect-doom)" -w "$CONNECT_AI_PAT"
  fi
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
WADS=(wads/arena.wad)
[[ -f wads/brand.wad ]] && WADS+=(wads/brand.wad)  # Connect AI signs + menu logo

"$PSDOOM" -iwad wads/freedoom2.wad -file "${WADS[@]}" -nopslev -psallusers \
  -warp 1 -skill 3 -window -geometry 1280x800 "$@" \
  >state/game.log 2> >(tee state/psdoom.err | grep --line-buffered -E 'KILL|ERROR' >&2) || true

python3 "$BRIDGE" score
