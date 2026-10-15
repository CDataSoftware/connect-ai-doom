#!/bin/bash
# Connect AI × DOOM: Claude scouts your data through the Connect AI MCP server, and
# the Doom level is built from what Connect AI let it reach.
#   ./raid.sh new                 ask Claude the default question (about a minute), then play
#   ./raid.sh new -q "<question>" ask Claude your own question, then play
#   ./raid.sh replay              play the last level again (no new Claude run)
#   ./raid.sh offline             made-up data, no network
#   ./raid.sh brief-only [question]  only ask Claude, no game (for rehearsing)
#   ./raid.sh login               save your Connect AI email and token in the Keychain
#   --claude-plays                Claude plays instead of you; add it to any mode above,
#                                 e.g. ./raid.sh replay --claude-plays
# With no mode, ./raid.sh is ./raid.sh new. The old names still work: --again (replay),
# --demo (offline), brief (brief-only).
# The side panel (http://localhost:8666) shows Claude's MCP calls live; NO_PANEL=1 skips it.
# Kills write nothing back: each is logged and its record read again live through Connect AI.
set -euo pipefail
cd "$(dirname "$0")"

PSDOOM=vendor/src/psdoom-ng/trunk/src/psdoom
BRIDGE="$PWD/connect_doom.py"
ZDBSP=vendor/src/zdbsp/build/zdbsp
mkdir -p state

MODE=live QUESTION="" PLAYS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    login)
      read -rp "Connect AI email: " CONNECT_AI_USER
      read -rsp "Connect AI personal access token: " CONNECT_AI_PAT; echo
      security add-generic-password -U -s connect-doom -a "$CONNECT_AI_USER" \
        -l "Connect AI PAT (connect-doom)" -w "$CONNECT_AI_PAT"
      echo "$CONNECT_AI_USER" > state/account
      echo "Saved. Now run ./raid.sh new"
      exit ;;
    new) MODE=live; shift ;;
    offline|--demo) MODE=demo; shift ;;
    replay|--again) MODE=again; shift ;;
    --claude-plays) PLAYS=1; shift ;;
    -q|--question) QUESTION="$2"; shift 2 ;;
    brief-only|brief) MODE=brief; shift; QUESTION="$*"; set -- ;;
    *) break ;;
  esac
done

if [[ ! -x "$PSDOOM" || ! -f wads/freedoom2.wad ]]; then
  echo "The game isn't built yet: run ./setup.sh first." >&2
  exit 1
fi
if [[ "$MODE" == live || "$MODE" == brief ]] && ! command -v claude >/dev/null; then
  echo "The briefing needs Claude Code's CLI (claude), signed in. Or play the last level: ./raid.sh replay, or play offline: ./raid.sh offline" >&2
  exit 1
fi

if [[ "$MODE" != demo ]]; then
  # Token lookup: env, then the macOS Keychain (no prompt; ./raid.sh login stores it), then,
  # if CONNECT_AI_OP_ITEM names one, 1Password once. The 1Password CLI re-asks per terminal
  # window and after 10 idle minutes, so that read is copied into the Keychain.
  if [[ -z "${CONNECT_AI_USER:-}" && -f state/account ]]; then   # the account ./raid.sh login saved
    CONNECT_AI_USER=$(<state/account)
  fi
  if [[ -z "${CONNECT_AI_PAT:-}" && -n "${CONNECT_AI_USER:-}" ]]; then
    CONNECT_AI_PAT=$(security find-generic-password -s connect-doom -a "$CONNECT_AI_USER" -w 2>/dev/null || true)
  fi
  if [[ -z "${CONNECT_AI_PAT:-}" && -n "${CONNECT_AI_OP_ITEM:-}" && -n "${CONNECT_AI_USER:-}" ]]; then
    CONNECT_AI_PAT=$(op --account my.1password.com read "$CONNECT_AI_OP_ITEM/credential")
    security add-generic-password -U -s connect-doom -a "$CONNECT_AI_USER" \
      -l "Connect AI PAT (connect-doom)" -w "$CONNECT_AI_PAT"
  fi
  if [[ -z "${CONNECT_AI_PAT:-}" ]]; then
    echo "No Connect AI token: run ./raid.sh login (or set CONNECT_AI_USER and CONNECT_AI_PAT)." >&2
    echo "To play offline with made-up data: ./raid.sh offline" >&2
    exit 1
  fi
  export CONNECT_AI_USER CONNECT_AI_PAT
fi

# A previous run's panel or poller (say, after a crash) would hold the port and serve stale state.
pkill -f "$BRIDGE (panel|poll)" 2>/dev/null || true

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
  [[ -f panel/sprites/demon.png ]] || python3 tools/make_sprites.py wads/freedoom2.wad panel/sprites assets/brand
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
[[ -f state/mission.json ]] || { echo "No level yet: run ./raid.sh new first." >&2; exit 1; }

# The level for this mission: map, signs, keycard messages.
python3 tools/make_branding.py wads/freedoom2.wad assets/brand state/brand.wad --mission state/mission.json
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

# Modern FPS controls, as in GZDoom: WASD to move, the mouse to turn, left click or Space to
# fire, E or right click to open doors, Shift to run, P to pause; the arrows turn too. Written
# fresh each launch (values are keyboard scancodes), so a booth machine always plays the same.
cat > state/controls.cfg <<'CFG'
key_up 17
key_down 31
key_strafeleft 30
key_straferight 32
key_left 75
key_right 77
key_fire 57
key_use 18
key_speed 42
key_strafe 56
use_mouse 1
mouseb_fire 0
mouseb_strafe -1
mouseb_forward -1
mouse_sensitivity 6
CFG
# The mouse turns but doesn't walk; its right button opens doors.
printf 'novert 1\ngrabmouse 1\nmouseb_use 1\n' > state/controls-extra.cfg

GAME_MODE=player; [[ -n "$PLAYS" ]] && GAME_MODE=claude
printf '{"mode": "%s", "started": %s}\n' "$GAME_MODE" "$(date +%s)" > state/game.json

# -nopslev: spawn monsters into the three arenas of the level
# -psallusers: don't filter monsters by Unix user
"$PSDOOM" -iwad wads/freedoom2.wad -file state/level.wad state/brand.wad -deh state/level.deh \
  -nopslev -psallusers -warp 1 -skill 3 -window -geometry "$GEOMETRY" -config state/controls.cfg -extraconfig state/controls-extra.cfg "$@" \
  >state/game.log 2> >(tee state/psdoom.err | grep --line-buffered -E 'KILL|ERROR' >&2) || true

python3 "$BRIDGE" score
