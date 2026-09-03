# -*- coding: utf-8 -*-
"""
Build situs statis Mfmam — versi CLOUDFLARE PAGES (reader dinamis).

Membaca site-content/ (settings.json, pages/*.json, series/*.json) lalu
menulis hasil ke folder `dist/`:

  index.html, page/N/, daftar-komik/, genre/     -> daftar seri (metadata)
  manga/<slug>/index.html                        -> halaman seri + daftar bab
                                                   (TANPA array `images`; bab
                                                   dibuka lewat hash #bab/N)
  data/<slug>.json                               -> data LENGKAP per seri
                                                   (termasuk `images[]`); dibaca
                                                   reader client-side saat
                                                   pengunjung membuka bab
  data/scrape-state.json                         -> state resume pagination
  search.json, chapters-index.json               -> pencarian (bab -> #bab/N)
  sitemap.xml, robots.txt, 404.html, _redirects
  assets/*                                       -> salin dari folder assets

Model ini dipilih agar jumlah FILE sesuai batas Cloudflare Pages Free
(maks 20.000 file). Halaman bab per-chapter TIDAK dibuat; reader digambar
oleh assets/script.js dari data/<slug>.json.

Satu file data boleh besar (≤25 MiB per file; aman). Untuk seri raksasa
(Martial Peak ±7,4 MB) tetap muat.

Jalankan:
  python build.py                # tulis ke dist/
  python build.py --dry-run      # hitung ringkasan tanpa menulis

Env opsional:
  SITE_URL      domain publik untuk sitemap (default https://mfmam.pages.dev)
  PAGE_SIZE     seri per halaman daftar (default 30)
"""
import os
import re
import sys
import json
import time
import html as H
import shutil

ROOT = os.path.dirname(os.path.abspath(__file__))
CONTENT = os.path.join(ROOT, 'site-content')
DIST = os.path.join(ROOT, 'dist')
# Data katalog (data/<slug>.json + manifest) dipisah ke dist-data/ agar
# TIDAK ikut di-deploy ke Cloudflare Pages (halaman statis jadi ringan).
# Isi dist-data/ di-upload ke R2 bucket dan disajikan Pages Function
# (functions/data/[slug].js) lewat binding R2 — lihat wrangler.toml.
DATA_OUT = os.path.join(ROOT, 'dist-data')

# Domain publik dipakai untuk <loc> sitemap. Bisa diset di env SITE_URL
# (tanpa garis miring akhir), mis. https://mfmam.pages.dev
SITE_URL = (os.environ.get('SITE_URL') or 'https://mfmam.pages.dev').strip()
if SITE_URL.endswith('/'):
    SITE_URL = SITE_URL[:-1]

# Maksimal seri per halaman daftar (beranda, daftar-komik, genre).
_PAGE_ENV = os.environ.get('PAGE_SIZE', os.environ.get('NETLIFY_PAGE_SIZE', '')).strip()
PAGE_SIZE = int(_PAGE_ENV) if (_PAGE_ENV.isdigit() and int(_PAGE_ENV) > 0) else 30

# Batas aman Cloudflare Pages Free.
MAX_FILES = 20000
MAX_FILE_BYTES = 25 * 1024 * 1024


def load_json(p, d):
    if not os.path.exists(p):
        return d
    try:
        with open(p, encoding='utf-8-sig') as fh:
            return json.load(fh)
    except Exception:
        return d


def slugify(s):
    s = re.sub(r'[^a-z0-9]+', '-', (s or '').strip().lower())
    return re.sub(r'-+', '-', s).strip('-')


def esc(s):
    return H.escape(str(s or ''), quote=True)


def fmt_num(n):
    """Nomor bab tanpa desimal bila bulat: 415.0 -> 415."""
    try:
        f = float(n)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return n


BULAN_ID = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun',
            'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des']


def fmt_date(s):
    """Format '2026-08-26' -> '26 Agu 2026'."""
    s = (s or '').strip()
    if not s:
        return ''
    m = re.match(r'(\d{4})-(\d{1,2})-(\d{1,2})', str(s))
    if not m:
        return s
    y, mo, d = m.groups()
    try:
        return '%d %s %s' % (int(d), BULAN_ID[int(mo)], y)
    except (ValueError, IndexError):
        return s


def ch_sort_key(c):
    """Urut bab: terbaru (num besar) di paling atas; tanpa num di bawah."""
    n = c.get('num')
    return (1, 0) if n is None else (0, -(n or 0))


def ch_sort_key_asc(c):
    """Urut bab URUTAN BACA: chapter kecil dulu; tanpa nomor di bawah."""
    n = c.get('num')
    return (1, 0) if n is None else (0, n or 0)
STATUS_PATTERNS = [
    # (pola status mentah -> kelas CSS). Status sumber berbahasa Indonesia
    # (Berjalan/Tamat) atau Inggris (Ongoing/Hiatus/Completed).
    (r'berjalan|ongoing|on\s?going|onupdate|sedang\s?tayang|publikasi', 'status-ongoing'),
    (r'hiatus|libur|pause|ditunda', 'status-hiatus'),
    (r'tamat|selesai|completed|complete|finished|end', 'status-completed'),
]


def status_class(st):
    s = (st or '').strip().lower()
    for pattern, cls in STATUS_PATTERNS:
        if re.search(pattern, s):
            return cls
    return ''


def status_badge_html(s):
    st = (s.get('status') or '').strip()
    if not st:
        return ''
    cls = status_class(st)
    extra = ' %s' % cls if cls else ''
    return '<span class="status-badge%s">%s</span>' % (extra, esc(st))


def reader_url(slug, c):
    """URL bab pada model reader DINAMIS: halaman seri + hash.
    `/manga/<slug>/#bab/<kunci>` — kunci = nomor bab (atau slug bila tak bernomor).
    Hash diproses oleh assets/script.js; data bab diambil dari data/<slug>.json."""
    n = c.get('num')
    if n is not None:
        key = fmt_num(n)
    else:
        key = c.get('slug') or 'chapter'
    return '/manga/%s/#bab/%s' % (esc(slug), esc(str(key)))


# Sumber konten dewasa yang disembunyikan dari daftar saat tombol Blur mati.
# ON  -> seri dari Mikoroku & Doujindesu ikut ditampilkan
# OFF -> kedua sumber disembunyikan (default, tampilan aman).
BLUR_SOURCES = {
    'mikoroku': 'Mikoroku',
    'doujindesu': 'Doujindesu',
}


def blur_source_of(s):
    """Nama sumber dewasa ('mikoroku' / 'doujindesu') bila seri berasal dari
    salah satunya; '' untuk seri biasa. Deteksi lewat source_url dan tautan
    bab eksternal yang memuat domain sumber tersebut."""
    hay = ' '.join([
        (s.get('source_url') or '').lower(),
    ] + [((c.get('external') or '').lower())
         for c in (s.get('chapters') or []) if c.get('external')])
    if 'mikoroku' in hay or 'mikodrive' in hay:
        return 'mikoroku'
    if 'doujin.desu' in hay or 'doujindesu' in hay:
        return 'doujindesu'
    return ''


def card_html(s):
    genres = ' / '.join(s.get('genres') or [])
    chs = sorted(s.get('chapters') or [], key=ch_sort_key)[:3]
    latest = ''
    if chs:
        items = ''.join(
            '<li><a href="%s">%s</a>%s</li>'
            % (reader_url(s.get('slug'), c), esc(ch_label(c)),
               ('<span class="ch-dt" data-date="%s">%s</span>'
                % (esc(c.get('date')), esc(fmt_date(c.get('date')))))
               if c.get('date') else '') for c in chs)
        latest = ('<div class="mc-latest"><div class="mc-latest-h">'
                  'Bab Terbaru</div><ul>%s</ul></div>' % items)
    upd = fmt_date(s.get('last_updated'))
    upd_html = ('<div class="mc-update">Update: %s</div>' % esc(upd)) if upd else ''
    badge = status_badge_html(s)
    blur = blur_source_of(s)
    adult = ' data-blur="%s"' % blur if blur else ''
    return ('<article class="manga-card"%s><a class="manga-link" '
            'href="/manga/%s/"><div class="thumb">%s'
            '<img class="cover" src="%s" alt="%s" loading="lazy" '
            'referrerpolicy="no-referrer" decoding="async"></div>'
            '<div class="mc-title">%s</div>'
            '<div class="mc-meta">%s</div>%s</a>%s</article>'
            % (adult, esc(s.get('slug')), badge,
               esc(s.get('cover_url') or '/assets/logo.png'),
               esc(s.get('title')), esc(s.get('title')), esc(genres),
               upd_html, latest))


def sun_grid(series_list):
    return ' '.join(card_html(s) for s in series_list)


def chunk_pages(items, size=None):
    size = size or PAGE_SIZE
    if not items:
        return []
    return [items[i:i + size] for i in range(0, len(items), size)]


def page_url(page, base_path=''):
    """URL halaman daftar statis.
    base_path=''          -> beranda: '/' (hlm 1), '/page/N/' (hlm >1)
    base_path='/daftar-komik' -> '/daftar-komik/' & '/daftar-komik/page/N/'
    base_path='/genre/x'  -> '/genre/x/' & '/genre/x/page/N/'"""
    if page <= 1:
        return (base_path + '/') if base_path else '/'
    return '%s/page/%d/' % (base_path, page)


def pagination_html(page, total_pages, base_path=''):
    if total_pages <= 1:
        return ''
    p = page_url
    links = []
    if page > 1:
        links.append('<a class="pg" rel="prev" href="%s">&#8592; Sebelumnya</a>'
                     % p(page - 1, base_path))
    else:
        links.append('<span class="pg disabled">&#8592; Sebelumnya</span>')
    lo = max(1, page - 2)
    hi = min(total_pages, page + 2)
    if lo > 1:
        links.append('<a class="pg" href="%s">1</a>' % p(1, base_path))
        if lo > 2:
            links.append('<span class="pg dots">&#8230;</span>')
    for i in range(lo, hi + 1):
        if i == page:
            links.append('<span class="pg cur" aria-current="page">%d</span>' % i)
        else:
            links.append('<a class="pg" href="%s">%d</a>' % (p(i, base_path), i))
    if hi < total_pages:
        if hi < total_pages - 1:
            links.append('<span class="pg dots">&#8230;</span>')
        links.append('<a class="pg" href="%s">%d</a>'
                     % (p(total_pages, base_path), total_pages))
    if page < total_pages:
        links.append('<a class="pg" rel="next" href="%s">Berikutnya &#8594;</a>'
                     % p(page + 1, base_path))
    else:
        links.append('<span class="pg disabled">Berikutnya &#8594;</span>')
    return ('<nav class="pagination" aria-label="Navigasi halaman">%s</nav>'
            % ''.join(links))


def series_sort_key(s):
    """Kunci urut daftar: update TERBARU di paling atas (reverse=True)."""
    d = (s.get('last_updated') or '').strip()
    if not d:
        chs = [c.get('date') for c in (s.get('chapters') or []) if c.get('date')]
        d = max(chs) if chs else ''
    m = re.match(r'^(\d{4})-(\d{1,2})-(\d{1,2})', d)
    if not m:
        return (0, 0, 0, 0)
    return (1, int(m.group(1)), int(m.group(2)), int(m.group(3)))


def ch_label(c):
    """Label singkat satu bab: 'Chapter 415' bila bernomor, selain itu judul."""
    n = c.get('num')
    if n is not None:
        return 'Chapter %s' % fmt_num(n)
    t = (c.get('title') or '').strip()
    return t[:40] or 'Chapter'
def render_page(title, body, site, tagline, extra=''):
    """Kerangka halaman. assets/script.js dimuat di SEMUA halaman (tema, nav,
    pencarian global & bab, reader dinamis). Tidak lagi menautkan widget
    Netlify Identity (hosting pindah ke Cloudflare Pages)."""
    widget = '<script src="/assets/script.js"></script>'
    return ('<!DOCTYPE html>\n<html lang="id"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="referrer" content="no-referrer">'
            '<title>%s</title><link rel="icon" href="/assets/logo.png">'
            '<link rel="stylesheet" href="/assets/style.css">%s\n</head>'
            '<body class="layout-site"><header class="site-header">'
            '<div class="container header-inner"><a class="brand" href="/">%s</a>'
            '<nav class="main-nav"><ul>'
            '<li><a href="/">Beranda</a></li>'
            '<li><a href="/daftar-komik/">Daftar Manhwa</a></li>'
            '<li><a href="/genre/">Genre</a></li>'
            '<li><a href="/kontak/">Kontak</a></li></ul></nav>'
            '<div class="actions">'
            '<button type="button" id="blur-btn" aria-pressed="false" '
            'title="Tampilkan seri dari Mikoroku &amp; Doujindesu">'
            'Blur: Mati</button>'
            '<button type="button" id="theme-btn" aria-pressed="false" '
            'title="Ganti tema gelap/terang">&#9789;</button>'
            '</div></div></header>'
            '<main class="container main">%s</main>'
            '<footer class="site-footer"><div class="container footer-inner">'
            '<p>&copy; %s %s &mdash; %s</p></div></footer>'
            '%s</body></html>'
            % (esc(title), extra, esc(site), body, time.strftime('%Y'),
               esc(site), esc(tagline), widget))


def write(path, content):
    full = os.path.join(DIST, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'w', encoding='utf-8') as fh:
        fh.write(content)


def write_bytes(path, data):
    full = os.path.join(DIST, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'wb') as fh:
        fh.write(data)


# --- penulisan data R2 (dist-data/) --------------------------------
# File di sini di-upload ke bucket R2 `mfmam-data` dengan key `data/<name>`.
# Struktur folder: dist-data/data/<slug>.json, dist-data/data/manifest.json, dst.


def _data_path(name):
    return os.path.join(DATA_OUT, 'data', name)


def write_data(name, content):
    full = _data_path(name)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'w', encoding='utf-8') as fh:
        fh.write(content)


def write_bytes_data(name, data):
    full = _data_path(name)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'wb') as fh:
        fh.write(data)


def meta_line(label, value):
    return ('<div class="meta-row"><span class="meta-label">%s</span>'
            '<span class="meta-value">%s</span></div>' % (esc(label), esc(value)))


def series_page_html(s):
    """Halaman seri: SAMPUL + metadata + daftar bab. TANPA array `images`
    (data lengkap ada di data/<slug>.json, dibaca reader via #bab/N)."""
    ch = sorted(s.get('chapters') or [], key=ch_sort_key_asc)
    badges = []
    if s.get('status'):
        badges.append('<span class="badge">%s</span>' % esc(s['status']))
    if s.get('type'):
        badges.append('<span class="badge badge-alt">%s</span>' % esc(s['type']))
    if s.get('alt_title'):
        badges.append('<span class="badge badge-dim">%s</span>'
                      % esc(s['alt_title']))
    meta = ''
    if s.get('last_updated'):
        meta += meta_line('Update', fmt_date(s['last_updated']))
    if s.get('author'):
        meta += meta_line('Pengarang', s['author'])
    if s.get('illustrator'):
        meta += meta_line('Ilustrator', s['illustrator'])
    if s.get('genres'):
        meta += '<div class="meta-row meta-genres"><span class="meta-label">' \
                'Genre</span><span class="meta-value chips">%s</span></div>' % \
                ''.join('<a class="chip" href="/genre/%s/">%s</a>'
                        % (esc(slugify(g)), esc(g)) for g in s['genres'])
    desc = ('<div class="seri-desc">%s</div>' % esc(s.get('desc'))
            if s.get('desc') else '')
    rows = ''.join('<a class="ch-row" href="%s"><span class="ch-no">%s</span>'
                   '<span class="ch-ti">%s</span>%s</a>'
                   % (reader_url(s.get('slug'), c),
                      esc(fmt_num(c.get('num'))),
                      esc(c.get('title', '') or ch_label(c)),
                      ('<span class="ch-dt" data-date="%s">%s</span>'
                       % (esc(c.get('date')), esc(fmt_date(c.get('date')))))
                      if c.get('date') else '') for c in ch)
    badges_join = ''.join(badges)
    search_ui = chapter_search_ui(s.get('title'))
    shell = ('<div id="reader" class="reader-shell" hidden '
             'aria-live="polite"></div>')
    blur = blur_source_of(s)
    page_attr = ' data-blur="%s"' % blur if blur else ''
    if blur:
        label = BLUR_SOURCES.get(blur, blur)
        note = ('<div class="blur-note"><p>Seri dari <strong>%s</strong> '
                'disembunyikan saat tombol Blur mati. Nyalakan Blur untuk '
                'menampilkan seri ini.</p>'
                '<button type="button" class="blur-enable-btn" '
                'data-blur-enable>&#128065; Nyalakan Blur</button></div>'
                % esc(label))
    else:
        note = ''
    return ('<div class="seri-page" data-slug="%s"%s>'
            '<div class="seri-head">'
            '<img class="seri-cover" src="%s" alt="%s" loading="lazy" '
            'referrerpolicy="no-referrer">'
            '<div class="seri-info"><h1>%s</h1>'
            '<div class="seri-meta">%s%s</div>%s</div></div>'
            '%s'
            '<h2 class="sec-title">Daftar Bab (%d)</h2>'
            '<nav class="ch-list">%s</nav></div>%s%s'
            % (esc(s.get('slug')), page_attr,
               esc(s.get('cover_url') or '/assets/logo.png'), esc(s['title']),
               esc(s['title']), badges_join, meta, desc,
               search_ui,
               len(ch), rows, note, shell))
def chapter_search_ui(series=None):
    """Kolom pencarian bab. Bila `series` diberikan, pencarian dibatasi ke seri
    tersebut; bila None, lintas seluruh manhwa."""
    scope = ' data-series="%s"' % esc(series) if series else ''
    if series:
        label = ('Ketik nomor bab untuk melompat langsung (mis. %s, 120, '
                 'bab 50):' % esc(series))
        placeholder = 'Ketik nomor bab…'
    else:
        label = ('Ketik judul manhwa atau nomor bab (mis. Eleceed, 120, '
                 'bab 50):')
        placeholder = 'Ketik nama manhwa / nomor bab…'
    return ('<section class="chapter-search" id="chapter-search"%s>'
            '<h2 class="sec-title">Cari Bab untuk Dibaca</h2>'
            '<form class="cs-form" id="chap-search-form" role="search">'
            '<label for="chap-search-input" class="cs-label">%s</label>'
            '<div class="cs-field">'
            '<input id="chap-search-input" type="search" autocomplete="off" '
            'value="" placeholder="%s">'
            '<button type="submit" class="cs-btn">Cari &#128269;</button>'
            '</div></form>'
            '<div id="chap-search-results" class="cs-results" hidden></div>'
            '</section>' % (scope, label, placeholder))


def chapter_index(series_list):
    """Indeks seluruh bab (lintas seri) untuk 'Cari Bab untuk Dibaca'.
    URL bab memakai reader dinamis: /manga/<slug>/#bab/<kunci>.
    Field `b` menandai bab milik seri dewasa (di-filter tombol Blur)."""
    blur = {s.get('slug'): 1 if blur_source_of(s) else 0 for s in series_list}
    out = []
    for s in series_list:
        st = s.get('title') or s.get('slug')
        slug = s.get('slug') or ''
        for c in (s.get('chapters') or []):
            num = c.get('num')
            if c.get('title'):
                t = c['title']
            elif num is not None:
                t = 'Chapter %s' % num
            else:
                t = 'Chapter'
            out.append({'s': st, 't': t, 'n': fmt_num(num),
                        'u': reader_url(slug, c),
                        'b': blur.get(slug, 0)})
    return out
def genre_cloud_html(items):
    """Awan chip semua genre (indeks Genre) lengkap dengan jumlah judul."""
    return '<div class="chips genre-cloud">%s</div>' % ' '.join(
        '<a class="chip" href="/genre/%s/">%s<span class="gcount">%d</span></a>'
        % (esc(slug), esc(g), n) for slug, g, n in items)


def build():
    settings = load_json(os.path.join(CONTENT, 'settings.json'),
                         {'site_name': 'Mfmam', 'tagline': 'Baca Komik Manhwa'})
    site = settings.get('site_name') or 'Mfmam'
    tagline = settings.get('tagline') or ''
    # Hapus sisa `dist/data/` dari build versi lama (data kini di R2); juga
    # bersihkan dist-data/ agar tidak menumpuk file yang sudah batal.
    shutil.rmtree(os.path.join(DIST, 'data'), ignore_errors=True)
    if os.path.isdir(DATA_OUT):
        shutil.rmtree(DATA_OUT, ignore_errors=True)
    series, pages = [], []
    src_map = {}   # slug -> path file JSON asli di site-content/series
    sdir = os.path.join(CONTENT, 'series')
    if os.path.isdir(sdir):
        for fn in sorted(os.listdir(sdir)):
            if not fn.endswith('.json'):
                continue
            pth = os.path.join(sdir, fn)
            d = load_json(pth, {})
            if d and d.get('slug') and d.get('chapters') is not None:
                series.append(d)
                src_map[d['slug']] = pth
    pdir = os.path.join(CONTENT, 'pages')
    if os.path.isdir(pdir):
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith('.json'):
                continue
            d = load_json(os.path.join(pdir, fn), {})
            if d and d.get('slug'):
                pages.append(d)

    series.sort(key=lambda s: (s.get('title') or s.get('slug') or '').lower())
    series.sort(key=series_sort_key, reverse=True)

    # --- salin aset statis ---
    write_bytes('assets/logo.png',
                open(os.path.join(ROOT, 'assets', 'logo.png'), 'rb').read())
    write_bytes('assets/style.css',
                open(os.path.join(ROOT, 'assets', 'style.css'), 'rb').read())
    write_bytes('assets/script.js',
                open(os.path.join(ROOT, 'assets', 'script.js'), 'rb').read())
    write('robots.txt', 'User-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n'
          % SITE_URL)

    # _redirects: URL cantik pada Cloudflare Pages.
    # HANYA untuk direktori HTML. JANGAN pakai catch-all
    #   `/*  /:splat/index.html  200`
    # karena akan ikut membajak request berkas statis (assets/*, data/*.json,
    # search.json, sitemap.xml, dst.) sehingga fetch reader & aset jadi gagal.
    write('_redirects', '\n'.join([
        '# Cloudflare Pages - URL cantik (tanpa catch-all)',
        '/manga/*  /manga/:splat/index.html  200',
        '/genre/*  /genre/:splat/index.html  200',
        '/daftar-komik/*  /daftar-komik/:splat/index.html  200',
        '/page/*  /page/:splat/index.html  200',
        '/kontak/*  /kontak/:splat/index.html  200',
        '',
    ]))

    hero = ('<section class="hero"><div class="hero-track"><div class="hero-slide">'
            '<div class="hero-scrim"></div><div class="hero-text"><h1>%s</h1>'
            '<a class="hero-link" href="/daftar-komik/">Baca &#8594;</a></div>'
            '</div></div></section>' % esc(site))
    total = len(series)
    home_pages = chunk_pages(series) or [[]]
    n_pages = len(home_pages)

    for i, items in enumerate(home_pages, 1):
        page_cards = sun_grid(items)
        if i == 1:
            head = hero
        else:
            head = ('<div class="pagi-head"><a class="crumb" href="/">'
                    '&#8592; Beranda</a>'
                    '<h1 class="page-title">Manhwa Terbaru - Halaman %d</h1>'
                    '</div>' % i)
        body_home = head + (('<div class="home-grid">%s</div>' % page_cards)
                            if page_cards else
                            '<p class="empty">Belum ada seri.</p>')
        body_home += pagination_html(i, n_pages, base_path='')
        body_home += ('<div class="more-wrap"><a class="more-btn" '
                      'href="/daftar-komik/">Lihat Semua &#8594;</a></div>')
        if i == 1:
            write('index.html', render_page(site, body_home, site, tagline))
        else:
            write('page/%d/index.html' % i,
                  render_page('%s - Halaman %d' % (site, i),
                              body_home, site, tagline))

    cs_ui = chapter_search_ui()
    for i, items in enumerate(home_pages, 1):
        page_cards = sun_grid(items)
        body = ('<h1 class="page-title">Daftar Manhwa</h1>'
                '<p class="count-line">Total %d judul. Gunakan kolom pencarian '
                'untuk langsung menuju bab yang ingin dibaca, atau klik sampul '
                'untuk detail &amp; daftar bab.</p>'
                '%s'
                '<div class="manga-grid">%s</div>'
                % (total, cs_ui, page_cards))
        body += pagination_html(i, n_pages, base_path='/daftar-komik')
        if i == 1:
            write('daftar-komik/index.html',
                  render_page('Daftar Manhwa - %s' % site, body, site, tagline))
        else:
            write('daftar-komik/page/%d/index.html' % i,
                  render_page('Daftar Manhwa - Halaman %d - %s' % (i, site),
                              body, site, tagline))
    # --- halaman genre: indeks semua genre + listing tiap genre (paginasi) ---
    # Urut seri sama dengan beranda (update terbaru dulu) karena `series`
    # sudah di-sort; setiap seri bisa muncul di beberapa genre.
    genre_map = {}
    for s in series:
        for g0 in (s.get('genres') or []):
            g = (g0 or '').strip()
            if g:
                genre_map.setdefault(g, []).append(s)
    # (slug, nama-asli, jumlah seri); urut abjad nama genre.
    genre_items = sorted(((slugify(g), g, len(genre_map[g]))
                          for g in genre_map), key=lambda x: x[1].lower())
    genre_sitemap = []
    for slug_, g, n in genre_items:
        g_pages = chunk_pages(genre_map[g]) or [[]]
        gn = len(g_pages)
        for i, items in enumerate(g_pages, 1):
            pg = sun_grid(items) if items else '<p class="empty">Belum ada seri.</p>'
            body_g = ('<div class="pagi-head"><a class="crumb" href="/genre/">'
                      '&#8592; Semua Genre</a>'
                      '<h1 class="page-title">Genre %s</h1></div>'
                      '<p class="count-line">%d judul dengan genre %s.</p>'
                      '<div class="manga-grid">%s</div>'
                      % (esc(g), len(genre_map[g]), esc(g), pg))
            body_g += pagination_html(i, gn, base_path='/genre/' + slug_)
            title_g = ('%s - Genre - Halaman %d - %s' % (g, i, site)
                       if i > 1 else '%s - Genre - %s' % (g, site))
            if i == 1:
                write('genre/%s/index.html' % slug_,
                      render_page(title_g, body_g, site, tagline))
                genre_sitemap.append(' <url><loc>%s/genre/%s/</loc></url>'
                                     % (SITE_URL, slug_))
            else:
                write('genre/%s/page/%d/index.html' % (slug_, i),
                      render_page(title_g, body_g, site, tagline))
                genre_sitemap.append(
                    ' <url><loc>%s/genre/%s/page/%d/</loc></url>'
                    % (SITE_URL, slug_, i))
    # Indeks semua genre (awan chip).
    if genre_items:
        body_genre = ('<div class="pagi-head"><a class="crumb" href="/">'
                      '&#8592; Beranda</a>'
                      '<h1 class="page-title">Genre</h1></div>'
                      '<p class="count-line">%d genre &middot; %d judul.</p>'
                      '%s' % (len(genre_items), total,
                              genre_cloud_html(genre_items)))
    else:
        body_genre = ('<h1 class="page-title">Genre</h1>'
                      '<p class="empty">Belum ada genre.</p>')
    write('genre/index.html',
          render_page('Genre - %s' % site, body_genre, site, tagline))

    write('chapters-index.json',
          json.dumps(chapter_index(series), ensure_ascii=False))

    search, sitemap = [], [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ' <url><loc>%s/</loc></url>' % SITE_URL,
        ' <url><loc>%s/daftar-komik/</loc></url>' % SITE_URL]
    for i in range(2, n_pages + 1):
        sitemap.append(' <url><loc>%s/page/%d/</loc></url>' % (SITE_URL, i))
        sitemap.append(' <url><loc>%s/daftar-komik/page/%d/</loc></url>'
                       % (SITE_URL, i))

    for s in series:
        search.append({'t': s.get('title') or s.get('slug'),
                       'u': '/manga/%s/' % s.get('slug'),
                       'g': s.get('genres') or [],
                       'b': 1 if blur_source_of(s) else 0})
        sitemap.append(' <url><loc>%s/manga/%s/</loc></url>'
                       % (SITE_URL, esc(s.get('slug'))))

    sitemap.append(' <url><loc>%s/genre/</loc></url>' % SITE_URL)
    sitemap.extend(genre_sitemap)

    # Halaman info (mis. kontak)
    for p in pages:
        content = '<div class="static-content">%s</div>' % ''.join(
            '<p>%s</p>' % esc(x) for x in (p.get('text') or '').splitlines()
            if x.strip())
        write('%s/index.html' % p['slug'],
              render_page('%s - %s' % (p.get('title') or p['slug'], site),
                          '<h1 class="page-title">%s</h1>%s'
                          % (esc(p.get('title')), content), site, tagline))
        sitemap.append(' <url><loc>%s/%s/</loc></url>'
                       % (SITE_URL, esc(p['slug'])))

    # --- halaman seri (metadata, tanpa images) + data lengkap per seri ---
    for s in series:
        slug = s.get('slug') or 'seri'
        write('manga/%s/index.html' % slug,
              render_page('%s - %s' % (s.get('title'), site),
                          series_page_html(s), site, tagline))
        # data/<slug>.json memuat SEMUA bab + images; dipanggil reader saat bab
        # dibuka (fetch + hash #bab/N). File boleh besar (≤25 MiB per file).
        # Karena disimpan di R2 (dist-data/data/), reader tetap mengakses
        # /data/<slug>.json lewat Pages Function yang membaca R2.
        _src = src_map.get(slug)
        if _src and os.path.exists(_src):
            write_bytes_data('%s.json' % slug, open(_src, 'rb').read())
        else:
            write_data('%s.json' % slug,
                       json.dumps(s, ensure_ascii=False, indent=2))

    # state resume pagination ikut dideploy agar run berikutnya bisa restore.
    _st = load_json(os.path.join(CONTENT, 'scrape-state.json'), {})
    if _st:
        write_data('scrape-state.json',
                   json.dumps(_st, ensure_ascii=False, indent=2))

    # manifest: daftar slug seri yang tersedia. Dipakai _restore_data.py
    # (dijalankan GitHub Actions) untuk men-download data yg sama kembali
    # sebelum scrape berikutnya — supaya data TIDAK perlu di-commit ke git,
    # tapi state (gambar/tanggal bab) tetap tersedia secara incremental.
    write_data('manifest.json',
               json.dumps([s.get('slug') for s in series if s.get('slug')],
                          ensure_ascii=False, indent=2))

    sitemap.append('</urlset>')
    write('search.json', json.dumps(search, ensure_ascii=False))
    write('sitemap.xml', '\n'.join(sitemap))
    write('404.html', render_page(
        '404 - %s' % site,
        '<div class="n404"><h1>404</h1><p>Halaman tidak ditemukan.</p>'
        '<a class="baca-btn" href="/">&#8592; Ke Beranda</a></div>', site,
        tagline))

    # --- ringkasan + pengaman batas Cloudflare Pages Free ---
    n_files = sum(len(fs) for _, _, fs in os.walk(DIST))
    total_bytes = 0
    biggest = ('', 0)
    for root, _, fs in os.walk(DIST):
        for fn in fs:
            fp = os.path.join(root, fn)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            total_bytes += sz
            if sz > biggest[1]:
                biggest = (os.path.relpath(fp, DIST), sz)
    # Statistik data R2 (dist-data/) — tidak ikut deploy Pages.
    n_data = sum(len(fs) for _, _, fs in os.walk(DATA_OUT)) if os.path.isdir(DATA_OUT) else 0
    data_bytes = 0
    if os.path.isdir(DATA_OUT):
        for root, _, fs in os.walk(DATA_OUT):
            for fn in fs:
                try:
                    data_bytes += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    continue
    print('[build] selesai: %d seri, %d bab, %d halaman daftar (maks %d/hlm)'
          % (len(series),
             sum(len(x.get('chapters') or []) for x in series),
             n_pages, PAGE_SIZE))
    print('[build] dist: %d file | %s | domain: %s'
          % (n_files, _fmt_size(total_bytes), SITE_URL))
    print('[build] data R2 (dist-data/): %d file | %s (di-upload ke bucket mfmam-data)'
          % (n_data, _fmt_size(data_bytes)))
    if biggest[1]:
        print('[build] file terbesar: %s (%s)'
              % (biggest[0], _fmt_size(biggest[1])))
    if n_files > MAX_FILES:
        print('  ! PERINGATAN: %d file > batas Cloudflare Pages Free (%d).'
              % (n_files, MAX_FILES))
    if biggest[1] > MAX_FILE_BYTES:
        print('  ! PERINGATAN: ada file > 25 MiB (%s) — melebihi batas Pages.'
              % biggest[0])
    return 0


def _fmt_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return '%.1f %s' % (n, unit)
        n /= 1024.0
    return '%.1f GB' % n


if __name__ == '__main__':
    if '--dry-run' in sys.argv:
        _sdir = os.path.join(CONTENT, 'series')
        n = len([f for f in os.listdir(_sdir) if f.endswith('.json')]) \
            if os.path.isdir(_sdir) else 0
        est_files = n * 2 + 30
        print('[build] dry-run: ~%d seri -> kira-kira %d file (batas 20000).'
              % (n, est_files))
        sys.exit(0)
    sys.exit(build())