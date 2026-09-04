# -*- coding: utf-8 -*-
"""
Scraper katalog Mfmam — orkestrator MULTI-SUMBER.

Sejak scraper dipecah per sumber, perintah tunggal ini menjalankan KETIGA
scraper secara berurutan dalam satu run:

  1) scraper_komikindo.py   — situs HTML tema WordPress/Madara (mis. komikindo.ch)
  2) scraper_mikoroku.py    — katalog JSON publik + feed Blogger (mikoroku.com)
  3) scraper_doujindesu.py  — API terenkripsi doujin.desu.xxx

Mesin bersama (network, retry, state pagination, dedupe, CLI, auto-build)
ada di scraper_common.py. Setiap scraper_*.py juga bisa dijalankan sendiri.

Jalankan:
  python scraper.py                # update SEMUA sumber (mode link)
  python scraper.py --images       # update + gambar bab + tanggal tiap bab
  python scraper.py --dates        # update + tanggal tiap bab (tanpa gambar)
  python scraper.py --test         # self-test ketiga parser (tanpa network)
  python scraper.py --delete ...   # hapus seri (lihat scraper_common)
  python scraper.py --refresh-images ...   # perbaiki URL gambar bab
"""
import os
import sys
import time

# Impor ketiga scraper SEBELUM menjalankan apa pun: tiap modul mendaftarkan
# adaptornya ke registry scraper_common lewat register_adapter().
import scraper_komikindo    # noqa: F401  (KomikindoAdapter)
import scraper_mikoroku     # noqa: F401  (MikorokuAdapter)
import scraper_doujindesu   # noqa: F401  (DoujindesuAdapter)

from scraper_common import (
    all_adapters, delete_series, refresh_series_images, run, ts,
)


def _summarize(adapters, t_all):
    print()
    print('=' * 60)
    print('Scraper multi-sumber selesai. Ringkasan adaptor:')
    for a in adapters:
        print('  - %-12s %s' % (a.name, a.description))
    print('Total durasi: %ds (%s)' % (round(time.time() - t_all), ts()))
    print('=' * 60)


def main():
    if '--test' in sys.argv:
        # Jalankan self-test semua adaptor tanpa menyentuh jaringan.
        for a in all_adapters():
            print('\n--- self-test %s ---' % a.name)
            a.test()
        print('\n[test] semua self-test selesai.')
        return 0
    if '--delete' in sys.argv:
        # Hapus seri dari katalog (berlaku untuk semua sumber sekaligus).
        return delete_series()
    if '--refresh-images' in sys.argv:
        # Perbaiki URL gambar per-seri; adaptor dipilih otomatis berdasarkan
        # source_url tiap file (adapter_for_url).
        return refresh_series_images(adapter=None)

    adapters = all_adapters()
    if not adapters:
        print('Tidak ada adaptor sumber yang terdaftar '
              '(scraper_*.py tidak terimpor).')
        return 1

    # Pilihan adaptor lewat env SCRAPE_ADAPTERS (dipisah koma), mis.
    # SCRAPE_ADAPTERS=komikindo,doujindesu. Kosongkan = semua sumber.
    sel_env = os.environ.get('SCRAPE_ADAPTERS', '').strip()
    if sel_env:
        wanted = [n.strip().lower() for n in sel_env.split(',') if n.strip()]
        keep = [a for a in adapters if a.name.lower() in wanted]
        if not keep:
            print('SCRAPE_ADAPTERS tidak cocok dengan adaptor mana pun: %s'
                  % sel_env)
            print('Adaptor yang tersedia: %s'
                  % ', '.join(a.name for a in adapters))
            return 1
        print('[scraper] adaptor terpilih: %s'
              % ', '.join(a.name for a in keep))
        adapters = keep

    # Auto-build hanya dijalankan SEKALI setelah SEMUA sumber diproses, agar
    # tidak membangun ulang situs 3x dalam satu perintah. Mengesampingkan
    # env SCRAPE_AUTO_BUILD cukup untuk adaptor yang bukan terakhir.
    env_build = os.environ.get('SCRAPE_AUTO_BUILD', '').strip().lower()
    last_mode = None
    if env_build in ('0', 'false', 'no', 'off'):
        last_mode = False          # user mematikan auto-build total
    elif env_build in ('1', 'true', 'yes', 'on'):
        last_mode = True           # user memaksa build selalu (sekali di akhir)

    t_all = time.time()
    print('[scraper] %s | menjalankan %d adaptor sumber secara berurutan...'
          % (ts(), len(adapters)))
    for i, a in enumerate(adapters):
        is_last = (i == len(adapters) - 1)
        # Adaptor terakhir memakai mode env/auto; adaptor lain TIDAK build.
        build_here = last_mode if is_last else False
        try:
            rc = run(a, auto_build=build_here)
        except KeyboardInterrupt:
            print('\n[scraper] dibatalkan pengguna (Ctrl-C).')
            return 130
        if rc:
            print('  [scraper] adaptor %s keluar dengan kode %s.' % (a.name, rc))
    _summarize(adapters, t_all)
    return 0


if __name__ == '__main__':
    sys.exit(main())