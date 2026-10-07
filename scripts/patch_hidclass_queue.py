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
Aplikasi yang memanggil HidD_SetNumInputBuffers tetap bisa mengubahnya. Perangkat lain tidak berubah (tetap 32).

Pemakaian:  python3 patch_hidclass_queue.py <folder-source-wine> [--length N]
Idempoten (aman dijalankan ulang)."""
import argparse
import sys
from pathlib import Path

MARKER = "DDPATCH hidclass valve queue"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wine_src")
    ap.add_argument("--length", type=int, default=4, help="panjang antrean untuk perangkat Valve (4..32; 2-3 tidak disarankan)")
    a = ap.parse_args()
    if not 4 <= a.length <= 32:
        sys.exit("[GAGAL] --length harus 4..32")
    p = Path(a.wine_src) / "dlls/hidclass.sys/device.c"
    t = p.read_text(encoding="utf-8")
    if MARKER in t:
        print("[SKIP] sudah dipatch:", p)
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
    }
    KeReleaseSpinLock( &queue->lock, irql );
""" % MARKER
    assert old_pop in t, "hid_queue_pop_report tidak ditemukan"
    t = t.replace(old_pop, new_pop, 1)

    p.write_text(t, encoding="utf-8")
    print(f"[OK] {p}: antrean Valve = {a.length}, lainnya 32")


if __name__ == "__main__":
    main()
