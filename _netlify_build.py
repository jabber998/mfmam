# -*- coding: utf-8 -*-
"""
Netlify build - bangun situs statis dari konten yang diedit lewat admin/ (Decap CMS).
Membaca site-content/ (settings.json, pages/*.json, series/*.json) lalu menulis
index, daftar, halaman genre, halaman info, halaman seri, halaman bab (mode link),
search.json, dan sitemap.xml ke folder repo (publish dir).

Jalankan:  python _netlify_build.py
"""
import os, re, json, time
import html as H

ROOT = os.path.dirname(os.path.abspath(__file__))
CONTENT = os.path.join(ROOT, 'site-content')


def load_json(p, d):
    if not os.path.exists(p):
        return d
    try:
        with open(p, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return d


def slugify(s):
    s = re.sub(r'[^a-z0-9]+', '-', (s or '').strip().lower())
    return re.sub(r'-+', '-', s).strip('-')


def esc(s):
    return H.escape(str(s or ''), quote=True)


def render_page(title, body, site, tagline, extra=''):
    # netlify-identity-widget: wajib di halaman root agar tautan undangan/
    # konfirmasi/reset dari Netlify Identity (#invite_token, #confirmation_token,
    # #recovery_token) bisa ditangkap dan memunculkan form-nya.
    widget = ('<script src="https://unpkg.com/netlify-identity-widget@1.9.2/build/'
              'netlify-identity-widget.js"></script>')
    return ('<!DOCTYPE html>\n<html lang="id"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>%s</title><link rel="icon" href="/assets/logo.png">'
            '<link rel="stylesheet" href="/assets/style.css">%s\n</head>'
            '<body class="layout-site"><header class="site-header">'
            '<div class="container header-inner"><a class="brand" href="/">%s</a>'
            '<nav class="main-nav"><ul>'
            '<li><a href="/">Beranda</a></li>'
            '<li><a href="/daftar-komik/">Daftar Manhwa</a></li>'
            '<li><a href="/genre/">Genre</a></li>'
            '<li><a href="/kontak/">Kontak</a></li></ul></nav></div></header>'
            '<main class="container main">%s</main>'
            '<footer class="site-footer"><div class="container footer-inner">'
            '<p>&copy; %s %s &mdash; %s</p></footer>%s</body></html>'
            % (esc(title), extra, esc(site), body, time.strftime('%Y'),
               esc(site), esc(tagline), widget))


def write(path, content):
    full = os.path.join(ROOT, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'w', encoding='utf-8') as fh:
        fh.write(content)
def build():
    settings = load_json(os.path.join(CONTENT, 'settings.json'),
                         {'site_name': 'Mfmam', 'tagline': 'Baca Komik Manhwa'})
    site = settings.get('site_name') or 'Mfmam'
    tagline = settings.get('tagline') or ''
    series, pages = [], []
    sdir = os.path.join(CONTENT, 'series')
    if os.path.isdir(sdir):
        for fn in sorted(os.listdir(sdir)):
            if not fn.endswith('.json'):
                continue
            d = load_json(os.path.join(sdir, fn), {})
            if d and d.get('slug'):
                series.append(d)
    pdir = os.path.join(CONTENT, 'pages')
    if os.path.isdir(pdir):
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith('.json'):
                continue
            d = load_json(os.path.join(pdir, fn), {})
            if d and d.get('slug'):
                pages.append(d)
    cards = ' '.join(
        '<a class="manga-card" href="/manga/%s/"><div class="thumb"><img '
        'class="cover" src="%s" alt="%s" loading="lazy"></div>'
        '<div class="mc-title">%s</div></a>'
        % (esc(s['slug']), esc(s.get('cover_url') or '/assets/logo.png'),
           esc(s['title']), esc(s['title'])) for s in series)
    body = ('<section class="hero"><div class="hero-track"><div class="hero-slide">'
            '<div class="hero-scrim"></div><div class="hero-text"><h1>%s</h1>'
            '<a class="hero-link" href="/daftar-komik/">Baca &#8594;</a></div>'
            '</div></div></section>' % esc(site))
    body += ('<div class="home-grid">%s</div>' % cards) if cards else \
            '<p class="empty">Belum ada seri. Tambah lewat /admin/.</p>'
    body += '<div class="more-wrap"><a class="more-btn" href="/daftar-komik/">Lihat Semua &#8594;</a></div>'
    write('index.html', render_page(site, body, site, tagline))
    write('daftar-komik/index.html',
          render_page('Daftar Manhwa - %s' % site,
                      '<h1 class="page-title">Daftar Manhwa</h1>'
                      '<div class="manga-grid">%s</div><p class="count-line">Total %d judul.</p>'
                      % (cards, len(series)), site, tagline))
    gmap = {}
    for s in series:
        for g in (s.get('genres') or []):
            gmap.setdefault(slugify(g), {'name': g, 'count': 0})
            gmap[slugify(g)]['count'] += 1
    chips = ' '.join('<a class="chip" href="/genre/%s/">%s (%d)</a>'
                     % (esc(k), esc(v['name']), v['count'])
                     for k, v in sorted(gmap.items()))
    write('genre/index.html', render_page('Genre - %s' % site,
          '<h1 class="page-title">Genre</h1><div class="chips">%s</div>' % chips,
          site, tagline))
    for k, v in gmap.items():
        items = [s for s in series
                 if k in [slugify(x) for x in (s.get('genres') or [])]]
        c = ' '.join('<a class="manga-card" href="/manga/%s/">%s</a>' % (esc(s['slug']), esc(s['title']))
                     for s in items)
        write('genre/%s/index.html' % k,
              render_page('%s - Genre - %s' % (v['name'], site),
                          '<h1 class="page-title">Genre: %s</h1><div class="manga-grid">%s</div>'
                          % (esc(v['name']), c), site, tagline))
    for p in pages:
        content = '<div class="static-content">%s</div>' % ''.join(
            '<p>%s</p>' % esc(x) for x in (p.get('text') or '').splitlines() if x.strip())
        write('%s/index.html' % p['slug'],
              render_page('%s - %s' % (p.get('title') or p['slug'], site),
                          '<h1 class="page-title">%s</h1>%s' % (esc(p.get('title')), content),
                          site, tagline))
    search = []
    sitemap = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
               '  <url><loc>https://mfmam.netlify.app/</loc></url>',
               '  <url><loc>https://mfmam.netlify.app/daftar-komik/</loc></url>']
    for s in series:
        ch = s.get('chapters') or []
        sch = ' '.join('<a class="ch-row" href="/%s/"><span class="ch-no">%s</span>'
                       '<span class="ch-ti">%s</span></a>'
                       % (esc(c.get('slug', '')), esc(c.get('num', '')),
                          esc(c.get('title', ''))) for c in ch)
        write('manga/%s/index.html' % s['slug'],
              render_page('%s - %s' % (s['title'], site),
                          '<div class="seri-page"><div class="seri-head">'
                          '<img class="seri-cover" src="%s" alt="%s">'
                          '<div class="seri-info"><h1>%s</h1>'
                          '<div class="seri-meta"><span class="badge">%d bab</span></div>'
                          '</div></div><h2 class="sec-title">Daftar Bab</h2>'
                          '<nav class="ch-list">%s</nav></div>'
                          % (esc(s.get('cover_url') or '/assets/logo.png'), esc(s['title']),
                             esc(s['title']), len(ch), sch), site, tagline))
        search.append({'t': s['title'], 'u': '/manga/%s/' % s['slug'],
                       'g': s.get('genres') or []})
        sitemap.append('  <url><loc>https://mfmam.netlify.app/manga/%s/</loc></url>' % s['slug'])
        for c in ch:
            cs = c.get('slug', '')
            if c.get('external'):
                content = ('<div class="reader-src" style="text-align:center;padding:36px 12px">'
                           '<p style="color:#9aa4b5">Bab dibaca di situs asal.</p>'
                           '<a class="baca-btn" href="%s" target="_blank" rel="nofollow noopener">'
                           'Baca di Sumber &#8594;</a></div>' % esc(c['external']))
            elif c.get('images'):
                content = ''.join('<img src="%s" alt="%s" loading="lazy" referrerpolicy="no-referrer">'
                                  % (esc(u), esc(c.get('title'))) for u in c['images'])
            else:
                content = '<p><em>Konten kosong.</em></p>'
            title = c.get('title') or 'Chapter'
            write('%s/index.html' % cs,
                  render_page('%s - %s' % (title, s['title']),
                              '<h1 class="reader-title">%s</h1>'
                              '<div class="reader-crumb"><a href="/manga/%s/">%s</a></div>'
                              '<div class="reader-content">%s</div>' % (esc(title), s['slug'],
                                                                        esc(s['title']), content),
                              site, tagline))
            sitemap.append('  <url><loc>https://mfmam.netlify.app/%s/</loc></url>' % cs)
    sitemap.append('</urlset>')
    write('search.json', json.dumps(search, ensure_ascii=False))
    write('sitemap.xml', '\n'.join(sitemap))
    write('404.html', render_page('404 - %s' % site,
          '<div class="n404"><h1>404</h1><p>Halaman tidak ditemukan.</p>'
          '<a class="baca-btn" href="/">&#8592; Ke Beranda</a></div>', site, tagline))
    print('[netlify-build] selesai: %d seri, %d halaman info, %d bab'
          % (len(series), len(pages),
             sum(len(x.get('chapters') or []) for x in series)))


if __name__ == '__main__':
    build()