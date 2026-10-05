# winebus-udev-build

Build ulang **hanya `winebus.so`** (unix lib winebus) dari source Wine, dengan backend **UDEV** (dan SDL) ikut dikompilasi.
Untuk Wine/Proton arm64ec di Winlator, yang log-nya menunjukkan:

```
udev_bus_init UDEV support not compiled in!
```

Tanpa backend itu, winebus hanya punya jalur SDL/evdev, sehingga `/dev/hidraw16` (Steam Deck pad) tidak pernah dibuka siapa pun.

## Cara kerja

- Unix lib winebus adalah satu file: `lib/wine/aarch64-unix/winebus.so`. Bagian PE (`winebus.sys`) tidak diubah.
- Karena PE dan unix lib berbicara lewat tabel `__wine_unix_call_funcs` dan struct bersama, **source yang dibuild harus identik dengan Wine yang sudah ada di Winlator** (repo + commit yang sama). Beda commit = risiko crash atau device tidak muncul.
- Build native aarch64 di GitHub Actions (`ubuntu-24.04-arm`) di dalam container Ubuntu 22.04 (glibc rendah, supaya cocok dengan rootfs Winlator). `libsdl2-dev` ikut dipasang supaya backend SDL **tidak hilang**.
- Hasil: `winebus.so` + `libudev.so.1` (di-dlopen oleh winebus) + `BUILDINFO.txt` + `bus-options.txt`.

## Cara pakai

1. Push repo ini ke GitHub (publik, agar runner arm64 gratis).
2. Cari tahu sumber Wine arm64ec yang dipakai Winlator-mu (repo + tag/commit). Ini **harus kamu isi sendiri**; default workflow (`ValveSoftware/wine`, `proton_9.0`) hanya contoh.
3. Actions → **build-winebus-udev** → Run workflow → isi `wine_repo`, `wine_ref`, pilih `base_image`.
4. Unduh artifact `winebus-udev-aarch64`.
5. Cek hasilnya (opsional tapi disarankan):
   ```
   bash scripts/verify-winebus.sh winebus.so /path/ke/winebus.so-asli
   ```
   Perhatikan baris `glibc maksimum` (harus ≤ glibc rootfs Winlator), soname SDL yang sama dengan aslinya, dan perbandingan simbol ekspor.

## Pasang ke Winlator

1. Temukan file aslinya: `find <folder-wine> -name winebus.so` (biasanya di `lib/wine/aarch64-unix/`). **Cadangkan dulu.**
2. Ganti dengan `winebus.so` baru.
3. Letakkan `libudev.so.1` di folder yang termasuk `LD_LIBRARY_PATH` proses Wine (mis. `<folder-wine>/lib/` atau `usr/lib` di rootfs). Jika dlopen gagal, jalankan dengan `LD_DEBUG=libs` untuk melihat path pencariannya.

## Konfigurasi runtime

| Pengaturan | Tujuan |
|---|---|
| `PROTON_ENABLE_HIDRAW=0x28de/0x1205` (env) | Menyuruh winebus memakai jalur hidraw untuk Steam Deck pad. Dibaca di `unixlib.c` fork Valve; cek `bus-options.txt` untuk memastikan ada di source-mu. |
| `config/winebus-udev.reg` | `DisableUdevd=1` (hindari netlink/udevd yang biasanya ditolak SELinux di Android) dan `DisableInput=1` (SDL yang menangani evdev, udev hanya hidraw). **Nama nilai belum diverifikasi**; samakan dengan `bus-options.txt`. |

## Verifikasi di Wine

Jalankan dengan `WINEDEBUG=+hid,+plugplay`. Yang diharapkan:
- Pesan `UDEV support not compiled in!` **hilang**.
- `L"UDEV" bus init` tidak lagi mengembalikan `0xc0000002`.
- Jika hidraw ditemukan: `bus_create_hid_device desc {vid 28de, pid 1205 ...}`.
- Di log fakeinput (`FAKE_EVDEV_LOG=1`): `deck: sysfs checked (...)` lalu `deck: /dev/hidraw16 opened as fd N`.

## Batasan yang perlu diketahui

Repo ini hanya memperbaiki sisi Wine. Agar Deck pad benar-benar ditemukan di Android, sisi `libfakeinput.so` Winlator juga perlu (lihat perbandingan dengan DroidDeck):
- `stat`/`fstat`/`access` untuk `/dev/hidraw16` mengembalikan `S_IFCHR` dengan `st_rdev` 240:16 (libudev mencocokkan device lewat ini).
- Mode `DisableUdevd` memindai `/dev` dan `/dev/input` lewat direktori; node `hidraw16` perlu muncul di listing `/dev` (mis. file placeholder di rootfs).
- Listing `/sys`, `/sys/class`, `/sys/bus` dan tree sysfs palsu (sudah ada sebagian di `DDDeck.java`) harus terbaca oleh libudev di bawah SELinux.

Build Wine **penuh** (arm64ec) tidak disediakan di sini: toolchain dan patch arm64ec berbeda per fork, jadi resep CI-nya sebaiknya diambil dari repo yang membuat paket Wine Winlator-mu. Jika perlu, taruh patch `.patch` di `patches/`; akan diterapkan otomatis sebelum build.

Skrip di repo ini ditulis tanpa bisa dijalankan di lingkungan pembuatnya (tanpa akses jaringan). Jalankan workflow sekali dan periksa `configure.log`/hasil verifikasi sebelum memakainya.
