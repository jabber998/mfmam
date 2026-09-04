# -*- coding: utf-8 -*-
"""
Restore data katalog dari situs yang sedang LIVE (Cloudflare Pages).

Sejak data (`site-content/series/*.json`) TIDAK lagi di-commit ke git (ukuran
katalog full bisa ber-GB), run GitHub Actions berikutnya perlu mengambil
kembali data yang sudah TERDEPLOY agar scraping tetap INCREMENTAL (gambar &
tanggal bab yang sudah ada tidak di-fetch ulang).

Cara kerja:
  1. Kumpulkan daftar slug dari `/data/manifest.json` (R2) DAN `/sitemap.xml`
     (statis, LIVE) lalu gabung — sitemap selalu lengkap, sedangkan manifest
     bisa tertimpa build sebagian (site-content/ tidak di-commit ke git).
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
    """Ambil ukuran berkas remote. Prioritas HEAD; bila HEAD gagal (Pages
    Function mfmam hanya mengimplementasikan onRequestGet, jadi HEAD dijawab
    404/405), fallback ke GET dengan Range bytes=0-0 lalu baca Content-Range
    utk mendapat total ukuran tanpa men-download seluruh isi."""
    # 1) HEAD
    try:
        req = urllib.request.Request(url, headers=UA, method='HEAD')
        with urllib.request.urlopen(req, timeout=timeout) as r:
            length = r.headers.get('Content-Length')
            if length:
                return int(length)
    except Exception:
        pass
    # 2) GET Range bytes=0-0
    try:
        hdr = dict(UA)
        hdr['Range'] = 'bytes=0-0'
        req = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cr = r.headers.get('Content-Range') or ''
            m = re.match(r'bytes \d+-\d+/(\d+)', cr)
            if m:
                return int(m.group(1))
            length = r.headers.get('Content-Length')
            if length:
                return int(length)
    except Exception:
        pass
    return None


def _slug_sources(base):
    """Kumpulkan daftar slug dari SEMUA sumber yang tersedia lalu gabung
    (union), supaya restore tidak kehilangan seri bila salah satu sumber
    tidak lengkap.

    - /data/manifest.json  (dari R2) -> TIDAK selalu lengkap: file ini
      ditulis ulang tiap build.py dari site-content/series/ (yang tidak
      di-commit), jadi bisa saja berisi sebagian kecil seri.
    - /sitemap.xml         (statis di dist, LIVE) -> daftar penuh
      /manga/<slug>/ yang benar-benar ter-deploy.
    """
    slugs = []

    # 1) manifest.json (R2)
    try:
        raw = _fetch(_url(base, '/data/manifest.json'), timeout=45)
        man = json.loads(raw.decode('utf-8-sig').strip())
        if isinstance(man, list):
            slugs.extend(man)
            print('   manifest.json: %d slug' % len(man))
    except Exception:
        print('   manifest.json: tidak terbaca (dilewati).')

    # 2) sitemap.xml (statis, live)
    try:
        sm = _fetch(_url(base, '/sitemap.xml'), timeout=60).decode('utf-8', 'replace')
        found = re.findall(r'/manga/([^/<]+)/', sm)
        if found:
            slugs.extend(found)
            print('   sitemap.xml:  %d slug' % len(found))
    except Exception:
        print('   sitemap.xml:  tidak terbaca (dilewati).')

    # buang duplikat, pertahankan urutan
    seen = set()
    return [s for s in slugs if not (s in seen or seen.add(s))]



def restore(base=''):
    base = (base or os.environ.get('SITE_URL') or _DEF).strip()
    if not base.startswith('http'):
        print('   ! SITE_URL tidak valid: %r' % base)
        return 0
    print('[restore] sumber data: %s' % base)
    slugs = _slug_sources(base)
    if not slugs:
        print('   ! tidak ada slug ditemukan (manifest & sitemap kosong/gagal); '
              'restore dilewati.')
        return 0
    os.makedirs(SERIES_DIR, exist_ok=True)
    ok = skip = fail = 0
    for slug in slugs:
        slug = re.sub(r'[^a-z0-9-]+', '-', str(slug).strip().lower()).strip('-')
        if not slug:
            continue
        dst = os.path.join(SERIES_DIR, slug + '.json')
        remote = _url(base, '/data/%s.json' % slug)
        # Lewati bila file lokal ada & ukurannya sama (HEAD; fallback GET Range).
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