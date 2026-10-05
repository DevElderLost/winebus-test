#!/usr/bin/env python3
# Verifikasi statis winebus.so hasil build NDK (tanpa menjalankan apa pun).
# Pemakaian: python3 scripts/verify_winebus_ndk.py out/winebus.so [winebus.so-asli] [--host-test]
#   --host-test : lewati cek aarch64/bionic (hanya untuk uji logika di Linux biasa)
import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

EXPECTED_EXPORTS = {"__wine_unix_call_funcs"}
EXPECTED_NTDLL = {"__wine_dbg_get_channel_flags", "__wine_dbg_header", "__wine_dbg_output",
                  "__wine_dbg_strdup", "ntdll_umbstowcs"}
NTDLL_LIKE = re.compile(r"^(__wine_dbg_[a-z_]+|ntdll_\w+|Nt[A-Z]\w*|Rtl[A-Z]\w*|Zw[A-Z]\w*)$")
BIONIC_NEEDED_OK = {"ntdll.so", "libdl.so", "libc.so", "libm.so"}

fail = 0


def ok(m):
    print(f"OK    {m}")


def bad(m):
    global fail
    fail = 1
    print(f"GAGAL {m}")


def warn(m):
    print(f"WARN  {m}")


def tool(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    sys.exit("ERROR: tidak ada " + "/".join(names))


READELF = tool("llvm-readelf", "readelf")
NM = tool("llvm-nm", "nm")


def sh(cmd):
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def info(path):
    dyn = sh([READELF, "-d", path])
    hdr = sh([READELF, "-h", path])
    raw = Path(path).read_bytes()
    exports, undef = set(), set()
    for line in sh([NM, "-D", "--defined-only", path]).splitlines():
        p = line.split()
        if p:
            exports.add(p[-1].split("@")[0])
    for line in sh([NM, "-D", "-u", path]).splitlines():
        p = line.split()
        if len(p) == 2 and p[0] == "U":
            undef.add(p[1].split("@")[0])
    table = None
    for line in sh([NM, "-D", "-S", path]).splitlines():
        p = line.split()
        if len(p) >= 4 and p[-1] == "__wine_unix_call_funcs":
            table = int(p[1], 16)
    return {
        "machine": re.search(r"Machine:\s+(.+)", hdr).group(1).strip() if "Machine" in hdr else "?",
        "soname": (re.search(r"Library soname: \[(.+?)\]", dyn) or [None, None])[1],
        "needed": re.findall(r"Shared library: \[(.+?)\]", dyn),
        "runpath": (re.search(r"Library (?:runpath|rpath): \[(.+?)\]", dyn) or [None, None])[1],
        "exports": exports, "undef": undef, "table": table, "raw": raw,
    }


def sdl_name(raw):
    m = re.search(rb"libSDL2[-\w.]*\.so[\w.]*", raw)
    return m.group(0).decode() if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("new")
    ap.add_argument("orig", nargs="?")
    ap.add_argument("--host-test", action="store_true")
    a = ap.parse_args()

    n = info(a.new)
    print(f"file: {a.new}  mesin: {n['machine']}")

    if a.host_test:
        warn("--host-test: cek aarch64/bionic dilewati")
    elif "AArch64" in n["machine"]:
        ok("ELF aarch64")
    else:
        bad(f"bukan ELF aarch64 ({n['machine']})")

    ok("SONAME winebus.so") if n["soname"] == "winebus.so" else bad(f"SONAME salah: {n['soname']}")

    print("NEEDED: " + " ".join(n["needed"]))
    ok("NEEDED memuat ntdll.so") if "ntdll.so" in n["needed"] else bad("NEEDED tidak memuat ntdll.so (stub terbuang?)")
    if not a.host_test:
        extra = [x for x in n["needed"] if x not in BIONIC_NEEDED_OK]
        if extra:
            bad("NEEDED bukan bionic/ntdll: " + " ".join(extra) + "  (libc.so.6 / libudev.so.1 = build glibc, tidak bisa dimuat di Android)")
        else:
            ok("semua NEEDED valid untuk bionic")
        if b"GLIBC_" in n["raw"]:
            bad("ada versi simbol GLIBC_* (build glibc)")
        else:
            ok("tidak ada simbol GLIBC_*")

    if n["runpath"]:
        ok(f"RUNPATH: {n['runpath']}")
    else:
        warn("tidak ada RUNPATH")

    if n["exports"] == EXPECTED_EXPORTS:
        ok("ekspor hanya __wine_unix_call_funcs")
    else:
        bad("ekspor tidak sesuai: " + " ".join(sorted(n["exports"] ^ EXPECTED_EXPORTS)))

    if n["table"] == 0x88:
        ok("tabel __wine_unix_call_funcs = 17 fungsi (0x88 byte)")
    else:
        bad(f"ukuran tabel panggilan {n['table']!r}, diharapkan 0x88 (beda versi source Wine?)")

    ntdll_imp = {s for s in n["undef"] if NTDLL_LIKE.match(s)}
    if ntdll_imp == EXPECTED_NTDLL:
        ok("impor ntdll = 5 simbol yang sama dengan build asli")
    else:
        bad("impor ntdll berbeda: " + " ".join(sorted(ntdll_imp ^ EXPECTED_NTDLL)))
    if any(s.startswith("udev_") for s in n["undef"]):
        bad("masih ada simbol udev_* tak terselesaikan (harus statis)")
    else:
        ok("libudev ter-link statis (tidak ada udev_* tak terselesaikan)")

    raw = n["raw"]
    if b"UDEV support not compiled in" in raw:
        bad("string 'UDEV support not compiled in' ada -> backend udev tidak terkompilasi")
    else:
        ok("backend UDEV terkompilasi")
    if b"hidraw" in raw.lower():
        ok("jalur hidraw ada")
    else:
        warn("string hidraw tidak ditemukan")
    sdl = sdl_name(raw)
    ok(f"backend SDL: dlopen('{sdl}')") if sdl else bad("soname SDL2 tidak tertanam -> backend SDL hilang")

    if a.orig:
        o = info(a.orig)
        print(f"--- perbandingan dengan asli: {a.orig}")
        print(f"asli  NEEDED: {' '.join(o['needed'])} | RUNPATH: {o['runpath']} | SDL: {sdl_name(o['raw'])}")
        print(f"baru  NEEDED: {' '.join(n['needed'])} | RUNPATH: {n['runpath']} | SDL: {sdl}")
        ok("ekspor identik dengan asli") if o["exports"] == n["exports"] else bad("ekspor beda dari asli")
        ok("ukuran tabel panggilan sama dengan asli") if o["table"] == n["table"] else bad(
            f"tabel panggilan beda: asli {o['table']} baru {n['table']} -> source Wine tidak cocok")
        o_imp = {s for s in o["undef"] if NTDLL_LIKE.match(s)}
        ok("impor ntdll identik dengan asli") if o_imp == ntdll_imp else bad(
            "impor ntdll beda dari asli: " + " ".join(sorted(o_imp ^ ntdll_imp)))
        if o["runpath"] != n["runpath"]:
            warn("RUNPATH beda dari asli (set --runpath saat build bila perlu)")
        if sdl_name(o["raw"]) != sdl:
            warn("soname SDL beda dari asli (set sdl_soname saat build bila perlu)")
    sys.exit(fail)


if __name__ == "__main__":
    main()
