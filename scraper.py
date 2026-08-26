# -*- coding: utf-8 -*-
"""
Scraper katalog Mfmam - versi CLOUD (GitHub Actions / Netlify build).
Membaca site-content/sources.json (dan optional site-content/manual-batch.txt),
menulis langsung ke site-content/series/<slug>.json (skema dipakai bersama
Decap CMS & _netlify_build.py).

Skema satu seri:
{
  slug, title, desc, genres[], cover_url,
  status, author, illustrator, alt_title, type, keywords,
  chapters: [ { slug, title, num, external, images[] } ]
}

Mode:
  * LINK (default): hanya judul bab + link sumber (external), tanpa gambar.
  * GAMBAR   (--images atau env SCRAPE_IMAGES=1): ikut mengambil URL gambar tiap
    bab dari halaman sumber. Gambar dicatat sebagai URL CDN/mirror sumber
    (hotlink; halaman dibangun dengan referrerpolicy="no-referrer"), jadi TIDAK
    disimpan ke repo. Bab yang sudah punya `images` tidak di-fetch ulang
    (incremental), sehingga run rutin cepat.

Pastikan Anda berhak menautkan/menampilkan sumber yang dipilih.

Jalankan:
  python scraper.py            # update (mode link)
  python scraper.py --images   # update + ambil gambar bab
  python scraper.py --test     # uji parser tanpa network
"""
import os, re, json, sys, time, html as H
from urllib.request import Request, urlopen
from urllib.parse import urljoin

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, 'site-content', 'sources.json')
SERIES_DIR = os.path.join(ROOT, 'site-content', 'series')
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      'Chrome/124.0 Safari/537.36')
DELAY = 0.5

# 0 = tanpa batas; selain itu = batas jumlah bab yang diambil gambarnya per run.
_MAX_CAP = os.environ.get('MAX_IMAGE_CHAPTERS', '')
MAX_IMAGE_CHAPTERS = int(_MAX_CAP) if _MAX_CAP.isdigit() else 0

CH_RE = re.compile(r'\b(?:chapter|bab)\s*(\d+(?:\.\d+)?)', re.I)
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


def clean(t):
    return re.sub(r'\s+', ' ', H.unescape(TAG_RE.sub('', t or ''))).strip()


def slugify(s):
    s = re.sub(r'[^a-z0-9]+', '-', (s or '').strip().lower())
    return re.sub(r'-+', '-', s).strip('-')


def fetch(url):
    req = Request(url, headers={'User-Agent': UA, 'Accept': 'text/html'})
    with urlopen(req, timeout=25) as r:
        return r.read().decode('utf-8', 'ignore')


def load_json_file(path):
    """Baca JSON toleran BOM (utf-8-sig). BOM sering muncul dari editor Windows."""
    try:
        with open(path, encoding='utf-8-sig') as fh:
            return json.load(fh)
    except Exception:
        return None


# ---------------------------------------------------------------- bab (link)


def parse_chapter_links(html, base_url):
    rows, seen = [], set()
    for m in ANCHOR_RE.finditer(html):
        href, label = m.group(1), clean(m.group(2))
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


def parse_chapter_images(html, base_url):
    """Kembalikan daftar URL gambar dari halaman bab (di dalam `<div id="chimg-">`
    atau container ber-class `chapter-image`)."""
    m = CHIMG_RE.search(html) or CHIMG_CLASS_RE.search(html)
    if not m:
        return []
    out, seen = [], set()
    for tag in IMG_TAG_RE.finditer(m.group(1)):
        sm = SRC_ATTR_RE.search(tag.group(0))
        if not sm:
            continue
        u = H.unescape(sm.group(1)).split(' ')[0]
        if u.startswith('//'):
            u = 'https:' + u
        elif u.startswith('/'):
            u = urljoin(base_url, u)
        if not u.startswith('http'):
            continue
        low = u.lower().split('?')[0]
        if not re.search(r'\.(jpe?g|png|webp|gif)$', low):
            continue
        if '/wp-content/' in low:
            continue  # logo/asset, bukan halaman bab
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def fill_chapter_images(chapters):
    """Ambil gambar bab yang belum punya `images` (incremental)."""
    fetched = 0
    for c in chapters:
        if MAX_IMAGE_CHAPTERS and fetched >= MAX_IMAGE_CHAPTERS:
            break
        ext = c.get('external')
        if not ext or c.get('images'):
            continue
        try:
            imgs = parse_chapter_images(fetch(ext), ext)
            c['images'] = imgs
            if imgs:
                fetched += 1
        except Exception as ex:
            print('   ! gambar bab `%s` gagal: %s' % (c.get('slug'), ex))
            c['images'] = []
        time.sleep(DELAY)
    n = sum(1 for c in chapters if c.get('images'))
    print('-> gambar terambil: %d/%d bab' % (n, len(chapters)))
    return chapters


# ---------------------------------------------------------------- scraping


def scrape_series(entry, want_images):
    url = entry.get('url')
    html = fetch(url)
    # bila URL ternyata halaman bab, ikuti tautan "Daftar Chapter" ke halaman seri
    su = find_series_url(html, url)
    if su and su != url:
        html = fetch(su)
        url = su
    mt = OG_IMG_RE.search(html)
    mt_t = OG_T_RE.search(html) or TITLE_RE.search(html)
    title = entry.get('title') or (clean(mt_t.group(1)) if mt_t
                                   else entry.get('slug'))
    info = extract_series_info(html)
    genres = extract_genres(html)
    for g in (info.get('genres') or []):
        if g not in genres:
            genres.append(g)
    chapters = parse_chapter_links(html, url)
    if want_images:
        chapters = fill_chapter_images(chapters)
    return {
        'slug': slugify(entry.get('slug') or title),
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
        'chapters': chapters,
    }


def write_series(slug, data):
    """Tulis site-content/series/<slug>.json; pertahankan data lama (desc,
    gambar bab yang sudah direkam) agar tidak terhapus saat di-rewrite."""
    path = os.path.join(SERIES_DIR, slug + '.json')
    prev = load_json_file(path) or {}
    old = {c.get('external'): c for c in (prev.get('chapters') or [])
           if c.get('external')}
    merged_ch, seen = [], set()
    for c in data.get('chapters') or []:
        ext = c.get('external') or ''
        if ext:
            if ext in seen:
                continue
            seen.add(ext)
            oc = old.get(ext)
            if oc and not c.get('images') and oc.get('images'):
                c['images'] = oc['images']   # jaga gambar lama (incremental)
        merged_ch.append(c)
    # pertahankan bab yang sudah ada tapi tak muncul di scrape terbaru
    for c in prev.get('chapters') or []:
        ext = c.get('external') or ''
        if ext and ext not in seen:
            seen.add(ext)
            merged_ch.append(c)
    merged = {
        'slug': slug,
        'title': data.get('title') or prev.get('title') or slug,
        'desc': data.get('desc') or prev.get('desc') or '',
        'keywords': data.get('keywords') or prev.get('keywords') or '',
        'status': data.get('status') or prev.get('status') or '',
        'type': data.get('type') or prev.get('type') or '',
        'author': data.get('author') or prev.get('author') or '',
        'illustrator': data.get('illustrator') or prev.get('illustrator') or '',
        'alt_title': data.get('alt_title') or prev.get('alt_title') or '',
        'genres': data.get('genres') or prev.get('genres') or [],
        'cover_url': data.get('cover_url') or prev.get('cover_url') or '',
        'chapters': merged_ch,
    }
    os.makedirs(SERIES_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(merged, fh, ensure_ascii=False, indent=2)
    return len(merged_ch)


# ---------------------------------------------------------------- runner


def run():
    want_images = ('--images' in sys.argv
                   or os.environ.get('SCRAPE_IMAGES', '').strip()
                   not in ('', '0'))
    entries = []
    src_data = load_json_file(SRC)
    if src_data is not None:
        entries += src_data
    manual_txt = os.path.join(ROOT, 'site-content', 'manual-batch.txt')
    if os.path.exists(manual_txt):
        with open(manual_txt, encoding='utf-8') as fh:
            for line in fh:
                url = line.strip()
                if not url or not url.startswith('http'):
                    continue
                # Lewati URL tanpa path (mis. homepage "https://site.com/") —
                # scraping homepage hanya menghasilkan bab acak dari banyak seri.
                if not re.search(r'https?://[^/]+/[^/]+', url):
                    print('   ! lewati URL tanpa path (bukan halaman seri): %s' % url)
                    continue
                seg = [s for s in url.rstrip('/').split('/') if s]
                hint = seg[-1] if seg else 'manga'
                mm = re.match(r'^(\d+)-([a-z0-9]+(?:-[a-z0-9]+)*)$', hint)
                if mm:
                    hint = mm.group(2)      # mis. 846048-eleceed -> eleceed
                title = ' '.join(x.capitalize() for x in hint.split('-'))
                entries.append({'url': url, 'slug': hint, 'title': title})
    if not entries:
        print('Tidak ada sumber (sources.json kosong & tanpa URL manual).')
        print('Isi sources.json, atau tempel URL lewat form Action.')
        return 0
    limit = None
    b = os.environ.get('BATCH_LIMIT', '')
    if b.isdigit():
        limit = int(b)
    if limit:
        entries = entries[:limit]
    total_ch = 0
    for i, e in enumerate(entries, 1):
        print('[%d/%d] %s' % (i, len(entries), e.get('url')))
        try:
            m = scrape_series(e, want_images=want_images)
        except Exception as ex:
            print('   ! gagal: %s' % ex)
            time.sleep(DELAY)
            continue
        slug = slugify(m['slug'])
        n = write_series(slug, m)
        total_ch += n
        print('   -> seri %s, %d bab (mode %s)'
              % (slug, n, 'gambar' if want_images else 'link'))
        time.sleep(DELAY)
    print('selesai (mode %s): %d seri diproses, %d bab ditulis ke '
          'site-content/series/' % ('gambar' if want_images else 'link',
                                    len(entries), total_ch))
    return 0


def test():
    html = ('<a href="/genre/action">Action</a>'
            '<a href="/e-chapter-1/">Eleceed Chapter 1</a>'
            '<a href="/e-chapter-2/">Eleceed Chapter 2</a>'
            '<a href="/x">About</a>')
    print(json.dumps(parse_chapter_links(html, 'https://ac/manga/eleceed/'),
                     ensure_ascii=False, indent=2))
    ich = ('<div class="chapter-image"><div id="chimg-auh">'
           '<img src="https://cdn.example/p1.jpg">'
           '<img src="https://cdn.example/fav.png"></div></div>')
    print(json.dumps(parse_chapter_images(ich, 'https://ac/manga/'), indent=2))


if __name__ == '__main__':
    if '--test' in sys.argv:
        test()
    else:
        sys.exit(run())