#!/bin/bash
# Connect AI × DOOM: Claude scouts your data through the Connect AI MCP server, and
# the Doom level is built from what Connect AI let it reach.
#   ./raid.sh new                 ask Claude the default question (about a minute), then play
#   ./raid.sh new -q "<question>" ask Claude your own question, then play
#   ./raid.sh replay              play the last level again (no new Claude run)
#   ./raid.sh booth               replay over and over: the next game starts a few seconds after one ends
#   ./raid.sh booth offline       the same with sample data; booth plays offline anyway without a token
#   ./raid.sh offline             sample data, no network
#   ./raid.sh brief-only [question]  only ask Claude, no game (for rehearsing)
#   ./raid.sh login               save your Connect AI email and token in the Keychain
#   ./raid.sh doctor              check this Mac is ready to play (build, token, network, screen)
#   ./raid.sh leaderboard [all]   how each model has played this level (or every level)
#   --claude-plays                Claude plays instead of you; add it to any mode above,
#                                 e.g. ./raid.sh replay --claude-plays
#   --loop                        auto replay (on unless --no-loop): once the level is clear, you
#                                 die, or Claude's run ends, every record comes back and the game
#                                 starts over, all day
#   --no-loop                     auto replay off: play the level once and stop there
#   --offline-plays               Claude plays offline: a scripted player that narrates with
#                                 Claude's lines from earlier runs, no network, no AI tokens. Claude
#                                 plays also falls back to it when Claude can't run (no network,
#                                 signed out).
#   --model <name>                another model plays instead of Claude Code, through Connect AI's
#                                 LLM Gateway, e.g. --model gemini-3.7-flash (implies --claude-plays;
#                                 the gateway key comes from the Keychain, see the README)
#   --models <a,b,...> [--runs N] a bench for the leaderboard: the models take turns, N rounds
#                                 (default 3), each run scored; claude-code is Claude Code, e.g.
#                                 ./raid.sh replay --saved-data --models claude-code,gemini-3.7-flash
#   --god-mode                    players can't die
#   --saved-data                  play the level's last saved records; nothing asks Connect AI
#   --no-music, --no-sound-effects  start with Freedoom's soundtrack, or the sound effects, off
# All seven switches are also in the game's Options menu (Esc > Options), so they can be
# flipped without restarting, and a booth keeps them from one game to the next; with auto
# replay on, a minute without a key or the mouse hands the game to Claude (IDLE_SECONDS).
# With no mode, ./raid.sh is ./raid.sh new. The old names still work: --again (replay),
# --demo (offline), brief (brief-only).
# The side panel (http://localhost:8666) shows Claude's MCP calls live; NO_PANEL=1 skips it. Its
# question box ends the game, briefs the new question and plays its level, in the same raid.
# NOGORE=1: hits throw bullet puffs instead of blood, and a kill vanishes in teleport fog.
# Kills write nothing back: each is logged and its record read again live through Connect AI.
set -euo pipefail
cd "$(dirname "$0")"

PSDOOM=vendor/src/psdoom-ng/trunk/src/psdoom
BRIDGE="$PWD/connect_doom.py"
ZDBSP=vendor/src/zdbsp/build/zdbsp
mkdir -p state

MODE=live QUESTION="" PLAYS="" LOOP="" BOOTH="" GOD="" SAVED="" OFFLINE="" NOMUSIC="" NOSFX="" MODEL="" MODELS="" RUNS=3
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
    doctor) MODE=doctor; shift ;;
    new) MODE=live; shift ;;
    offline|--demo) MODE=demo; shift ;;
    replay|--again) MODE=again; shift ;;
    booth) BOOTH=1; shift ;;
    --claude-plays) PLAYS=1; shift ;;
    --offline-plays) PLAYS=1 OFFLINE=1; shift ;;
    --model) PLAYS=1 MODEL="${2:?--model needs a model name, e.g. --model gemini-3.7-flash}"; shift 2 ;;
    --models) PLAYS=1 LOOP=1 MODELS="${2:?--models needs a list, e.g. --models claude-code,gemini-3.7-flash}"; shift 2 ;;
    --runs) RUNS="${2:?--runs needs a number}"; shift 2 ;;
    leaderboard) shift; exec python3 leaderboard.py "$@" ;;
    --god-mode) GOD=1; shift ;;
    --saved-data) SAVED=1; shift ;;
    --no-music) NOMUSIC=1; shift ;;
    --no-sound-effects) NOSFX=1; shift ;;
    --loop|--auto-replay) LOOP=1; shift ;;
    --no-loop) LOOP=0; shift ;;
    -q|--question) QUESTION="$2"; shift 2 ;;
    brief-only|brief) MODE=brief; shift; QUESTION="$*"; set -- ;;
    # The rest go to psdoom (e.g. -warp 2). A mistyped mode or help stops here: falling
    # through to `new` would start a paid briefing and take over the screen.
    -h|--help|help|[!-]*) sed -n '4,41p' "$0" | sed 's/^# \{0,1\}//'
      [[ "$1" == -h || "$1" == --help || "$1" == help ]] && exit 0
      echo "Unknown mode: $1" >&2; exit 2 ;;
    *) break ;;
  esac
done

# Auto replay is on unless --no-loop: a cleared level or a death starts over, not sits there.
if [[ -z "$LOOP" ]]; then LOOP=1; fi
[[ "$LOOP" == 0 ]] && LOOP=""

# booth loops the last level, or offline with sample data; it never runs a new briefing.
if [[ -n "$BOOTH" && "$MODE" != demo ]]; then MODE=again; fi

if [[ ! -x "$PSDOOM" || ! -f wads/freedoom2.wad ]] && [[ "$MODE" != doctor ]]; then
  echo "The game isn't built yet: run ./setup.sh first." >&2
  exit 1
fi
# A game built from an older patch can't draw the themed level (it runs out of visplanes and
# quits mid-game) or give each room its monsters.
BUILT=$(cat vendor/src/psdoom-ng/.connect-doom-patch 2>/dev/null || true)
if [[ -n "$BUILT" && "$BUILT" != "$(shasum -a 256 patches/psdoom-ng-connect-doom.patch | cut -d' ' -f1)" ]] \
   && [[ "$MODE" != doctor ]]; then
  # A pull brought a new patch: rebuild just the game (no downloads, so bad Wi-Fi is fine).
  echo "The game patch changed since this Mac built it: rebuilding the game (about a minute)..." >&2
  if ! ./setup.sh --game >&2; then
    echo "Rebuilding the game failed: run ./setup.sh (its log is vendor/build.log)." >&2
    exit 1
  fi
fi
if [[ "$MODE" == live || "$MODE" == brief ]] && ! command -v claude >/dev/null; then
  echo "The briefing needs Claude Code's CLI (claude), signed in. Or play the last level: ./raid.sh replay, or play offline: ./raid.sh offline" >&2
  exit 1
fi

if [[ "$MODE" != demo ]]; then
  # Token lookup: env, then a local .env (gitignored: CONNECT_AI_USER=... and CONNECT_AI_PAT=...
  # lines), then the macOS Keychain (no prompt; ./raid.sh login stores it), then, if
  # CONNECT_AI_OP_ITEM names one, 1Password once. The 1Password CLI re-asks per terminal
  # window and after 10 idle minutes, so that read is copied into the Keychain.
  if [[ -f .env ]]; then
    while IFS='=' read -r name value; do
      [[ "$name" =~ ^CONNECT_AI_[A-Z_]+$ && -z "${!name:-}" ]] && printf -v "$name" '%s' "$value"
    done < .env
  fi
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
  if [[ -z "${CONNECT_AI_PAT:-}" && -n "$BOOTH" ]]; then
    echo "No Connect AI account on this Mac, so booth mode plays offline, with sample data." >&2
    MODE=demo
  elif [[ -z "${CONNECT_AI_PAT:-}" && "$MODE" != doctor ]]; then
    echo "No Connect AI token: run ./raid.sh login (or set CONNECT_AI_USER and CONNECT_AI_PAT)." >&2
    echo "To play offline with sample data: ./raid.sh offline" >&2
    exit 1
  fi
  export CONNECT_AI_USER CONNECT_AI_PAT
fi

GATEWAY=$MODEL   # a model that plays through Connect AI's LLM Gateway, so the gateway key is needed
if [[ -n "$MODELS" ]]; then
  # --models: play.py's supervisor hands each run to the next model and scores it (leaderboard.py)
  export PLAY_MODELS="$MODELS" PLAY_RUNS="$RUNS"
  for m in ${MODELS//,/ }; do [[ "$m" != claude-code ]] && GATEWAY=$m; done
fi
# The LLM Gateway takes a gateway virtual key (cgw_sk_...), not the Connect AI token: for
# --model, a bench, and the Options menu's Model row (offline, nothing asks the Keychain)
if [[ -z "${CONNECT_AI_GATEWAY_KEY:-}" && ( -n "$GATEWAY" || "$MODE" == live || "$MODE" == again ) ]]; then
  CONNECT_AI_GATEWAY_KEY=$(security find-generic-password -s connect-doom -a llm-gateway -w 2>/dev/null || true)
fi
if [[ -n "$GATEWAY" && -z "${CONNECT_AI_GATEWAY_KEY:-}" ]]; then
  echo "No LLM Gateway key for $GATEWAY. Issue a virtual key in Connect AI (LLM Gateway > Routing >" >&2
  echo "Virtual Keys), then save it: security add-generic-password -U -s connect-doom -a llm-gateway -w" >&2
  exit 1
fi
if [[ -n "$MODEL" ]]; then export PLAY_GATEWAY_MODEL="$MODEL"; fi
# The Options menu's Model row: Claude Code, then the models the gateway key may use (its
# project's allowlist, as of the last game: gateway_run.py fetches it for the next one in the
# background, so only the first launch waits on the gateway), then any named above
MENU_MODELS=claude-code ALLOWED=""
if [[ -n "${CONNECT_AI_GATEWAY_KEY:-}" ]]; then
  export CONNECT_AI_GATEWAY_KEY
  ALLOWED=$(python3 gateway_run.py models 2>/dev/null || true)
fi
for m in ${ALLOWED//,/ } $MODEL ${MODELS//,/ }; do [[ ",$MENU_MODELS," == *",$m,"* ]] || MENU_MODELS+=",$m"; done
export PSDOOM_AGENT_MODELS="$MENU_MODELS"

if [[ -n "$BOOTH" ]]; then
  # Back-to-back games for a booth. The screen stays awake between games; Ctrl-C in this
  # Terminal stops it. BOOTH_PAUSE sets the seconds between games, BOOTH_GAMES a limit.
  # Without Connect AI a replay falls back to saved records or offline by itself (see below).
  if [[ "$MODE" == again && ! -f state/mission.json ]]; then
    echo "No level on this Mac yet (./raid.sh new makes one), so booth mode plays offline, with sample data." >&2
    MODE=demo
  fi
  GAME=replay; [[ "$MODE" == demo ]] && GAME=offline
  if command -v caffeinate >/dev/null; then caffeinate -d -w $$ & fi
  # The Options menu's switches carry over from game to game (play.py keeps them in
  # state/options.json); a new booth session starts from the flags.
  rm -f state/options.json
  export CONNECT_DOOM_BOOTH=1
  games=0
  while :; do
    "$PWD/raid.sh" "$GAME" ${PLAYS:+--claude-plays} "$([[ -n "$LOOP" ]] && echo --loop || echo --no-loop)" ${GOD:+--god-mode} ${SAVED:+--saved-data} \
      ${NOMUSIC:+--no-music} ${NOSFX:+--no-sound-effects} \
      ${OFFLINE:+--offline-plays} ${MODEL:+--model "$MODEL"} "$@" || { echo "The game didn't start, so booth mode stopped." >&2; exit 1; }
    games=$((games + 1))
    if [[ -n "${BOOTH_GAMES:-}" ]] && ((games >= BOOTH_GAMES)); then exit; fi
    echo "Next game in ${BOOTH_PAUSE:-10} seconds. Ctrl-C to stop."
    /bin/sleep "${BOOTH_PAUSE:-10}"
  done
fi

# The Options menu's switches: the flags, or in a booth the last game's (state/options.json).
# A new level asked for from the panel starts from the switches of the game before it, too.
load_options() {
  if [[ -f state/options.json ]]; then
    read -r PLAYS LOOP GOD SAVED OFFLINE NOMUSIC NOSFX < <(python3 -c 'import json
o = json.load(open("state/options.json"))
print(*[1 if o.get(k) else "-" for k in ("claude_plays", "auto_replay", "god_mode", "saved_data", "offline_claude")],
      *["-" if o.get(k, 1) else 1 for k in ("music", "sound_effects")])' \
      2>/dev/null || true)
    # '%s': macOS's bash 3.2 assigns nothing for printf -v with an empty format, so every switch flipped
    for v in PLAYS LOOP GOD SAVED OFFLINE NOMUSIC NOSFX; do [[ "${!v}" == - ]] && printf -v "$v" '%s' ''; done
  elif [[ -n "$PLAYS" ]]; then
    GOD=1   # Claude starts in god mode, as switching Claude plays on in the menu does
  fi
  export PSDOOM_OPT_GOD=${GOD:-0} PSDOOM_OPT_SAVED=${SAVED:-0} PSDOOM_OPT_OFFLINE=${OFFLINE:-0}
  # the Model row's pick: the last booth game's, else --model's, else Claude Code
  PSDOOM_AGENT_MODEL=$(python3 -c 'import json; print(json.load(open("state/options.json")).get("model") or "")' 2>/dev/null || true)
  export PSDOOM_AGENT_MODEL=${PSDOOM_AGENT_MODEL:-${MODEL:-claude-code}}
  PSDOOM_OPT_MUSIC=1 PSDOOM_OPT_SFX=1
  [[ -n "$NOMUSIC" ]] && PSDOOM_OPT_MUSIC=0
  [[ -n "$NOSFX" ]] && PSDOOM_OPT_SFX=0
  export PSDOOM_OPT_MUSIC PSDOOM_OPT_SFX
}
[[ -z "${CONNECT_DOOM_BOOTH:-}" ]] && rm -f state/options.json
load_options

if [[ "$MODE" == doctor ]]; then
  # One line per check; an x line says what to run. Exits 1 if anything needed to play is missing.
  # Without a token only offline play is checked: offline and booth work without one.
  BAD=0
  ok() { echo "  ✓ $*"; }
  no() { echo "  ✗ $*"; BAD=1; }
  note() { echo "  - $*"; }
  echo "connect-doom on macOS $(sw_vers -productVersion 2>/dev/null) ($(uname -m))"
  if [[ -x "$PSDOOM" && -x "$ZDBSP" && -f wads/freedoom2.wad ]]; then ok "game, map builder and Freedoom art built"
  else no "the game isn't fully built: run ./setup.sh (its log is vendor/build.log)"; fi
  if python3 -c 'import sys; assert sys.version_info >= (3, 9)' 2>/dev/null; then ok "$(python3 --version)"
  else no "python3 3.9 or newer is needed: xcode-select --install"; fi
  if python3 -c 'import PIL' 2>/dev/null; then ok "Pillow"; else no "Pillow is missing: run ./setup.sh"; fi
  if command -v claude >/dev/null; then ok "Claude Code CLI, for ./raid.sh new"
  else note "no Claude Code CLI: replay and offline work, new needs it"; fi
  ANSWERS=""
  if [[ -n "${CONNECT_AI_PAT:-}" ]]; then
    ok "Connect AI token for ${CONNECT_AI_USER:-?}"
    if n=$(python3 -c 'import connect_ai_mcp as c; print(len(c.Client(timeout=15).connections()))' 2>&1); then
      ok "Connect AI answers: $n connections"; ANSWERS=1
    else
      no "Connect AI didn't answer (${n##*$'\n'}). Check the Wi-Fi and the token, or play ./raid.sh offline"
    fi
  else
    note "no Connect AI token: ./raid.sh offline and ./raid.sh booth play sample data; ./raid.sh login for yours"
  fi
  CONNECT_AI_GATEWAY_KEY=${CONNECT_AI_GATEWAY_KEY:-$(security find-generic-password -s connect-doom -a llm-gateway -w 2>/dev/null || true)}
  if [[ -n "$CONNECT_AI_GATEWAY_KEY" ]]; then
    echo "  … asking the LLM Gateway, and each of its models (a few seconds)"
    while IFS=$'\t' read -r status text; do
      case "$status" in ok) ok "$text" ;; error) no "$text" ;; *) note "$text" ;; esac
    done < <(CONNECT_AI_GATEWAY_KEY=$CONNECT_AI_GATEWAY_KEY python3 gateway_run.py check 2>/dev/null || true)
  else
    note "no LLM Gateway key: Claude Code plays; for other models (--model, the menu's Model row), see the README"
  fi
  if [[ -f state/mission.json ]]; then
    ok "a level to replay: $(python3 -c 'import json; print(json.load(open("state/mission.json"))["question"][:70])')"
    if [[ -n "$ANSWERS" ]] && grep -q '"query"' state/mission.json; then
      echo "  … running this level's queries (up to ${CONNECT_AI_TIMEOUT:-30} s each)"
      while IFS=$'\t' read -r status text; do
        case "$status" in
          ok) ok "$text" ;;
          error) no "$text" ;;
          *) note "$text" ;;
        esac
      done < <(python3 "$BRIDGE" check 2>/dev/null || true)
    fi
  elif [[ -n "${CONNECT_AI_PAT:-}" ]]; then note "no level yet: run ./raid.sh new once, then replay plays it"; fi
  if [[ "$(cat vendor/src/psdoom-ng/.connect-doom-patch 2>/dev/null)" == "$(shasum -a 256 patches/psdoom-ng-connect-doom.patch | cut -d' ' -f1)" ]]
  then ok "the game build is current, for the full-screen layout"
  else no "the game was built from an older patch: run ./setup.sh again"; fi
  port=${PANEL_PORT:-8666}
  if ! lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then ok "port $port free for the panel"
  elif pgrep -f "$BRIDGE panel" >/dev/null; then note "port $port: an old panel is running; raid.sh replaces it"
  else no "port $port is taken by another app: run with PANEL_PORT=8667"; fi
  screen=$(osascript -l JavaScript -e 'ObjC.import("AppKit"); var v = $.NSScreen.mainScreen.visibleFrame;
    Math.round(v.size.width) + "x" + Math.round(v.size.height)' 2>/dev/null || true)
  [[ -n "$screen" ]] && note "screen: ${screen}. The game and panel fill the main screen"
  screens=$(osascript -l JavaScript -e 'ObjC.import("AppKit"); $.NSScreen.screens.count' 2>/dev/null || echo 1)
  if ((screens > 1)); then
    note "$screens screens: clicking an app on another screen hides the game's full-screen stage until you click the game again. At a booth, use one screen."
  fi
  if ((BAD)); then echo "Not ready: fix the ✗ lines."; exit 1; fi
  if [[ -n "${CONNECT_AI_PAT:-}" ]]; then echo "Ready: ./raid.sh booth plays the last level game after game, ./raid.sh new makes a fresh one."
  else echo "Ready to play offline: ./raid.sh booth plays sample data game after game."; fi
  exit
fi

# A previous run's panel or poller (say, after a crash) would hold the port and serve stale state.
pkill -f "$BRIDGE (panel|poll)" 2>/dev/null || true
pkill -f "$PWD/tools/stage.js" 2>/dev/null || true

# An offline game killed before its exit trap left its sample in mission.json and the real level
# set aside: put the real level back before anything reads it.
if [[ -f state/mission-live.json ]]; then mv -f state/mission-live.json state/mission.json; fi

# A question asked from the panel of a raid that was killed mid-briefing is stale.
rm -f state/ask.json state/asking.json state/ask-failed.json

PIDS=()
# with set -e a failed kill would be the exit code; game.json tells the panel a game is on
# An offline game sets the real level aside in mission-live.json; put it back for the next replay.
# The game too (psdoom ignores SIGTERM): a raid that dies must not leave it frozen on the screen.
# disown -a first: bash says nothing about jobs it no longer tracks, so no "Terminated: 15" lines.
trap 'disown -a 2>/dev/null || true; kill ${PIDS[@]+"${PIDS[@]}"} 2>/dev/null || true
  pkill -9 -P $$ -x psdoom 2>/dev/null || true; rm -f state/game.json
  if [[ -f state/mission-live.json ]]; then mv -f state/mission-live.json state/mission.json; fi' EXIT
# Keep the screen from dimming or locking mid-game.
if command -v caffeinate >/dev/null; then caffeinate -d -w $$ & fi
PANEL_PORT=${PANEL_PORT:-8666}
PANEL_URL="http://localhost:$PANEL_PORT"

# Full screen, for booths and screen recordings: tools/stage.js covers the main screen with
# the panel's stage page, menu bar and Dock hidden. The game's borderless window sits on the
# left two thirds, as tall as fits, with branded bars above and below; the panel fills the
# right third. LAYOUT=off keeps psdoom's 1280x800 window and opens the panel in a browser tab.
# GEOMETRY=WxH sets the game's size either way.
GEOMETRY=${GEOMETRY:-1280x800}
STAGE=""
if [[ "${LAYOUT:-full}" == full && -z "${NO_PANEL:-}" ]]; then
  PATCHED=$(shasum -a 256 patches/psdoom-ng-connect-doom.patch 2>/dev/null | cut -d' ' -f1 || true)
  if [[ "$(cat vendor/src/psdoom-ng/.connect-doom-patch 2>/dev/null)" != "$PATCHED" ]]; then
    echo "The game was built from an older patch: run ./setup.sh again for the full-screen layout." >&2
  else
    # The screen the Terminal is on: its top-left corner in global (top-down) coordinates, its
    # width and height, and the strip at the top the notch or menu bar keeps.
    STAGE=$(osascript -l JavaScript -e 'ObjC.import("AppKit"); var s = $.NSScreen.mainScreen, f = s.frame;
      var top = $.NSScreen.screens.objectAtIndex(0).frame.size.height;
      [f.origin.x, top - f.origin.y - f.size.height, f.size.width, f.size.height, s.safeAreaInsets.top]
        .map(Math.round).join(" ")' 2>/dev/null || true)
  fi
fi
if [[ -n "$STAGE" ]]; then
  read -r SX SY SW SH ST <<<"$STAGE"
  STAGE_W=$((SW * 2 / 3))
  # psdoom snaps to its aspect-corrected modes (n x 320x240 or n x 256x200): the biggest that
  # fits the stage and leaves 48 px above and below for the bars.
  GW=320 GH=240
  for mode in 320x240 640x480 960x720 1280x960 1600x1200 256x200 512x400 768x600 1024x800 1280x1000; do
    w=${mode%x*} h=${mode#*x}
    if ((w <= STAGE_W && h <= SH - ST - 96 && w * h > GW * GH)); then GW=$w GH=$h; fi
  done
  [[ "$GEOMETRY" == 1280x800 ]] && GEOMETRY="${GW}x${GH}"
  GW=${GEOMETRY%x*} GH=${GEOMETRY#*x}
  GX=$(((STAGE_W - GW) / 2)) GY=$((ST + (SH - ST - GH) / 2))
  export SDL_VIDEO_WINDOW_POS="$((SX + GX)),$((SY + GY))" PSDOOM_FRAMELESS=1
fi

open_panel() {
  if [[ -n "$STAGE" ]]; then
    osascript -l JavaScript "$PWD/tools/stage.js" "$PANEL_URL/?stage=$GX,$GY,$GW,$GH,$STAGE_W,$ST" >/dev/null 2>&1 &
    PIDS+=($!)
    export PSDOOM_STAGE_PID=$!   # the game floats while the stage is showing
    return
  fi
  open "$PANEL_URL"
}

if [[ "${NO_PANEL:-}" != 1 ]]; then
  [[ -f panel/sprites/knight.png ]] || python3 tools/make_sprites.py wads/freedoom2.wad panel/sprites assets/brand
  # RAID_PID: the panel's question box ends this raid's game (its psdoom) so the next is a new level
  RAID_PID=$$ python3 "$BRIDGE" panel "$PANEL_PORT" 2>/dev/null &
  PIDS+=($!)
  /bin/sleep 0.5
  open_panel
fi

case "$MODE" in
  brief) python3 brief.py ${QUESTION:+"$QUESTION"}; exit ;;
  live) python3 brief.py ${QUESTION:+"$QUESTION"} >/dev/null ;;
  demo)
    # Set the real level aside, not the sample (its rooms carry rows): on a Mac that never had a
    # real level, mission.json may hold only the sample.
    if python3 -c 'import json, sys; r = json.load(open("state/mission.json")).get("rooms") or [{}]
sys.exit(0 if any("query" in x for x in r) and not all("rows" in x for x in r) else 1)' 2>/dev/null; then
      cp state/mission.json state/mission-live.json
    fi
    python3 "$BRIDGE" poll --demo ;;
esac
[[ -f state/mission.json ]] || { echo "No level yet: run ./raid.sh new first." >&2; exit 1; }

# A question asked from the panel's box (connect_doom.py ask): Claude briefs it while the stage
# shows the briefing, and the next game is its level. If the briefing fails or finds no rooms,
# the last level comes back and the panel says so.
new_level() {
  mv -f state/ask.json state/asking.json
  local question
  question=$(python3 -c 'import json; print(json.load(open("state/asking.json"))["question"])')
  echo "New question from the panel: $question" >&2
  cp state/mission.json state/mission-before.json
  if python3 brief.py "$question" >/dev/null 2>>state/brief.err && python3 -c 'import json, sys
sys.exit(0 if json.load(open("state/mission.json")).get("rooms") else 1)' 2>/dev/null; then
    rm -f state/mission-live.json state/mission-before.json   # a real level now, not the offline one
    MODE=live
  else
    mv -f state/mission-before.json state/mission.json
    python3 -c 'import json, sys, time
json.dump({"question": sys.argv[1], "at": time.time()}, open("state/ask-failed.json", "w"))' "$question"
    echo "Claude couldn't build a level for that question; playing the last level again." >&2
  fi
  rm -f state/asking.json
  load_options
}

GAME_PIDS=()
while :; do
  # The last game's agent channel goes first: its state.json still holds that game's switches,
  # and the bridge reads Saved data from it before options.json and the flags.
  mkdir -p state/agent && rm -f state/agent/*
  # The records to start with. Without Wi-Fi, poll --once falls back to the ones this level last
  # fetched; if it never fetched any (exit 3), play offline.
  if [[ "$MODE" != demo ]] && ! python3 "$BRIDGE" poll --once; then
    echo "Can't reach Connect AI, and this level has no saved records yet: playing offline instead." >&2
    MODE=demo
    cp state/mission.json state/mission-live.json
    python3 "$BRIDGE" poll --demo
  fi

  # The level for this mission: map, signs, keycard messages (and, with NOGORE=1, no blood or corpses).
  python3 tools/make_branding.py wads/freedoom2.wad assets/brand state/brand.wad --mission state/mission.json
  python3 tools/make_arena.py state/level-raw.wad --mission state/mission.json --records state/cache.json --brand
  "$ZDBSP" -o state/level.wad state/level-raw.wad >/dev/null
  if [[ "${NOGORE:-}" == 1 ]]; then
    python3 tools/make_deh.py state/mission.json state/level.deh --no-gore
  else
    python3 tools/make_deh.py state/mission.json state/level.deh
  fi
  # Each room's monsters, after its app's theme (tools/themes.py): a record's and an urgent record's.
  PSDOOM_MONSTERS=$(python3 tools/themes.py monsters state/mission.json)
  export PSDOOM_MONSTERS

  if [[ "$MODE" != demo ]]; then
    python3 "$BRIDGE" poll 2>>state/poll.log &
    PIDS+=($!) GAME_PIDS+=($!)
  fi

  export PSDOOMPSCMD="python3 $BRIDGE list"
  export PSDOOM_LABELS="$PWD/state/labels.tsv"  # the record under the crosshair, written by the bridge
  export PSDOOMVAULTCMD="python3 $BRIDGE vault"   # the red door asks Connect AI again, live
  export PSDOOMKILLCMD="python3 $BRIDGE kill"
  export PSDOOMRENICECMD="python3 $BRIDGE hit"

  rm -f state/play.jsonl  # the panel's Claude plays feed is only for this game
  # The agent channel patched into psdoom: Claude's commands in, game state out. It's always
  # open, so Claude plays can be switched on from the Options menu; play.py loop follows the
  # menu's switches and starts or stops Claude.
  export PSDOOM_AGENT_DIR="$PWD/state/agent" PSDOOM_AGENT_CLAUDE=${PLAYS:-0} PSDOOM_AGENT_LOOP=${LOOP:-0}
  python3 play.py loop 2>>state/play.err &
  PIDS+=($!) GAME_PIDS+=($!)

  # Modern FPS controls, as in GZDoom: WASD to move, the mouse to turn, left click or Space to
  # fire, walk into a door (or E, or right click) to open it, Shift to run, P to pause; the arrows turn too. Written
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
  # The mouse turns but doesn't walk; its right button opens doors. While Claude plays (the
  # Options menu switch), the game leaves the mouse free for clicking through Claude's feed.
  # show_endoom 0: Cmd-Q quits at once, with no ENDOOM text screen waiting for a key.
  printf 'novert 1\ngrabmouse 1\nmouseb_use 1\nshow_endoom 0\n' > state/controls-extra.cfg

  GAME_MODE=player; [[ -n "$PLAYS" ]] && GAME_MODE=claude
  printf '{"mode": "%s", "started": %s}\n' "$GAME_MODE" "$(date +%s)" > state/game.json

  # Cmd-Q on the stage ends the raid. psdoom ignores SIGTERM, so it gets SIGKILL.
  if [[ -n "${PSDOOM_STAGE_PID:-}" && -z "${STAGE_WATCHED:-}" ]]; then
    ( while kill -0 "$PSDOOM_STAGE_PID" 2>/dev/null; do /bin/sleep 1; done; pkill -9 -P $$ -x psdoom || true ) &
    PIDS+=($!) STAGE_WATCHED=1
  fi

  # -nopslev: spawn monsters into the three arenas of the level
  # -psallusers: don't filter monsters by Unix user
  # In the background and waited on with 2>/dev/null: Cmd-Q and the question box end the game with
  # SIGKILL, and a foreground job would print bash's "Killed: 9" line in the Terminal.
  "$PSDOOM" -iwad wads/freedoom2.wad -file state/level.wad state/brand.wad -deh state/level.deh \
    -nopslev -psallusers -warp 1 -skill 1 -window -geometry "$GEOMETRY" -config state/controls.cfg -extraconfig state/controls-extra.cfg "$@" \
    >state/game.log 2> >(tee state/psdoom.err | grep --line-buffered -E 'KILL|ERROR' >&2) &
  wait $! 2>/dev/null || true

  # The game is over, or the panel ended it for a new question.
  disown -a 2>/dev/null || true   # as in the EXIT trap: no "Terminated: 15" lines
  kill ${GAME_PIDS[@]+"${GAME_PIDS[@]}"} 2>/dev/null || true
  GAME_PIDS=()
  [[ -f state/ask.json ]] || break
  if [[ -n "${PSDOOM_STAGE_PID:-}" ]] && ! kill -0 "$PSDOOM_STAGE_PID" 2>/dev/null; then break; fi
  rm -f state/game.json   # the panel shows the briefing, not the game, until the next one starts
  new_level
done

python3 "$BRIDGE" score
