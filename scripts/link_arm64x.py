#!/usr/bin/env python3
"""Tautkan DLL PE berformat ARM64X (ARM64 native + ARM64EC + thunk X64 dalam SATU file), sama seperti
dll bawaan Wine/Winlator di folder aarch64-windows.

Wine sendiri hanya membuat dua DLL terpisah per modul:
  dlls/<m>/aarch64-windows/<m>.dll  -> ARM64 murni   (Machine 0xAA64)
  dlls/<m>/arm64ec-windows/<m>.dll  -> ARM64EC murni (CHPE metadata, tanpa kode ARM64 native)
Winlator memuat <wine>/lib/wine/aarch64-windows/<m>.dll dan mengharapkan ARM64X (Machine 0xA64E, CodeMap
ARM64 + ARM64EC + X64, section .a64xrm/.hexpthk). Script ini mengambil input link kedua arsitektur lalu
memanggil lld-link /machine:arm64x. Butuh LLVM >= 20 (lld 19 gagal me-resolve arsip ARM64EC).

Pemakaian (dari folder wine-build, setelah semua target <arch>-windows terbuild):
  python3 scripts/link_arm64x.py --dll setupapi --out out/aarch64-windows/setupapi.dll [--build-dir wine-build]
"""
import argparse
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

# arsitektur: (nama folder, nama target clang untuk objek pembantu)
ARCHS = ("aarch64", "arm64ec")


def run(cmd, cwd, check=True, shell=False):
    p = subprocess.run(cmd, cwd=cwd, shell=shell, capture_output=True, text=True)
    if check and p.returncode:
        sys.stderr.write(f"[GAGAL] {cmd if shell else ' '.join(cmd)}\n{p.stdout[-1500:]}\n{p.stderr[-3000:]}\n")
        sys.exit(1)
    return p


def capture_link(build, dll, arch, moddir, ext):
    """Jalankan ulang link winegcc dengan -v untuk membaca perintah winebuild + clang yang sebenarnya dipakai."""
    target = f"dlls/{moddir}/{arch}-windows/{dll}.{ext}"
    out = build / target
    run(["make", "-j2", target], build)  # pastikan objek + import lib dependensi sudah ada
    if out.exists():
        out.unlink()  # paksa make mencetak rule link
    dry = run(["make", "-n", target], build).stdout
    m = re.search(r"^(tools/winegcc/winegcc -o .*?)(?=^\S|\Z)", dry, re.S | re.M)
    if not m:
        sys.exit(f"[GAGAL] rule link {target} tidak ditemukan di make -n")
    cmd = m.group(1).replace("\\\n", " ").replace("winegcc -o", "winegcc -v -o", 1)
    p = run(cmd, build, shell=True)  # sekaligus membangun ulang DLL arsitektur itu
    log = (p.stdout + "\n" + p.stderr).split("\n")
    wb = next(l for l in log if l.startswith("./tools/winebuild/winebuild") and " --dll " in l)
    link = next(l for l in log if "clang" in l and "-shared" in l and "-implib" in l)
    return wb, shlex.split(link)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dll", required=True, help="nama modul tanpa ekstensi (setupapi, cfgmgr32, hidclass)")
    ap.add_argument("--moddir", help="folder modul di dlls/ bila beda dari nama (mis. hidclass.sys)")
    ap.add_argument("--ext", default="dll", help="ekstensi modul (dll atau sys)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--build-dir", default=".")
    ap.add_argument("--keep-debug", action="store_true", help="jangan strip debug DWARF")
    a = ap.parse_args()
    build = Path(a.build_dir).resolve()
    out = Path(a.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="arm64x-"))

    inputs = {}
    for arch in ARCHS:
        wb, link = capture_link(build, a.dll, arch, a.moddir or a.dll, a.ext)
        spec_o = tmp / f"spec_{arch}.o"
        wb = re.sub(r"-o \S+", f"-o {spec_o}", wb, count=1)
        if arch == "arm64ec":
            wb = re.sub(r" -r\S+", "", wb)  # resource cukup sekali (sudah ada di sisi aarch64); lld menolak dua obj resource
        run(wb, build, shell=True)
        objs = [x for x in link if x.endswith(".o") and not Path(x).parts[0].startswith("tmp")]
        libs = [x for x in link if x.endswith(".a")]
        delay = [x[4:] for x in link if x.startswith("-Wl,-delayload")]
        entry = next((x[4:] for x in link if x.startswith("-Wl,-entry:")), "-entry:DllMainCRTStartup")
        subsys = next((x[4:] for x in link if x.startswith("-Wl,-subsystem:")), "-subsystem:console")
        inputs[arch] = dict(spec=str(spec_o), objs=objs, libs=libs, delay=delay, entry=entry, subsys=subsys)
        print(f"[{arch}] {len(objs)} objek, {len(libs)} lib, delayload={len(delay)}")

    # lld ARM64X butuh _load_config_used versi native (versi ARM64EC dari libwinecrt0); CHPE metadata diisi lld.
    s = tmp / "loadcfg_native.s"
    s.write_text('\t.section .rdata,"dr"\n\t.globl _load_config_used\n\t.balign 8\n_load_config_used:\n'
                 "\t.word 0x140\n\t.fill 0x13c, 1, 0\n")
    loadcfg = tmp / "loadcfg_native.o"
    run(["clang", "-target", "aarch64-windows", "--no-default-config", "-c", str(s), "-o", str(loadcfg)], build)

    n, e = inputs["aarch64"], inputs["arm64ec"]
    cmd = ["lld-link", "-machine:arm64x", "-dll", f"-out:{out}", f"-implib:{tmp / (a.dll + '.lib')}",
           "-filealign:0x1000", "-kill-at", "-nodefaultlib", n["subsys"], "-debug:dwarf", n["entry"],
           n["spec"], str(loadcfg), *n["objs"], e["spec"], *e["objs"], *n["libs"], *e["libs"], *n["delay"]]
    run(cmd, build)
    if not a.keep_debug:
        run(["llvm-strip", "--strip-debug", str(out)], build)
    # tanda "Wine builtin DLL" (winegcc --wine-builtin melakukan hal yang sama untuk DLL biasa)
    run(["tools/winebuild/winebuild", "--target", "aarch64-windows", "--builtin", str(out)], build)

    hdr = run(["llvm-readobj", "--file-headers", str(out)], build).stdout
    mach = re.search(r"Machine: (IMAGE_FILE_MACHINE_\w+)", hdr).group(1)
    print(f"[OK] {out} ({out.stat().st_size} byte) Machine={mach}")
    if mach != "IMAGE_FILE_MACHINE_ARM64X":
        sys.exit(f"[GAGAL] hasil bukan ARM64X ({mach})")


if __name__ == "__main__":
    main()
