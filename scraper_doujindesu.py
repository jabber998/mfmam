# -*- coding: utf-8 -*-
"""
Scraper #3 — Doujindesu (doujin.desu.xxx), klien API terenkripsi.

Situs ini adalah SPA; halaman HTML-nya memuat JSON yang di-ENCRYPT (lihat
`_enc_resp_`) dan hanya bisa dibaca lewat API internal yang diproteksi
(app-secret + salt + device-id). Adaptor ini:

  /manga/<slug>/   -> API /api/manga/<slug>   (metadata + daftar bab)
  /reader/<id>     -> API /api/chapters/<id>  (URL gambar bab)

Jalankan:
  python scraper_doujindesu.py [--images|--dates|--test|--delete|--refresh-images]

Catatan: URL gambar bab bertanda-tangan & basi ~24 jam; di mode link gambar
lama tidak dipertahankan (lihat STALE_IMAGE_SOURCES di scraper_common).
"""
import sys
import json
import math
import os
import random
import time
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from scraper_common import (
    UA, _SSL_CTX,
    SourceAdapter, register_adapter,
    clean_title, slugify, secs, chapter_label, polite_delay, logv,
    fresh_cutoff, chapter_within_window, merge_existing_chapter_data,
)
DOUJIN_WEB = 'https://doujin.desu.xxx'
DOUJIN_API_BASE = DOUJIN_WEB + '/api'
DOUJIN_APP_SECRET = 'dfdf72051dbfdc7d76889ebd31324e74'
DOUJIN_SALT = ('doujindesu-scrapers-cannot-read-this-super-secret-'
               'salt-2026-v2')
DOUJIN_HOUR_MS = 3600000
DOUJIN_DEVICE_ID = 'mf_' + ''.join(
    random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=24))


def _doujin_key(a):
    s = DOUJIN_SALT + "_" + str(a)
    l = 0
    for ch in s:
        l = ((l << 5) - l) + ord(ch)
        l &= 0xFFFFFFFF  # l |= 0 -> 32-bit
        if l >= 0x80000000:
            l -= 0x100000000
        if l < -0x80000000:
            l += 0x100000000
    d = abs(l) or 123456789
    out = []
    for _ in range(32):
        d = (d * 1664525 + 1013904223) % 4294967296
        out.append(chr(33 + (d % 93)))
    return "".join(out)


def _doujin_stream_decode(hexdata, key):
    raw = bytes.fromhex(hexdata)
    out = []
    n = 42
    kl = len(key)
    for x, f in enumerate(raw):
        p = ord(key[x % kl])
        S = f ^ p ^ (x * 13) ^ n
        out.append(S & 0xFF)
        n = (n + f) % 256
    return bytes(out)


def _doujin_decrypt(hexdata):
    now_ms = time.time() * 1000
    h = int(math.floor(now_ms / 3600000.0))
    last = None
    for cand in (h, h - 1, h + 1):
        try:
            raw = _doujin_stream_decode(hexdata, _doujin_key(cand))
            text = unquote(raw.decode("latin1"))
            return json.loads(text), cand
        except Exception as ex:
            last = ex
    raise last


def _doujin_parse(raw):
    obj = None
    try:
        obj = json.loads(raw.decode('utf-8-sig', 'ignore'))
    except Exception:
        return None
    enc = None
    if isinstance(obj, dict):
        enc = obj.get('_enc_resp_')
    if isinstance(enc, str):
        res = _doujin_decrypt(enc)
        return res[0]
    return obj


def _doujin_headers():
    return {
        'User-Agent': UA,
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'id,en;q=0.8',
        'X-App-Secret': DOUJIN_APP_SECRET,
        'X-Device-ID': DOUJIN_DEVICE_ID,
        'Referer': DOUJIN_WEB + '/',
        'Origin': DOUJIN_WEB,
    }


def _doujin_api_get(path):
    p = path
    if not p.startswith('/'):
        p = '/' + p
    url = DOUJIN_API_BASE + p
    req = Request(url, headers=_doujin_headers())
    with urlopen(req, timeout=30, context=_SSL_CTX) as r:
        return _doujin_parse(r.read())


def _is_doujindesu_entry(entry):
    url = ''
    try:
        url = (entry.get('url') or '').strip()
    except Exception:
        pass
    kind = ''
    try:
        kind = (entry.get('kind') or '').strip().lower()
    except Exception:
        pass
    if kind == 'doujindesu':
        return True
    if not url:
        return False
    host = ''
    try:
        host = (urlsplit(url).hostname or '').lower()
    except Exception:
        return False
    if host == 'doujin.desu.xxx':
        return True
    if host.endswith('.doujin.desu.xxx'):
        return True
    return False
def _doujin_fill_images(chapters):
    """Isi URL gambar bab lewat API /api/chapters/<id>; nilai incremental:
    bab yang sudah punya `images` dilewati. URL gambar bertanda tangan dan
    basi ~24 jam, jadi bab yang gambarnya kedaluwarsa tetap di-fetch ulang."""
    cutoff = fresh_cutoff()
    pending = []
    for c in chapters:
        ext = c.get('external') or ''
        if ext and not c.get('images') and chapter_within_window(c, cutoff):
            pending.append(c)
    if cutoff:
        old = [c for c in chapters
               if (c.get('external') or '') and not c.get('images')
               and not chapter_within_window(c, cutoff)]
        if old:
            print('  [gambar] jendela segar %s: %d bab lama dilewati'
                  % (cutoff, len(old)))
    if not pending:
        return chapters
    t0 = time.time()
    fetched = 0
    dur = secs(t0)
    for c in pending:
        chid = ''
        try:
            ext = c.get('external') or ''
            chid = ext.rsplit('/', 1)[-1]
        except Exception:
            chid = ''
        try:
            data = _doujin_api_get('/chapters/' + chid)
            urls = []
            if isinstance(data, dict):
                raw_urls = data.get('content_urls') or []
                for u in raw_urls:
                    if isinstance(u, str):
                        if u.strip().startswith('http'):
                            urls.append(u)
            if urls:
                c['images'] = urls
                fetched = fetched + 1
                created = None
                if isinstance(data, dict):
                    created = data.get('created_at')
                if not c.get('date'):
                    if created:
                        c['date'] = str(created)[:10]
                label = chapter_label(c)
                n_urls = '%d' % len(urls)
                dur = secs(t0)
                logv('  [gambar] %s: %s gambar diambil' % (label, n_urls))
            else:
                label = chapter_label(c)
                print('  [gambar] ' + label + ': 0 gambar ditemukan')
        except Exception as ex:
            slug = c.get('slug')
            print('   ! gambar bab `' + str(slug) + '` gagal: ' + str(ex))
        polite_delay()
    total = str(fetched)
    n_pend = '%d' % len(pending)
    print('-> gambar terambil: ' + total + '/' + n_pend + ' bab (' + dur + ')')
    return chapters


def _doujin_slug_from_url(url):
    u = (url or '').split('?')[0].rstrip('/')
    seg = [x for x in u.split('/') if x]
    if not seg:
        return ''
    return seg[-1]


def _doujin_ch_num_key(c2):
    num = c2.get('num')
    if num is None:
        return 1e18
    try:
        return float(num)
    except Exception:
        return 1e18


def _doujin_scrape_series(entry, want_images=False, want_dates=False):
    """Scrape SATU seri doujindesu lewat API:
      - URL /manga/<slug>/ -> /api/manga/<slug> (metadata + daftar bab);
      - URL /reader/<id>   -> /api/chapters/<id> dulu utk mencari manga_slug."""
    url = (entry.get('url') or '').strip()
    slug = _doujin_slug_from_url(url)
    t0 = time.time()
    data = None
    if '/reader/' in url:
        chid = slug
        ch = _doujin_api_get('/chapters/' + chid)
        if isinstance(ch, dict):
            slug = ch.get('manga_slug') or ''
    if not slug:
        raise ValueError('slug doujindesu kosong: ' + url)
    print('  [seri] memuat API: %s' % (DOUJIN_WEB + '/manga/' + slug))
    data = _doujin_api_get('/manga/' + slug)
    if not isinstance(data, dict):
        raise ValueError('respons manga doujindesu tidak valid')
    title = clean_title(data.get('title') or slug)
    desc = data.get('description') or ''
    cover = data.get('cover_url') or ''
    status_raw = data.get('status') or ''
    status = str(status_raw).strip().capitalize()
    if status.lower() == 'ongoing':
        status = 'Berjalan'
    mtype = data.get('type') or ''
    author = ''
    tlist = data.get('term_list') or ''
    if isinstance(tlist, str):
        for seg in tlist.split('|'):
            bits = seg.split(':')
            if len(bits) == 3 and bits[1] == 'author':
                author = bits[0]
                break
    alt_title = data.get('alt_titles') or ''
    slug2 = data.get('slug') or slug
    genres = []
    for g in (data.get('manga_genres') or []):
        if isinstance(g, dict):
            gg = g.get('genres') or {}
            nm = gg.get('name') or ''
            if nm:
                genres.append(nm)
    chapters = []
    for ch in (data.get('chapters') or []):
        if not isinstance(ch, dict):
            continue
        chid = ch.get('id') or ''
        num = ch.get('chapter_number')
        if num is None:
            num = 1
        try:
            num = float(num)
        except Exception:
            pass
        nstr = str(num)
        try:
            if float(num).is_integer():
                nstr = str(int(num))
        except Exception:
            pass
        ctitle = ch.get('title') or ('Chapter ' + nstr)
        ch_slug = slugify(slug2 + '-chapter-' + nstr)
        ext = ''
        if chid:
            ext = DOUJIN_WEB + '/reader/' + chid
        c = {'title': ctitle or 'Chapter'}
        if num is not None:
            c['num'] = num
        if ext:
            c['external'] = ext
        c['slug'] = ch_slug
        c['images'] = []
        created = ch.get('created_at') or ''
        if created:
            c['date'] = str(created)[:10]
        chapters.append(c)
    if not chapters:
        raise ValueError('tidak ada bab di ' + url)
    # urutkan bab: naik (paling lama di bawah -> dibaca dulu)
    chapters.sort(key=_doujin_ch_num_key)
    # Tempel kembali gambar/tanggal bab dari file seri lama (hasil restore dari
    # Cloudflare). Bab yang ALREADY punya URL gambar TIDAK di-fetch ulang lewat
    # API (incremental: hanya bab yang gambarnya kosong/hapus yang diisi ulang).
    chapters = merge_existing_chapter_data(chapters, slug2)
    if want_images:
        chapters = _doujin_fill_images(chapters)
    last_upd = data.get('updated_at') or ''
    if last_upd:
        last_upd = str(last_upd)[:10]
    print('  [seri] judul     : %s' % title)
    print('  [seri] status    : %s' % status)
    print('  [seri] type      : %s' % mtype)
    print('  [seri] genre     : %d (%s)' % (len(genres), ', '.join(genres[:8])))
    print('  [seri] cover     : %s' % ('ada' if cover else 'TIDAK ADA'))
    print('  [seri] slug      : %s' % slug2)
    print('  [seri] bab       : %d' % len(chapters))
    print('  [seri] update    : %s' % (last_upd or '-'))
    print('  [seri] durasi    : %s (API)' % secs(t0))
    return {
        'slug': slug2,
        'source_url': DOUJIN_WEB + '/manga/' + slug2 + '/',
        'title': title,
        'desc': desc,
        'keywords': tlist if isinstance(tlist, str) else '',
        'status': status,
        'type': mtype,
        'author': author,
        'illustrator': '',
        'alt_title': alt_title,
        'genres': genres,
        'cover_url': cover,
        'last_updated': last_upd,
        'chapters': chapters,
    }
    num = c2.get('num')
    if num is None:
        return 1e18
    try:
        return float(num)
    except Exception:
        return 1e18
# -------------------------------------------------------------- adaptor


class DoujindesuAdapter(SourceAdapter):
    """Adaptor untuk doujin.desu.xxx: SPA dengan API terenkripsi."""

    name = 'doujindesu'
    description = 'API terenkripsi doujin.desu.xxx (SPA)'

    def matches(self, entry):
        return _is_doujindesu_entry(entry)

    def match_url(self, url):
        url = (url or '').strip()
        if not url:
            return False
        try:
            host = (urlsplit(url).hostname or '').lower()
        except Exception:
            return False
        return host == 'doujin.desu.xxx' or host.endswith('.doujin.desu.xxx')

    def is_listing_url(self, url):
        # doujin.desu.xxx/explore (+query search/genre/page/) adalah direktori
        # daftar seri, bukan satu halaman seri.
        try:
            path = (urlsplit(url or '').path or '').rstrip('/').lower()
        except Exception:
            path = (url or '').strip().split('?', 1)[0].rstrip('/').lower()
        return path in ('/explore', '/doujin', '/manhwa')

    def expand_seed(self, entry, want_images):
        """Halaman /explore -> daftar entri seri (loop paginasi offset).

        Endpoint: /manga?limit=24&offset={(page-1)*24}.
        Header respons x-total-count memberitahu jumlah total seri."""
        url = (entry.get('url') or '').strip()
        if not self.is_listing_url(url):
            return [entry]
        try:
            qd = dict(pair.split('=', 1) for pair in
                      (url.split('?', 1)[1].split('&') if '?' in url else [])
                      if '=' in pair)
            page0 = int(qd.get('page', '1') or '1')
        except Exception:
            page0 = 1
        limit = 24
        offset = (max(1, page0) - 1) * limit
        out = []
        seen = set()
        total = None
        cap = int(os.environ.get('DOUJIN_EXPLORE_MAX', '400'))
        while True:
            qs = 'limit=%d&offset=%d&sort=newest' % (limit, offset)
            data = None
            try:
                req = Request(DOUJIN_API_BASE + '/manga?' + qs,
                              headers=_doujin_headers())
                with urlopen(req, timeout=30, context=_SSL_CTX) as r:
                    raw0 = r.read()
                got = r.headers.get('x-total-count')
                if got:
                    total = int(got)
                obj0 = _doujin_parse(raw0)
                if isinstance(obj0, list):
                    data = obj0
            except Exception as ex:
                print('   ! explore offset %d gagal: %s' % (offset, ex))
                break
            if not data:
                break
            for it in data:
                if not isinstance(it, dict):
                    continue
                slug = (it.get('slug') or '').strip()
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                out.append({'url': DOUJIN_WEB + '/manga/' + slug + '/'})
            if total is not None and offset + len(data) >= total:
                break
            if len(data) < limit:
                break
            if len(out) >= cap:
                print('   ! capai batas DOUJIN_EXPLORE_MAX=%d seri; berhenti'
                      % cap)
                break
            offset += limit
        print('[explore] %d seri dari doujin.desu.xxx (total=%s)'
              % (len(out), total))
        return out

    def scrape_series(self, entry, want_images=False, want_dates=False):
        return _doujin_scrape_series(entry, want_images=want_images,
                                     want_dates=want_dates)

    def sitemap_series_entries(self, sitemap_url):
        # /explore dipakai sebagai "sitemap" (daftar seri generik).
        return self.expand_seed(
            {'url': 'https://doujin.desu.xxx/explore/'}, False)

    def refresh_chapter(self, series, chapter):
        """Ambil ulang URL gambar (bertanda tangan, basi ~24 jam) lewat API."""
        ext = (chapter.get('external') or '').strip()
        chid = ext.rsplit('/', 1)[-1] if ext else ''
        if not chid:
            return None
        data = _doujin_api_get('/chapters/' + chid)
        urls = []
        if isinstance(data, dict):
            for u in (data.get('content_urls') or []):
                if isinstance(u, str) and u.strip().startswith('http'):
                    urls.append(u)
        if not urls:
            return None
        created = ''
        if isinstance(data, dict):
            created = data.get('created_at') or ''
        cdate = chapter.get('date') or (str(created)[:10] if created else '')
        return urls, cdate

    def test(self):
        urls = [
            'https://doujin.desu.xxx/manga/punifuwa-esthe-de-yuruama-oshasee-suru',
            '/reader/12345',
            '',
        ]
        for u in urls:
            print('%r -> slug %r' % (u, _doujin_slug_from_url(u)))
        chs = [{'num': 2}, {'num': 1}, {'title': 'Awal'}]
        chs.sort(key=_doujin_ch_num_key)
        print(json.dumps(chs, ensure_ascii=False))
        print('-> self-test Doujindesu OK')


DOUJINDESU_ADAPTER = register_adapter(DoujindesuAdapter())


# ---------------------------------------------------------------- main


def main():
    from scraper_common import run, delete_series, refresh_series_images
    if '--test' in sys.argv:
        DOUJINDESU_ADAPTER.test()
    elif '--delete' in sys.argv:
        sys.exit(delete_series())
    elif '--refresh-images' in sys.argv:
        sys.exit(refresh_series_images(adapter=DOUJINDESU_ADAPTER))
    else:
        sys.exit(run(DOUJINDESU_ADAPTER))


if __name__ == '__main__':
    main()