#!/usr/bin/env python3
"""Implementasikan CM_Get_Device_Interface_List{,_Size}{A,W} (+_Ex) di setupapi.dll milik source Wine.

Masalah: SDL3 (HIDAPI Windows) memanggil CM_Get_Device_Interface_List_SizeW lalu CM_Get_Device_Interface_ListW.
Di Wine fungsi-fungsi itu stub (return CR_FAILURE), sehingga HIDAPI tidak menemukan perangkat HID apa pun
(termasuk Steam Deck hidraw16) dan driver Deck SDL tidak pernah mengirim feature report.

Perbaikan: enumerasi lewat SetupDiGetClassDevsW + SetupDiEnumDeviceInterfaces + SetupDiGetDeviceInterfaceDetailW
lalu susun daftar multi-SZ. Yang diubah: dlls/setupapi/stubs.c, dlls/setupapi/setupapi.spec, dlls/cfgmgr32/cfgmgr32.spec.

Opsi --convert-stubs (untuk build arm64ec dengan lld 19): semua "@ stub X" di setupapi.spec diganti
"@ cdecl X() setupapi_unimplemented" (satu handler yang meniru stub asli: RaiseException noncontinuable), dan semua
"@ stub X" di cfgmgr32.spec diganti forward "setupapi.X". Alasan: stub buatan winebuild Wine 9.0 untuk ARM64EC
tidak diterima lld 19 (__wine_stub_* dan #__wine_spec_unimplemented_stub "undefined").

Pemakaian:  python3 patch_setupapi_cfgmgr.py <folder-source-wine> [--convert-stubs]
Idempotent, tanpa file .bak, mencetak status per file dan memverifikasi hasil.
Bila source Wine sudah mengimplementasikannya (tidak lagi stub), file dilewati tanpa error.
"""
import re
import sys
from pathlib import Path

MARK = "DroidDeck-cfgmgr-iflist"
MARK_STUB = "DroidDeck-stub-handler"

STUB_LINE = re.compile(r"^(\s*(?:@|\d+)\s+)stub((?:\s+-\S+)*)\s+([^\s(]+)\s*$")

STUB_HANDLER = r'''
/* DroidDeck-stub-handler: pengganti "@ stub" (ARM64EC/lld 19). Perilaku sama dengan stub asli: exception Wine, tidak bisa dilanjutkan. */
void __cdecl setupapi_unimplemented(void)
{
    FIXME( "unimplemented function called\n" );
    RaiseException( 0x80000100 /* EXCEPTION_WINE_STUB */, EXCEPTION_NONCONTINUABLE, 0, NULL );
}
'''

C_BLOCK = r'''
/* DroidDeck-cfgmgr-iflist: daftar interface perangkat lewat SetupDi*, dipakai HIDAPI/SDL3. */
#ifndef CM_GET_DEVICE_INTERFACE_LIST_ALL_DEVICES
#define CM_GET_DEVICE_INTERFACE_LIST_ALL_DEVICES 0x00000001
#endif
static CONFIGRET interface_list_collect(const GUID *class, const WCHAR *id, ULONG flags, WCHAR **out, ULONG *count)
{
    SP_DEVICE_INTERFACE_DATA iface;
    SP_DEVICE_INTERFACE_DETAIL_DATA_W *detail;
    DWORD idx, size, used = 0, cap = 512, len;
    WCHAR *list, *grown;
    HDEVINFO set;

    *out = NULL;
    *count = 0;
    if (!class) return CR_INVALID_POINTER;
    if (flags > CM_GET_DEVICE_INTERFACE_LIST_ALL_DEVICES) return CR_INVALID_FLAG;

    set = SetupDiGetClassDevsW(class, id, NULL, DIGCF_DEVICEINTERFACE |
                               ((flags & CM_GET_DEVICE_INTERFACE_LIST_ALL_DEVICES) ? 0 : DIGCF_PRESENT));
    if (set == INVALID_HANDLE_VALUE) return CR_FAILURE;

    if (!(list = HeapAlloc(GetProcessHeap(), 0, cap * sizeof(WCHAR))))
    {
        SetupDiDestroyDeviceInfoList(set);
        return CR_OUT_OF_MEMORY;
    }

    for (idx = 0; ; idx++)
    {
        iface.cbSize = sizeof(iface);
        if (!SetupDiEnumDeviceInterfaces(set, NULL, class, idx, &iface)) break;

        size = 0;
        SetupDiGetDeviceInterfaceDetailW(set, &iface, NULL, 0, &size, NULL);
        if (size < sizeof(*detail)) continue;
        if (!(detail = HeapAlloc(GetProcessHeap(), 0, size))) continue;
        detail->cbSize = sizeof(*detail);
        if (SetupDiGetDeviceInterfaceDetailW(set, &iface, detail, size, NULL, NULL))
        {
            len = lstrlenW(detail->DevicePath) + 1;
            if (used + len + 1 > cap)
            {
                while (used + len + 1 > cap) cap *= 2;
                if (!(grown = HeapReAlloc(GetProcessHeap(), 0, list, cap * sizeof(WCHAR))))
                {
                    HeapFree(GetProcessHeap(), 0, detail);
                    HeapFree(GetProcessHeap(), 0, list);
                    SetupDiDestroyDeviceInfoList(set);
                    return CR_OUT_OF_MEMORY;
                }
                list = grown;
            }
            memcpy(list + used, detail->DevicePath, len * sizeof(WCHAR));
            used += len;
        }
        HeapFree(GetProcessHeap(), 0, detail);
    }
    SetupDiDestroyDeviceInfoList(set);

    list[used++] = 0;  /* terminator multi-SZ (daftar kosong = satu NUL) */
    *out = list;
    *count = used;
    return CR_SUCCESS;
}

/***********************************************************************
 *      CM_Get_Device_Interface_List_Size_ExW (SETUPAPI.@)
 */
CONFIGRET WINAPI CM_Get_Device_Interface_List_Size_ExW(PULONG len, LPGUID class, DEVINSTID_W id,
                                                       ULONG flags, HMACHINE machine)
{
    WCHAR *list;
    CONFIGRET ret;

    TRACE("%p %s %s 0x%08lx %p\n", len, debugstr_guid(class), debugstr_w(id), flags, machine);
    if (!len) return CR_INVALID_POINTER;
    if (machine) return CR_MACHINE_UNAVAILABLE;
    if ((ret = interface_list_collect(class, id, flags, &list, len))) return ret;
    HeapFree(GetProcessHeap(), 0, list);
    return CR_SUCCESS;
}

/***********************************************************************
 *      CM_Get_Device_Interface_List_SizeW (SETUPAPI.@)
 */
CONFIGRET WINAPI CM_Get_Device_Interface_List_SizeW(PULONG len, LPGUID class, DEVINSTID_W id, ULONG flags)
{
    return CM_Get_Device_Interface_List_Size_ExW(len, class, id, flags, NULL);
}

/***********************************************************************
 *      CM_Get_Device_Interface_List_Size_ExA (SETUPAPI.@)
 */
CONFIGRET WINAPI CM_Get_Device_Interface_List_Size_ExA(PULONG len, LPGUID class, DEVINSTID_A id,
                                                       ULONG flags, HMACHINE machine)
{
    WCHAR *idW = NULL;
    CONFIGRET ret;
    int n;

    if (id)
    {
        n = MultiByteToWideChar(CP_ACP, 0, id, -1, NULL, 0);
        if (!(idW = HeapAlloc(GetProcessHeap(), 0, n * sizeof(WCHAR)))) return CR_OUT_OF_MEMORY;
        MultiByteToWideChar(CP_ACP, 0, id, -1, idW, n);
    }
    ret = CM_Get_Device_Interface_List_Size_ExW(len, class, idW, flags, machine);
    HeapFree(GetProcessHeap(), 0, idW);
    return ret;
}

/***********************************************************************
 *      CM_Get_Device_Interface_List_SizeA (SETUPAPI.@)
 */
CONFIGRET WINAPI CM_Get_Device_Interface_List_SizeA(PULONG len, LPGUID class, DEVINSTID_A id, ULONG flags)
{
    return CM_Get_Device_Interface_List_Size_ExA(len, class, id, flags, NULL);
}

/***********************************************************************
 *      CM_Get_Device_Interface_List_ExW (SETUPAPI.@)
 */
CONFIGRET WINAPI CM_Get_Device_Interface_List_ExW(LPGUID class, DEVINSTID_W id, PZZWSTR buffer,
                                                  ULONG len, ULONG flags, HMACHINE machine)
{
    WCHAR *list;
    ULONG count;
    CONFIGRET ret;

    TRACE("%s %s %p %lu 0x%08lx %p\n", debugstr_guid(class), debugstr_w(id), buffer, len, flags, machine);
    if (!buffer) return CR_INVALID_POINTER;
    if (machine) return CR_MACHINE_UNAVAILABLE;
    if ((ret = interface_list_collect(class, id, flags, &list, &count))) return ret;
    if (len < count) ret = CR_BUFFER_SMALL;
    else memcpy(buffer, list, count * sizeof(WCHAR));
    HeapFree(GetProcessHeap(), 0, list);
    return ret;
}

/***********************************************************************
 *      CM_Get_Device_Interface_ListW (SETUPAPI.@)
 */
CONFIGRET WINAPI CM_Get_Device_Interface_ListW(LPGUID class, DEVINSTID_W id, PZZWSTR buffer,
                                               ULONG len, ULONG flags)
{
    return CM_Get_Device_Interface_List_ExW(class, id, buffer, len, flags, NULL);
}

/***********************************************************************
 *      CM_Get_Device_Interface_List_ExA (SETUPAPI.@)
 */
CONFIGRET WINAPI CM_Get_Device_Interface_List_ExA(LPGUID class, DEVINSTID_A id, PZZSTR buffer,
                                                  ULONG len, ULONG flags, HMACHINE machine)
{
    WCHAR *idW = NULL, *list;
    ULONG count;
    CONFIGRET ret;
    int n;

    if (!buffer) return CR_INVALID_POINTER;
    if (machine) return CR_MACHINE_UNAVAILABLE;
    if (id)
    {
        n = MultiByteToWideChar(CP_ACP, 0, id, -1, NULL, 0);
        if (!(idW = HeapAlloc(GetProcessHeap(), 0, n * sizeof(WCHAR)))) return CR_OUT_OF_MEMORY;
        MultiByteToWideChar(CP_ACP, 0, id, -1, idW, n);
    }
    ret = interface_list_collect(class, idW, flags, &list, &count);
    HeapFree(GetProcessHeap(), 0, idW);
    if (ret) return ret;
    if (len < count) ret = CR_BUFFER_SMALL;
    else if (!WideCharToMultiByte(CP_ACP, 0, list, count, buffer, len, NULL, NULL)) ret = CR_FAILURE;
    HeapFree(GetProcessHeap(), 0, list);
    return ret;
}

/***********************************************************************
 *      CM_Get_Device_Interface_ListA (SETUPAPI.@)
 */
CONFIGRET WINAPI CM_Get_Device_Interface_ListA(LPGUID class, DEVINSTID_A id, PZZSTR buffer,
                                               ULONG len, ULONG flags)
{
    return CM_Get_Device_Interface_List_ExA(class, id, buffer, len, flags, NULL);
}
'''

OLD_STUB = re.compile(
    r"/\*{20,}\n \*\s+CM_Get_Device_Interface_List_Size(?:_Ex)?[AW] \(SETUPAPI\.@\)\n \*/\n"
    r"CONFIGRET WINAPI CM_Get_Device_Interface_List_Size(?:_Ex)?[AW]\([^{]*\)\n\{.*?\n\}\n\n?",
    re.S,
)

SETUPAPI_SPEC = [
    ("@ stub CM_Get_Device_Interface_ListA", "@ stdcall CM_Get_Device_Interface_ListA(ptr str ptr long long)"),
    ("@ stub CM_Get_Device_Interface_ListW", "@ stdcall CM_Get_Device_Interface_ListW(ptr wstr ptr long long)"),
    ("@ stub CM_Get_Device_Interface_List_ExA", "@ stdcall CM_Get_Device_Interface_List_ExA(ptr str ptr long long ptr)"),
    ("@ stub CM_Get_Device_Interface_List_ExW", "@ stdcall CM_Get_Device_Interface_List_ExW(ptr wstr ptr long long ptr)"),
]
CFGMGR_SPEC = [
    (a, b + " setupapi." + b.split()[2].split("(")[0]) for a, b in SETUPAPI_SPEC
]


def upstream_implemented(src):
    """True bila Wine/Proton versi ini sudah punya implementasi CM_Get_Device_Interface_List* (bukan stub).

    Wine 9.0 menaruhnya sebagai stub di dlls/setupapi/stubs.c. Di Wine/Proton yang lebih baru fungsinya
    sudah diimplementasikan upstream (dan/atau dipindah file), jadi stubs.c tidak lagi memuat namanya.
    Syarat: tidak ada baris '@ stub CM_Get_Device_Interface_List*' di kedua spec DAN ada definisi fungsinya di file .c.
    """
    for spec in (src / "dlls/setupapi/setupapi.spec", src / "dlls/cfgmgr32/cfgmgr32.spec"):
        for line in spec.read_text(encoding="utf-8", errors="replace").split("\n"):
            if re.match(r"^\s*(?:@|\d+)\s+stub\b.*\bCM_Get_Device_Interface_List", line):
                return False
    pat = re.compile(r"^CONFIGRET\s+(?:WINAPI\s+)?CM_Get_Device_Interface_List_?(?:Size)?_?(?:Ex)?W\s*\(", re.M)
    for d in ("dlls/setupapi", "dlls/cfgmgr32"):
        for c in sorted((src / d).glob("*.c")):
            if pat.search(c.read_text(encoding="utf-8", errors="replace")):
                return True
    return False


def dump_where(src):
    """Diagnostik bila struktur source tidak dikenali: tunjukkan di mana nama fungsi muncul."""
    shown = 0
    for d in ("dlls/setupapi", "dlls/cfgmgr32"):
        for f in sorted((src / d).glob("*")):
            if not f.is_file() or f.suffix not in (".c", ".h", ".spec"):
                continue
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
                if "CM_Get_Device_Interface_List" in line and shown < 25:
                    print(f"    {f.relative_to(src)}:{i}: {line.strip()[:110]}")
                    shown += 1
    if not shown:
        print("    (nama CM_Get_Device_Interface_List* tidak ditemukan di setupapi/cfgmgr32)")


def patch_spec(path, table):
    text = path.read_text(encoding="utf-8")
    changed = 0
    out = []
    for line in text.split("\n"):
        for old, new in table:
            if line.strip() == old:
                line = new
                changed += 1
                break
        out.append(line)
    if changed:
        path.write_text("\n".join(out), encoding="utf-8")
    present = all(any(l.strip() == new for l in out) for _, new in table)
    return changed, present


def convert_stubs(src, stubs):
    """Ganti semua '@ stub' di setupapi.spec / cfgmgr32.spec; tambah handler ke stubs.c. Mengembalikan 0 bila sukses."""
    targets = [
        (src / "dlls/setupapi/setupapi.spec", lambda m: f"{m.group(1)}cdecl{m.group(2)} {m.group(3)}() setupapi_unimplemented"),
        (src / "dlls/cfgmgr32/cfgmgr32.spec", lambda m: f"{m.group(1)}stdcall{m.group(2)} {m.group(3)}() setupapi.{m.group(3)}"),
    ]
    ok = True
    for path, repl in targets:
        lines = path.read_text(encoding="utf-8").split("\n")
        n = 0
        for i, line in enumerate(lines):
            m = STUB_LINE.match(line)
            if m:
                lines[i] = repl(m)
                n += 1
        if n:
            path.write_text("\n".join(lines), encoding="utf-8")
        left = sum(1 for l in lines if re.match(r"^\s*(?:@|\d+)\s+stub\b", l))
        print(f"[{'DIPATCH' if n else 'SUDAH ADA'}] {path.relative_to(src)}: {n} stub diganti, sisa {left}")
        ok &= left == 0
    text = stubs.read_text(encoding="utf-8")
    if MARK_STUB in text:
        print(f"[SUDAH ADA] handler stub di {stubs.relative_to(src)}")
    else:
        stubs.write_text(text.rstrip("\n") + "\n" + STUB_HANDLER, encoding="utf-8")
        print(f"[DIPATCH] handler setupapi_unimplemented ditambahkan ke {stubs.relative_to(src)}")
    ok &= MARK_STUB in stubs.read_text(encoding="utf-8")
    print(f"[{'OK' if ok else 'GAGAL'}] verifikasi convert-stubs")
    return 0 if ok else 1


def main():
    convert = "--convert-stubs" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    src = Path(args[0] if args else ".").resolve()
    stubs = src / "dlls/setupapi/stubs.c"
    specs = [(src / "dlls/setupapi/setupapi.spec", SETUPAPI_SPEC), (src / "dlls/cfgmgr32/cfgmgr32.spec", CFGMGR_SPEC)]
    for p in [stubs] + [s for s, _ in specs]:
        if not p.is_file():
            print(f"[GAGAL] tidak ditemukan: {p}")
            return 1

    text = stubs.read_text(encoding="utf-8")
    ok = True
    if MARK in text:
        print(f"[SUDAH ADA] {stubs.relative_to(src)}")
    elif "CM_Get_Device_Interface_List_SizeW" in text and not OLD_STUB.search(text):
        print(f"[LEWAT] {stubs.relative_to(src)}: tidak lagi berupa stub (kemungkinan sudah diimplementasi upstream)")
        return convert_stubs(src, stubs) if convert else 0
    elif upstream_implemented(src):
        print(f"[LEWAT] {stubs.relative_to(src)}: Wine ini sudah mengimplementasikan CM_Get_Device_Interface_List* (bukan stub lagi)")
        return convert_stubs(src, stubs) if convert else 0
    else:
        new, n = OLD_STUB.subn("", text)
        if n != 4:
            print(f"[GAGAL] {stubs.relative_to(src)}: ditemukan {n} stub List_Size (harus 4) dan implementasi upstream tidak terdeteksi")
            print("  Lokasi nama fungsi di source Wine ini:")
            dump_where(src)
            return 1
        new = new.rstrip("\n") + "\n" + C_BLOCK
        stubs.write_text(new, encoding="utf-8")
        print(f"[DIPATCH] {stubs.relative_to(src)} (4 stub dihapus, 8 fungsi ditambah)")

    for path, table in specs:
        changed, present = patch_spec(path, table)
        print(f"[{'DIPATCH' if changed else 'SUDAH ADA'}] {path.relative_to(src)} ({changed} baris)")
        ok &= present

    if convert:
        if convert_stubs(src, stubs):
            return 1
    final = stubs.read_text(encoding="utf-8")
    checks = {
        "marker": final.count(MARK) == 1,
        "tanpa stub List_Size": re.search(r"CM_Get_Device_Interface_List\w*\([^)]*\)\n\{\n    FIXME", final) is None,
        "8 fungsi": len(re.findall(r"^CONFIGRET WINAPI CM_Get_Device_Interface_List(?:_Size)?(?:_Ex)?[AW]\(", final, re.M)) == 8,
        "spec": ok,
    }
    for k, v in checks.items():
        print(f"[{'OK' if v else 'GAGAL'}] verifikasi {k}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
