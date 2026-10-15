#!/bin/bash
# Access Granted: Claude scouts your data through the Connect AI MCP server, and
# the Doom level is built from what Connect AI let it reach.
#   ./raid.sh                     brief Claude with the default question, then play
#   ./raid.sh -q "<question>"     brief Claude with your own question
#   ./raid.sh --again             replay the last briefing (no Claude run)
#   ./raid.sh --demo              made-up data, no network
#   ./raid.sh brief [question]    only run the briefing
#   --claude-plays                Claude drives the marine (optional, off by default); combine
#                                 with any mode above, e.g. ./raid.sh --again --claude-plays
# The side panel (http://localhost:8666) shows Claude's MCP calls live; NO_PANEL=1 skips it.
# Kills are dry run: logged to state/kills.jsonl, nothing is written to any app.
set -euo pipefail
cd "$(dirname "$0")"

PSDOOM=vendor/src/psdoom-ng/trunk/src/psdoom
BRIDGE="$PWD/connect_doom.py"
ZDBSP=vendor/src/zdbsp/build/zdbsp
mkdir -p state

MODE=live QUESTION="" PLAYS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --demo) MODE=demo; shift ;;
    --again) MODE=again; shift ;;
    --claude-plays) PLAYS=1; shift ;;
    -q|--question) QUESTION="$2"; shift 2 ;;
    brief) MODE=brief; shift; QUESTION="$*"; set -- ;;
    *) break ;;
  esac
done

if [[ "$MODE" != demo ]]; then
  # Token lookup: env, then the macOS Keychain (no prompt), then 1Password once.
  # The 1Password CLI re-asks per terminal window and after 10 idle minutes, so
  # the first 1Password read is copied into the Keychain for every later launch.
  : "${CONNECT_AI_USER:=demo@example.com}"
  if [[ -z "${CONNECT_AI_PAT:-}" ]]; then
    CONNECT_AI_PAT=$(security find-generic-password -s connect-doom -a "$CONNECT_AI_USER" -w 2>/dev/null || true)
  fi
  if [[ -z "$CONNECT_AI_PAT" ]]; then
    OP_ITEM="${CONNECT_AI_OP_ITEM:-op://Private/Connect AI PAT}"
    CONNECT_AI_PAT=$(op --account my.1password.com read "$OP_ITEM/credential")
    security add-generic-password -U -s connect-doom -a "$CONNECT_AI_USER" \
      -l "Connect AI PAT (connect-doom)" -w "$CONNECT_AI_PAT"
  fi
  export CONNECT_AI_USER CONNECT_AI_PAT
fi

PIDS=()
trap 'kill ${PIDS[@]+"${PIDS[@]}"} 2>/dev/null || true' EXIT  # with set -e a failed kill would be the exit code
PANEL_PORT=${PANEL_PORT:-8666}
if [[ "${NO_PANEL:-}" != 1 ]]; then
  python3 "$BRIDGE" panel "$PANEL_PORT" 2>/dev/null &
  PIDS+=($!)
  /bin/sleep 0.5
  open "http://localhost:$PANEL_PORT"
fi

case "$MODE" in
  brief) python3 brief.py ${QUESTION:+"$QUESTION"}; exit ;;
  live) python3 brief.py ${QUESTION:+"$QUESTION"} >/dev/null ;;
  demo) python3 "$BRIDGE" poll --demo ;;
esac
[[ -f state/mission.json ]] || { echo "No briefing yet: run ./raid.sh first." >&2; exit 1; }

# The level for this mission: map, signs, keycard messages.
python3 tools/make_branding.py wads/freedoom2.wad brand/BRANDKIT state/brand.wad --mission state/mission.json
python3 tools/make_arena.py state/level-raw.wad --mission state/mission.json --brand
"$ZDBSP" -o state/level.wad state/level-raw.wad >/dev/null
python3 tools/make_deh.py state/mission.json state/level.deh

if [[ "$MODE" != demo ]]; then
  python3 "$BRIDGE" poll --once
  python3 "$BRIDGE" poll 2>>state/poll.log &
  PIDS+=($!)
fi

export PSDOOMPSCMD="python3 $BRIDGE list"
export PSDOOMKILLCMD="python3 $BRIDGE kill"
export PSDOOMRENICECMD="python3 $BRIDGE hit"

if [[ -n "$PLAYS" ]]; then
  # The agent channel patched into psdoom: Claude's commands in, game state out.
  mkdir -p state/agent && rm -f state/agent/*
  export PSDOOM_AGENT_DIR="$PWD/state/agent" PSDOOM_AGENT_GOD=1
  ( /bin/sleep 4; exec python3 play.py run ) 2>>state/play.err &
  PIDS+=($!)
fi

# -nopslev: spawn monsters into the three arenas of the level
# -psallusers: don't filter monsters by Unix user
"$PSDOOM" -iwad wads/freedoom2.wad -file state/level.wad state/brand.wad -deh state/level.deh \
  -nopslev -psallusers -warp 1 -skill 3 -window -geometry 1280x800 "$@" \
  >state/game.log 2> >(tee state/psdoom.err | grep --line-buffered -E 'KILL|ERROR' >&2) || true

python3 "$BRIDGE" score
