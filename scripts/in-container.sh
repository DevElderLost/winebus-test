#!/usr/bin/env bash
# Dijalankan DI DALAM container arm64 (ubuntu/debian). Memasang dependensi lalu memanggil build-winebus.sh.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
  build-essential git ca-certificates flex bison pkg-config autoconf automake \
  libudev-dev libsdl2-dev linux-libc-dev binutils file patch \
  clang lld llvm

# configure Wine mencari nama tool TANPA versi; di Ubuntu/Debian namanya ber-versi (-14, -18, dst)
for t in clang lld-link ld.lld llvm-dlltool llvm-ar llvm-ranlib llvm-strip llvm-objdump; do
  command -v "$t" >/dev/null 2>&1 && continue
  p="$(ls /usr/bin/"$t"-[0-9]* /usr/lib/llvm-*/bin/"$t" 2>/dev/null | sort -V | tail -n1 || true)"
  [ -n "$p" ] && ln -sf "$p" "/usr/local/bin/$t"
done
for t in clang lld-link llvm-dlltool; do
  command -v "$t" >/dev/null 2>&1 || { echo "ERROR: $t tidak ditemukan setelah instalasi" >&2; exit 1; }
done
clang --version | head -n1

exec bash "$(dirname "${BASH_SOURCE[0]}")/build-winebus.sh"
