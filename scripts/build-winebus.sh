#!/usr/bin/env bash
# Build HANYA unix lib winebus (winebus.so) dari source Wine, dengan backend UDEV (dan SDL) ikut dikompilasi.
# Harus dijalankan di host aarch64. Variabel:
#   WINE_REPO  URL git source Wine (wajib)
#   WINE_REF   tag/branch/commit (wajib) -> HARUS sama dengan source Wine yang dipakai Winlator
#   JOBS       jumlah job make (default: nproc)
#   ALLOW_NO_SDL=1  izinkan lanjut walau SDL2 tidak terdeteksi (tidak disarankan: backend SDL akan hilang)
set -euo pipefail

: "${WINE_REPO:?set WINE_REPO}"
: "${WINE_REF:?set WINE_REF}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${WORK:-$ROOT/work}"
OUT="${OUT:-$ROOT/out}"
JOBS="${JOBS:-$(nproc)}"
SRC="$WORK/wine-src"
BLD="$WORK/wine-build"

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[ "$(uname -m)" = aarch64 ] || die "harus dijalankan di aarch64 (arm64); host ini: $(uname -m)"

log "Ambil source: $WINE_REPO @ $WINE_REF"
rm -rf "$SRC" "$BLD" "$OUT"
mkdir -p "$SRC" "$BLD" "$OUT"
git -C "$SRC" init -q
git -C "$SRC" remote add origin "$WINE_REPO"
git -C "$SRC" fetch --depth 1 origin "$WINE_REF"
git -C "$SRC" checkout -q FETCH_HEAD
COMMIT="$(git -C "$SRC" rev-parse HEAD)"
[ -f "$SRC/dlls/winebus.sys/bus_udev.c" ] || die "dlls/winebus.sys/bus_udev.c tidak ada di source ini"

shopt -s nullglob
for p in "$ROOT"/patches/*.patch; do
  log "Terapkan patch: $(basename "$p")"
  git -C "$SRC" apply --check "$p" || die "patch tidak cocok dengan source: $p"
  git -C "$SRC" apply "$p"
done
shopt -u nullglob

log "Opsi/env yang dibaca winebus di source ini (acuan untuk registry & env)"
{
  echo "# main.c: opsi registry"
  grep -n -E 'check_bus_option|L"(Disable|Enable)[A-Za-z ]*"' "$SRC/dlls/winebus.sys/main.c" || true
  echo "# getenv di dlls/winebus.sys"
  grep -n -E 'getenv\("' "$SRC"/dlls/winebus.sys/*.c || true
} | tee "$WORK/bus-options.txt"

if [ ! -f "$SRC/configure" ]; then
  log "configure tidak ada di repo (fork Valve men-generate-nya saat build): jalankan autoreconf"
  (
    cd "$SRC"
    autoreconf -f || { autoconf -f && autoheader -f; }
  ) || die "autoreconf gagal; cek log di atas"
  [ -f "$SRC/configure" ] || die "configure masih tidak ada setelah autoreconf"
fi
chmod +x "$SRC/configure"

log "Preflight: PE cross-compiler (wajib untuk ARM64)"
for t in clang lld-link llvm-dlltool; do
  command -v "$t" >/dev/null 2>&1 || die "$t tidak ada di PATH (pasang clang lld llvm)"
done
printf 'void *__os_arm64x_dispatch_ret = 0;\nint __cdecl mainCRTStartup(void) { return 0; }\n' > "$WORK/pe-test.c"
clang -target aarch64-windows -fuse-ld=lld -Wl,-subsystem:console --no-default-config -nostdlib -nodefaultlibs \
  "$WORK/pe-test.c" -o "$WORK/pe-test.exe" \
  || die "clang tidak bisa menghasilkan PE aarch64 (-target aarch64-windows -fuse-ld=lld)"
rm -f "$WORK/pe-test.c" "$WORK/pe-test.exe"

log "configure (hanya yang dibutuhkan winebus)"
cd "$BLD"
"$SRC/configure" --disable-tests \
  --without-x --without-freetype --without-gnutls --without-pulse --without-alsa --without-oss \
  --without-vulkan --without-wayland --without-opengl --without-osmesa --without-cups \
  --without-sane --without-gphoto --without-gstreamer --without-v4l2 --without-usb \
  --without-pcap --without-krb5 --without-netapi --without-capi --without-fontconfig \
  --without-unwind --without-dbus --without-pcsclite --without-ffmpeg --without-opencl \
  2>&1 | tee "$WORK/configure.log"

grep -q "^aarch64_TARGET\|^PE_ARCHS" "$BLD/Makefile" 2>/dev/null \
  || echo "peringatan: PE_ARCHS tidak terlihat di Makefile, cek $WORK/configure.log"
CFG="$BLD/include/config.h"
log "Flag terkait di config.h"
grep -E 'UDEV|SDL|HIDRAW|INOTIFY|LINUX_INPUT' "$CFG" | tee "$WORK/config-flags.txt" || true
grep -qE '^#define [A-Z_]*UDEV[A-Z_]* ' "$CFG" \
  || die "libudev tidak terdeteksi oleh configure. Cek $WORK/configure.log (libudev-dev + pkg-config terpasang?)"
if ! grep -q 'SONAME_LIBSDL2' "$CFG"; then
  [ "${ALLOW_NO_SDL:-0}" = 1 ] || die "SDL2 tidak terdeteksi: build ini akan kehilangan backend SDL (gamepad biasa). Pasang libsdl2-dev atau set ALLOW_NO_SDL=1"
fi

log "Build winebus.so"
if ! make -j"$JOBS" dlls/winebus.sys/all; then
  log "Target tunggal gagal, fallback ke build penuh (lama)"
  make -j"$JOBS"
fi

LIB="$(find "$BLD/dlls/winebus.sys" -maxdepth 1 \( -name winebus.so -o -name winebus.sys.so \) | head -n1)"
[ -n "$LIB" ] || die "winebus.so tidak ditemukan setelah build"
cp "$LIB" "$OUT/winebus.so"
strip --strip-unneeded "$OUT/winebus.so"

log "Bundel libudev.so.1 (+ dependensi non-libc)"
UDEV_LIB="$(ldconfig -p | awk '/libudev\.so\.1 /{print $NF; exit}')"
[ -n "$UDEV_LIB" ] || die "libudev.so.1 tidak ditemukan di container"
cp -L "$UDEV_LIB" "$OUT/libudev.so.1"
ldd "$UDEV_LIB" | awk '/=>/ {print $3}' \
  | grep -vE '/(libc|libm|libdl|libpthread|librt|ld-linux[^/]*)\.so' \
  | while read -r dep; do [ -f "$dep" ] && cp -L "$dep" "$OUT/$(basename "$dep")"; done || true

cp "$WORK/bus-options.txt" "$WORK/config-flags.txt" "$OUT/"
{
  echo "wine_repo:   $WINE_REPO"
  echo "wine_ref:    $WINE_REF"
  echo "wine_commit: $COMMIT"
  echo "built_on:    $(. /etc/os-release && echo "$PRETTY_NAME") / glibc $(ldd --version | head -n1 | awk '{print $NF}')"
  echo "max_glibc_symbol_needed: $(objdump -T "$OUT/winebus.so" | grep -o 'GLIBC_[0-9.]*' | sort -Vu | tail -n1)"
  echo "sha256:"
  (cd "$OUT" && sha256sum winebus.so libudev.so.1)
} | tee "$OUT/BUILDINFO.txt"

log "Selesai: $OUT"
ls -l "$OUT"
