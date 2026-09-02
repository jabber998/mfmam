# -*- coding: utf-8 -*-
"""
Scraper #2 — Mikoroku.com (katalog JSON publik + feed Blogger).

mikoroku.com adalah SPA: halaman seri-nya TIDAK memuat konten HTML
server-side. Katalognya diambul dari berkas JSON publik (biasanya di
GitHub raw, lihat `kind: "json"` di sources.json), mis.:

  {
    "url": "https://raw.githubusercontent.com/moemaomao/mymangadata/main/all-manga.json",
    "kind": "json",
    "detail_prefix": "https://mikoroku.com/detail?slug=",
    "reader_prefix": "https://mikoroku.com/reader?slug=",
    "blogger_feed": "https://www.mikodrive.my.id"
  }

Daftar bab (+ gambar) per seri diambul dari feed Blogger (mikodrive.my.id).

Jalankan:
  python scraper_mikoroku.py [--images|--dates|--test|--delete|--refresh-images]
"""
import re
import sys
import json
import time
import urllib.parse as _up
from urllib.parse import urljoin, urlsplit

from scraper_common import (
    CH_RE,
    SourceAdapter, register_adapter,
    fetch_json, secs, slugify, clean_title, merge_existing_chapter_data,
)
# Tipe sumber berkas JSON katalog (daftar seri dari GitHub raw, dll).
JSON_KINDS = ('json', 'json-list', 'github-json')


def _json_item_ref(item, entry):
    """URL referensi "detail" sebuah seri dari item JSON. Pakai field `url`/
    `external` bila ada, selain itu gabung `detail_prefix` (yang di-entry)
    dengan slug seri. Prefix boleh berupa path biasa atau query (?slug=...)."""
    ref = item.get('url') or item.get('external') or item.get('link') or ''
    if not ref:
        prefix = (entry.get('detail_prefix') or '').strip()
        if prefix:
            prefix = prefix.rstrip('/')
            slug = (item.get('slug') or '').strip('/')
            if prefix.endswith(('?', '=')):
                ref = prefix + slug
            else:
                ref = prefix + '/' + slug
    return ref


def expand_json_source(entry):
    """Ubah satu entri sumber ber-kind JSON menjadi daftar entri seri.

    Mendukung:
      - JSON bertipe list (array seri) atau dict berisi list (`items`/`data`/
        `list`/`manga`/`series`).
      - Setiap item berupa objek dengan `title` (wajib) dan optional `slug`,
        `desc`, `genres`, `img`/`cover`, `status`, `type`, `author`, `artist`,
        `altTitle`, `chapters` (list bab).
      - `detail_prefix` di entri sumber -> membuat URL referensi detail seri.

    Setiap entri yang dihasilkan membawa metadata lengkap di `_meta` (dipakai
    langsung oleh scrape_series; tidak ada fetch HTML)."""
    url = (entry.get('url') or '').strip()
    try:
        data = fetch_json(url)
    except Exception as ex:
        print('   ! gagal baca JSON %s: %s' % (url, ex))
        return None
    items = data if isinstance(data, list) else None
    if items is None and isinstance(data, dict):
        for k in ('items', 'data', 'list', 'manga', 'series', 'result'):
            v = data.get(k)
            if isinstance(v, list):
                items = v
                break
    if not isinstance(items, list):
        print('   ! JSON %s tidak berupa daftar seri (type=%s).'
              % (url, type(data).__name__))
        return None
    out, seen = [], set()
    for it in items:
        if not isinstance(it, dict):
            continue
        title = clean_title(it.get('title') or '')
        if not title:
            continue
        slug = (it.get('slug') or '').strip() or slugify(title)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        meta = dict(it)
        meta['title'] = title
        meta['slug'] = slug
        ref = _json_item_ref(it, entry)
        out.append({
            'url': ref or url,
            'slug': slug,
            'title': title,
            'kind': 'json',
            'auto_title': True,
            '_meta': meta,
            # Konfigurasi sumber turunkan ke tiap entri seri agar
            # _series_from_json_meta tetap tahu cara ambil daftar bab.
            'detail_prefix': (entry.get('detail_prefix') or '').strip(),
            'reader_prefix': (entry.get('reader_prefix') or '').strip(),
            'blogger_feed': (entry.get('blogger_feed') or '').strip(),
        })
    print('   -> daftar JSON: %d seri ditemukan di %s' % (len(out), url))
    return out


def _norm_chapter(ch, base, series_slug):
    """Normalisasi satu bab dari metadata JSON menjadi dict bab skema scraper."""
    ext = (ch.get('url') or ch.get('external') or ch.get('link')
           or ch.get('reader') or '')
    if ext and not ext.startswith('http'):
        ext = urljoin(base or '', ext)
    title = clean_title(ch.get('title') or ch.get('name') or ch.get('label')
                        or (('Chapter ' + str(ch.get('num'))) if ch.get('num')
                            is not None else ''))
    num = ch.get('num')
    if num is None:
        m = CH_RE.search(title)
        if m:
            num = float(m.group(1))
    images = ch.get('images') or ch.get('image') or ch.get('img') or []
    if isinstance(images, str):
        images = [images]
    images = [u for u in images if isinstance(u, str) and u.strip()]
    c = {'title': title or 'Chapter'}
    if num is not None:
        c['num'] = num
    if ext:
        c['external'] = ext
    c['slug'] = slugify(series_slug + '-chapter-' + str(num)
                        if num is not None else title)
    if images:
        c['images'] = images
    return c
# ------------------------------------------------- daftar bab dari feed Blogger
# Situs SPA seperti mikoroku.com menyimpan daftar bab per seri di feed Blogger
# (mis. https://www.mikodrive.my.id/feeds/posts/default?q=<judul seri>).
# Tiap entry feed = satu bab, dan konten entry berisi URL gambar bab.


def _blogger_images_from_content(content):
    """Kumpulkan URL gambar bab dari HTML konten feed Blogger.
    Untuk tiap <img>: pakai `data-original` (ukuran penuh) dulu, fallback `src`;
    buang jejak (logo/avatar/icon/placeholder)."""
    urls = []
    for tag in re.finditer(r'<img\b[^>]*>', content or '', re.I):
        src = tag.group(0)
        m = re.search(r'data-original\s*=\s*"([^"]+)"', src, re.I) \
            or re.search(r'src\s*=\s*"([^"]+)"', src, re.I)
        if not m:
            continue
        u = m.group(1).strip()
        low = u.lower()
        if not u.startswith('http'):
            continue
        if any(x in low for x in ('logo', 'avatar', 'icon', 'favicon',
                                  'placeholder', 'emoji', '/favicon')):
            continue
        if not re.search(r'\.(?:jpe?g|png|webp|gif)(?:[?#]|$)', low) \
                and 'googleusercontent' not in low:
            continue
        if u not in urls:
            urls.append(u)
    return urls


def fetch_blogger_chapters(title, feed_base='https://www.mikodrive.my.id',
                           reader_prefix='', want_images=True):
    """Ambil daftar bab (+gambar) satu seri dari feed Blogger.

    Feed `.../feeds/posts/default?alt=json&max-results=500&q=<judul seri>`
    (lihat reader page mikoroku). Tiap entry = satu bab; kontennya berisi URL
    gambar bab. Kembalikan list dict bab skema scraper (title/num/external/
    images/date). Bila feed gagal/kosong, kembalikan [] (diam-diam)."""
    title = clean_title(title or '')
    if not title:
        return []
    t0 = time.time()
    url = (feed_base.rstrip('/') + '/feeds/posts/default?alt=json'
           '&max-results=500&q=' + _up.quote(title))
    print('  [bab] feed Blogger: %s' % url)
    try:
        d = fetch_json(url)
    except Exception as ex:
        print('   ! gagal baca feed bab %s: %s' % (title, ex))
        return []
    entries = (d.get('feed') or {}).get('entry') or []
    if not isinstance(entries, list):
        return []
    norm = lambda s: re.sub(r'[^a-z0-9]', '', (s or '').lower())
    n_title = norm(title)
    out = []
    for e in entries:
        t = clean_title((e.get('title') or {}).get('$t') or '')
        if not t:
            continue
        # pastikan entry ini memang bab seri bersangkutan
        if n_title and not (norm(t).startswith(n_title) or n_title in norm(t)):
            continue
        content = (e.get('content') or {}).get('$t') or ''
        images = _blogger_images_from_content(content) if want_images else []
        num = None
        mch = CH_RE.search(t)
        if mch:
            num = float(mch.group(1))
        slug = slugify(t)
        c = {'title': t}
        if num is not None:
            c['num'] = num
        if reader_prefix:
            if '?' in reader_prefix:
                if reader_prefix.endswith('='):
                    c['external'] = reader_prefix + slug
                else:
                    c['external'] = reader_prefix.rstrip('&') + '?slug=' + slug
            else:
                c['external'] = reader_prefix.rstrip('/') + '/' + slug
        if images:
            c['images'] = images
        pub = (e.get('published') or {}).get('$t') or ''
        if pub[:10] and re.match(r'\d{4}-\d{2}-\d{2}', pub):
            c['date'] = pub[:10]
        out.append(c)
    if out:
        print('   [bab] %d bab ditemukan lewat feed (%s)'
              % (len(out), secs(t0)))
    else:
        print('   [bab] feed seri %r kosong' % title)
    return out
def _series_from_json_meta(entry, want_images=False):
    """Bangun data seri langsung dari metadata JSON (tanpa fetch HTML).

    Entry dihasilkan oleh expand_json_source -- membawa `_meta` (objek JSON
    asli) dan optional `kind`. Bab dibaca dari `_meta['chapters']` (bila ada);
    URL referensi seri memakai `detail_prefix` dari entri sumber. Jika entri
    mengonfigurasi `blogger_feed`, daftar bab (+ gambar) diambil dari feed
    Blogger (cocok untuk situs SPA seperti mikoroku.com)."""
    meta = entry.get('_meta') or {}
    slug = (meta.get('slug') or entry.get('slug') or '').strip() \
        or slugify(meta.get('title') or '')
    title = clean_title(meta.get('title') or entry.get('title') or slug)
    slug = slugify(slug) or 'seri'
    genres = meta.get('genres') or []
    if isinstance(genres, str):
        genres = [g.strip() for g in genres.replace(';', ',').split(',')
                  if g.strip()]
    chapters = []
    base = (entry.get('url') or '').rsplit('/', 1)[0]
    for ch in (meta.get('chapters') or []):
        if not isinstance(ch, dict):
            continue
        chapters.append(_norm_chapter(ch, base, slug))
    # Sumber daftar bab dari feed Blogger (mis. mikoroku.com)
    blogger_feed = (entry.get('blogger_feed') or '').strip()
    if blogger_feed:
        reader_prefix = (entry.get('reader_prefix') or '').strip()
        chs = fetch_blogger_chapters(
            title, feed_base=blogger_feed,
            reader_prefix=reader_prefix,
            want_images=want_images)
        if chs:
            seen_ext = {c.get('external') for c in chapters if c.get('external')}
            for c in chs:
                if c.get('external') and c['external'] in seen_ext:
                    continue
                if c.get('external'):
                    seen_ext.add(c['external'])
                chapters.append(c)
    # Urutkan: nomor naik bila ada
    if any(c.get('num') is not None for c in chapters):
        chapters.sort(key=lambda c: (1, 0) if c.get('num') is None
                      else (0, float(c['num'])))
    chapters = merge_existing_chapter_data(chapters, slug)
    print('  [seri] judul     : %s' % title)
    print('  [seri] status    : %s' % (meta.get('status') or '-'))
    print('  [seri] type      : %s' % (meta.get('type') or '-'))
    print('  [seri] genre     : %d (%s)' % (
        len(genres), ', '.join(genres[:8]) + ('...' if len(genres) > 8 else '')))
    print('  [seri] cover     : %s'
          % ('ada' if meta.get('img') or meta.get('cover') else 'TIDAK ADA'))
    print('  [seri] slug      : %s' % slug)
    print('  [seri] bab       : %d' % len(chapters))
    print('  [seri] sumber    : %s (JSON%s)' % (
        (entry.get('url') or '-').rstrip('/'),
        ' + feed Blogger' if blogger_feed else ''))
    return {
        'slug': slug,
        'source_url': (entry.get('url') or '').rstrip('/'),
        'title': title,
        'desc': meta.get('desc') or '',
        'keywords': meta.get('keywords') or meta.get('tags') or '',
        'status': meta.get('status') or '',
        'type': meta.get('type') or '',
        'author': meta.get('author') or '',
        'illustrator': meta.get('artist') or '',
        'alt_title': meta.get('altTitle') or meta.get('alt_title') or '',
        'genres': genres,
        'cover_url': meta.get('img') or meta.get('cover') or '',
        'last_updated': meta.get('last_updated') or meta.get('updated') or '',
        'chapters': chapters,
    }
# -------------------------------------------------------------- adaptor


MIKOROKU_HOSTS = ('mikoroku.com', 'mikodrive.my.id')


class MikorokuAdapter(SourceAdapter):
    """Adaptor untuk mikoroku.com: katalog JSON publik + feed Blogger.
    Halaman HTML seri-nya SPA tanpa konten server-side, jadi metadata dibangun
    langsung dari JSON dan daftar bab dari feed Blogger.
    """

    name = 'mikoroku'
    description = 'Katalog JSON + feed Blogger (mikoroku.com)'

    def __init__(self):
        self.catalog_url = ''
        self.blogger_feed = ''
        self.reader_prefix = ''
        self._load_config()

    def _load_config(self):
        """Baca konfigurasi sumber mikoroku dari sources.json (catalog_url,
        blogger_feed, reader_prefix) supaya refresh bab tahu sumber feed."""
        try:
            from scraper_common import load_json_file, SRC
            for e in (load_json_file(SRC) or []):
                if not self.matches(e):
                    continue
                self.catalog_url = (e.get('url') or self.catalog_url or '')
                self.blogger_feed = (e.get('blogger_feed') or
                                     self.blogger_feed or '')
                self.reader_prefix = (e.get('reader_prefix') or
                                      self.reader_prefix or '')
        except Exception:
            pass

    def matches(self, entry):
        kind = (entry.get('kind') or '').strip().lower()
        if kind in JSON_KINDS:
            return True
        # URL site / prefix yang mengarah ke mikoroku
        url = (entry.get('url') or '').strip()
        if self.match_url(url):
            return True
        for k in ('detail_prefix', 'reader_prefix', 'blogger_feed'):
            if any(h in (entry.get(k) or '').lower() for h in MIKOROKU_HOSTS):
                return True
        return False

    def match_url(self, url):
        url = (url or '').strip()
        if not url:
            return False
        # URL katalog JSON publik yang dikonfigurasi (mis. GitHub raw) adalah
        # milik mikoroku juga -- dipakai routing --refresh-images.
        if self.catalog_url and url.rstrip('/') == self.catalog_url.rstrip('/'):
            return True
        try:
            host = (urlsplit(url).hostname or '').lower()
        except Exception:
            host = ''
        for h in MIKOROKU_HOSTS:
            if host == h or host.endswith('.' + h):
                return True
        return False

    def is_listing_url(self, url):
        return False

    def expand_seed(self, entry, want_images):
        if (entry.get('kind') or '').strip().lower() in JSON_KINDS:
            return expand_json_source(entry) or [entry]
        return [entry]

    def scrape_series(self, entry, want_images=False, want_dates=False):
        if (entry.get('kind') or '').strip().lower() in JSON_KINDS \
                or entry.get('_meta'):
            return _series_from_json_meta(entry, want_images=want_images)
        url = (entry.get('url') or '').strip()
        if url.lower().endswith('.json'):
            sub = expand_json_source(entry)
            if sub:
                return _series_from_json_meta(sub[0], want_images=want_images)
        raise ValueError(
            'mikoroku butuh katalog JSON (kind "json" di sources.json); '
            'halaman SPA tidak punya konten HTML untuk di-scrape: %s' % url)

    def sitemap_series_entries(self, sitemap_url):
        print('   ! mikoroku memakai katalog JSON, bukan sitemap. '
              'Buat entri `{"url": "<json>", "kind": "json", '
              '"detail_prefix": ..., "blogger_feed": ...}` di sources.json.')
        return []

    def refresh_chapter(self, series, chapter):
        """Cari ulang URL gambar bab lewat feed Blogger seri bersangkutan."""
        title = series.get('title') or ''
        if not title or not self.blogger_feed:
            return None
        chs = fetch_blogger_chapters(title, feed_base=self.blogger_feed,
                                     reader_prefix=self.reader_prefix,
                                     want_images=True)
        norm = lambda s: re.sub(r'[^a-z0-9]', '', (s or '').lower())
        want = norm(chapter.get('title') or '')
        ext = (chapter.get('external') or '').strip().rstrip('/').lower()
        for c in chs:
            if not c.get('images'):
                continue
            if want and norm(c.get('title') or '') == want:
                return c.get('images') or [], c.get('date') or ''
            if ext and (c.get('external') or '').strip().rstrip('/').lower() == ext:
                return c.get('images') or [], c.get('date') or ''
        return None

    def test(self):
        # Uji normalisasi bab JSON mini tanpa jaringan.
        ch = {'title': 'Chapter 5', 'url': '/reader?slug=seri-5',
              'num': '5'}
        out = _norm_chapter(ch, 'https://mikoroku.com/reader', 'seri-uji')
        print(json.dumps(out, ensure_ascii=False, indent=2))
        print('-> self-test Mikoroku OK')


MIKOROKU_ADAPTER = register_adapter(MikorokuAdapter())


# ---------------------------------------------------------------- main


def main():
    from scraper_common import run, delete_series, refresh_series_images
    if '--test' in sys.argv:
        MIKOROKU_ADAPTER.test()
    elif '--delete' in sys.argv:
        sys.exit(delete_series())
    elif '--refresh-images' in sys.argv:
        sys.exit(refresh_series_images(adapter=MIKOROKU_ADAPTER))
    else:
        sys.exit(run(MIKOROKU_ADAPTER))


if __name__ == '__main__':
    main()