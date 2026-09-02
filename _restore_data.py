# -*- coding: utf-8 -*-
"""
Restore data katalog dari situs yang sedang LIVE (Cloudflare Pages).

Sejak data (`site-content/series/*.json`) TIDAK lagi di-commit ke git (ukuran
katalog full bisa ber-GB), run GitHub Actions berikutnya perlu mengambil
kembali data yang sudah TERDEPLOY agar scraping tetap INCREMENTAL (gambar &
tanggal bab yang sudah ada tidak di-fetch ulang).

Cara kerja:
  1. Ambil `/data/manifest.json` dari SITE_URL (daftar slug).
  2. Download tiap `/data/<slug>.json` -> `site-content/series/<slug>.json`
     (lewati bila file lokal sudah ADA dan ukurannya sama — hemat bandwidth).
  3. Ambil `/data/scrape-state.json` (state pagination) ke site-content/.

SITE_URL diambil dari env (default https://mfmam.pages.dev). Kegagalan
download TIDAK mematikan proses: bila situs belum ada / kosong, restore
dilewati dan scraper akan mengisi dari awal (sekali saja).

Jalankan:
  python _restore_data.py                      # dari SITE_URL env / default
  python _restore_data.py --url https://x.pages.dev
"""
import os
import re
import sys
import json
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
SERIES_DIR = os.path.join(ROOT, 'site-content', 'series')
STATE_PATH = os.path.join(ROOT, 'site-content', 'scrape-state.json')

_DEF = 'https://mfmam.pages.dev'
UA = {'User-Agent': 'Mozilla/5.0 (Mfmam-restore)'}


def _url(base, path):
    return base.rstrip('/') + path


def _fetch(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=timeout).read()


def _head_len(url, timeout=30):
    """Ambil Content-Length via HEAD; None bila tidak ada / gagal."""
    req = urllib.request.Request(url, headers=UA, method='HEAD')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        length = r.headers.get('Content-Length')
        try:
            return int(length) if length else None
        except (TypeError, ValueError):
            return None


def restore(base=''):
    base = (base or os.environ.get('SITE_URL') or _DEF).strip()
    if not base.startswith('http'):
        print('   ! SITE_URL tidak valid: %r' % base)
        return 0
    print('[restore] sumber data: %s' % base)
    man_url = _url(base, '/data/manifest.json')
    try:
        raw = _fetch(man_url)
    except Exception as ex:
        print('   ! situs belum punya data/manifest.json (%s); '
              'restore dilewati.' % ex)
        return 0
    try:
        slugs = json.loads(raw.decode('utf-8-sig').strip())
    except Exception as ex:
        print('   ! manifest tidak terbaca: %s' % ex)
        return 0
    if not isinstance(slugs, list):
        print('   ! manifest bukan daftar slug; dilewati.')
        return 0
    os.makedirs(SERIES_DIR, exist_ok=True)
    ok = skip = fail = 0
    for slug in slugs:
        slug = re.sub(r'[^a-z0-9-]+', '-', str(slug).strip().lower()).strip('-')
        if not slug:
            continue
        dst = os.path.join(SERIES_DIR, slug + '.json')
        remote = _url(base, '/data/%s.json' % slug)
        # Lewati bila file lokal ada & ukurannya sama (HEAD cukup, no download).
        if os.path.exists(dst):
            try:
                rlen = _head_len(remote)
                if rlen is not None and rlen == os.path.getsize(dst):
                    skip += 1
                    continue
            except Exception:
                pass
        try:
            data = _fetch(remote, timeout=90)
            with open(dst, 'wb') as fh:
                fh.write(data)
            ok += 1
        except Exception as ex:
            fail += 1
            print('   ! gagal unduh %s: %s' % (slug, ex))
    # state pagination
    try:
        raw = _fetch(_url(base, '/data/scrape-state.json'), timeout=30)
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, 'wb') as fh:
            fh.write(raw)
    except Exception:
        pass
    print('[restore] selesai: %d baru, %d dilewati (sama), %d gagal '
          'dari %d seri.' % (ok, skip, fail, len(slugs)))
    return 1 if ok or skip else 0


def main():
    base = ''
    if '--url' in sys.argv:
        k = sys.argv.index('--url')
        if k + 1 < len(sys.argv):
            base = sys.argv[k + 1].strip()
    return restore(base)


if __name__ == '__main__':
    sys.exit(main())