# -*- coding: utf-8 -*-
"""Hapus seri dari sumber D (Doujindesu / Mikoroku) dari katalog.

Dijalankan lewat GitHub Action (hapus-sumber-d.yml) ATAU manual:

  python _hapus_sumber_d.py            # hapus semua seri doujindesu+mikoroku
  python _hapus_sumber_d.py --dry-run  # hanya print, tanpa menghapus

Alur (di CI):
  1. Sebelum ini, _restore_data.py sudah mengembalikan SEMUA seri dari situs
     live / R2 ke site-content/series/ (termasuk seri sumber D).
  2. Script ini menghapus file JSON milik doujindesu/mikoroku dari
     site-content/series/ (deteksi source_url + tautan bab eksternal).
  3. Script menulis daftar slug terhapus ke `_hapus-r2-slugs.txt` (satu per
     baris) agar workflow bisa menghapus blob lama di R2:
        data/<slug>.json
     Tanpa penghapusan R2 itu, run berikutnya (_restore_data.py) akan
     mengembalikan seri tersebut lagi, sehingga hapusnya tidak permanen.
  4. Workflow lanjut: python build.py -> _upload_r2.py -> deploy Pages.

Nama file/slug dari sumber D dideteksi dengan pola DOMAIN yang sama
dengan build.py (blur_source_of): doujin.desu/doujindesu dan
mikoroku/mikodrive.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SERIES_DIR = os.path.join(ROOT, 'site-content', 'series')
SLUGS_OUT = os.path.join(ROOT, '_hapus-r2-slugs.txt')

# Domain sumber D. Kunci = label sumber (buat log); nilai = substring
# yang muncul di source_url / external bab seri tersebut.
BANNED = (
    ('doujindesu', ('doujin.desu', 'doujindesu')),
    ('mikoroku', ('mikoroku', 'mikodrive')),
)


def _is_banned(d):
    """Kembalikan label sumber bila seri milik doujindesu/mikoroku, else ''."""
    hay = ((d.get('source_url') or '') + ' ' + ' '.join(
        (c.get('external') or '') for c in (d.get('chapters') or [])
        if c.get('external'))).lower()
    for label, needles in BANNED:
        if any(n in hay for n in needles):
            return label
    return ''


def _load(path):
    try:
        with open(path, encoding='utf-8-sig') as fh:
            import json
            return json.load(fh) or {}
    except Exception:
        return {}


def main():
    dry = '--dry-run' in sys.argv
    if not os.path.isdir(SERIES_DIR):
        print('Tidak ada folder site-content/series; tidak ada yang dihapus.')
        return 0

    removed = []
    for fn in sorted(os.listdir(SERIES_DIR)):
        if not fn.lower().endswith('.json'):
            continue
        pth = os.path.join(SERIES_DIR, fn)
        d = _load(pth)
        if not d:
            continue
        label = _is_banned(d)
        if not label:
            continue
        slug = d.get('slug') or fn[:-5]
        removed.append((slug, fn, label))

    if dry:
        print('[hapus-sumber-d] DRY-RUN: %d seri akan dihapus dari %s'
              % (len(removed), SERIES_DIR))
        for slug, fn, label in removed:
            print('   - [%s] %s  (%s)' % (label, slug, fn))
        return 0

    # Hapus file JSON lokal.
    slugs = []
    for slug, fn, label in removed:
        pth = os.path.join(SERIES_DIR, fn)
        try:
            os.remove(pth)
        except OSError as ex:
            print('   ! gagal hapus %s: %s' % (pth, ex))
            continue
        slugs.append(slug)
        print('   - dihapus [%s] %s  (%s)' % (label, slug, fn))

    # Tulis daftar slug untuk langkah hapus R2 di workflow.
    try:
        with open(SLUGS_OUT, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(slugs) + ('\n' if slugs else ''))
    except OSError as ex:
        print('   ! gagal tulis %s: %s' % (SLUGS_OUT, ex))

    print('[hapus-sumber-d] selesai: %d seri dihapus.' % len(slugs))
    if slugs:
        print('[hapus-sumber-d] slug terhapus tersimpan di %s (%d)' %
              (SLUGS_OUT, len(slugs)))
    return 0


if __name__ == '__main__':
    sys.exit(main())