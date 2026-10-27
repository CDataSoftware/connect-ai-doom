#!/bin/bash
# Build the browser version into web/dist: the patched psdoom compiled to WebAssembly, the
# offline sample level, and the side panel's full-screen stage with the game on it (web/web.js). Needs ./setup.sh once (psdoom-ng's source, zdbsp,
# Freedoom) and Emscripten (brew install emscripten).
#   web/build.sh          build
#   web/build.sh serve    build, then serve it on http://127.0.0.1:8777
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
DIST=$ROOT/web/dist
SRC=$ROOT/vendor/src/psdoom-web
command -v emcc >/dev/null || { echo "Emscripten is missing: brew install emscripten" >&2; exit 1; }
[[ -d vendor/src/psdoom-ng/.git && -x vendor/src/zdbsp/build/zdbsp && -f wads/freedoom2.wad ]] \
  || { echo "Run ./setup.sh first." >&2; exit 1; }

# psdoom-ng at the pinned commit, with the game's patch and then the web build's.
rm -rf "$SRC" && mkdir -p "$SRC"
git -C vendor/src/psdoom-ng archive 83d02bafe91439117421a0ccc3e238a0b9544e93 trunk | tar x -C "$SRC"
(cd "$SRC" && patch -s -p1 < "$ROOT/patches/psdoom-ng-connect-doom.patch" && patch -s -p1 < "$ROOT/web/psdoom-web.patch")

echo "Compiling psdoom to WebAssembly..."
rm -rf "$DIST" && mkdir -p "$DIST"
(
  cd "$SRC/trunk"
  # The single-player game: no network play, no ENDOOM text screen.
  SRCS="am_map d_items d_iwad d_main d_net doomdef doomstat dstrings f_finale f_wipe g_game hu_lib hu_stuff
    i_main info i_joystick i_scale i_system i_timer i_video i_web m_argv m_bbox m_cheat m_config m_fixed m_menu
    m_misc md5 memio m_random p_ceilng p_doors p_enemy p_floor p_inter p_lights p_map p_maputl p_mobj p_plats
    p_pspr p_saveg p_setup p_sight p_spec p_switch p_telept p_tick p_user pr_process r_bsp r_data r_draw r_main
    r_plane r_segs r_sky r_things sounds s_sound st_lib st_stuff tables v_video wi_stuff w_checksum w_wad w_file
    w_file_stdc z_zone deh_ammo deh_cheat deh_frame deh_io deh_main deh_mapping deh_misc deh_ptr deh_sound
    deh_text deh_thing deh_weapon w_merge i_pcsound i_sdlsound i_sdlmusic i_oplmusic midifile mus2mid"
  FILES=$(for f in $SRCS; do printf 'src/%s.c ' "$f"; done)
  FILES="$FILES opl/opl.c opl/opl_queue.c opl/opl_sdl.c opl/opl_timer.c opl/dbopl.c pcsound/pcsound.c pcsound/pcsound_sdl.c"
  # shellcheck disable=SC2086
  emcc -O2 -I. -Isrc -Iopl -Ipcsound -Itextscreen -DHAVE_CONFIG_H -sUSE_SDL=1 -Wno-everything $FILES \
    -sASYNCIFY -sALLOW_MEMORY_GROWTH -sINVOKE_RUN=0 -sFORCE_FILESYSTEM \
    -sEXPORTED_RUNTIME_METHODS=FS,callMain,ENV -sEXPORTED_FUNCTIONS=_main,_pr_kill -o "$DIST/psdoom.js"
)

echo "The offline level..."
L=$DIST/level
rm -rf "$L" && mkdir -p "$L"
export CONNECT_DOOM_STATE=$L
python3 connect_doom.py poll --demo >/dev/null
python3 tools/make_branding.py wads/freedoom2.wad assets/brand "$L/brand.wad" --mission "$L/mission.json"
python3 tools/make_arena.py "$L/level-raw.wad" --mission "$L/mission.json" --records "$L/cache.json" --brand
vendor/src/zdbsp/build/zdbsp -o "$L/level.wad" "$L/level-raw.wad" >/dev/null
python3 tools/make_deh.py "$L/mission.json" "$L/level.deh"
python3 tools/themes.py monsters "$L/mission.json" > "$L/monsters.txt"
python3 connect_doom.py state > "$L/state.json"   # the panel's state before the first kill
rm -f "$L/level-raw.wad"

# The panel, with web.js ahead of its script: the stage layout, the game, and the panel's /state.json.
cp -R panel/fonts panel/brand panel/sprites panel/favicon* web/web.js web/about.html wads/freedoom2.wad "$DIST/"
python3 - "$DIST/index.html" <<'PY'
import sys
page = open("panel/index.html").read()
assert page.count("\n<script>\n") == 1, "panel/index.html: expected one inline <script>"
open(sys.argv[1], "w").write(page.replace("\n<script>\n", '\n<script src="web.js"></script>\n<script>\n', 1))
PY
echo "Built web/dist."
if [[ "${1:-}" == serve ]]; then
  echo "Open http://127.0.0.1:8777"
  exec python3 -m http.server 8777 --bind 127.0.0.1 -d "$DIST"
fi
