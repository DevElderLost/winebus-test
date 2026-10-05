#!/usr/bin/env bash
# Pemakaian: verify-winebus.sh <winebus.so baru> [winebus.so asli dari Winlator]
# Memeriksa bahwa build baru memang membawa UDEV+SDL dan (jika ada pembanding) tidak berubah secara tak terduga.
set -uo pipefail
new="${1:?pakai: $0 <winebus.so> [asli]}"
old="${2:-}"
fail=0
ok()   { printf 'OK    %s\n' "$*"; }
bad()  { printf 'GAGAL %s\n' "$*"; fail=1; }
warn() { printf 'WARN  %s\n' "$*"; }

file "$new"
readelf -h "$new" | grep -q 'AArch64' && ok "ELF aarch64" || bad "bukan ELF aarch64"

if strings -a "$new" | grep -q 'UDEV support not compiled in'; then
  bad "string 'UDEV support not compiled in' masih ada -> UDEV tidak terkompilasi"
else
  ok "UDEV terkompilasi (string stub tidak ada)"
fi
if strings -a "$new" | grep -q 'libudev\.so'; then
  ok "memuat libudev lewat dlopen: $(strings -a "$new" | grep -m1 'libudev\.so')"
else
  bad "soname libudev tidak tertanam"
fi
if strings -a "$new" | grep -q 'libSDL2'; then
  ok "backend SDL ada: $(strings -a "$new" | grep -m1 'libSDL2')"
else
  warn "soname SDL2 tidak ditemukan -> backend SDL mungkin hilang"
fi

echo "NEEDED:"; readelf -d "$new" | awk '/NEEDED/{print "  " $0}'
echo "glibc maksimum yang dibutuhkan: $(objdump -T "$new" | grep -o 'GLIBC_[0-9.]*' | sort -Vu | tail -n1)"

if [ -n "$old" ]; then
  echo "--- perbandingan dengan $old"
  echo "SDL asli:  $(strings -a "$old" | grep -m1 'libSDL2' || echo '-')"
  echo "SDL baru:  $(strings -a "$new" | grep -m1 'libSDL2' || echo '-')"
  echo "UDEV asli: $(strings -a "$old" | grep -m1 -E 'libudev\.so|UDEV support not compiled in' || echo '-')"
  if diff <(nm -D --defined-only "$old" | awk '{print $NF}' | sort) \
          <(nm -D --defined-only "$new" | awk '{print $NF}' | sort) >/dev/null; then
    ok "simbol yang diekspor identik"
  else
    warn "simbol ekspor berbeda: kemungkinan source tidak sama persis dengan Wine di Winlator"
  fi
fi
exit $fail
