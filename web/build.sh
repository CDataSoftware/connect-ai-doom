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
    -sEXPORTED_RUNTIME_METHODS=FS,callMain,ENV,HEAP32,stringToNewUTF8 -sEXPORTED_FUNCTIONS=_main,_pr_kill,_web_keys,_web_player,_web_set_models,_malloc,_free -o "$DIST/psdoom.js"
)

# The level: web/level's snapshot of a real one (web/snapshot.py) played on its saved records,
# with its briefing log, leaderboard and Claude's lines; without one, the offline sample.
L=$DIST/level
rm -rf "$L" && mkdir -p "$L"
export CONNECT_DOOM_STATE=$L
if [[ -f web/level/mission.json ]]; then
  echo "The level in web/level..."
  cp web/level/* "$L/"
  python3 -c 'import connect_doom as c; c.restore_saved(c.read_json(c.MISSION), "snapshot") or exit("web/level: no saved records")'
else
  echo "The offline level..."
  python3 connect_doom.py poll --demo >/dev/null
  echo '{}' > "$L/said.json"
fi
python3 tools/make_branding.py wads/freedoom2.wad assets/brand "$L/brand.wad" --mission "$L/mission.json"
python3 tools/make_arena.py "$L/level-raw.wad" --mission "$L/mission.json" --records "$L/cache.json" --brand
vendor/src/zdbsp/build/zdbsp -o "$L/level.wad" "$L/level-raw.wad" >/dev/null
python3 tools/make_deh.py "$L/mission.json" "$L/level.deh" --no-gore   # no blood: records vanish in teleport fog
python3 tools/themes.py monsters "$L/mission.json" > "$L/monsters.txt"
python3 connect_doom.py state > "$L/state.json"   # the panel's state before the first kill
rm -f "$L/level-raw.wad" "$L/saved-records.json" "$L/run.jsonl" "$L/leaderboard.jsonl"

# The panel, with web.js ahead of its script: the stage layout, the game, and the panel's /state.json.
cp -R panel/fonts panel/brand panel/sprites panel/favicon* web/web.js web/replay.js web/ai.js web/og.jpg "$DIST/"
# Freedoom without the maps the game never loads, gzipped: Pages serves it as is, and the page unzips it
python3 web/trim_wad.py wads/freedoom2.wad "$DIST/freedoom2.wad"
gzip -9 -n "$DIST/freedoom2.wad"
# What the site distributes comes with its licenses (licenses.html)
mkdir -p "$DIST/licenses"
cp web/licenses/freedoom.txt "$DIST/licenses/"
cp LICENSE "$DIST/licenses/connect-ai-doom.txt"
cp "$SRC/trunk/COPYING" "$DIST/licenses/gpl-2.0.txt"
# The site's pages, each with Plausible analytics (the site's only; the booth's local panel has none)
python3 - "$DIST" <<'PY'
import sys
analytics = """<script async src="https://plausible.joekarlsson.io/js/pa-CMybn7KnTCbpVs_M_GdO7.js"></script>
<script>
  window.plausible=window.plausible||function(){(plausible.q=plausible.q||[]).push(arguments)},plausible.init=plausible.init||function(i){plausible.o=i||{}};
  plausible.init()
</script>"""
def write_with_analytics(name, page):
    assert page.count("</title>") == 1, f"{name}: expected one <title>"
    open(f"{sys.argv[1]}/{name}", "w").write(page.replace("</title>", "</title>\n" + analytics, 1))
for name in ("about.html", "licenses.html"):
    write_with_analytics(name, open(f"web/{name}").read())
page = open("panel/index.html").read()
assert page.count("\n<script>\n") == 1, "panel/index.html: expected one inline <script>"
assert page.count("<title>Connect AI × DOOM</title>") == 1, "panel/index.html: expected its <title>"
page = page.replace("<title>Connect AI × DOOM</title>", "<title>Connect AI × DOOM (DooMCP)</title>", 1)
page = page.replace("\n<script>\n", '\n<script src="replay.js"></script>\n<script src="ai.js"></script>\n<script src="web.js"></script>\n<script>\n', 1)
# what a link to it shows in search results and chat apps
meta = """<meta name="description" content="Play a Doom level built from business data, read by Claude through the CData Connect AI MCP server. Every monster is a record.">
<meta property="og:type" content="website">
<meta property="og:title" content="Connect AI × DOOM (DooMCP)">
<meta property="og:description" content="Every monster is a record. Play a Doom level Claude built from business data through the CData Connect AI MCP server, or watch Claude play it.">
<meta property="og:image" content="og.jpg">
<meta name="twitter:card" content="summary_large_image">"""
write_with_analytics("index.html", page.replace("</head>", meta + "\n</head>", 1))
PY
echo "Built web/dist."
if [[ "${1:-}" == serve ]]; then
  echo "Open http://127.0.0.1:8777"
  exec python3 -m http.server 8777 --bind 127.0.0.1 -d "$DIST"
fi
