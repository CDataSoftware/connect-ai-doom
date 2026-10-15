#!/bin/bash
# Connect AI × DOOM: Claude scouts your data through the Connect AI MCP server, and
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
# with set -e a failed kill would be the exit code; game.json tells the panel a game is on
trap 'kill ${PIDS[@]+"${PIDS[@]}"} 2>/dev/null || true; rm -f state/game.json' EXIT
PANEL_PORT=${PANEL_PORT:-8666}
PANEL_URL="http://localhost:$PANEL_PORT"

# Side by side for screen recording: Doom on the left, the panel in its own Chrome
# window on the right. LAYOUT=off keeps psdoom's 1280x800 and opens a browser tab;
# GEOMETRY=WxH sets the game's size. The game window can be resized either way.
GEOMETRY=${GEOMETRY:-1280x800}
SIDE=""
if [[ "${LAYOUT:-side}" == side ]] && [[ -z "${NO_PANEL:-}" ]] && [[ -d "${CHROME_APP:-/Applications/Google Chrome.app}" ]]; then
  # The main screen's usable area: left, top (below the menu bar), width, height.
  SIDE=$(osascript -l JavaScript -e 'ObjC.import("AppKit"); var s = $.NSScreen.mainScreen;
    var f = s.frame, v = s.visibleFrame;
    [v.origin.x, f.size.height - v.origin.y - v.size.height, v.size.width, v.size.height].map(Math.round).join(" ")' \
    2>/dev/null || true)
fi
if [[ -n "$SIDE" ]]; then
  read -r SX SY SW SH <<<"$SIDE"
  # psdoom snaps to its aspect-corrected modes (n x 320x240 or n x 256x200), so pick the
  # biggest that leaves the panel at least 440 px and fits under the title bar (28 px).
  GW=640 GH=480
  for mode in 320x240 640x480 960x720 1280x960 1600x1200 256x200 512x400 768x600 1024x800 1280x1000; do
    w=${mode%x*} h=${mode#*x}
    if ((w <= SW - 440 && h <= SH - 28 && w * h > GW * GH)); then GW=$w GH=$h; fi
  done
  PANEL_W=$((SW - GW))
  [[ "$GEOMETRY" == 1280x800 ]] && GEOMETRY="${GW}x${GH}"
  export SDL_VIDEO_WINDOW_POS="$SX,$SY"
fi

open_panel() {
  if [[ -n "$SIDE" ]] && osascript -e "tell application \"Google Chrome\"
      set w to make new window
      set bounds of w to {$((SX + SW - PANEL_W)), $SY, $((SX + SW)), $((SY + SH))}
      set URL of active tab of w to \"$PANEL_URL\"
    end tell" >/dev/null 2>&1; then
    return
  fi
  open "$PANEL_URL"
}

if [[ "${NO_PANEL:-}" != 1 ]]; then
  [[ -f panel/sprites/demon.png ]] || python3 tools/make_sprites.py wads/freedoom2.wad panel/sprites brand/BRANDKIT
  python3 "$BRIDGE" panel "$PANEL_PORT" 2>/dev/null &
  PIDS+=($!)
  /bin/sleep 0.5
  open_panel
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
export PSDOOM_LABELS="$PWD/state/labels.tsv"  # the record under the crosshair, written by the bridge
export PSDOOMKILLCMD="python3 $BRIDGE kill"
export PSDOOMRENICECMD="python3 $BRIDGE hit"

rm -f state/play.jsonl  # the panel's Claude plays feed is only for this game
if [[ -n "$PLAYS" ]]; then
  # The agent channel patched into psdoom: Claude's commands in, game state out.
  mkdir -p state/agent && rm -f state/agent/*
  export PSDOOM_AGENT_DIR="$PWD/state/agent" PSDOOM_AGENT_GOD=1
  ( /bin/sleep 4; exec python3 play.py run ) 2>>state/play.err &
  PIDS+=($!)
fi

GAME_MODE=player; [[ -n "$PLAYS" ]] && GAME_MODE=claude
printf '{"mode": "%s", "started": %s}\n' "$GAME_MODE" "$(date +%s)" > state/game.json

# -nopslev: spawn monsters into the three arenas of the level
# -psallusers: don't filter monsters by Unix user
"$PSDOOM" -iwad wads/freedoom2.wad -file state/level.wad state/brand.wad -deh state/level.deh \
  -nopslev -psallusers -warp 1 -skill 3 -window -geometry "$GEOMETRY" "$@" \
  >state/game.log 2> >(tee state/psdoom.err | grep --line-buffered -E 'KILL|ERROR' >&2) || true

python3 "$BRIDGE" score
