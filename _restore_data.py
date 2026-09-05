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

--- Bila situs dilindungi Cloudflare Access (hanya pemilik yang bisa baca) ---
Restore tetap jalan dengan SERVICE TOKEN (direkomendasikan) atau COOKIE:

  Service Token (Zero Trust -> Access -> Service Auth -> Create Service Token):
    setenv CF_ACCESS_CLIENT_ID=<client_id>
    setenv CF_ACCESS_CLIENT_SECRET=<client_secret>
    python _restore_data.py

  Cookie browser (setelah login, DevTools -> Application -> Cookies):
    setenv CF_AUTH_COOKIE="CF_Authorization=xxxxx"   # atau nilai cookie saja
    python _restore_data.py

CLI alternatif: --cf-client-id, --cf-client-secret, --cf-cookie.

Jalankan:
  python _restore_data.py                      # dari SITE_URL env / default
  python _restore_data.py --url https://x.pages.dev
"""
import os
import re
import sys
import json
import urllib.request
import urllib.parse
from urllib.error import HTTPError, URLError

ROOT = os.path.dirname(os.path.abspath(__file__))
SERIES_DIR = os.path.join(ROOT, 'site-content', 'series')
STATE_PATH = os.path.join(ROOT, 'site-content', 'scrape-state.json')

_DEF = 'https://mfmam.pages.dev'
UA = {'User-Agent': 'Mozilla/5.0 (Mfmam-restore)'}

# Prioritas otorisasi: Service Token (2 header) lalu cookie CF_Authorization.
# Bila keduanya kosong, request tetap anonim (situs belum dilindungi Access).


def _cf_cookie_value(raw):
    """Normalisasi nilai cookie CF_Authorization.

    Terima salah satu:
      - string cookie mentah dari DevTools (bisa berisi banyak pasangan)   -> ambil CF_Authorization
      - `CF_Authorization=<nilai>` (eksplisit dengan nama kunci)
      - nilai cookie itu sendiri (tanpa nama kunci)
    """
    raw = (raw or '').strip()
    if not raw:
        return ''
    for part in raw.split(';'):
        part = part.strip()
        if part.lower().startswith('cf_authorization='):
            return part.split('=', 1)[1]
    if raw.lower().startswith('cf_authorization='):
        return raw.split('=', 1)[1]
    return raw


def _auth_headers():
    """Header request + otorisasi Cloudflare Access bila dikonfigurasi (via env)."""
    h = dict(UA)
    cid = os.environ.get('CF_ACCESS_CLIENT_ID', '').strip()
    csec = os.environ.get('CF_ACCESS_CLIENT_SECRET', '').strip()
    if cid and csec:
        h['CF-Access-Client-Id'] = cid
        h['CF-Access-Client-Secret'] = csec
    ck = os.environ.get('CF_AUTH_COOKIE', '').strip()
    if ck:
        h['Cookie'] = 'CF_Authorization=' + _cf_cookie_value(ck)
    return h


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Ikuti redirect hanya bila masih ke HOST YANG SAMA.

    Saat Access mengalihkan (karena tanpa kredensial) ke halaman login,
    jangan diikuti — supaya Service Token / cookie TIDAK terkirim ke host
    lain (access.cloudflare.com).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old = urllib.parse.urlsplit(req.full_url).netloc.lower()
        new = urllib.parse.urlsplit(newurl).netloc.lower()
        if old and new and new != old:
            raise URLError('redirect ke host lain ditolak (halaman login '
                           'Cloudflare Access?): %r' % newurl)
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


_OPENER = None
_ACCESS_HINTED = [False]


def _hint_access():
    """Cetak petunjuk sekali saja bila respons terlihat seperti halaman Access."""
    if _ACCESS_HINTED[0]:
        return
    _ACCESS_HINTED[0] = True
    print('   ! PERHATIAN: respons terlihat seperti halaman/kredensial '
          'Cloudflare Access.')
    print('     Bila situs sudah dilindungi Access, set Service Token:')
    print('       CF_ACCESS_CLIENT_ID + CF_ACCESS_CLIENT_SECRET')
    print('     (atau cookie CF_AUTH_COOKIE) lalu jalankan ulang restore.')


def _url(base, path):
    return base.rstrip('/') + path


def _opener():
    """Opener tunggal dengan redirect handler aman (tidak bocor header)."""
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(_SafeRedirect())
    return _OPENER


def _fetch(url, timeout=60):
    req = urllib.request.Request(url, headers=_auth_headers())
    try:
        return _opener().open(req, timeout=timeout).read()
    except URLError as ex:
        msg = str(ex)
        if ('cloudflareaccess' in msg.lower() or 'login' in msg.lower()
                or 'access' in msg.lower()):
            _hint_access()
        raise
    except HTTPError as ex:
        if ex.code in (302, 307, 308, 401, 403):
            body = b''
            try:
                body = ex.read(512).lower()
            except Exception:
                pass
            if (b'access denied' in body or b'access.cloudflare.com' in body
                    or ex.code in (302, 307)):
                _hint_access()
        raise


def _head_len(url, timeout=30):
    """Ambil ukuran berkas remote. Prioritas HEAD; bila HEAD gagal (Pages
    Function mfmam hanya mengimplementasikan onRequestGet, jadi HEAD dijawab
    404/405), fallback ke GET dengan Range bytes=0-0 lalu baca Content-Range
    utk mendapat total ukuran tanpa men-download seluruh isi."""
    # 1) HEAD
    try:
        req = urllib.request.Request(url, headers=_auth_headers(),
                                     method='HEAD')
        with _opener().open(req, timeout=timeout) as r:
            length = r.headers.get('Content-Length')
            if length:
                return int(length)
    except Exception:
        pass
    # 2) GET Range bytes=0-0
    try:
        hdr = _auth_headers()
        hdr['Range'] = 'bytes=0-0'
        req = urllib.request.Request(url, headers=hdr)
        with _opener().open(req, timeout=timeout) as r:
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


def _looks_like_access_html(raw):
    """True bila respons berbentuk halaman HTML (kemungkinan halaman login
    Cloudflare Access yang balas 200, bukan redirect). Manifest/sitemap asli
    selalu JSON/XML, bukan HTML."""
    return bool(raw) and (b'<!doctype' in raw.lower() or b'<html' in raw.lower())


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
        if _looks_like_access_html(raw):
            _hint_access()
            return []
        man = json.loads(raw.decode('utf-8-sig').strip())
        if isinstance(man, list):
            slugs.extend(man)
            print('   manifest.json: %d slug' % len(man))
    except Exception:
        print('   manifest.json: tidak terbaca (dilewati).')

    # 2) sitemap.xml (statis, live)
    try:
        sm = _fetch(_url(base, '/sitemap.xml'), timeout=60).decode('utf-8', 'replace')
        if sm.lstrip().lower().startswith(('<!doctype', '<html')):
            _hint_access()
            return []
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
    cid = (os.environ.get('CF_ACCESS_CLIENT_ID') or '').strip()
    csec = (os.environ.get('CF_ACCESS_CLIENT_SECRET') or '').strip()
    ck = (os.environ.get('CF_AUTH_COOKIE') or '').strip()
    if cid and csec:
        shown = cid if len(cid) <= 8 else cid[:8] + '...'
        print('[restore] otorisasi: Service Token Cloudflare Access (id=%s)'
              % shown)
    elif ck:
        print('[restore] otorisasi: cookie CF_Authorization (panjang %d)'
              % len(ck))
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
    # Otorisasi Cloudflare Access (override env): --cf-client-id,
    # --cf-client-secret, --cf-cookie.
    for opt, env in (('--cf-client-id', 'CF_ACCESS_CLIENT_ID'),
                     ('--cf-client-secret', 'CF_ACCESS_CLIENT_SECRET'),
                     ('--cf-cookie', 'CF_AUTH_COOKIE')):
        if opt in sys.argv:
            k = sys.argv.index(opt)
            if k + 1 < len(sys.argv):
                os.environ[env] = sys.argv[k + 1].strip()
    return restore(base)


if __name__ == '__main__':
    sys.exit(main())