#!/bin/bash
# Build everything connect-doom needs on macOS (Apple Silicon or Intel).
# Homebrew dropped SDL 1.2 mixer/net, so those two are built into vendor/local.
set -euo pipefail
set -E  # the ERR trap below also fires inside functions and subshells
cd "$(dirname "$0")"
ROOT=$PWD
L=$ROOT/vendor/local
SRC=$ROOT/vendor/src
LOG=$ROOT/vendor/build.log
mkdir -p "$SRC" "$L" wads state
: > "$LOG"

# What a fresh Mac might be missing, said plainly instead of as a build error.
if [[ $(uname) != Darwin ]]; then echo "connect-doom builds on macOS only." >&2; exit 1; fi
if ! xcode-select -p >/dev/null 2>&1; then
  echo "Xcode's command line tools are missing: run  xcode-select --install  then ./setup.sh again." >&2; exit 1
fi
if ! command -v brew >/dev/null; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [[ -x $b ]] && eval "$("$b" shellenv)" && break; done
fi
if ! command -v brew >/dev/null; then
  echo "Homebrew is missing: install it from https://brew.sh, then run ./setup.sh again." >&2; exit 1
fi
JOBS=$(sysctl -n hw.ncpu 2>/dev/null || echo 4)
STEP="Homebrew packages"
# Everything below logs to vendor/build.log; on a failure, say which step and show the log's end.
trap 'echo "Setup failed at: $STEP. The end of vendor/build.log:" >&2; tail -15 "$LOG" >&2' ERR

echo "Homebrew packages (sdl12-compat, pkgconf, cmake, automake)..."
HOMEBREW_NO_AUTO_UPDATE=1 brew install sdl12-compat pkgconf cmake automake >>"$LOG" 2>&1
AUTOMAKE_SHARE=$(ls -d "$(brew --prefix)"/share/automake-*/ | head -1)
fresh_config() {  # 2012-era config.sub doesn't know arm64 Macs
  find "$1" -name config.sub -exec cp "$AUTOMAKE_SHARE/config.sub" {} \;
  find "$1" -name config.guess -exec cp "$AUTOMAKE_SHARE/config.guess" {} \;
}

build_sdl_lib() {  # name version configure-args...
  local dir="$1-$2"; shift 2
  [[ -f "$L/lib/lib${dir%%-*}.dylib" ]] && return
  curl -fsSL "https://www.libsdl.org/projects/${dir%%-*}/release/$dir.tar.gz" | tar xz -C "$SRC"
  fresh_config "$SRC/$dir"
  (cd "$SRC/$dir" && ./configure --prefix="$L" "$@" && make -j"$JOBS" && make install) >>"$LOG" 2>&1
}
echo "SDL_net / SDL_mixer 1.2..."; STEP="SDL_net / SDL_mixer"
build_sdl_lib SDL_net 1.2.8 --disable-gui
build_sdl_lib SDL_mixer 1.2.12 --disable-music-mod --disable-music-mp3 --disable-music-ogg \
  --disable-music-flac --disable-music-native-midi --disable-music-native-midi-gpl \
  --disable-music-fluidsynth-midi --disable-smpegtest

echo "psdoom-ng..."; STEP="psdoom-ng"
if [[ ! -d "$SRC/psdoom-ng" ]]; then
  git clone -q https://github.com/keymon/psdoom-ng.git "$SRC/psdoom-ng"
  git -C "$SRC/psdoom-ng" checkout -q 83d02bafe91439117421a0ccc3e238a0b9544e93   # the commit the patch is for
fi
# The patch the build carries, by checksum (raid.sh checks it too): a new or changed patch
# goes onto a clean checkout, and the game is reconfigured and rebuilt.
PATCH_SUM=$(shasum -a 256 patches/psdoom-ng-connect-doom.patch | cut -d' ' -f1)
STAMP=$SRC/psdoom-ng/.connect-doom-patch
if [[ "$(cat "$STAMP" 2>/dev/null)" != "$PATCH_SUM" ]]; then
  git -C "$SRC/psdoom-ng" checkout -q -- .
  git -C "$SRC/psdoom-ng" apply --whitespace=nowarn "$ROOT/patches/psdoom-ng-connect-doom.patch"
  fresh_config "$SRC/psdoom-ng/trunk"
  rm -f "$SRC/psdoom-ng/trunk/src/psdoom"   # the checkout put back upstream's Makefile: reconfigure
fi
(
  cd "$SRC/psdoom-ng/trunk"
  export PKG_CONFIG_PATH=$L/lib/pkgconfig CPPFLAGS="-I$L/include -I$L/include/SDL" LDFLAGS="-L$L/lib"
  export CFLAGS="-O2 -Wno-implicit-function-declaration -Wno-int-conversion -Wno-incompatible-pointer-types -Wno-implicit-int"
  # upstream commits a Makefile configured on the author's Linux box: reconfigure
  [[ -x src/psdoom ]] || { make distclean || true; ./configure --prefix="$L"; }
  make -j"$JOBS"
) >>"$LOG" 2>&1
echo "$PATCH_SUM" > "$STAMP"

echo "zdbsp node builder..."; STEP="zdbsp"
if [[ ! -x "$SRC/zdbsp/build/zdbsp" ]]; then
  if [[ ! -d "$SRC/zdbsp" ]]; then
    git clone -q https://github.com/rheit/zdbsp.git "$SRC/zdbsp"
    git -C "$SRC/zdbsp" checkout -q f324e34c8bdbf9800d53b18ebd578a51fd7f9998
  fi
  (cd "$SRC/zdbsp" && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
     -DCMAKE_C_FLAGS="-Wno-implicit-function-declaration -Wno-int-conversion -include stdlib.h" \
   && cmake --build build -j"$JOBS") >>"$LOG" 2>&1
fi

echo "Freedoom..."; STEP="Freedoom download"
if [[ ! -f wads/freedoom2.wad ]]; then
  curl -fsSL -o vendor/freedoom.zip https://github.com/freedoom/freedoom/releases/download/v0.13.0/freedoom-0.13.0.zip
  unzip -o -q -j vendor/freedoom.zip '*/freedoom2.wad' -d wads
fi
STEP="Pillow (Python imaging)"
# Homebrew's Python refuses plain pip installs (PEP 668); a --user install leaves Homebrew's files alone.
python3 -c 'import PIL' 2>/dev/null || python3 -m pip install --user pillow >>"$LOG" 2>&1 \
  || python3 -m pip install --user --break-system-packages pillow >>"$LOG"
STEP="panel sprites"
python3 tools/make_sprites.py wads/freedoom2.wad panel/sprites assets/brand   # the panel's sprites and logos
trap - ERR
echo "Done. Check this Mac with ./raid.sh doctor. Next: ./raid.sh login, then ./raid.sh new (or ./raid.sh offline to try it without an account)"
