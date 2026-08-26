# -*- coding: utf-8 -*-
"""
Scraper katalog Mfmam - versi CLOUD (untuk GitHub Actions).
Membaca site-content/sources.json lalu menulis langsung ke
site-content/series/<slug>.json (skema sama dengan koleksi admin Decap CMS),
TANPA menyimpan gambar: hanya judul bab + link sumber (mode LINK).

Isi site-content/sources.json (list):
  [ { "slug":"eleceed", "url":"https://WEB/manga/eleceed/", "title":"Eleceed" } ]

Jalankan:
  python scraper.py            # update site-content/series/*.json
  python scraper.py --test     # uji parser tanpa network

Catatan: pastikan Anda berhak menautkan sumber yang Anda pilih.
"""
import os, re, json, sys, time, html as H
from urllib.request import Request, urlopen
from urllib.parse import urljoin

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, 'site-content', 'sources.json')
SERIES_DIR = os.path.join(ROOT, 'site-content', 'series')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36'
DELAY = 1.0

CH_RE = re.compile(r'\b(?:chapter|bab)\s*(\d+(?:\.\d+)?)', re.I)
ANCHOR_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
TAG_RE = re.compile(r'<[^>]+>')
OG_IMG_RE = re.compile(r'<meta\b[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)', re.I)
OG_T_RE = re.compile(r'<meta\b[^>]*property=["\']og:title["\'][^>]*content=["\']([^"\']+)', re.I)
TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.I | re.S)
GENRE_RE = re.compile(r'<a\b[^>]*href=["\'][^"\']*/genre/[^"\']*["\'][^>]*>(.*?)</a>', re.I | re.S)


def clean(t):
    return re.sub(r'\s+', ' ', H.unescape(TAG_RE.sub('', t or ''))).strip()


def slugify(s):
    s = re.sub(r'[^a-z0-9]+', '-', (s or '').strip().lower())
    return re.sub(r'-+', '-', s).strip('-')


def fetch(url):
    req = Request(url, headers={'User-Agent': UA, 'Accept': 'text/html'})
    with urlopen(req, timeout=25) as r:
        return r.read().decode('utf-8', 'ignore')


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


def extract_genres(html):
    out = []
    for m in GENRE_RE.finditer(html):
        l = clean(m.group(1))
        if l and l not in out:
            out.append(l)
    return out[:6]


def scrape_series(entry):
    html = fetch(entry['url'])
    mt = OG_IMG_RE.search(html)
    mt_t = OG_T_RE.search(html) or TITLE_RE.search(html)
    title = entry.get('title') or (clean(mt_t.group(1)) if mt_t else entry.get('slug'))
    return {
        'slug': slugify(entry.get('slug') or title),
        'title': title,
        'genres': extract_genres(html),
        'cover_url': mt.group(1) if mt else '',
        'chapters': parse_chapter_links(html, entry['url']),
    }
def write_series(slug, data):
    """Tulis site-content/series/<slug>.json, pertahankan desc bila sudah ada."""
    path = os.path.join(SERIES_DIR, slug + '.json')
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as fh:
                existing = json.load(fh)
        except Exception:
            existing = {}
    merged = {
        'slug': slug,
        'title': data.get('title') or existing.get('title') or slug,
        'desc': existing.get('desc') or data.get('desc') or '',
        'genres': data.get('genres') or existing.get('genres') or [],
        'cover_url': data.get('cover_url') or existing.get('cover_url') or '',
        'chapters': data.get('chapters') or existing.get('chapters') or [],
    }
    # dedupe bab berdasarkan external, jaga urutan
    seen = set()
    deduped = []
    for c in merged['chapters']:
        u = (c.get('external') or '').strip()
        if u and u in seen:
            continue
        if u:
            seen.add(u)
        deduped.append(c)
    merged['chapters'] = deduped
    os.makedirs(SERIES_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(merged, fh, ensure_ascii=False, indent=2)
    return len(deduped)


def run():
    # kumpulkan daftar seri: sources.json (persisten) + manual-batch.txt (sekali pakai)
    entries = []
    if os.path.exists(SRC):
        with open(SRC, encoding='utf-8') as fh:
            entries += json.load(fh)
    manual_txt = os.path.join(ROOT, 'site-content', 'manual-batch.txt')
    if os.path.exists(manual_txt):
        with open(manual_txt, encoding='utf-8') as fh:
            for line in fh:
                url = line.strip()
                if not url or url.startswith(('http', 'https')) is False:
                    continue
                try:
                    seg = [s for s in url.rstrip('/').split('/') if s]
                    hint = seg[-1] if seg else 'manga'
                except Exception:
                    hint = 'manga'
                entries.append({'url': url, 'slug': hint, 'title': hint})
    if not entries:
        print('Tidak ada sumber (sources.json kosong & tanpa URL manual).')
        print('Sebelumnya scraper: pastikan sudah diisi, atau tempel URL lewat form Action.')
        return 0
    # batas batch (default: semua)
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
            m = scrape_series(e)
        except Exception as ex:
            print('   ! gagal: %s' % ex)
            time.sleep(DELAY)
            continue
        slug = slugify(m['slug'])
        n = write_series(slug, m)
        total_ch += n
        print('   -> seri %s, %d bab' % (slug, n))
        time.sleep(DELAY)
    print('selesai: %d seri diproses, %d bab taut ditulis ke site-content/series/'
          % (len(entries), total_ch))
    return 0


def test():
    html = ('<a href="/genre/action">Action</a>'
            '<a href="/e-chapter-1/">Eleceed Chapter 1</a>'
            '<a href="/e-chapter-2/">Eleceed Chapter 2</a>'
            '<a href="/x">About</a>')
    print(json.dumps(parse_chapter_links(html, 'https://ac/manga/eleceed/'),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    if '--test' in sys.argv:
        test()
    else:
        sys.exit(run())