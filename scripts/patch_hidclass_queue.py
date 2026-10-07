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

Pemakaian:  python3 patch_hidclass_queue.py <folder-source-wine> [--length N]
Idempoten (aman dijalankan ulang)."""
import argparse
import re
import sys
from pathlib import Path

MARKER = "DDPATCH hidclass valve queue"
MARKER_CLAMP = "DDPATCH hidclass valve clamp"


def add_clamp(t, max_len):
    """Batasi IOCTL_SET_NUM_DEVICE_INPUT_BUFFERS (HidD_SetNumInputBuffers) untuk perangkat Valve. Gagal keras bila
    struktur kode tidak dikenali, supaya CI tidak diam-diam menghasilkan hidclass tanpa perbaikan."""
    key = "case IOCTL_SET_NUM_DEVICE_INPUT_BUFFERS:"
    assert t.count(key) == 1, f"{key} harus muncul tepat sekali, ditemukan {t.count(key)}x"
    i = t.index(key)
    heads = list(re.finditer(r"^(?:static\s+)?NTSTATUS\s+WINAPI\s+(\w+)\(\s*DEVICE_OBJECT\s*\*\s*device\s*,\s*IRP\s*\*\s*irp\s*\)", t[:i], re.M))
    assert heads, "fungsi pembungkus (DEVICE_OBJECT *device, IRP *irp) tidak ditemukan di atas case SET_NUM; periksa device.c:\n" + t[max(0, i - 600):i + 400]
    block = f"""{key}
        /* {MARKER_CLAMP}: HIDAPI/SDL memanggil HidD_SetNumInputBuffers(handle, 64) saat open dan mengembalikan ring Valve
         * ke 64 entri (~256 ms pada 250 report/detik). Batasi untuk perangkat Valve. */
        {{
            BASE_DEVICE_EXTENSION *dd_ext = device->DeviceExtension;
            IO_STACK_LOCATION *dd_sp = IoGetCurrentIrpStackLocation( irp );
            if (dd_ext && dd_ext->u.pdo.information.VendorID == 0x28de
                && dd_sp->Parameters.DeviceIoControl.InputBufferLength == sizeof(ULONG) && irp->AssociatedIrp.SystemBuffer)
            {{
                ULONG *dd_len = irp->AssociatedIrp.SystemBuffer;
                if (*dd_len > {max_len}) *dd_len = {max_len};
            }}
        }}"""
    return t.replace(key, block, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wine_src")
    ap.add_argument("--length", type=int, default=4, help="panjang antrean untuk perangkat Valve (4..32; 2-3 tidak disarankan)")
    ap.add_argument("--max-length", type=int, default=None, help="batas atas HidD_SetNumInputBuffers untuk perangkat Valve (default = --length)")
    ap.add_argument("--no-trace-depth", action="store_true", help="jangan tambah TRACE kedalaman ring")
    a = ap.parse_args()
    if not 4 <= a.length <= 32:
        sys.exit("[GAGAL] --length harus 4..32")
    p = Path(a.wine_src) / "dlls/hidclass.sys/device.c"
    t = p.read_text(encoding="utf-8")
    max_len = a.max_length if a.max_length is not None else a.length
    if not 2 <= max_len <= 512:
        sys.exit("[GAGAL] --max-length harus 2..512")
    if MARKER in t and MARKER_CLAMP in t:
        print("[SKIP] sudah dipatch:", p)
        return
    if MARKER in t:
        # patch v1 sudah ada; hanya tambah pembatas HidD_SetNumInputBuffers
        t = add_clamp(t, max_len)
        p.write_text(t, encoding="utf-8")
        print(f"[OK] {p}: ditambah pembatas SetNumInputBuffers Valve <= {max_len}")
        return

    old_sig = "static struct hid_queue *hid_queue_create( void )\n{"
    new_sig = f"""/* {MARKER}: perangkat Valve (Steam Deck) mengirim ~250 report/detik; ring 32 entri membuat pembaca
 * yang lambat melihat report berumur ~125 ms. Pakai ring kecil agar yang terbaca hampir selalu yang terbaru. */
static ULONG hid_default_queue_length( BASE_DEVICE_EXTENSION *ext )
{{
    if (ext && ext->u.pdo.information.VendorID == 0x28de) return {a.length};
    return 32;
}}

static struct hid_queue *hid_queue_create( ULONG length )
{{"""
    assert old_sig in t, "hid_queue_create tidak ditemukan"
    t = t.replace(old_sig, new_sig, 1)

    old_len = "    queue->length = 32;\n"
    assert old_len in t, "queue->length = 32 tidak ditemukan"
    t = t.replace(old_len, "    queue->length = length;\n", 1)

    old_call = "if (!(queue = hid_queue_create())) irp->IoStatus.Status = STATUS_NO_MEMORY;"
    assert old_call in t, "pemanggil hid_queue_create tidak ditemukan"
    t = t.replace(old_call, "if (!(queue = hid_queue_create( hid_default_queue_length( ext ) ))) irp->IoStatus.Status = STATUS_NO_MEMORY;", 1)

    TRACE_LINE = "" if a.no_trace_depth else (
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
""" % (MARKER, TRACE_LINE)
    assert old_pop in t, "hid_queue_pop_report tidak ditemukan"
    t = t.replace(old_pop, new_pop, 1)

    t = add_clamp(t, max_len)
    p.write_text(t, encoding="utf-8")
    print(f"[OK] {p}: antrean Valve = {a.length} (SetNumInputBuffers dibatasi <= {max_len}), lainnya 32")


if __name__ == "__main__":
    main()
