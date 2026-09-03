# -*- coding: utf-8 -*-
"""
Scraper #1 — KomikIndo (dan situs HTML bertema WordPress/Madara).

Menangani halaman seri & halaman daftar (listing) berbentuk HTML statis:
  https://komikindo.ch/komik/<slug>/         (halaman seri)
  https://komikindo.ch/komik-terbaru/        (halaman daftar, bisa /page/N/)

Hanya entri milik sumber ini yang diproses (sources.json / manual-batch /
tempelan URL). Entri doujindesu (kind "doujindesu") dan mikoroku (kind
"json"/"github-json") diserahkan ke scraper_*.py masing-masing.

Jalankan:
  python scraper_komikindo.py [--images|--dates|--test|--delete|--refresh-images]
"""
import re
import sys
import json
import time
import html as H
from urllib.parse import urljoin, urlsplit

from scraper_common import (
    CH_RE, MAX_IMAGE_CHAPTERS, MAX_CHAPTER_DATES, PARALLEL_MIN_CHAPTERS,
    SCRAPE_WORKERS,
    SourceAdapter, register_adapter, sitemap_series_entries,
    fetch, polite_delay, pause_on_failures, logv, secs, slugify, clean,
    clean_title, chapter_label, chapter_images_ok, _parallel_for,
    _is_asset_url, series_slug_from_url, resolve_canonical_slug,
    merge_existing_chapter_data, extract_chapter_date, extract_last_updated,
    fresh_cutoff, chapter_within_window,
)

ANCHOR_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                       re.I | re.S)
TAG_RE = re.compile(r'<[^>]+>')
OG_IMG_RE = re.compile(
    r'<meta\b[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)', re.I)
OG_T_RE = re.compile(
    r'<meta\b[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)', re.I)
TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)
GENRE_RE = re.compile(
    r'<a\b[^>]*href=["\'][^"\']*/genre/[^"\']*["\'][^>]*>(.*?)</a>', re.I | re.S)
GENRE_CLASS_RE = re.compile(r'\bgenres-([a-z0-9]+)\b', re.I)

IMG_TAG_RE = re.compile(r'<img\b[^>]*>', re.I)
SRC_ATTR_RE = re.compile(r'\bsrc\s*=\s*["\']([^"\']+)["\']', re.I)
CHIMG_RE = re.compile(r'<div[^>]*id="chimg-[^"]*"[^>]*>(.*?)</div>',
                      re.I | re.S)
CHIMG_CLASS_RE = re.compile(
    r'<div[^>]*class="[^"]*\bchapter-image\b[^"]*"[^>]*>(.*?)</div>',
    re.I | re.S)
DESC_RE = re.compile(
    r'<div[^>]*class="[^"]*\bentry-content-single\b[^"]*"[^>]*>(.*?)</div>',
    re.I | re.S)
META_DESC_RE = re.compile(
    r'<meta[^>]*name="description"[^>]*content="([^"]*)"', re.I)
INFO_ROW_RE = re.compile(r'<b>\s*(.*?)\s*</b>\s*(.*?)(?=<b>\s*|$)',
                         re.I | re.S)

# label di halaman sumber -> kunci JSON (dinormalisasi huruf kecil).
INFO_KEYMAP = {
    'status': 'status',
    'pengarang': 'author',
    'penulis': 'author',
    'ilustrator': 'illustrator',
    'artist': 'illustrator',
    'judul alternatif': 'alt_title',
    'genre': 'genres',
    'tema': 'themes',
    'jenis komik': 'type',
    'type': 'type',
    'rating': 'rating',
    'rilis': 'released',
    'updated': 'updated',
}


def parse_chapter_links(html, base_url):
    rows, seen = [], set()
    for m in ANCHOR_RE.finditer(html):
        href, label = m.group(1), clean_title(m.group(2))
        if not label:
            continue
        if 'chapter' not in (label + ' ' + href).lower():
            continue
        if href.startswith(('mailto:', 'javascript:', '#')):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        nm = CH_RE.search(label)
        rows.append({'title': label, 'external': url,
                     'num': float(nm.group(1)) if nm else None})
    used = set()
    for c in rows:
        base = slugify(c['title'])[:40] or 'chapter'
        s, k = base, 2
        while s in used:
            s = '%s-%d' % (base, k)
            k += 1
        used.add(s)
        c['slug'] = s
    return rows


# ---------------------------------------------------------------- info seri


def extract_genres(html):
    out = []
    for m in GENRE_RE.finditer(html):
        l = clean(m.group(1))
        if l and l not in out:
            out.append(l)
    # fallback: class CSS gaya genres-action genres-comedy, dll.
    if not out:
        for m in GENRE_CLASS_RE.finditer(html):
            l = m.group(1).replace('-', ' ').strip()
            if l:
                l = ' '.join(w.capitalize() for w in l.split())
            if l and l not in out:
                out.append(l)
    return out[:10]


def extract_desc(html):
    m = DESC_RE.search(html)
    if m:
        d = clean(m.group(1))
        if len(d) > 20:
            return d
    m = META_DESC_RE.search(html)
    if m:
        return clean(m.group(1))
    return ''


def extract_series_info(html):
    """Ambil baris '<b>Label</b> Value' dan petakan ke key skema."""
    info = {}
    for m in INFO_ROW_RE.finditer(html):
        label = re.sub(r'\s+', ' ', m.group(1)).strip()
        label = re.sub(r':\s*$', '', label.lower())
        val = clean(m.group(2))
        if not label or not val:
            continue
        key = INFO_KEYMAP.get(label)
        if not key:
            continue
        if key == 'genres':
            info.setdefault('genres', []).extend(
                [x for x in re.split(r'[,;]', val) if x])
        else:
            info[key] = val
    return info


def find_series_url(html, base_url):
    """Jika URL sumber ternyata halaman bab (berisi reader), cari tautan ke
    halaman daftar seri (mis. `<a ...>Daftar Chapter</a>` atau `/komik/..`)."""
    if not re.search(r'id="chimg-|class="[^"]*\bchapter-image\b[^"]*"',
                     html, re.I):
        return None
    m = re.search(r'<a\b[^>]*href="([^"]+)"[^>]*>(?:(?!</a>).)*Daftar\s+Chapter',
                  html, re.I | re.S)
    if not m:
        m = re.search(
            r'<a\b[^>]*href="([^"]*/(?:komik|manga|seri)/[^"]*)"[^>]*>',
            html, re.I)
    if m:
        out = urljoin(base_url, m.group(1))
        if re.search(r'/(?:komik|manga|seri)/', out):
            return out
    return None
# ---------------------------------------------------------------- gambar


IMG_ATTR_ORDER = ('data-src', 'data-lazy-src', 'data-original', 'data-url',
                  'data-echo', 'data-srcset', 'src')


def _img_src_from_tag(tag):
    """Ambil URL dari satu tag <img>; dukung lazy-load (data-src/dst) & srcset.

    URL di path CDN sumber bisa mengandung SPASI (mis. '4 End' pada
    /data/35777737/4 End/..); spasi diubah jadi '%20' supaya URL tidak
    terpotong dan tetap valid saat disimpan/ditampilkan."""
    for attr in IMG_ATTR_ORDER:
        m = re.search(attr + r'\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if not m:
            continue
        val = m.group(1).strip()
        if not val or val.startswith('data:') or val.startswith('java'):
            continue
        if 'srcset' in attr:
            cands = [c.strip() for c in val.split(',') if c.strip()]
            if not cands:
                continue
            # pilih kandidat terbesar (paling akhir) yang URL-nya benar-benar
            # berakhiran ekstensi gambar; ini juga menangani URL ber-spasi yang
            # membuat token `split(' ')[0]` terpotong.
            chosen = ''
            for cand in reversed(cands):
                mc = re.search(r'(https?://[^"\s]+?\.(?:jpe?g|png|webp|gif|avif))',
                               cand, re.I)
                if mc:
                    chosen = mc.group(1)
                    break
            if not chosen:
                continue
            val = chosen
        return re.sub(r'\s+', '%20', val)
    return None


def _collect_img_urls(texts, base_url):
    """Kumpulkan URL gambar dari satu/beberapa fragmen HTML (dedupe)."""
    out, seen = [], set()
    for frag in texts:
        for tag in IMG_TAG_RE.finditer(frag):
            u = _img_src_from_tag(tag.group(0))
            if not u:
                continue
            u = H.unescape(u)
            # URL bisa berisi spasi di path (sumber menulis '4 End'); ubah jadi
            # %20 agar valid & tidak terpotong. JANGAN pakai split(' ')[0].
            u = re.sub(r'\s+', '%20', u)
            if u.startswith('//'):
                u = 'https:' + u
            elif u.startswith('/'):
                u = urljoin(base_url, u)
            if not u.startswith('http'):
                continue
            low = u.lower().split('?')[0].split('#')[0]
            if not re.search(r'\.(jpe?g|png|webp|gif|avif)$', low):
                continue
            if 'wp-content/uploads' not in low and '/wp-content/' in low:
                continue  # aset tema di luar uploads (logo/plugin)
            if _is_asset_url(u):
                continue
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
    return out


def parse_chapter_images(html, base_url):
    """Kumpulkan URL gambar halaman bab.

    Prioritas:
      1) container resmi (<div id="chimg-.."> / .chapter-image);
      2) kalau kosong/tak ada, scan SEMUA <img> di halaman (mendukung
         lazy-load data-src/srcset) agar gambar tetap didapat walau struktur
         halaman beda — gambar tidak di-skip begitu saja.
    """
    m = CHIMG_RE.search(html) or CHIMG_CLASS_RE.search(html)
    if m:
        out = _collect_img_urls([m.group(1)], base_url)
        if out:
            return out
        # container ada tapi kosong -> tetap coba scan seluruh halaman
    return _collect_img_urls([html], base_url)
def fill_chapter_images(chapters):
    """Ambil gambar bab yang BELUM punya gambar benar. Bab dengan gambar benar
    DILEWATI (incremental); bab dengan URL gambar salah/rusak di-fetch ulang
    agar mendapat URL yang benar. Bila jumlah bab yang perlu diambil melebihi
    PARALLEL_MIN_CHAPTERS (50) dan SCRAPE_WORKERS>1, fetching dijalankan
    PARALEL. Setelah itu `date` bab yang kosong diisi via fill_chapter_dates."""
    t0 = time.time()
    consec = 0
    cutoff = fresh_cutoff()
    pending = [c for c in chapters
               if c.get('external') and not chapter_images_ok(c)
               and chapter_within_window(c, cutoff)]
    if cutoff:
        old = [c for c in chapters
               if c.get('external') and not chapter_images_ok(c)
               and not chapter_within_window(c, cutoff)]
        if old:
            print('  [gambar] jendela segar %s: %d bab lama dilewati'
                  % (cutoff, len(old)))
    tries = {id(c): 0 for c in pending}
    n_skip = len(chapters) - len(pending)
    if n_skip:
        print('  [gambar] %d bab sudah punya gambar benar (dilewati, '
              'incremental)' % n_skip)
    cap = MAX_IMAGE_CHAPTERS or len(pending)
    work = pending[:cap]
    if len(work) < len(pending):
        print('  [gambar] batas MAX_IMAGE_CHAPTERS=%d; sisa %d bab dilewati.'
              % (cap, len(pending) - len(work)))
    fetched = 0

    def worker(c):
        t1 = time.time()
        try:
            html = fetch(c['external'])
            imgs = parse_chapter_images(html, c['external'])
            if imgs:
                c['images'] = imgs
                if not c.get('date'):
                    c['date'] = extract_chapter_date(html)
                logv('  [gambar] %s: %d gambar diambil (%s)'
                     % (chapter_label(c), len(imgs), secs(t1)))
                polite_delay()
                return True
            print('  [gambar] %s: 0 gambar ditemukan' % chapter_label(c))
        except Exception as ex:
            print('   ! gambar bab `%s` gagal: %s' % (c.get('slug'), ex))
        polite_delay()
        return False

    if len(work) > PARALLEL_MIN_CHAPTERS and SCRAPE_WORKERS > 1:
        print('  [gambar] MODE PARALEL: %d bab (>%d) diambil dengan %d worker'
              % (len(work), PARALLEL_MIN_CHAPTERS, SCRAPE_WORKERS))
        ok, _fail = _parallel_for(work, worker, SCRAPE_WORKERS, 'gambar')
        fetched += ok
        # bab yang masih gagal / gambarnya tdk ketemu: ulangi secara krucut
        # (maks 2x) supaya pola retry selaras dgn mode sequential.
        retry = [c for c in work if not chapter_images_ok(c)]
        while retry and fetched < cap:
            c = retry.pop(0)
            tries[id(c)] += 1
            if tries[id(c)] > 2:
                continue
            if worker(c):
                fetched += 1
            polite_delay()
    else:
        # jalur sequential (standar): incremental + retry
        while work:
            c = work.pop(0)
            if MAX_IMAGE_CHAPTERS and fetched >= MAX_IMAGE_CHAPTERS:
                print('  [gambar] batas MAX_IMAGE_CHAPTERS=%d tercapai; sisa '
                      '%d bab dilewati.' % (MAX_IMAGE_CHAPTERS, len(work) + 1))
                break
            tries[id(c)] += 1
            if tries[id(c)] > 2:
                continue   # sudah 2x gagal, serahkan ke run berikutnya
            t1 = time.time()
            try:
                html = fetch(c['external'])
                imgs = parse_chapter_images(html, c['external'])
                if imgs:
                    c['images'] = imgs
                    if not c.get('date'):
                        c['date'] = extract_chapter_date(html)
                    fetched += 1
                    consec = 0
                    logv('  [gambar] %s: %d gambar diambil (%s)'
                         % (chapter_label(c), len(imgs), secs(t1)))
                else:
                    # Halaman termuat tapi gambar tak ketemu -> JANGAN di-skip:
                    # coba lagi di akhir run ini; run berikutnya tetap mencoba.
                    if tries[id(c)] < 2:
                        work.append(c)
                    else:
                        c['images'] = []
                    print('  [gambar] %s: 0 gambar ditemukan (akan dicoba lagi)'
                          % chapter_label(c))
            except Exception as ex:
                consec += 1
                print('   ! gambar bab `%s` gagal: %s' % (c.get('slug'), ex))
                consec = pause_on_failures(consec)
                if not c.get('images') and tries[id(c)] < 2:
                    work.append(c)   # coba sekali lagi di akhir sambil lalu
            if MAX_IMAGE_CHAPTERS and fetched >= MAX_IMAGE_CHAPTERS:
                break
            polite_delay()
    n = sum(1 for c in chapters if chapter_images_ok(c))
    print('-> gambar terambil: %d/%d bab (%s)' % (n, len(chapters), secs(t0)))
    # Isi tanggal utk bab yang BELUM punya `date` (mencakup bab yang sudah
    # punya gambar dari run sebelumnya). Incremental: bab ber-tanggal dilewati.
    return fill_chapter_dates(chapters)
def fill_chapter_dates(chapters):
    """Isi `date` bab yang belum punya tanggal (incremental + retry).
    Otomatis paralel bila bab yang perlu diisi > PARALLEL_MIN_CHAPTERS."""
    t0 = time.time()
    consec = 0
    cutoff = fresh_cutoff()
    pending = [c for c in chapters
               if c.get('external') and not c.get('date')
               and chapter_within_window(c, cutoff)]
    if cutoff:
        old = [c for c in chapters
               if c.get('external') and not c.get('date')
               and not chapter_within_window(c, cutoff)]
        if old:
            print('  [tanggal] jendela segar %s: %d bab lama dilewati'
                  % (cutoff, len(old)))
    tries = {id(c): 0 for c in pending}
    n_skip = len(chapters) - len(pending)
    if n_skip:
        print('  [tanggal] %d bab sudah punya tanggal (dilewati, incremental)'
              % n_skip)
    cap = MAX_CHAPTER_DATES or len(pending)
    work = pending[:cap]
    if len(work) < len(pending):
        print('  [tanggal] batas MAX_CHAPTER_DATES=%d; sisa %d bab dilewati.'
              % (cap, len(pending) - len(work)))
    fetched = 0

    def worker(c):
        t1 = time.time()
        try:
            d = extract_chapter_date(fetch(c['external']))
            if d:
                c['date'] = d
                logv('  [tanggal] %s: %s (%s)'
                     % (chapter_label(c), d, secs(t1)))
                polite_delay()
                return True
        except Exception as ex:
            print('   ! tanggal bab `%s` gagal: %s' % (c.get('slug'), ex))
        polite_delay()
        return False

    if len(work) > PARALLEL_MIN_CHAPTERS and SCRAPE_WORKERS > 1:
        print('  [tanggal] MODE PARALEL: %d bab (>%d) diisi dengan %d worker'
              % (len(work), PARALLEL_MIN_CHAPTERS, SCRAPE_WORKERS))
        ok, _fail = _parallel_for(work, worker, SCRAPE_WORKERS, 'tanggal')
        fetched += ok
        retry = [c for c in work if not c.get('date')]
        while retry and fetched < cap:
            c = retry.pop(0)
            tries[id(c)] += 1
            if tries[id(c)] > 2:
                continue
            if worker(c):
                fetched += 1
            polite_delay()
    else:
        while work:
            c = work.pop(0)
            if MAX_CHAPTER_DATES and fetched >= MAX_CHAPTER_DATES:
                print('  [tanggal] batas MAX_CHAPTER_DATES=%d tercapai; sisa '
                      '%d bab dilewati.' % (MAX_CHAPTER_DATES, len(work) + 1))
                break
            tries[id(c)] += 1
            if tries[id(c)] > 2:
                continue   # sudah 2x gagal, serahkan ke run berikutnya
            t1 = time.time()
            try:
                d = extract_chapter_date(fetch(c['external']))
                if d:
                    c['date'] = d
                    fetched += 1
                    logv('  [tanggal] %s: %s (%s)'
                         % (chapter_label(c), d, secs(t1)))
                    consec = 0
            except Exception as ex:
                consec += 1
                print('   ! tanggal bab `%s` gagal: %s' % (c.get('slug'), ex))
                consec = pause_on_failures(consec)
                if not c.get('date') and tries[id(c)] < 2:
                    work.append(c)   # coba sekali lagi di akhir
            if MAX_CHAPTER_DATES and fetched >= MAX_CHAPTER_DATES:
                break
            polite_delay()
    n = sum(1 for c in chapters if c.get('date'))
    print('-> tanggal bab terisi: %d/%d bab (%s)' % (n, len(chapters), secs(t0)))
    return chapters
# ---------------------------------------------------------------- listing


# Path direktori berisi daftar banyak manga (mis. /komik/, /komik/page/2/,
# /komik-terbaru/, /komik-terbaru/page/2/).
LISTING_PATH_RE = re.compile(
    r'/((?:komik|komikindo|manga|manhwa|series|daftar-komik|manga-list'
    r'|komik-terbaru|manhwa-terbaru|manga-terbaru|komik-baru|komik-update'
    r'|komik-lengkap|komik-ongoing|komik-completed|komik-populer)'
    r'(?:/(?:page|hal|halaman)/\d+)?/?)$', re.I)

# URL halaman SERI dari sitemap: satu segmen setelah prefiks direktori seri
# (…/komik/<slug>/ dst). URL bab (…/komik/<slug>/<n>/ atau …/chapter-…/),
# genre, artikel TIDAK cocok karena punya segmen berbeda.
SERIES_URL_RE = re.compile(
    r'/(?:komik|komikindo|manga|manhwa|series|daftar-komik|manga-list|'
    r'komik-terbaru|manhwa-terbaru|manga-terbaru|komik-baru|komik-update)/'
    r'([^/?#]+?)/?$', re.I)
# Sub-sitemap yang relevan (berisi seri) disaring dari index, agar tidak
# mengikuti belasan/ ratusan sub-sitemap tak relevan (post/artikel/genre).
SITEMAP_SUB_RE = re.compile(r'(?:manga|series|komik|manhwa)', re.I)
# Batas aman maksimal sub-sitemap yang diikuti dalam sekali index (pengaman).
MAX_SITEMAP_SUBS = 40


def is_listing_url(url):
    """Benar bila URL menunjuk ke halaman direktori (daftar manga), bukan ke
    satu halaman seri. Contoh: https://komikindo.ch/komik/ -> True."""
    if not url:
        return False
    return bool(LISTING_PATH_RE.search(url.rstrip('/')))


def parse_list_links(html, base_url):
    """Ekstrak link ke halaman seri dari halaman direktori.
    Bentuk: .../komik/<slug>/ ; mengabaikan link pagination (/komik/page/N/)."""
    out, seen = [], set()
    for m in re.finditer(r'<a\b[^>]*href="([^"]+)"', html):
        href = urljoin(base_url, m.group(1))
        mm = re.search(r'/(?:komik|manga|series)/([^/.]+?)/?$', href)
        if not mm:
            continue
        seg = mm.group(1)
        if seg == 'page':
            continue  # pagination /komik/page/N/
        if href in seen:
            continue
        seen.add(href)
        slug = seg
        sx = re.match(r'^(\d+)-(.+)$', slug)
        if sx:
            slug = sx.group(2)          # 846048-eleceed -> eleceed
        if not slug or slug in ('page',):
            continue
        title = clean_title(' '.join(w.capitalize() for w in slug.split('-')))
        out.append({'url': href, 'slug': slug, 'title': title,
                    'auto_title': True})
    return out


def scrape_series_html(entry, want_images=False, want_dates=False):
    """Scrape SATU seri dari halaman HTML (tema WordPress/Madara)."""
    url = entry.get('url')
    t0 = time.time()
    print('  [seri] memuat: %s' % url)
    html = fetch(url)
    # bila URL ternyata halaman bab, ikuti tautan "Daftar Chapter" ke halaman seri
    su = find_series_url(html, url)
    if su and su != url:
        print('  [seri] URL adalah halaman bab; ikuti ke daftar chapter: %s' % su)
        html = fetch(su)
        url = su
    mt = OG_IMG_RE.search(html)
    mt_t = OG_T_RE.search(html) or TITLE_RE.search(html)
    page_title = clean_title(mt_t.group(1)) if mt_t else ''
    # Judul tebakan dari URL (mis. "one-piece" -> "One Piece") jangan dipakai
    # bila halaman sumber memberi judul lebih lengkap ("One Piece: Ace Story" ->
    # "One Piece Ace Story"). Judul eksplisit dari sources.json tetap menang.
    if entry.get('title') and not entry.get('auto_title'):
        title = clean_title(entry.get('title') or '')
    else:
        title = (page_title or clean_title(entry.get('title')
                                           or entry.get('slug') or ''))
    info = extract_series_info(html)
    genres = extract_genres(html)
    for g in (info.get('genres') or []):
        if g not in genres:
            genres.append(g)
    chapters = parse_chapter_links(html, url)
    upd = extract_last_updated(html)
    # Slug KANONIK dari URL halaman seri (bukan tebakan entry) supaya dua URL
    # sumber yang menunjuk seri sama menghasilkan slug sama -> anti-duplikat.
    slug = series_slug_from_url(url) or slugify(entry.get('slug') or '') or 'seri'
    slug = resolve_canonical_slug(slug)
    # Tempel gambar/tanggal dari file lama (incremental); bab dengan URL gambar
    # salah tetap di-fetch ulang oleh fill_chapter_images.
    chapters = merge_existing_chapter_data(chapters, slug)
    print('  [seri] judul     : %s' % title)
    print('  [seri] status    : %s' % (info.get('status') or '-'))
    print('  [seri] type      : %s' % (info.get('type') or '-'))
    print('  [seri] genre     : %d (%s)' % (
        len(genres), ', '.join(genres[:8]) + ('...' if len(genres) > 8 else '')))
    print('  [seri] cover     : %s' % ('ada (og:image)' if mt else 'TIDAK ADA'))
    print('  [seri] slug      : %s' % slug)
    print('  [seri] bab       : %d terdeteksi di halaman' % len(chapters))
    print('  [seri] update    : %s' % (upd or '-'))
    print('  [seri] durasi    : %s (sampai halaman terbaca)' % secs(t0))
    if want_images:
        chapters = fill_chapter_images(chapters)
    elif want_dates:
        chapters = fill_chapter_dates(chapters)
    return {
        'slug': slug,
        'source_url': url.rstrip('/'),
        'title': clean(title),
        'desc': extract_desc(html),
        'keywords': info.get('themes', ''),
        'status': info.get('status', ''),
        'type': info.get('type', ''),
        'author': info.get('author', ''),
        'illustrator': info.get('illustrator', ''),
        'alt_title': info.get('alt_title', ''),
        'genres': genres,
        'cover_url': mt.group(1) if mt else '',
        'last_updated': upd,
        'chapters': chapters,
    }
# ----------------------------------------------------------- adaptor KomikIndo


# Sumber yang TIDAK ditangani adaptor ini (milik scraper lain).
NOT_OURS_HOSTS = ('doujin.desu.xxx', 'mikoroku.com', 'mikodrive.my.id',
                  'raw.githubusercontent.com', 'githubusercontent.com')
JSON_EXCLUDED_KINDS = ('json', 'json-list', 'github-json', 'doujindesu')


class KomikindoAdapter(SourceAdapter):
    """Adaptor untuk situs HTML tema WordPress/Madara (mis. komikindo.ch).
    Menangani halaman seri, halaman daftar, sitemap, dan gambar bab dari HTML.
    """

    name = 'komikindo'
    description = 'Situs HTML tema WordPress/Madara (mis. komikindo.ch)'

    def matches(self, entry):
        """Entri milik adaptor ini: bukan JSON/mikoroku/doujindesu, dan host
        bukan anasir sumber lain."""
        kind = (entry.get('kind') or '').strip().lower()
        if kind in JSON_EXCLUDED_KINDS:
            return False
        return self.match_url(entry.get('url') or '')

    def match_url(self, url):
        url = (url or '').strip()
        if not url:
            return False
        try:
            host = (urlsplit(url).hostname or '').lower()
        except Exception:
            host = ''
        for h in NOT_OURS_HOSTS:
            if host == h or host.endswith('.' + h):
                return False
        return True

    def is_listing_url(self, url):
        return is_listing_url(url)

    def expand_seed(self, entry, want_images):
        url = (entry.get('url') or '').strip()
        if not is_listing_url(url) and not entry.get('listing', False):
            return [entry]
        try:
            html = fetch(url)
        except Exception as ex:
            print('   ! gagal scan halaman daftar %s: %s' % (url, ex))
            return [entry]
        links = parse_list_links(html, url)
        if not links:
            print('   ! tidak ada link manga di %s (diproses sebagai '
                  'halaman seri).' % url)
            return [entry]
        print('   -> halaman daftar: %d manga ditemukan di %s'
              % (len(links), url))
        return links

    def scrape_series(self, entry, want_images=False, want_dates=False):
        return scrape_series_html(entry, want_images=want_images,
                                  want_dates=want_dates)

    def sitemap_series_entries(self, sitemap_url):
        return sitemap_series_entries(sitemap_url, SERIES_URL_RE,
                                      SITEMAP_SUB_RE, MAX_SITEMAP_SUBS)

    def refresh_chapter(self, series, chapter):
        """Ambil ulang URL gambar & tanggal bab dari halaman HTML sumber."""
        ext = (chapter.get('external') or '').strip()
        if not ext:
            return None
        html = fetch(ext)
        imgs = parse_chapter_images(html, ext)
        cdate = chapter.get('date') or extract_chapter_date(html)
        return imgs, cdate

    def test(self):
        html = ('<a href="/genre/action">Action</a>'
                '<a href="/e-chapter-1/">Eleceed Chapter 1</a>'
                '<a href="/e-chapter-2/">Eleceed Chapter 2</a>'
                '<a href="/x">About</a>')
        print(json.dumps(parse_chapter_links(html, 'https://ac/manga/eleceed/'),
                         ensure_ascii=False, indent=2))
        ich = ('<div class="chapter-image"><div id="chimg-auh">'
               '<img src="https://cdn.example/p1.jpg">'
               '<img src="https://cdn.example/fav.png"></div></div>')
        print(json.dumps(parse_chapter_images(ich, 'https://ac/manga/'),
                         indent=2))
        print('-> self-test KomikIndo OK')


KOMIKINDO_ADAPTER = register_adapter(KomikindoAdapter())


# ---------------------------------------------------------------- main


def main():
    from scraper_common import run, delete_series, refresh_series_images
    if '--test' in sys.argv:
        KOMIKINDO_ADAPTER.test()
    elif '--delete' in sys.argv:
        sys.exit(delete_series())
    elif '--refresh-images' in sys.argv:
        sys.exit(refresh_series_images(adapter=KOMIKINDO_ADAPTER))
    else:
        sys.exit(run(KOMIKINDO_ADAPTER))


if __name__ == '__main__':
    main()