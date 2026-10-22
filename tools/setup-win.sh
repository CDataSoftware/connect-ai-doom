#!/bin/bash
# Build the game and the map builder on Windows, inside an MSYS2 shell. setup.ps1 runs this
# with MSYSTEM set (MINGW64 on x64, CLANGARM64 on Arm), so MINGW_PACKAGE_PREFIX and
# MINGW_CHOST name the right packages and compiler target.
#   tools/setup-win.sh --game   only rebuild the game from a changed patch (no downloads)
# The finished programs and the DLLs they need go to vendor/bin, which raid.py runs them from.
set -euo pipefail
set -E
cd "$(dirname "$0")/.."
ROOT=$PWD
SRC=$ROOT/vendor/src
BIN=$ROOT/vendor/bin
LOG=$ROOT/vendor/build.log
mkdir -p "$SRC" "$BIN" wads state
: > "$LOG"

if [[ -z "${MINGW_PACKAGE_PREFIX:-}" ]]; then
  echo "Run this through setup.ps1: it needs an MSYS2 MinGW shell." >&2; exit 1
fi
JOBS=$(nproc 2>/dev/null || echo 4)
GAME_ONLY=""; [[ "${1:-}" == --game ]] && GAME_ONLY=1
STEP="MSYS2 packages"
trap 'echo "Setup failed at: $STEP. The end of vendor/build.log:" >&2; tail -15 "$LOG" >&2' ERR

if [[ -z "$GAME_ONLY" ]]; then
  echo "MSYS2 packages (compiler, SDL 1.2, cmake)..."   # setup.ps1 has just run pacman -Syu
  P=$MINGW_PACKAGE_PREFIX
  pacman -S --noconfirm --needed "$P-toolchain" "$P-SDL" "$P-SDL_mixer" "$P-SDL_net" "$P-cmake" "$P-ninja" \
    git make autoconf automake libtool patch unzip >>"$LOG" 2>&1
fi

# The patches the build carries and this recipe, by checksum (raid.py checks it too): a change
# to either goes onto a clean checkout, and the game is reconfigured and rebuilt.
patch_sum() {
  cat patches/psdoom-ng-connect-doom.patch patches/psdoom-ng-windows.patch tools/setup-win.sh | sha256sum | cut -d' ' -f1
}

echo "psdoom-ng..."; STEP="psdoom-ng"
if [[ ! -d "$SRC/psdoom-ng" ]]; then
  git -c core.autocrlf=false clone -q https://github.com/keymon/psdoom-ng.git "$SRC/psdoom-ng"
  git -C "$SRC/psdoom-ng" config core.autocrlf false
  git -C "$SRC/psdoom-ng" checkout -q 83d02bafe91439117421a0ccc3e238a0b9544e93   # the commit the patches are for
fi
PATCH_SUM=$(patch_sum)
STAMP=$SRC/psdoom-ng/.connect-doom-patch
if [[ "$(cat "$STAMP" 2>/dev/null)" != "$PATCH_SUM" ]]; then
  git -C "$SRC/psdoom-ng" checkout -q -- .
  git -C "$SRC/psdoom-ng" apply --whitespace=nowarn "$ROOT/patches/psdoom-ng-connect-doom.patch"
  git -C "$SRC/psdoom-ng" apply --whitespace=nowarn "$ROOT/patches/psdoom-ng-windows.patch"
  rm -f "$SRC/psdoom-ng/trunk/src/psdoom.exe"   # the checkout put back upstream's Makefile: reconfigure
fi
(
  trap - ERR   # the script's trap reports once, from outside this log-bound subshell
  cd "$SRC/psdoom-ng/trunk"
  # 2012-era config.sub doesn't know Arm Windows
  AM=$(ls -d /usr/share/automake-*/ | tail -1)
  find . -name config.sub -exec cp "$AM/config.sub" {} \;
  find . -name config.guess -exec cp "$AM/config.guess" {} \;
  # gnu11: current clang and GCC default to C23, where true and false are keywords that Doom's
  # boolean enum redefines
  export CFLAGS="-std=gnu11 -O2 -Wno-implicit-function-declaration -Wno-int-conversion -Wno-incompatible-pointer-types -Wno-implicit-int"
  # A console program, not a GUI one (SDL's -mwindows): the bridge commands the game runs with
  # popen and system then share raid.py's console instead of each flashing a window of its own,
  # and the game's stdout and stderr reach raid.py instead of files next to the exe. configure
  # takes SDL's link flags from sdl-config, so it gets one without the flag.
  printf '#!/bin/sh\n"%s/bin/sdl-config" "$@" | sed "s/ -mwindows//g"\n' "$MINGW_PREFIX" > "$ROOT/vendor/sdl-config"
  chmod +x "$ROOT/vendor/sdl-config"
  export SDL_CONFIG=$ROOT/vendor/sdl-config
  # upstream commits a Makefile configured on the author's Linux box: reconfigure
  if [[ ! -x src/psdoom.exe ]]; then
    make distclean || true
    ./configure --host="$MINGW_CHOST"
    # llvm-rc rejects "05-1" in the version numbers; make regenerates resource.rc from the .in file
    sed -i -E 's/^(PRODUCTVERSION|FILEVERSION) .*/\1 2012, 2, 5, 1/' src/resource.rc src/resource.rc.in
  fi
  # just the game and the libraries it links: not setup/, the configuration tool
  for d in wince textscreen opl pcsound; do make -C "$d" -j"$JOBS"; done
  make -C src -j"$JOBS" psdoom.exe
) >>"$LOG" 2>&1
echo "$PATCH_SUM" > "$STAMP"

# The exe and the DLLs it loads (SDL and friends) side by side, so the game runs without MSYS2 on PATH.
# A full setup starts vendor/bin over, so DLLs an older build needed don't linger.
collect() {  # exe
  cp "$1" "$BIN/"
  ldd "$1" | awk '/=>/ {print $3}' | { grep -i "^$MINGW_PREFIX" || true; } | while read -r dll; do cp "$dll" "$BIN/"; done
}
[[ -z "$GAME_ONLY" ]] && rm -f "$BIN"/*.exe "$BIN"/*.dll
collect "$SRC/psdoom-ng/trunk/src/psdoom.exe"
if [[ -n "$GAME_ONLY" ]]; then trap - ERR; echo "Game rebuilt."; exit 0; fi

echo "zdbsp node builder..."; STEP="zdbsp"
if [[ ! -x "$SRC/zdbsp/build/zdbsp.exe" ]]; then
  if [[ ! -d "$SRC/zdbsp" ]]; then
    git -c core.autocrlf=false clone -q https://github.com/rheit/zdbsp.git "$SRC/zdbsp"
    git -C "$SRC/zdbsp" checkout -q f324e34c8bdbf9800d53b18ebd578a51fd7f9998
  fi
  # nodebuild.cpp calls std::swap without including <utility>, blockmapbuilder.cpp std::fill without <algorithm>
  (trap - ERR; cd "$SRC/zdbsp" && cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
     -DCMAKE_C_FLAGS="-Wno-implicit-function-declaration -Wno-int-conversion -include stdlib.h" \
     -DCMAKE_CXX_FLAGS="-include utility -include algorithm" \
   && cmake --build build -j"$JOBS") >>"$LOG" 2>&1
fi
collect "$SRC/zdbsp/build/zdbsp.exe"

echo "Freedoom..."; STEP="Freedoom download"
if [[ ! -f wads/freedoom2.wad ]]; then
  curl -fsSL -o vendor/freedoom.zip https://github.com/freedoom/freedoom/releases/download/v0.13.0/freedoom-0.13.0.zip
  unzip -o -q -j vendor/freedoom.zip '*/freedoom2.wad' -d wads
fi
trap - ERR
echo "Game and map builder built."
