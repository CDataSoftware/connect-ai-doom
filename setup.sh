#!/bin/bash
# Build everything connect-doom needs on macOS (Apple Silicon or Intel).
# Homebrew dropped SDL 1.2 mixer/net, so those two are built into vendor/local.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$PWD
L=$ROOT/vendor/local
SRC=$ROOT/vendor/src
LOG=$ROOT/vendor/build.log
mkdir -p "$SRC" "$L" wads state
: > "$LOG"

brew install sdl12-compat pkgconf cmake automake >>"$LOG"
AUTOMAKE_SHARE=$(ls -d "$(brew --prefix)"/share/automake-*/ | head -1)
fresh_config() {  # 2012-era config.sub doesn't know arm64 Macs
  find "$1" -name config.sub -exec cp "$AUTOMAKE_SHARE/config.sub" {} \;
  find "$1" -name config.guess -exec cp "$AUTOMAKE_SHARE/config.guess" {} \;
}

build_sdl_lib() {  # name version configure-args...
  local dir="$1-$2"; shift 2
  [[ -f "$L/lib/lib${dir%%-*}.dylib" ]] && return
  curl -sSL "https://www.libsdl.org/projects/${dir%%-*}/release/$dir.tar.gz" | tar xz -C "$SRC"
  fresh_config "$SRC/$dir"
  (cd "$SRC/$dir" && ./configure --prefix="$L" "$@" && make -j8 && make install) >>"$LOG" 2>&1
}
echo "SDL_net / SDL_mixer 1.2..."
build_sdl_lib SDL_net 1.2.8 --disable-gui
build_sdl_lib SDL_mixer 1.2.12 --disable-music-mod --disable-music-mp3 --disable-music-ogg \
  --disable-music-flac --disable-music-native-midi --disable-music-native-midi-gpl \
  --disable-music-fluidsynth-midi --disable-smpegtest

echo "psdoom-ng..."
if [[ ! -d "$SRC/psdoom-ng" ]]; then
  git clone -q https://github.com/keymon/psdoom-ng.git "$SRC/psdoom-ng"
  git -C "$SRC/psdoom-ng" apply "$ROOT/patches/psdoom-ng-connect-doom.patch"
  fresh_config "$SRC/psdoom-ng/trunk"
fi
(
  cd "$SRC/psdoom-ng/trunk"
  export PKG_CONFIG_PATH=$L/lib/pkgconfig CPPFLAGS="-I$L/include -I$L/include/SDL" LDFLAGS="-L$L/lib"
  export CFLAGS="-O2 -Wno-implicit-function-declaration -Wno-int-conversion -Wno-incompatible-pointer-types -Wno-implicit-int"
  # upstream commits a Makefile configured on the author's Linux box: reconfigure
  [[ -x src/psdoom ]] || { make distclean || true; ./configure --prefix="$L"; }
  make -j8
) >>"$LOG" 2>&1

echo "zdbsp node builder..."
if [[ ! -x "$SRC/zdbsp/build/zdbsp" ]]; then
  [[ -d "$SRC/zdbsp" ]] || git clone -q --depth 1 https://github.com/rheit/zdbsp.git "$SRC/zdbsp"
  (cd "$SRC/zdbsp" && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
     -DCMAKE_C_FLAGS="-Wno-implicit-function-declaration -Wno-int-conversion -include stdlib.h" \
   && cmake --build build -j8) >>"$LOG" 2>&1
fi

echo "Freedoom..."
if [[ ! -f wads/freedoom2.wad ]]; then
  curl -sSL -o vendor/freedoom.zip https://github.com/freedoom/freedoom/releases/download/v0.13.0/freedoom-0.13.0.zip
  unzip -o -q -j vendor/freedoom.zip '*/freedoom2.wad' -d wads
fi
# Homebrew's Python refuses plain pip installs (PEP 668); a --user install leaves Homebrew's files alone.
python3 -c 'import PIL' 2>/dev/null || python3 -m pip install --user pillow >>"$LOG" 2>&1 \
  || python3 -m pip install --user --break-system-packages pillow >>"$LOG"
python3 tools/make_sprites.py wads/freedoom2.wad panel/sprites assets/brand   # the panel's sprites and logos
echo "Done. Try: ./raid.sh --demo"
