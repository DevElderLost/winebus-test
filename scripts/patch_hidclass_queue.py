#!/usr/bin/env python3
"""Perkecil antrean report input hidclass.sys untuk perangkat Valve (VID 0x28DE, mis. Steam Deck 0x1205).

Masalah: hidclass.sys menyimpan report input per handle dalam ring 32 entri (FIFO, terlama dibaca dulu). Steam Deck
mengirim ~250 report/detik, sedangkan driver HIDAPI SDL membaca lewat hid_read non-blocking dan, di Wine, ReadFile
overlapped hampir selalu belum selesai saat WaitForSingleObject(ev, 0) dipanggil, sehingga hanya ~1 report terbaca per
siklus update aplikasi (testcontroller: ~39x/detik). Ring pun selalu penuh dan SDL membaca report yang usianya ~31 x 4 ms
(~125 ms), ditambah satu siklus update. Hasilnya input terasa tertunda dan tap singkat terlewat. Di jalur evdev tidak ada
masalah karena driver SDL-nya membaca STATE, bukan antrean.

Perbaikan: untuk perangkat Valve, ring dibuat kecil (default 4 = usia report <= ~8 ms) sehingga yang terbaca selalu report yang hampir terbaru.
Ditambah perbaikan hid_queue_pop_report: saat ring kosong ia dulu mengembalikan sisa report yang sudah dibuang (basi),
yang pada ring kecil bisa membuat state mundur ke report lama; sekarang mengembalikan NULL (pemanggil memakai last_report).
BUG v1 yang diperbaiki di sini: HIDAPI (dipakai SDL) selalu memanggil HidD_SetNumInputBuffers(handle, 64) tepat setelah
membuka perangkat (log Wine: "HidD_SetNumInputBuffers ... num_buffer 64"), sehingga ring kecil di atas langsung dikembalikan
ke 64 dan delay justru menjadi ~256 ms. Sekarang permintaan itu juga dibatasi (--max-length, default sama dengan --length)
khusus perangkat Valve. Perangkat lain tidak berubah (tetap 32 / sesuai permintaan aplikasi).

Opsional (default hidup): TRACE kedalaman ring setiap pembacaan ("ddpatch queue ... remaining N/L"), supaya log +hid
berikutnya membuktikan sisa antrean mendekati 0.

Lintas versi Wine/Proton: nama struktur ekstensi perangkat berbeda antar versi (Wine 9: BASE_DEVICE_EXTENSION dengan
u.pdo.information.VendorID; versi lebih baru bisa berganti nama). Karena itu patch TIDAK menulis nama tipe/variabel
secara hardcode: ekspresi VendorID dibaca dari kode yang sudah ada di device.c (pemakaian '...information' di fungsi
yang sama). Bila struktur source tidak dikenali, file TIDAK diubah sama sekali dan skrip keluar dengan kode 3
(workflow memperlakukannya sebagai "lewati hidclass", bukan kegagalan build).

Kode keluar: 0 = berhasil/sudah dipatch, 3 = struktur source tidak cocok (tidak ada yang diubah), lainnya = galat.

Pemakaian:  python3 patch_hidclass_queue.py <folder-source-wine> [--length N]
Idempoten (aman dijalankan ulang).
"""
import argparse
import re
import sys
from collections import Counter
from pathlib import Path

MARKER = "DDPATCH hidclass valve queue"
MARKER_CLAMP = "DDPATCH hidclass valve clamp"
MARKER_RAWIN = "DDPATCH hidclass valve rawinput switch"

# pemakaian field 'information' (HID_COLLECTION_INFORMATION) lewat pointer ekstensi, mis. ext->u.pdo.information
INFO_RE = re.compile(r"\b(\w+)\s*->\s*((?:\w+\s*\.\s*)*)information\b")


class Incompatible(Exception):
    """Struktur source Wine tidak cocok dengan patch ini (biasanya versi Wine/Proton lain)."""


def need(cond, msg):
    if not cond:
        raise Incompatible(msg)


def vendor_expr(t, idx, what):
    """Cari ekspresi (variabel ekstensi, jalur field) VendorID di fungsi C yang memuat posisi idx.

    1) Pakai pemakaian '<var>->...information' yang sudah ada di fungsi yang sama.
    2) Bila tidak ada: jalur field diambil dari pemakaian terbanyak di file, variabel ekstensi dari deklarasi
       '<var> = device->DeviceExtension' (atau '<var> = fungsi( device )') di fungsi tersebut.
    Mengembalikan (var, ekspresi VendorID)."""
    a = t.rfind("\n{\n", 0, idx)
    b = t.find("\n}\n", idx)
    need(a >= 0 and b >= 0, f"{what}: batas fungsi C tidak ditemukan")
    body = t[a:b]
    m = INFO_RE.search(body)
    if m:
        var, path = m.group(1), re.sub(r"\s+", "", m.group(2))
    else:
        paths = Counter(re.sub(r"\s+", "", x.group(2)) for x in INFO_RE.finditer(t))
        need(paths, f"{what}: field '...information' tidak ditemukan di device.c")
        path = paths.most_common(1)[0][0]
        cands = re.findall(r"\b(\w+)\s*=\s*(?:device\s*->\s*DeviceExtension|\w+\s*\(\s*device\s*\))\s*;", body[: idx - a])
        need(cands, f"{what}: variabel ekstensi perangkat tidak ditemukan di fungsi pembungkus")
        var = cands[-1]
    return var, f"{var}->{path}information.VendorID"


def add_clamp(t, max_len):
    """Batasi IOCTL_SET_NUM_DEVICE_INPUT_BUFFERS (HidD_SetNumInputBuffers) untuk perangkat Valve. Gagal bila
    struktur kode tidak dikenali, supaya CI tidak diam-diam menghasilkan hidclass tanpa perbaikan."""
    key = "case IOCTL_SET_NUM_DEVICE_INPUT_BUFFERS:"
    need(t.count(key) == 1, f"{key} harus muncul tepat sekali, ditemukan {t.count(key)}x")
    i = t.index(key)
    heads = list(re.finditer(r"^(?:static\s+)?NTSTATUS\s+WINAPI\s+(\w+)\(\s*DEVICE_OBJECT\s*\*\s*device\s*,\s*IRP\s*\*\s*irp\s*\)", t[:i], re.M))
    need(heads, "fungsi pembungkus (DEVICE_OBJECT *device, IRP *irp) tidak ditemukan di atas case SET_NUM")
    var, vid = vendor_expr(t, i, "clamp SetNumInputBuffers")
    block = f"""{key}
        /* {MARKER_CLAMP}: HIDAPI/SDL memanggil HidD_SetNumInputBuffers(handle, 64) saat open dan mengembalikan ring Valve
         * ke 64 entri (~256 ms pada 250 report/detik). Batasi untuk perangkat Valve. */
        {{
            IO_STACK_LOCATION *dd_sp = IoGetCurrentIrpStackLocation( irp );
            if ({var} && {vid} == 0x28de
                && dd_sp->Parameters.DeviceIoControl.InputBufferLength == sizeof(ULONG) && irp->AssociatedIrp.SystemBuffer)
            {{
                ULONG *dd_len = irp->AssociatedIrp.SystemBuffer;
                if (*dd_len > {max_len}) *dd_len = {max_len};
            }}
        }}"""
    return t.replace(key, block, 1)


def add_rawinput_switch(t):
    """Saklar runtime untuk Proton/Wine 9.x: setiap laporan HID dikirim dulu ke wineserver sebagai pesan WM_INPUT
    (__wine_send_input, sinkron) SEBELUM masuk antrean baca aplikasi. Di Wine 10+ jalur ini diganti (NtUserSendHardwareInput +
    struct hid_packet) yang tidak ada di win32u/server 9.x. Dengan env DD_HID_VALVE_NORAWINPUT=1 (dibaca sekali per proses)
    pengiriman rawinput DILEWATI khusus perangkat Valve (0x28de); SDL/HIDAPI membaca lewat ReadFile, bukan WM_INPUT. Default
    (env tidak diatur) perilaku TIDAK berubah. Kembali (teks, diterapkan); bila jangkar tidak ada (Wine 10+) teks tidak diubah."""
    anchor = "if (IsEqualGUID( ext->class_guid, &GUID_DEVINTERFACE_HID ) && !steam_overlay_open)"
    if t.count(anchor) != 1 or "__wine_send_input" not in t:
        return t, False
    idx = t.index(anchor)
    var, vid = vendor_expr(t, idx, "saklar rawinput Valve")
    helper = f"""/* {MARKER_RAWIN}: DD_HID_VALVE_NORAWINPUT=1 melewati pengiriman WM_INPUT (wineserver) untuk perangkat Valve. */
static BOOL dd_valve_skip_rawinput( USHORT vendor_id )
{{
    static volatile LONG state; /* 0 = belum dibaca, 1 = lewati, 2 = jangan lewati */
    LONG s = state;
    if (vendor_id != 0x28de) return FALSE;
    if (!s)
    {{
        WCHAR buf[4];
        DWORD n = GetEnvironmentVariableW( L"DD_HID_VALVE_NORAWINPUT", buf, sizeof(buf) / sizeof(buf[0]) );
        s = (n == 1 && buf[0] == L'1') ? 1 : 2;
        InterlockedExchange( &state, s );
    }}
    return s == 1;
}}

"""
    # sisipkan helper sebelum fungsi yang memuat jangkar
    a = t.rfind("\nstatic void hid_device_queue_input", 0, idx)
    need(a >= 0, "awal hid_device_queue_input tidak ditemukan untuk menyisipkan helper")
    t = t[:a + 1] + helper + t[a + 1:]
    new_anchor = f"if (IsEqualGUID( ext->class_guid, &GUID_DEVINTERFACE_HID ) && !steam_overlay_open && !dd_valve_skip_rawinput( {var} ? {vid} : 0 ))"
    return t.replace(anchor, new_anchor, 1), True


def apply_patch(t, a):
    max_len = a.max_length if a.max_length is not None else a.length
    if MARKER in t:
        # patch v1 sudah ada; hanya tambah pembatas HidD_SetNumInputBuffers
        return add_clamp(t, max_len), f"ditambah pembatas SetNumInputBuffers Valve <= {max_len}"

    old_sig = "static struct hid_queue *hid_queue_create( void )\n{"
    need(old_sig in t, "hid_queue_create tidak ditemukan")
    new_sig = f"""/* {MARKER}: perangkat Valve (Steam Deck) mengirim ~250 report/detik; ring 32 entri membuat pembaca
 * yang lambat melihat report berumur ~125 ms. Pakai ring kecil agar yang terbaca hampir selalu yang terbaru. */
static ULONG hid_default_queue_length( USHORT vendor_id )
{{
    if (vendor_id == 0x28de) return {a.length};
    return 32;
}}

static struct hid_queue *hid_queue_create( ULONG length )
{{"""
    t = t.replace(old_sig, new_sig, 1)

    old_len = "    queue->length = 32;\n"
    need(old_len in t, "queue->length = 32 tidak ditemukan")
    t = t.replace(old_len, "    queue->length = length;\n", 1)

    old_call = "if (!(queue = hid_queue_create())) irp->IoStatus.Status = STATUS_NO_MEMORY;"
    need(old_call in t, "pemanggil hid_queue_create tidak ditemukan")
    var, vid = vendor_expr(t, t.index(old_call), "pemanggil hid_queue_create")
    new_call = (f"if (!(queue = hid_queue_create( hid_default_queue_length( {var} ? {vid} : 0 ) ))) "
                "irp->IoStatus.Status = STATUS_NO_MEMORY;")
    t = t.replace(old_call, new_call, 1)

    trace_line = "" if a.no_trace_depth else (
        "        TRACE( \"ddpatch queue %p remaining %u/%u\\n\", queue, (unsigned)((queue->write_idx + queue->length - queue->read_idx) % queue->length),\n"
        "               (unsigned)queue->length );\n")
    old_pop = """    KeAcquireSpinLock( &queue->lock, &irql );
    report = queue->reports[i];
    queue->reports[i] = NULL;
    if (i != queue->write_idx) queue->read_idx = next;
    KeReleaseSpinLock( &queue->lock, irql );
"""
    new_pop = """    KeAcquireSpinLock( &queue->lock, &irql );
    if (i == queue->write_idx) report = NULL;  /* %s: ring kosong; slot ini hanya sisa report yang sudah dibuang */
    else
    {
        report = queue->reports[i];
        queue->reports[i] = NULL;
        queue->read_idx = next;
%s    }
    KeReleaseSpinLock( &queue->lock, irql );
""" % (MARKER, trace_line)
    need(old_pop in t, "hid_queue_pop_report tidak ditemukan")
    t = t.replace(old_pop, new_pop, 1)

    t = add_clamp(t, max_len)
    return t, f"antrean Valve = {a.length} (SetNumInputBuffers dibatasi <= {max_len}), lainnya 32"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wine_src")
    ap.add_argument("--length", type=int, default=4, help="panjang antrean untuk perangkat Valve (4..32; 2-3 tidak disarankan)")
    ap.add_argument("--max-length", type=int, default=None, help="batas atas HidD_SetNumInputBuffers untuk perangkat Valve (default = --length)")
    ap.add_argument("--no-rawinput-switch", action="store_true", help="jangan tambah saklar env DD_HID_VALVE_NORAWINPUT (Wine 9.x)")
    ap.add_argument("--no-trace-depth", action="store_true", help="jangan tambah TRACE kedalaman ring")
    a = ap.parse_args()
    if not 4 <= a.length <= 32:
        sys.exit("[GAGAL] --length harus 4..32")
    max_len = a.max_length if a.max_length is not None else a.length
    if not 2 <= max_len <= 512:
        sys.exit("[GAGAL] --max-length harus 2..512")
    p = Path(a.wine_src) / "dlls/hidclass.sys/device.c"
    if not p.is_file():
        print(f"[TIDAK COCOK] tidak ditemukan: {p}")
        return 3
    t = p.read_text(encoding="utf-8")
    notes = []
    if MARKER in t and MARKER_CLAMP in t:
        new = t  # antrean + pembatas sudah ada; mungkin hanya saklar rawinput yang belum
    else:
        try:
            new, msg = apply_patch(t, a)  # semua perubahan di memori; file baru ditulis hanya bila semuanya berhasil
        except Incompatible as e:
            print(f"[TIDAK COCOK] {p}: {e}")
            print("  device.c di versi Wine/Proton ini berbeda dari yang dikenali patch; file TIDAK diubah.")
            return 3
        notes.append(msg)
    if not a.no_rawinput_switch and MARKER_RAWIN not in new:
        try:
            new, applied = add_rawinput_switch(new)
        except Incompatible as e:
            print(f"[INFO] saklar rawinput dilewati: {e}")
            applied = False
        notes.append("saklar env DD_HID_VALVE_NORAWINPUT ditambah" if applied else "saklar rawinput tidak berlaku di versi ini (dilewati)")
    if new == t:
        print("[SKIP] sudah dipatch:", p)
        return 0
    msg = "; ".join(notes)
    p.write_text(new, encoding="utf-8")
    print(f"[OK] {p}: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
