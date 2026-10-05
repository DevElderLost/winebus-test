#!/usr/bin/env python3
"""Perbaiki error:
  configure: error: PE cross-compilation is required for ARM64,
  please install clang/llvm-dlltool/lld, or llvm-mingw.

Penyebab: scripts/build-winebus.sh memakai --without-mingw, sehingga daftar
arsitektur PE kosong; di host aarch64 configure Wine menolak itu. Selain itu
clang/lld/llvm belum terpasang di container.

Perubahan (idempotent, tanpa .bak):
  1. scripts/in-container.sh   : pasang clang lld llvm + symlink tanpa versi
  2. scripts/build-winebus.sh  : hapus --without-mingw + preflight PE compiler

Pakai:  python3 fix_winebus_pe.py [path-repo]   (default: direktori saat ini)
"""
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
ok = True


def report(name, status):
    print(f"[{status:<8}] {name}")


def patch(rel, steps):
    """steps: list of (marker, old, new). marker = penanda 'sudah diterapkan'."""
    global ok
    p = ROOT / rel
    if not p.is_file():
        report(rel, "HILANG")
        ok = False
        return
    text = p.read_text(encoding="utf-8")
    orig = text
    for marker, old, new in steps:
        if marker in text:
            continue
        if old not in text:
            report(f"{rel} (pola '{old[:30]}...' tidak ditemukan)", "GAGAL")
            ok = False
            return
        text = text.replace(old, new, 1)
    if text == orig:
        report(rel, "SUDAH")
    else:
        p.write_text(text, encoding="utf-8")
        report(rel, "DIPATCH")


# ---------- 1. in-container.sh ----------
APT_OLD = "libudev-dev libsdl2-dev linux-libc-dev binutils file patch\n"
APT_NEW = (
    "libudev-dev libsdl2-dev linux-libc-dev binutils file patch \\\n"
    "  clang lld llvm\n"
)

LINK_OLD = 'exec bash "$(dirname "${BASH_SOURCE[0]}")/build-winebus.sh"'
LINK_NEW = r'''# configure Wine mencari nama tool TANPA versi; di Ubuntu/Debian namanya ber-versi (-14, -18, dst)
for t in clang lld-link ld.lld llvm-dlltool llvm-ar llvm-ranlib llvm-strip llvm-objdump; do
  command -v "$t" >/dev/null 2>&1 && continue
  p="$(ls /usr/bin/"$t"-[0-9]* /usr/lib/llvm-*/bin/"$t" 2>/dev/null | sort -V | tail -n1 || true)"
  [ -n "$p" ] && ln -sf "$p" "/usr/local/bin/$t"
done
for t in clang lld-link llvm-dlltool; do
  command -v "$t" >/dev/null 2>&1 || { echo "ERROR: $t tidak ditemukan setelah instalasi" >&2; exit 1; }
done
clang --version | head -n1

exec bash "$(dirname "${BASH_SOURCE[0]}")/build-winebus.sh"'''

patch(
    "scripts/in-container.sh",
    [
        ("  clang lld llvm\n", APT_OLD, APT_NEW),
        ("llvm-dlltool llvm-ar", LINK_OLD, LINK_NEW),
    ],
)

# ---------- 2. build-winebus.sh ----------
CFG_OLD = '"$SRC/configure" --without-mingw --disable-tests \\\n'
CFG_NEW = '"$SRC/configure" --disable-tests \\\n'

PRE_OLD = 'log "configure (hanya yang dibutuhkan winebus)"\n'
PRE_NEW = r'''log "Preflight: PE cross-compiler (wajib untuk ARM64)"
for t in clang lld-link llvm-dlltool; do
  command -v "$t" >/dev/null 2>&1 || die "$t tidak ada di PATH (pasang clang lld llvm)"
done
printf 'void *__os_arm64x_dispatch_ret = 0;\nint __cdecl mainCRTStartup(void) { return 0; }\n' > "$WORK/pe-test.c"
clang -target aarch64-windows -fuse-ld=lld -Wl,-subsystem:console --no-default-config -nostdlib -nodefaultlibs \
  "$WORK/pe-test.c" -o "$WORK/pe-test.exe" \
  || die "clang tidak bisa menghasilkan PE aarch64 (-target aarch64-windows -fuse-ld=lld)"
rm -f "$WORK/pe-test.c" "$WORK/pe-test.exe"

log "configure (hanya yang dibutuhkan winebus)"
'''

POST_OLD = 'CFG="$BLD/include/config.h"\n'
POST_NEW = (
    'grep -q "^aarch64_TARGET\\|^PE_ARCHS" "$BLD/Makefile" 2>/dev/null \\\n'
    '  || echo "peringatan: PE_ARCHS tidak terlihat di Makefile, cek $WORK/configure.log"\n'
    'CFG="$BLD/include/config.h"\n'
)

patch(
    "scripts/build-winebus.sh",
    [
        ('configure" --disable-tests', CFG_OLD, CFG_NEW),
        ("Preflight: PE cross-compiler", PRE_OLD, PRE_NEW),
        ("PE_ARCHS tidak terlihat", POST_OLD, POST_NEW),
    ],
)

# ---------- 3. migrasi preflight versi lama (link CRT, gagal: libcmt.lib/oldnames.lib) ----------
OLD_PRE = r'''echo 'int main(void){return 0;}' > "$WORK/pe-test.c"
clang -target aarch64-windows -fuse-ld=lld -Wl,-subsystem:console "$WORK/pe-test.c" -o "$WORK/pe-test.exe" \
  || die "clang tidak bisa menghasilkan PE aarch64 (-target aarch64-windows -fuse-ld=lld)"'''
NEW_PRE = r'''printf 'void *__os_arm64x_dispatch_ret = 0;\nint __cdecl mainCRTStartup(void) { return 0; }\n' > "$WORK/pe-test.c"
clang -target aarch64-windows -fuse-ld=lld -Wl,-subsystem:console --no-default-config -nostdlib -nodefaultlibs \
  "$WORK/pe-test.c" -o "$WORK/pe-test.exe" \
  || die "clang tidak bisa menghasilkan PE aarch64 (-target aarch64-windows -fuse-ld=lld)"'''
patch("scripts/build-winebus.sh", [("--no-default-config -nostdlib", OLD_PRE, NEW_PRE)])

# ---------- verifikasi ----------
print("\n== Verifikasi ==")
b = (ROOT / "scripts/build-winebus.sh")
i = (ROOT / "scripts/in-container.sh")
checks = []
if b.is_file():
    t = b.read_text(encoding="utf-8")
    checks += [
        ("build-winebus.sh: --without-mingw sudah hilang", "--without-mingw --disable-tests" not in t),
        ("build-winebus.sh: preflight PE ada", "Preflight: PE cross-compiler" in t),
        ("build-winebus.sh: preflight pakai -nostdlib", "--no-default-config -nostdlib" in t),
    ]
if i.is_file():
    t = i.read_text(encoding="utf-8")
    checks += [
        ("in-container.sh: clang lld llvm di apt", "clang lld llvm" in t),
        ("in-container.sh: symlink tool ada", "llvm-dlltool llvm-ar" in t),
    ]
for name, cond in checks:
    print(f"[{'OK' if cond else 'GAGAL':<8}] {name}")
    ok = ok and cond

sys.exit(0 if ok else 1)
