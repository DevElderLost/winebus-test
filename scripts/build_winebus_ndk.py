#!/usr/bin/env python3
# Build winebus.so (unix lib) untuk Wine arm64ec di Winlator (Android bionic).
#
# Kenapa bukan container Ubuntu: Wine di Winlator berjalan di bionic (linker Android).
# Binary glibc (NEEDED libc.so.6 / libudev.so.1) tidak bisa di-dlopen di sana
# -> winebus gagal dimuat (STATUS_DLL_NOT_FOUND, c0000135).
#
# Pendekatan: tanpa ./configure. Hanya 5 file C milik dlls/winebus.sys yang dikompilasi
# langsung dengan clang dari Android NDK, dengan config.h minimal. libudev diganti
# libudev-zero yang di-link STATIS (Android tidak punya libudev). Simbol ntdll diambil
# dari stub ber-soname ntdll.so supaya NEEDED sama seperti build asli.
#
# Pemakaian:  python3 scripts/build_winebus_ndk.py [--out out] [--work work]
#   --host-test : uji logika build di Linux biasa (clang + glibc), BUKAN untuk Winlator.
import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

WINE_UNIX_SOURCES = ["bus_iohid", "bus_sdl", "bus_udev", "hid", "unixlib"]
UDEV_SOURCES = ["udev", "udev_list", "udev_device", "udev_monitor", "udev_enumerate"]
# simbol yang boleh datang dari ntdll.so (sisanya HARUS ada di libc/libdl/libm bionic)
NTDLL_OK = re.compile(r"^(__wine_dbg_[a-z_]+|ntdll_[A-Za-z0-9_]+|Nt[A-Z]\w*|Rtl[A-Z]\w*|Zw[A-Z]\w*)$")


def log(msg):
    print(f"==> {msg}", flush=True)


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def run(cmd, **kw):
    print("+ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def capture(cmd):
    return subprocess.run([str(c) for c in cmd], check=True, capture_output=True, text=True).stdout


def download(url, tries=3):
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    die(f"gagal mengunduh {url}: {last}")


def extract_tar(data, dest, only_subdir=None):
    # buang komponen path pertama; opsional hanya subfolder tertentu; file biasa saja
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf:
            parts = Path(m.name).parts[1:]
            if not parts or ".." in parts or not m.isfile():
                continue
            if only_subdir and parts[0] != only_subdir:
                continue
            out = dest.joinpath(*parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(tf.extractfile(m).read())
            n += 1
    return n


def fetch_wine(repo, ref, dest):
    dest.mkdir(parents=True)
    git = ["git", "-C", dest]
    run(["git", "init", "-q", dest])
    run(git + ["remote", "add", "origin", repo])
    run(git + ["sparse-checkout", "init", "--cone"])
    run(git + ["sparse-checkout", "set", "include", "dlls/winebus.sys"])
    try:
        run(git + ["fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", ref])
    except subprocess.CalledProcessError:
        log("fetch parsial gagal, ulangi tanpa --filter")
        run(git + ["fetch", "-q", "--depth", "1", "origin", ref])
    run(git + ["checkout", "-q", "FETCH_HEAD"])
    commit = capture(git + ["rev-parse", "HEAD"]).strip()
    for need in ("dlls/winebus.sys/unixlib.c", "include/wine/unixlib.h", "include/ntstatus.h"):
        if not (dest / need).is_file():
            die(f"source Wine tidak lengkap, file tidak ada: {need}")
    return commit


class Toolchain:
    def __init__(self, host_test, api, ndk_dir):
        self.host_test = host_test
        if host_test:
            self.cc = ["clang"]
            self.cflags = []
            self.ldflags = ["-fuse-ld=lld"]
            self.extra_link = ["-ldl"]
            self.nm = shutil.which("llvm-nm") or "nm"
            self.readelf = shutil.which("llvm-readelf") or "readelf"
            self.libs = [p for pat in ("libc.so.*", "libdl.so.*", "libm.so.*", "libpthread.so.*")
                         for p in Path("/lib/x86_64-linux-gnu").glob(pat)]
            self.ndk_rev = "host-test"
            return
        if not ndk_dir:
            die("Android NDK tidak ditemukan: set ANDROID_NDK_DIR / ANDROID_NDK_LATEST_HOME / ANDROID_NDK_HOME")
        tc = Path(ndk_dir) / "toolchains/llvm/prebuilt/linux-x86_64"
        cc = tc / "bin" / f"aarch64-linux-android{api}-clang"
        if not cc.is_file():
            die(f"clang NDK tidak ada: {cc}")
        self.cc = [str(cc)]
        self.cflags = []
        self.ldflags = []
        self.extra_link = ["-ldl"]
        self.nm = str(tc / "bin/llvm-nm")
        self.readelf = str(tc / "bin/llvm-readelf")
        libdir = tc / "sysroot/usr/lib/aarch64-linux-android" / str(api)
        self.libs = [libdir / f"lib{n}.so" for n in ("c", "dl", "m")]
        missing = [str(p) for p in self.libs if not p.is_file()]
        if missing:
            die("stub libc NDK tidak ditemukan: " + ", ".join(missing))
        self.ndk_rev = "?"
        props = Path(ndk_dir) / "source.properties"
        if props.is_file():
            m = re.search(r"Pkg\.Revision\s*=\s*(\S+)", props.read_text())
            if m:
                self.ndk_rev = m.group(1)

    def undefined(self, path):
        names = set()
        for line in capture([self.nm, "-D", "-u", path]).splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "U":  # abaikan weak (w/v)
                names.add(parts[1].split("@")[0])
        return names

    def libc_exports(self):
        names = set()
        for lib in self.libs:
            for line in capture([self.nm, "-D", "--defined-only", lib]).splitlines():
                parts = line.split()
                if parts:
                    names.add(parts[-1].split("@")[0])
        return names


def main():
    ap = argparse.ArgumentParser()
    e = os.environ.get
    ap.add_argument("--out", default="out")
    ap.add_argument("--work", default="work")
    ap.add_argument("--wine-repo", default=e("WINE_REPO") or "https://github.com/ValveSoftware/wine.git")
    ap.add_argument("--wine-ref", default=e("WINE_REF") or "proton_9.0")
    ap.add_argument("--android-api", default=e("ANDROID_API") or "28")
    ap.add_argument("--sdl-ref", default=e("SDL_REF") or "release-2.30.9")
    ap.add_argument("--sdl-soname", default=e("SDL_SONAME") or "libSDL2-2.0.so")
    ap.add_argument("--udev-ref", default=e("UDEV_REF") or "1.0.3")
    ap.add_argument("--runpath", default=e("WINEBUS_RUNPATH") or "/data/data/com.winlator.cmod/files/imagefs/usr/lib")
    ap.add_argument("--ndk", default=e("ANDROID_NDK_DIR") or e("ANDROID_NDK_LATEST_HOME") or e("ANDROID_NDK_HOME")
                    or e("ANDROID_NDK_ROOT") or "")
    ap.add_argument("--host-test", action="store_true")
    a = ap.parse_args()

    work, out = Path(a.work).resolve(), Path(a.out).resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    out.mkdir(parents=True, exist_ok=True)
    tc = Toolchain(a.host_test, a.android_api, a.ndk)
    log(f"toolchain: {' '.join(tc.cc)} (NDK {tc.ndk_rev}, API {a.android_api})")
    run(tc.cc + ["--version"])

    log(f"Ambil source Wine (sparse): {a.wine_repo} @ {a.wine_ref}")
    wine = work / "wine"
    commit = fetch_wine(a.wine_repo, a.wine_ref, wine)
    log(f"commit Wine: {commit}")

    log(f"Ambil header SDL2 ({a.sdl_ref}) dan libudev-zero ({a.udev_ref})")
    n = extract_tar(download(f"https://codeload.github.com/libsdl-org/SDL/tar.gz/{a.sdl_ref}"),
                    work / "sdl", only_subdir="include")
    if n < 10 or not (work / "sdl/include/SDL.h").is_file():
        die("header SDL2 tidak lengkap")
    n = extract_tar(download(f"https://codeload.github.com/illiliti/libudev-zero/tar.gz/{a.udev_ref}"), work / "udevz")
    if not (work / "udevz/udev.h").is_file():
        die("libudev-zero tidak lengkap")

    # config.h minimal: hanya makro yang dibaca winebus (bus_udev.c, bus_sdl.c)
    cfg = work / "cfg"
    cfg.mkdir()
    config_h = (
        "#define HAVE_LIBUDEV_H 1\n"
        "#define HAVE_UDEV 1\n"
        "#define HAVE_LINUX_HIDRAW_H 1\n"
        "#define HAVE_LINUX_INPUT_H 1\n"
        "#define HAVE_SYS_INOTIFY_H 1\n"
        "#define HAVE_SDL_H 1\n"
        f'#define SONAME_LIBSDL2 "{a.sdl_soname}"\n'
    )
    (cfg / "config.h").write_text(config_h)
    inc = work / "inc"
    inc.mkdir()
    shutil.copy(work / "udevz/udev.h", inc / "libudev.h")

    obj = work / "obj"
    obj.mkdir()
    log("Kompilasi libudev-zero (statis)")
    zflags = ["-std=c99", "-fPIC", "-D_XOPEN_SOURCE=700", "-fvisibility=hidden", "-O2", "-w"]
    zobjs = []
    for s in UDEV_SOURCES:
        o = obj / f"z_{s}.o"
        run(tc.cc + tc.cflags + zflags + ["-c", work / "udevz" / f"{s}.c", "-o", o])
        zobjs.append(o)
    libudev_a = obj / "libudev.a"
    ar = shutil.which("llvm-ar") or str(Path(tc.nm).with_name("llvm-ar"))
    if not Path(ar).exists() and not shutil.which(ar):
        ar = "ar"
    run([ar, "rc", libudev_a] + zobjs)

    log("Kompilasi unix lib winebus")
    wsrc = wine / "dlls/winebus.sys"
    cflags = ["-D__WINESRC__", "-D_UCRT", "-DWINE_UNIX_LIB", "-D_REENTRANT", "-fPIC", "-O2",
              "-fvisibility=hidden", "-fno-stack-protector", "-fno-strict-aliasing",
              "-U_FORTIFY_SOURCE", "-D_FORTIFY_SOURCE=0", "-w",
              f"-I{cfg}", f"-I{wsrc}", f"-I{wine / 'include'}", f"-I{work / 'sdl/include'}", f"-I{inc}"]
    objs = []
    for s in WINE_UNIX_SOURCES:
        o = obj / f"{s}.o"
        run(tc.cc + tc.cflags + cflags + ["-c", wsrc / f"{s}.c", "-o", o])
        objs.append(o)

    common = tc.cc + tc.ldflags + ["-shared", "-Wl,-Bsymbolic", "-Wl,-soname,winebus.so",
                                   "-Wl,--exclude-libs,ALL", "-Wl,-z,max-page-size=16384",
                                   f"-Wl,-rpath,{a.runpath}", "-Wl,--enable-new-dtags"]

    log("Link tahap 1 (tanpa ntdll) untuk menghitung simbol yang harus datang dari ntdll.so")
    probe = obj / "winebus_probe.so"
    run(common + objs + [libudev_a] + tc.extra_link + ["-o", probe])
    undefined = tc.undefined(probe)
    libc = tc.libc_exports()
    from_ntdll = sorted(s for s in undefined if s not in libc)
    bad = [s for s in from_ntdll if not NTDLL_OK.match(s)]
    if bad:
        die("simbol tak terselesaikan yang BUKAN dari ntdll (tidak ada di libc/libdl/libm bionic): "
            + ", ".join(bad))
    if any(s.startswith("udev_") for s in undefined):
        die("masih ada simbol udev_* tak terselesaikan (libudev-zero tidak ter-link statis)")
    log("impor dari ntdll.so: " + " ".join(from_ntdll))

    stub_c = obj / "ntdll_stub.c"
    stub_c.write_text("".join(f"void {s}(void) {{}}\n" for s in from_ntdll))
    stub_so = obj / "ntdll.so"
    run(tc.cc + tc.ldflags + ["-shared", "-fPIC", "-nostdlib", "-Wl,-soname,ntdll.so", stub_c, "-o", stub_so])

    log("Link akhir (-z defs: simbol tak terselesaikan = error)")
    final = obj / "winebus.so"
    run(common + ["-Wl,-z,defs"] + objs + [libudev_a, stub_so] + tc.extra_link + ["-o", final])

    shutil.copy(final, out / "winebus.so")
    sha = hashlib.sha256((out / "winebus.so").read_bytes()).hexdigest()
    dyn = capture([tc.readelf, "-d", out / "winebus.so"])
    needed = re.findall(r"Shared library: \[(.+?)\]", dyn)

    opts = sorted(set(re.findall(r'check_bus_option\(L"([^"]+)"', (wsrc / "main.c").read_text()
                                 + (wsrc / "bus_sdl.c").read_text() + (wsrc / "bus_udev.c").read_text())))
    (out / "bus-options.txt").write_text("\n".join(opts) + "\n")
    (out / "config-flags.txt").write_text(
        "# config.h minimal\n" + config_h + "\n# CFLAGS\n" + " ".join(cflags) + "\n")
    (out / "BUILDINFO.txt").write_text(
        f"wine_repo: {a.wine_repo}\nwine_ref: {a.wine_ref}\nwine_commit: {commit}\n"
        f"toolchain: {' '.join(tc.cc)}\nndk_revision: {tc.ndk_rev}\nandroid_api: {a.android_api}\n"
        f"sdl_headers: {a.sdl_ref}\nsdl_soname: {a.sdl_soname}\nlibudev_zero: {a.udev_ref} (statis)\n"
        f"runpath: {a.runpath}\nneeded: {' '.join(needed)}\nimpor_ntdll: {' '.join(from_ntdll)}\n"
        f"sha256: {sha}\n")
    log("selesai: " + str(out / "winebus.so"))
    print(dyn)


if __name__ == "__main__":
    main()
