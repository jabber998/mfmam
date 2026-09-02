# -*- coding: utf-8 -*-
"""
Mesin bersama (shared engine) untuk scraper katalog Mfmam.

Modul ini TIDAK mengenal sumber tertentu (komikindo/mikoroku/doujindesu);
semua logika yang spesifik per-sumber tinggal di adaptor:

  scraper_komikindo.py   -> situs HTML tema WordPress/Madara (mis. komikindo.ch)
  scraper_mikoroku.py    -> katalog JSON publik + feed Blogger (mikoroku.com)
  scraper_doujindesu.py  -> API terenkripsi doujin.desu.xxx

Setiap adaptor mengimplementasikan `SourceAdapter` dan didaftarkan via
`register_adapter()`. CLI yang dipakai bersama (run/delete/refresh-images/state
pagination/auto-build) berada di modul ini.

Run satu sumber:
  python scraper_komikindo.py [--images|--dates|--test|--delete|--refresh-images]
Run SEMUA sumber (jalan lama):
  python scraper.py [--images|--dates|--test|--delete|--refresh-images]

Skema satu seri (site-content/series/<slug>.json):
{
  id, slug, title, desc, genres[], cover_url,
  status, author, illustrator, alt_title, type, keywords, last_updated,
  chapters: [ { slug, title, num, external, images[], date } ]
}
"""
import os
import re
import json
import sys
import time
import random
import ssl
import shutil
import subprocess
import threading
import html as H
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen
from urllib.parse import urljoin, urlsplit
from urllib.error import HTTPError, URLError

# Tampilkan progres langsung (tanpa buffer) saat log di-redirect ke file.
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, 'site-content', 'sources.json')
SERIES_DIR = os.path.join(ROOT, 'site-content', 'series')

# State resume pagination: setelah satu halaman daftar (listing) selesai
# diproses, scraper menyimpan URL halaman berikutnya ke file ini. Run
# berikutnya otomatis melanjutkan dari URL tersimpan itu bila sebelumnya
# sudah pernah mengisi halaman. Matikan dengan env SCRAPE_RESUME=0.
STATE_PATH = os.path.join(ROOT, 'site-content', 'scrape-state.json')
_RESUME_ENV = os.environ.get('SCRAPE_RESUME', '').strip().lower()
SCRAPE_RESUME = not (_RESUME_ENV in ('0', 'false', 'no', 'off'))
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      'Chrome/124.0 Safari/537.36')

# Bypass verifikasi SSL untuk sumber yang sertifikatnya bermasalah (mis.
# doujindesu.xxx / mikoroku.com yang ter-block di sebagian ISP dan punya
# cert mismatch). Mati secara default; nyalakan dengan SCRAPE_INSECURE_SSL=1
# HANYA untuk sumber yang Anda percaya (risiko MITM).
_INSECURE_SSL = (os.environ.get('SCRAPE_INSECURE_SSL', '').strip().lower()
                 in ('1', 'true', 'yes', 'on'))
_SSL_CTX = ssl._create_unverified_context() if _INSECURE_SSL else None

# Jeda antar request (detik). Diperbesar + jitter acak agar tidak kelihatan
# seperti bot dan kecil kemungkinan diblokir situs sumber. Atur lewat env
# SCRAPE_DELAY bila ingin nilai lain, mis. `$env:SCRAPE_DELAY="3"`.
_delay_env = os.environ.get('SCRAPE_DELAY', '').strip()
DELAY = float(_delay_env) if _delay_env else 3.0

# ---- log detail ----
# `SCRAPE_VERBOSE=0` mematikan log per-request/per-bab; ringkasan tetap tampil.
_verbose_env = os.environ.get('SCRAPE_VERBOSE', '1').strip().lower()
VERBOSE = _verbose_env not in ('0', 'false', 'no', '')

T0 = time.time()            # awal proses global (untuk total durasi)


def ts():
    """Timestamp HH:MM:SS untuk log."""
    return time.strftime('%H:%M:%S')


def secs(t0):
    """Detik sejak t0, dibulatkan 1 desimal."""
    return '%.1fs' % (time.time() - t0)


def logv(msg):
    """Cetak baris log detail (hanya bila SCRAPE_VERBOSE != 0)."""
    if VERBOSE:
        print(msg)


def polite_delay():
    """Sleep 50%-150% dari DELAY (jitter) supaya ritme request tidak tetap."""
    time.sleep(DELAY * random.uniform(0.5, 1.5))

# Jeda antar percobaan ulang sebuah request yang gagal (timeout/DNS/HTTP).
RETRY_DELAYS = (5, 15, 35)
# Setelah N kegagalan beruntun, jeda panjang agar sumber tidak makin ketat /
# DNS pulih dulu. (circuit breaker)
CIRCUIT_BREAK_AFTER = 4
CIRCUIT_BREAK_PAUSE = (20.0, 45.0)

# 0 = tanpa batas; selain itu = batas jumlah bab yang diambil gambarnya per run.
_MAX_CAP = os.environ.get('MAX_IMAGE_CHAPTERS', '')
MAX_IMAGE_CHAPTERS = int(_MAX_CAP) if _MAX_CAP.isdigit() else 0

# 0 = tanpa batas; selain itu = batas jumlah bab yang diisi tanggalnya per run.
_MAX_D = os.environ.get('MAX_CHAPTER_DATES', '')
MAX_CHAPTER_DATES = int(_MAX_D) if _MAX_D.isdigit() else 0

# ---- pemrosesan paralel ----
# Ambang jumlah bab yang perlu diambil => aktifkan mode paralel otomatis
# (mis. seri dengan lebih dari 50 bab yang butuh gambar).
# Atur lewat env SCRAPE_PARALLEL_MIN (default 50).
_PMIN_ENV = os.environ.get('SCRAPE_PARALLEL_MIN', '').strip()
PARALLEL_MIN_CHAPTERS = int(_PMIN_ENV) if _PMIN_ENV.isdigit() else 50
# Jumlah worker paralel (default 4). Atur lewat env SCRAPE_WORKERS.
_WORKERS_ENV = os.environ.get('SCRAPE_WORKERS', '').strip()
SCRAPE_WORKERS = int(_WORKERS_ENV) if _WORKERS_ENV.isdigit() else 4
# Jumlah SERI yang diproses secara bersamaan (paralel lintas-seri) dalam satu
# run. Default 1 = seri diproses satu per satu (perilaku lama). >1 => beberapa
# seri di-scrape bareng (lebih cepat, tapi ritme request ke sumber juga
# meningkat). Atur lewat env SCRAPE_SERIES_WORKERS.
_SSW_ENV = os.environ.get('SCRAPE_SERIES_WORKERS', '').strip()
SERIES_WORKERS = max(1, int(_SSW_ENV) if _SSW_ENV.isdigit() else 1)

# Batas seri BARU (belum pernah ada di katalog) yang boleh ditambahkan dalam
# sekali run. Begitu tercapai, scraper BERHENTI memproses seri berikutnya,
# termasuk scan halaman lanjutan. Default 200; atur lewat env SCRAPE_MAX_NEW.
_NEW_ENV = os.environ.get('SCRAPE_MAX_NEW', '').strip()
MAX_NEW_SERIES = int(_NEW_ENV) if _NEW_ENV.isdigit() else 200

# Auto-build: setelah run selesai, langsung jalankan `python build.py`
# agar halaman statis ikut diperbarui. Default: otomatis bila ada seri BARU.
# Set SCRAPE_AUTO_BUILD=1 untuk selalu menjalankan, atau =0 untuk mati.
AUTO_BUILD_ENV = os.environ.get('SCRAPE_AUTO_BUILD', '').strip().lower()

# Nomor bab: 'Chapter 12' / 'Bab 3.5'.
CH_RE = re.compile(r'\b(?:chapter|bab)\s*(\d+(?:\.\d+)?)', re.I)

# Nomor halaman pada URL daftar: https://sumber/komik-terbaru/page/2/
PAGE_NUM_RE = re.compile(r'(/page/)(\d+)(/?)', re.I)

# URL <loc> di dalam file sitemap (XML). Dipakai oleh sitemap_series_entries.
SITEMAP_LOC_RE = re.compile(r'<loc>\s*(https?://[^<\s]+?)\s*</loc>', re.I)

# Tanggal update terakhir, dikutip dari halaman seri sumber HTML.
UPD_TXT_RE = re.compile(
    r'Update\s+chapter\s+terbaru\s+komik\b.*?\badalah\s+tanggal\s+'
    r'([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})', re.I | re.S)
UPD_META_RE = re.compile(
    r'<meta[^>]+(?:article:modified_time|og:updated_time)[^>]+'
    r'content="(\d{4}-\d{2}-\d{2})"', re.I)
UPD_TIME_RE = re.compile(
    r'<time\b[^>]*datetime="(\d{4}-\d{2}-\d{2})"', re.I)

# Tanggal terbit per-bab, dikutip dari halaman bab sumber HTML.
CH_DATE_RE = re.compile(
    r'<meta[^>]+(?:article:published_time|article:modified_time|'
    r'og:updated_time)[^>]+content="(\d{4}-\d{2}-\d{2})"', re.I)
CH_DATE_LD_RE = re.compile(
    r'"(?:datePublished|dateModified|uploadDate)"\s*:\s*'
    r'"(\d{4}-\d{2}-\d{2})[T ]', re.I)
CH_DATE_TIME_RE = re.compile(
    r'<time\b[^>]*datetime="(\d{4}-\d{2}-\d{2})"', re.I)

# Nama bulan (sumber memakai Bahasa Indonesia / Inggris) -> nomor bulan.
MONTH_NUM = {
    'januari': 1, 'februari': 2, 'maret': 3, 'april': 4, 'mei': 5, 'juni': 6,
    'juli': 7, 'agustus': 8, 'september': 9, 'oktober': 10, 'november': 11,
    'desember': 12,
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11,
    'december': 12,
}

TAG_RE = re.compile(r'<[^>]+>')


def clean(t):
    return re.sub(r'\s+', ' ', H.unescape(TAG_RE.sub('', t or ''))).strip()


def clean_title(t):
    """Judul bersih khas situs sumber:
    'Komik One Piece: Ace Story - KomikIndo' -> 'One Piece Ace Story'.
    - titik dua diganti spasi (bagian setelah ':' TIDAK dibuang),
    - awalan branding 'Komik/Manga/Manhwa/Baca Komik' dibuang,
    - akhiran ' - KomikIndo' dibuang."""
    t = clean(t)
    t = re.sub(r'\s*:\s*', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    t = re.sub(r'\s*-\s*(?:KomikIndo|Komikindo|Baca\s+Komik|Komik\s+Indonesia)[\w .]*$',
               '', t, flags=re.I).strip()
    t = re.sub(r'^(?:Baca\s+)?(?:Komik|Manga|Manhwa|Komik\s+Indo)\s+',
               '', t, flags=re.I).strip()
    return t


def slugify(s):
    s = re.sub(r'[^a-z0-9]+', '-', (s or '').strip().lower())
    return re.sub(r'-+', '-', s).strip('-')


def extract_last_updated(html):
    """Kembalikan tanggal update terakhir `YYYY-MM-DD` dari halaman seri HTML."""
    m = UPD_TXT_RE.search(html)
    if m:
        mo = MONTH_NUM.get(m.group(1).strip().lower())
        if mo:
            return '%04d-%02d-%02d' % (int(m.group(3)), mo, int(m.group(2)))
    m = UPD_META_RE.search(html) or UPD_TIME_RE.search(html)
    if m:
        return m.group(1)
    return ''


def extract_chapter_date(html):
    """Kembalikan tanggal terbit bab `YYYY-MM-DD` dari HTML halaman bab."""
    m = CH_DATE_RE.search(html) or CH_DATE_LD_RE.search(html) \
        or CH_DATE_TIME_RE.search(html)
    return m.group(1) if m else ''
def fetch(url, timeout=30):
    """Ambil HTML halaman dengan retry + backoff untuk gangguan sementara
    (timeout, DNS gagal, handshake timeout, HTTP 429/503/403). Log detail:
    status, ukuran respons, dan durasi tiap request."""
    logv('[fetch] %s' % url)
    t0 = time.time()
    last = None
    # percobaan ke-0 langsung, lalu retry sesuai RETRY_DELAYS
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            req = Request(url, headers={'User-Agent': UA, 'Accept': 'text/html',
                                        'Accept-Language': 'id,en;q=0.8'})
            with urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
                raw = r.read()
            logv('  [fetch] OK HTTP %s | %d KB | %s | %s'
                 % (r.status, max(1, len(raw) // 1024), secs(t0), url))
            return raw.decode('utf-8', 'ignore')
        except HTTPError as ex:
            last = ex
            if ex.code in (403, 429, 503):
                print('   ! HTTP %s (kemungkinan rate-limit) %s [%s]'
                      % (ex.code, url, ts()))
            else:
                print('   ! HTTP %s %s [%s]' % (ex.code, url, ts()))
        except Exception as ex:
            last = ex
            print('   ! request %s gagal: %s [%s]' % (url, ex, ts()))
        if attempt < len(RETRY_DELAYS):
            w = RETRY_DELAYS[attempt]
            print('     coba ulang dalam %ds (percobaan %d/%d) [%s]...'
                  % (w, attempt + 1, len(RETRY_DELAYS), ts()))
            time.sleep(w)
    raise last if last else Exception('fetch gagal: %s' % url)


def fetch_json(url):
    """Ambil berkas JSON dari URL `url` dan kembalikan objek Python.
    Toleran terhadap BOM di awal berkas."""
    raw = fetch(url)
    return json.loads(raw.lstrip('\ufeff'))


def pause_on_failures(consecutive):
    """Kalau sudah banyak kegagalan beruntun, istirahat panjang (circuit breaker)."""
    if consecutive >= CIRCUIT_BREAK_AFTER:
        pause = random.uniform(*CIRCUIT_BREAK_PAUSE)
        print('   ! %d kegagalan beruntun; istirahat %ds agar sumber pulih...'
              % (consecutive, round(pause)))
        time.sleep(pause)
        return 0
    return consecutive


def load_json_file(path):
    """Baca JSON toleran BOM (utf-8-sig). BOM sering muncul dari editor Windows."""
    try:
        with open(path, encoding='utf-8-sig') as fh:
            return json.load(fh)
    except Exception:
        return None


# ---------------------------------------------------------------- bab (label)


def chapter_label(c):
    """Label singkat satu bab untuk log: 'Chapter 12' atau judul bila tak bernomor."""
    n = c.get('num')
    if n is not None:
        num = int(n) if float(n).is_integer() else n
        return 'Chapter %s' % num
    return (c.get('title') or c.get('slug') or 'chapter').strip()


def chapter_images_ok(c):
    """Benar bila bab dianggap sudah punya gambar yang BENAR, sehingga bisa
    dilewati (incremental). Salah bila: tidak ada gambar, ada URL aset/sampah
    (favicon/logo/thumbnail), ekstensi salah, atau URL memuat spasi mentah."""
    imgs = c.get('images') or []
    if not imgs:
        return False
    for u in imgs:
        if not isinstance(u, str) or not u.strip().startswith('http'):
            return False
        low = u.lower().split('?')[0].split('#')[0]
        if re.search(r'\s', u):
            return False                     # spasi mentah (harus %20)
        if not re.search(r'\.(jpe?g|png|webp|gif|avif)$', low):
            return False
        if _is_asset_url(u):
            return False
    return True


def _is_asset_url(u):
    """Benar bila URL kemungkinan logo/iklan/aset tema, bukan gambar bab."""
    low = u.lower()
    if any(k in low for k in (
            '/wp-content/themes/', '/wp-content/plugins/', '/wp-includes/',
            '/favicon', '/fav.', '/sponsor', '/banner/', '/icons/', '/emoji/',
            '/smiley', 'googleusercontent.com/gadgets', 'data:image',
            'komikindo-e', 'logo', 'avatar', 'icon-', 'favicon', 'sponsor',
            'ads.', 'i0.wp.com', 'i1.wp.com', 'i2.wp.com', 'i3.wp.com')):
        return True
    # thumbnail WordPress (mis. ...-190x285.jpg, ...-600x315.png) dan versi
    # kecil Jetpack (?w=50, ?resize=...) bukan gambar bab.
    if re.search(r'-\d{2,4}x\d{2,4}\.', low):
        return True
    if re.search(r'(?:^|[?&])(?:w|resize|h)=\d+', low):
        return True
    return False
def _parallel_for(items, worker, workers, label):
    """Jalankan `worker(item)` untuk tiap item secara paralel (max `workers`
    thread) atau sequential bila workers<=1. `worker` harus menangkap
    exception & mengembalikan True/False. Kembalikan (sukses, gagal)."""
    if not items:
        return 0, 0
    if workers <= 1:
        ok = fail = 0
        for it in items:
            try:
                if worker(it):
                    ok += 1
                else:
                    fail += 1
            except Exception as ex:
                fail += 1
                print('   ! %s gagal: %s' % (label, ex))
        return ok, fail
    ok = fail = 0
    chunks = [items[i::workers] for i in range(min(workers, len(items)))]
    with ThreadPoolExecutor(max_workers=len(chunks)) as ex:
        for o, f in ex.map(lambda ch: _run_parallel_chunk(ch, worker, label),
                           chunks):
            ok += o
            fail += f
    return ok, fail


def _run_parallel_chunk(chunk, worker, label):
    """Kerjakan satu potong daftar untuk ThreadPoolExecutor."""
    ok = fail = 0
    for it in chunk:
        try:
            if worker(it):
                ok += 1
            else:
                fail += 1
        except Exception as ex:
            fail += 1
            print('   ! %s gagal: %s' % (label, ex))
    return ok, fail


# ------------------------------------------------------------- helper seri


def make_series_id(length=6):
    """Id pendek acak per seri (a-z0-9). Dipakai sebagai prefix slug bab."""
    return ''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789',
                                  k=length))


def prefix_slug(series_id, slug):
    """Prefix slug bab dengan id seri: 'ab12cd-chapter-414'.
    Dengan begitu bab dari seri berbeda TIDAK saling menimpa folder/URL
    (mis. 'Chapter 10' milik Eleceed vs One Piece tetap terpisah)."""
    slug = (slug or 'chapter').strip('-')
    p = (series_id or '') + '-'
    if p == '-':
        return slug
    return slug if slug.startswith(p) else p + slug


def series_slug_from_url(url):
    """Slug KANONIK dari URL halaman seri (segmen path terakhir). Mengabaikan
    prefix nomor ('846048-eleceed' -> 'eleceed') dan query string. Dipakai
    sebagai kunci anti-duplikat: URL yang berbeda utk seri sama -> slug sama."""
    u = (url or '').split('?')[0].rstrip('/')
    seg = [s for s in u.split('/') if s]
    if not seg:
        return ''
    last = seg[-1]
    if last.lower() in ('komik', 'manga', 'series', 'manhwa', 'index'):
        return ''
    m = re.match(r'^\d+-(.+)$', last)
    if m:
        last = m.group(1)
    return slugify(last)


def resolve_canonical_slug(slug):
    """Slug yang berasal dari URL BAB disederhanakan menjadi slug SERI.
    Contoh:
      'one-piece-ace-story-chapter-4-end'      -> 'one-piece-ace-story'
      'boruto-two-blue-vortex'                 -> 'boruto-two-blue-vortex'
    Dipakai supaya sumber berupa halaman bab tetap menghasilkan slug seri
    yang sama dan tidak membuat entri duplikat."""
    s = (slug or '').strip().strip('-')
    for marker in ('-chapter', '-bab'):
        m = re.split(marker + r'(?:[-_]|$)', s, flags=re.I, maxsplit=1)
        if len(m) > 1:
            s = m[0].strip('-')
    return s or (slug or '').strip('-')


def normalize_series_title(t):
    """Normalisasi judul seri untuk perbandingan: huruf kecil, hapus tanda
    baca, rapikan spasi. 'One Piece: Ace Story' == 'One Piece Ace Story'."""
    t = re.sub(r'[^a-z0-9]+', ' ', (t or '').lower())
    return re.sub(r'\s+', ' ', t).strip()


def _chapter_ext_set(data):
    """Set URL bab (external) ternormalisasi milik satu seri."""
    return {((c.get('external') or '').strip().rstrip('/').lower())
            for c in (data.get('chapters') or []) if c.get('external')}


def _overlap_count(a, b):
    """Jumlah URL bab yang sama di antara dua seri."""
    return len(_chapter_ext_set(a) & _chapter_ext_set(b))


def is_same_series(a, b):
    """Benar bila dua data seri (dict file JSON) adalah seri yang SAMA:
    slug/source_url sama, overlap bab besar, atau judul sama + bab beririsan."""
    aslug = (a.get('slug') or '').strip().lower()
    bslug = (b.get('slug') or '').strip().lower()
    if aslug and aslug == bslug:
        return True
    asrc = (a.get('source_url') or '').strip().rstrip('/').lower()
    bsrc = (b.get('source_url') or '').strip().rstrip('/').lower()
    if asrc and bsrc and asrc == bsrc:
        return True
    ov = _overlap_count(a, b)
    if ov >= 2:
        return True
    if ov >= 1 and normalize_series_title(a.get('title')) \
            and normalize_series_title(a.get('title')) == \
            normalize_series_title(b.get('title')):
        return True
    return False
def cleanup_duplicate_series():
    """Gabungkan file katalog yang ternyata SERI SAMA (slug/source_url sama,
    overlap bab >=2, atau judul sama dgn bab irisan) menjadi satu file dan
    hapus file duplikatnya. Dipanggil otomatis di awal run() untuk membersihkan
    duplikat yang sudah terlanjur ada tanpa kehilangan bab."""
    files = list_series_files()
    consumed, merged_any = set(), False
    for i, p in enumerate(files):
        if p in consumed:
            continue
        a = load_json_file(p) or {}
        group = [p]
        for q in files[i + 1:]:
            if q in consumed:
                continue
            b = load_json_file(q) or {}
            if is_same_series(a, b):
                group.append(q)
        if len(group) < 2:
            consumed.add(p)
            continue

        # pilih file "utama": bab terbanyak -> gambar terbanyak -> slug
        # terpanjang (lebih deskriptif) -> mtime terbaru.
        def score(fp):
            d = load_json_file(fp) or {}
            ch = d.get('chapters') or []
            return (len(ch), sum(1 for c in ch if c.get('images')),
                    len((d.get('slug') or '')), os.path.getmtime(fp))
        pri = sorted(group, key=score, reverse=True)[0]
        pd = load_json_file(pri) or {}
        by_ext = {c.get('external'): c for c in (pd.get('chapters') or [])
                  if c.get('external')}
        for dup in group:
            if dup == pri:
                continue
            dd = load_json_file(dup) or {}
            added = 0
            for c in dd.get('chapters') or []:
                ext = c.get('external') or ''
                if ext and ext in by_ext:
                    continue
                by_ext[ext] = c
                added += 1
            if not pd.get('source_url') and dd.get('source_url'):
                pd['source_url'] = dd['source_url']
            if len(pd.get('title') or '') < len(dd.get('title') or ''):
                pd['title'] = dd['title']
            try:
                os.remove(dup)
            except OSError as ex:
                print('   ! gagal hapus duplikat %s: %s' % (dup, ex))
            print('  [dedup] gabung "%s" -> "%s" (+%d bab unik)'
                  % (os.path.basename(dup), os.path.basename(pri), added))
            consumed.add(dup)
        consumed.add(pri)
        pd['chapters'] = list(by_ext.values())
        with open(pri, 'w', encoding='utf-8') as fh:
            json.dump(pd, fh, ensure_ascii=False, indent=2)
        merged_any = True
    if merged_any:
        print('  [dedup] duplikat telah digabung; jalankan '
              '`python build.py` agar halaman ikut diperbarui.')
    return merged_any


def merge_existing_chapter_data(chapters, slug):
    """Tempel kembali gambar/tanggal bab dari file seri lama (incremental).
    Bab yang URL gambarnya SALAH tetap di-fetch ulang oleh pengisi gambar
    karena chapter_images_ok(...) mengembalikan False."""
    prev = load_json_file(os.path.join(SERIES_DIR, slug + '.json')) or {}
    old = {c.get('external'): c for c in (prev.get('chapters') or [])
           if c.get('external')}
    for c in chapters:
        oc = old.get(c.get('external') or '')
        if oc:
            if not c.get('images') and oc.get('images'):
                c['images'] = oc['images']
            if not c.get('date') and oc.get('date'):
                c['date'] = oc['date']
    return chapters


def find_existing_series(data):
    """Deteksi DUPLIKAT seri di katalog. Blok urutan prioritas:
      1) slug sama;
      2) source_url sama;
      3) OVERLAP URL bab (external) >= 2 (dua seri dengan bab yang sama
         adalah seri yang sama walau slug-nya beda);
      4) judul ternormalisasi sama DAN minimal 1 bab sama.
    Kembalikan dict seri lama (atau None)."""
    target = (data.get('slug') or '').strip().lower()
    src = (data.get('source_url') or '').rstrip('/').lower()
    best, best_overlap = None, 0
    for p in list_series_files():
        d = load_json_file(p) or {}
        dslug = (d.get('slug') or os.path.splitext(os.path.basename(p))[0])
        if target and dslug.strip().lower() == target:
            return d
        dsrc = (d.get('source_url') or '').rstrip('/').lower()
        if src and dsrc and dsrc == src:
            return d
        if is_same_series(data, d):
            ov = _overlap_count(data, d)
            if ov >= best_overlap:
                best, best_overlap = d, ov
    return best
# Sumber yang URL gambar bab-nya "bertanda tangan" dan kedaluwarsa ~24 jam
# (mis. doujindesu). Untuk sumber ini, gambar lama TIDAK dipertahankan di mode
# link; halaman reader cukup menampilkan tombol sumber sebagai fallback.
STALE_IMAGE_SOURCES = ('doujin.desu.xxx',)


def write_series(slug, data):
    """Tulis site-content/series/<slug>.json; pertahankan data lama (desc,
    gambar bab yang sudah direkam) agar tidak terhapus saat di-rewrite."""
    path = os.path.join(SERIES_DIR, slug + '.json')
    prev = load_json_file(path) or {}
    # Id acak per seri: dibuat sekali, dipakai ulang di run berikutnya.
    series_id = (prev.get('id') or '').strip() or make_series_id()
    old = {c.get('external'): c for c in (prev.get('chapters') or [])
           if c.get('external')}
    merged_ch, seen = [], set()
    added = updated = preserved = 0
    for c in data.get('chapters') or []:
        ext = c.get('external') or ''
        if ext:
            if ext in seen:
                continue
            seen.add(ext)
            oc = old.get(ext)
            if oc:
                updated += 1
            else:
                added += 1
            if oc and not c.get('images') and oc.get('images'):
                _src_u = (data.get('source_url') or '').lower()
                if not any(d in _src_u for d in STALE_IMAGE_SOURCES):
                    c['images'] = oc['images']   # jaga gambar lama (incremental)
            if oc and not c.get('date') and oc.get('date'):
                c['date'] = oc['date']        # jaga tanggal bab lama
        merged_ch.append(c)
    # pertahankan bab yang sudah ada tapi tak muncul di scrape terbaru
    for c in prev.get('chapters') or []:
        ext = c.get('external') or ''
        if ext and ext not in seen:
            seen.add(ext)
            merged_ch.append(c)
            preserved += 1
    # slump bab dibedakan per seri: prefix dengan id seri biar tidak campur aduk.
    for c in merged_ch:
        c['slug'] = prefix_slug(series_id, c.get('slug', ''))
    merged = {
        'id': series_id,
        'slug': slug,
        'source_url': data.get('source_url') or prev.get('source_url') or '',
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
        'last_updated': data.get('last_updated') or prev.get('last_updated') or '',
        'chapters': merged_ch,
    }
    os.makedirs(SERIES_DIR, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(merged, fh, ensure_ascii=False, indent=2)
    print('  [write] %s: +%d baru | %d diperbarui | %d dipertahankan | '
          'total %d bab' % (slug, added, updated, preserved, len(merged_ch)))
    return len(merged_ch)
# ------------------------------------------------------- pagination & state


def interactive():
    """Benar bila scraper dijalankan di terminal (bisa prompt), bukan CI/pipe."""
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def ask_yes_no(prompt):
    """Prompt ya/tidak di terminal; kembalikan bool."""
    if not interactive():
        return False
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ('y', 'ya', 'yes', '1', 'lanjut', 'terus')


def ask_text(prompt):
    """Prompt teks di terminal; kembalikan string ('' bila dibatalkan)."""
    if not interactive():
        return ''
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ''


def url_to_entry(url):
    """Buat entri seed dari satu URL (dipakai manual-batch & input langsung)."""
    url = (url or '').strip()
    if url.startswith('\ufeff'):
        url = url[1:].strip()
    if not url or not url.startswith('http'):
        return None
    # Lewati URL tanpa path (mis. homepage "https://site.com/").
    if not re.search(r'https?://[^/]+/[^/]+', url):
        print('   ! lewati URL tanpa path (bukan halaman seri/daftar): %s' % url)
        return None
    seg = [s for s in url.rstrip('/').split('/') if s]
    hint = seg[-1] if seg else 'manga'
    mm = re.match(r'^(\d+)-([a-z0-9]+(?:-[a-z0-9]+)*)$', hint)
    if mm:
        hint = mm.group(2)      # mis. 846048-eleceed -> eleceed
    return {'url': url, 'slug': hint,
            'title': clean_title(' '.join(w.capitalize() for w in hint.split('-'))),
            'auto_title': True}


def next_page_after(url):
    """URL halaman daftar BERIKUTNYA dari sebuah halaman daftar.
    https://x/komik-terbaru/        -> https://x/komik-terbaru/page/2/
    https://x/komik-terbaru/page/2/ -> https://x/komik-terbaru/page/3/"""
    m = PAGE_NUM_RE.search(url or '')
    if m:
        n = int(m.group(2)) + 1
        return url[:m.start()] + m.group(1) + str(n) + (m.group(3) or '')
    return url.rstrip('/') + '/page/2/'


def listing_key(url):
    """Kunci state sebuah halaman daftar: base URL tanpa bagian /page/N/.
    'https://x/komik-terbaru/' dan 'https://x/komik-terbaru/page/2/' dikunci
    ke base yang sama ('https://x/komik-terbaru/')."""
    u = (url or '').strip().strip('/')
    u = PAGE_NUM_RE.sub('', u).strip('/')
    return '/' if u == '/' else (u + '/')


def load_state():
    """Baca file state resume pagination; kembalikan dict kosong bila belum
    ada / format rusak (kegagalan membaca tidak boleh mematikan scraper)."""
    d = load_json_file(STATE_PATH) or {}
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get('listings'), dict):
        d['listings'] = {}
    return d


def save_state(state):
    """Tulis file state resume pagination. Gagal menulis tidak fatal (cuma
    kehilangan posisi resume untuk run berikutnya)."""
    if not state or not state.get('listings'):
        return
    try:
        os.makedirs(os.path.split(STATE_PATH)[0], exist_ok=True)
        with open(STATE_PATH, 'w', encoding='utf-8') as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except OSError as ex:
        print('   ! gagal menulis state resume %s: %s' % (STATE_PATH, ex))


def _state_rec(state, key):
    """Ambil record state untuk satu kunci listing; buat baru bila belum ada."""
    listings = state.setdefault('listings', {})
    rec = listings.setdefault(key, {})
    if not isinstance(rec.get('pages_done'), list):
        rec['pages_done'] = []
    return rec


def record_resume_page(state, key, page_url, next_url=None):
    """Catat halaman daftar yang baru diproses beserta URL halaman berikutnya,
    lalu simpan state. Bila `next_url` None, nilai `next_page` lama
    dipertahankan (dipakai saat menandai halaman dasar/base dari sources.json)."""
    if not key:
        return
    rec = _state_rec(state, key)
    done = [p for p in rec['pages_done'] if p and p != page_url]
    done.append(page_url)
    rec['pages_done'] = done
    if next_url is not None:
        rec['next_page'] = next_url or ''
    rec['last_scan'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    save_state(state)


def clear_resume_next(state, key):
    """Akhir daftar tercapai (halaman tak memuat seri/seri baru): bersihkan
    `next_page` agar run berikutnya mulai dari base lagi, tidak mengulang
    halaman kosong berulang kali. Halaman yang sudah diproses tetap disimpan."""
    if not key:
        return
    rec = (state.get('listings') or {}).get(key)
    if rec:
        rec['next_page'] = ''
        rec['last_scan'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        save_state(state)


def run_build():
    """Bangun ulang situs statis dengan menjalankan `python build.py`
    di folder root. Dipanggil otomatis di akhir run() bila ada seri baru
    (lihat env SCRAPE_AUTO_BUILD). Kembalikan kode keluar build."""
    print('\n[build] menjalankan `python build.py` ...')
    t0 = time.time()
    try:
        proc = subprocess.run([sys.executable, 'build.py'], cwd=ROOT)
    except Exception as ex:
        print('  [build] ! gagal menjalankan build: %s' % ex)
        return 1
    print('[build] selesai: kode keluar %s (%s)'
          % (getattr(proc, 'returncode', '?'), secs(t0)))
    return getattr(proc, 'returncode', 1)
# ------------------------------------------------- bootstrap via sitemap


def sitemap_series_entries(sitemap_url, series_url_re, sitemap_sub_re=None,
                           max_subs=40):
    """Baca sitemap (index atau urlset) lalu kumpulkan entri seri.

    - Index (mis. sitemap.xml): SEMUA sub-sitemap diikuti, tapi yang bernama
      'manga/series/komik/manhwa' diambil lebih dulu.
    - Hanya URL halaman SERI yang cocok dgn `series_url_re` yang dipakai;
      URL bab/daftar/genre/artikel dibuang.
    - Kegagalan satu sub-sitemap TIDAK mematikan proses; lanjut ke berikutnya.

    Kembalikan list entri {'url', 'slug', 'title', 'auto_title'}. Daftarnya
    bisa sangat besar (ribuan seri); BATCH_LIMIT/MAX_NEW_SERIES tetap berlaku
    saat pemrosesan, jadi tidak masalah bila hasilnya panjang.
    """
    print('[sitemap] baca: %s' % sitemap_url)
    xml = fetch(sitemap_url)
    locs = SITEMAP_LOC_RE.findall(xml)
    if not locs:
        print('   ! tidak ada <loc> di sitemap %s' % sitemap_url)
        return []
    if re.search(r'<sitemapindex', xml, re.I):
        subs = sorted({s for s in locs
                       if sitemap_sub_re and sitemap_sub_re.search(urlsplit(s).path)})
        if not subs:
            subs = sorted(set(locs))[:max_subs]
            print('   ! sub-sitemap bertema seri tidak terdeteksi; memakai '
                  'sub-sitemap terbatas (%d) -- hasil mungkin tak lengkap.'
                  % len(subs))
        elif len(subs) > max_subs:
            subs = subs[:max_subs]
        urls = []
        for su in subs:
            try:
                sub = fetch(su)
            except Exception as ex:
                print('   ! sub-sitemap gagal %s: %s' % (su, ex))
                polite_delay()
                continue
            urls.extend(SITEMAP_LOC_RE.findall(sub))
            polite_delay()
    else:
        urls = locs
    seen, out = set(), []
    for u in urls:
        u = (u or '').strip()
        if not u or u in seen:
            continue
        seen.add(u)
        if not series_url_re.search(u):
            continue
        e = url_to_entry(u)
        if e:
            out.append(e)
    print('[sitemap] selesai: %d URL discan, %d seri ditemukan'
          % (len(seen), len(out)))
    return out


# --------------------------------------------------------- kontrak adaptor


class SourceAdapter(object):
    """Kontrak adaptor satu sumber untuk mesin scraper_common.

    Setiap scraper (komikindo / mikoroku / doujindesu) mendefinisikan satu
    adaptor turunan yang hanya tahu cara membaca SUMBERNYA SENDIRI. Mesin
    run() memanggil hook di bawah ini; implementasi source-specific (regex
    HTML, API, JSON) tinggal di modul masing-masing.
    """

    name = 'generic'
    description = 'Sumber generik'

    # Tipe sumber JSON (katalog publik) yang dianggap milik adaptor ini.
    json_kinds = ()

    def matches(self, entry):
        """Benar bila `entry` (dict sources.json) harus diproses adaptor ini."""
        return False

    def match_url(self, url):
        """Benar bila `url` adalah milik sumber adaptor ini (manual-batch,
        tempelan URL interaktif, dst)."""
        return False

    def is_listing_url(self, url):
        """Benar bila URL menunjuk halaman direktori/daftar, bukan halaman seri."""
        return False

    def expand_seed(self, entry, want_images):
        """Perluas satu entri sumber: halaman daftar -> daftar entri seri.
        Entri yang sudah berupa halaman seri cukup dikembalikan apa adanya."""
        return [entry]

    def scrape_series(self, entry, want_images=False, want_dates=False):
        """Scrape SATU seri -> dict data seri skema scraper (chapters dll)."""
        raise NotImplementedError('%s: scrape_series belum diimplementasi'
                                  % self.name)

    def sitemap_series_entries(self, sitemap_url):
        """Baca sitemap sumber -> daftar entri seri (default: tidak didukung)."""
        print('   ! sumber %s tidak mendukung seed dari sitemap.' % self.name)
        return []

    def refresh_chapter(self, series, chapter):
        """Ambil ulang URL gambar (+tanggal) satu bab utk --refresh-images.
        Kembalikan (images_list, date) atau None bila tidak bisa/gagal."""
        return None

    def test(self):
        """Self-test parser (dipanggil dengan --test)."""
        print('%s: tidak ada self-test khusus.' % self.name)


# Daftar adaptor terdaftar. Setiap scraper_*.py menelepon register_adapter()
# saat diimpor. Dipakai scraper.py (multi-sumber) dan routing --refresh-images.
_ADAPTERS = []


def register_adapter(adapter):
    """Daftarkan adaptor sumber ke registry global (satu per sumber)."""
    if not isinstance(adapter, SourceAdapter):
        raise TypeError('register_adapter butuh instance SourceAdapter')
    for i, a in enumerate(_ADAPTERS):
        if a.name == adapter.name:
            _ADAPTERS[i] = adapter
            return adapter
    _ADAPTERS.append(adapter)
    return adapter


def adapter_by_name(name):
    """Ambil adaptor terdaftar berdasarkan nama ('komikindo'/'mikoroku'/...)."""
    for a in _ADAPTERS:
        if a.name == name:
            return a
    return None


def all_adapters():
    """Daftar adaptor terdaftar (urutan registrasi). Dipakai scraper.py
    multi-sumber untuk menjalankan SEMUA sumber dalam satu perintah."""
    return list(_ADAPTERS)


def adapter_for_url(url):
    """Pilih adaptor yang paling cocok untuk sebuah URL (dipakai routing
    `--refresh-images` multi-sumber dan scraper.py)."""
    url = (url or '').strip()
    for a in _ADAPTERS:
        if url and a.match_url(url):
            return a
    return None
# ------------------------------------------------------------- pemeliharaan


def list_series_files():
    """Daftar path file JSON seri di site-content/series/, urut slug."""
    if not os.path.isdir(SERIES_DIR):
        return []
    files = [f for f in os.listdir(SERIES_DIR)
             if f.lower().endswith('.json')]
    files.sort(key=lambda f: f.lower())
    return [os.path.join(SERIES_DIR, f) for f in files]


def read_series_meta(path):
    """Baca metadata ringan sebuah file seri (slug, title, id, jumlah bab)."""
    d = load_json_file(path) or {}
    return {
        'path': path,
        'slug': d.get('slug') or os.path.splitext(os.path.basename(path))[0],
        'title': d.get('title') or d.get('slug') or os.path.basename(path),
        'id': (d.get('id') or '').strip(),
        'chapters': len(d.get('chapters') or []),
    }


def remove_built_artifacts(meta):
    """Hapus artefak hasil build untuk satu seri: halaman detail
    `manga/<slug>/` dan folder bab `<id>-...` di root (bila ada)."""
    removed = []
    detail = os.path.join(ROOT, 'manga', meta['slug'])
    if os.path.isdir(detail):
        shutil.rmtree(detail, ignore_errors=True)
        removed.append(detail)
    # Folder bab hasil build dinamai '<id>-chapter-N' (lihat prefix_slug).
    if meta['id']:
        prefix = meta['id'] + '-'
        try:
            for name in os.listdir(ROOT):
                p = os.path.join(ROOT, name)
                if os.path.isdir(p) and name.startswith(prefix):
                    shutil.rmtree(p, ignore_errors=True)
                    removed.append(p)
        except OSError:
            pass
    return removed


def _script_name():
    """Nama script yang sedang dijalankan (untuk pesan bantuan CLI)."""
    try:
        return os.path.basename(sys.argv[0]) or 'scraper.py'
    except Exception:
        return 'scraper.py'


def delete_series():
    """Hapus seri dari katalog (file JSON) beserta artefak build-nya.

    Usage:
      python %s --delete              # interaktif: pilih seri / semua
      python %s --delete all          # hapus SEMUA seri (konfirmasi)
      python %s --delete <slug> ...   # hapus seri tertentu (non-interaktif)
    Lewati konfirmasi dengan --force atau env SCRAPE_FORCE=1.
    """ % (_script_name(), _script_name(), _script_name())
    files = list_series_files()
    if not files:
        print('Tidak ada seri untuk dihapus (site-content/series/ kosong).')
        return 0

    metas = [read_series_meta(p) for p in files]

    # --- Tentukan target dari argumen baris perintah ---
    try:
        di = sys.argv.index('--delete')
    except ValueError:
        di = -1
    targets = []
    want_all = False
    if di >= 0:
        rest = [a for a in sys.argv[di + 1:]
                if a and not a.startswith('--')]
        if 'all' in [a.lower() for a in rest]:
            want_all = True
        elif rest:
            lower_rest = set(a.lower() for a in rest)
            targets = [m for m in metas
                       if m['slug'].lower() in lower_rest
                       or m['title'].lower() in lower_rest]

    # --- Mode interaktif: tampilkan daftar dan minta pilihan ---
    if not want_all and not targets and interactive():
        print('Seri di katalog (%d):' % len(metas))
        for i, m in enumerate(metas, 1):
            print('  %2d. %s  (%d bab)' % (i, m['title'], m['chapters']))
        print('  all. Hapus SEMUA seri')
        sel = ask_text(
            '\nPilih nomor seri yang dihapus (pisahkan koma/space, atau '
            '"all" untuk semua, kosongkan untuk batal):\n> ')
        if not sel:
            print('Dibatalkan; tidak ada yang dihapus.')
            return 0
        if sel.strip().lower() == 'all':
            want_all = True
        else:
            chosen = []
            for p in re.split(r'[,\s]+', sel.strip()):
                if not p:
                    continue
                if p.isdigit():
                    idx = int(p)
                    if 1 <= idx <= len(metas):
                        chosen.append(idx)
                    else:
                        print('   ! nomor %s di luar jangkauan; dilewati.' % p)
                else:
                    for m in metas:
                        if (m['slug'].lower() == p.lower()
                                or m['title'].lower() == p.lower()):
                            chosen.append(metas.index(m) + 1)
            targets = [metas[i - 1] for i in sorted(set(chosen))]

    if want_all:
        targets = metas

    if not targets:
        print('Tidak ada seri yang dipilih; tidak ada yang dihapus.')
        return 0

    # --- Konfirmasi ---
    force = ('--force' in sys.argv
             or os.environ.get('SCRAPE_FORCE', '').strip() not in ('', '0'))
    if not force:
        summary = '\n'.join('  - %s (%d bab)' % (t['title'], t['chapters'])
                            for t in targets)
        print('Akan dihapus:\n%s' % summary)
        if not ask_yes_no('\nYakin ingin menghapus %d seri ini? [y/tidak]: '
                          % len(targets)):
            print('Dibatalkan; tidak ada yang dihapus.')
            return 0

    # --- Eksekusi ---
    for m in targets:
        try:
            os.remove(m['path'])
        except OSError as ex:
            print('   ! gagal hapus %s: %s' % (m['path'], ex))
            continue
        arts = remove_built_artifacts(m)
        extra = ''
        if arts:
            rel = [os.path.relpath(a, ROOT) for a in arts]
            extra = ' [artefak: %s]' % ', '.join(rel)
        print('-> dihapus: %s (%d bab)%s'
              % (m['title'], m['chapters'], extra))
    print('selesai: %d seri dihapus.' % len(targets))
    print('Jalankan `python build.py` untuk membersihkan sisa '
          'halaman yang sudah tidak terpakai (search/sitemap).')
    return 0
CHAPTER_FLAGS = ('--chapters', '--chapter')


def _parse_refresh_args(argv, start):
    """Pisahkan argumen setelah --refresh-images menjadi (target, spec bab).
    Nilai --chapters/--chapter diambil sebagai spec; ini mencegah nilai seperti
    '1,2' ikut dianggap slug seri. Argumen lain berawalan '--' (mis. --force)
    dilewati begitu saja."""
    rest, spec = [], ''
    i, n = start, len(argv)
    while i < n:
        a = argv[i]
        if a in CHAPTER_FLAGS:
            if i + 1 < n:
                spec = argv[i + 1].strip()
                i += 2
                continue
        elif not a.startswith('--'):
            rest.append(a)
        i += 1
    return rest, spec


def select_chapters(chapters, spec):
    """Saring daftar bab sesuai spec pemilihan bab.

    Didukung:
      kosong / 'all' / 'semua' / '*' -> semua bab
      'latest[:N]' / 'terbaru[:N]'   -> N bab TERBARU (default 1)
      '1,3,5'                        -> bab nomor 1, 3, dan 5
      '2-10' atau '2..10'            -> rentang bab 2 s.d. 10
      teks lain                      -> cocokkan potongan judul/slug bab

    Kembalikan (bab_terpilih, deskripsi_singkat)."""
    if not spec:
        return chapters, 'semua bab'
    s = spec.strip().lower()
    if s in ('all', 'semua', '*'):
        return chapters, 'semua bab'
    m = re.match(r'^(?:latest|terbaru|baru)(?::(\d+))?$', s)
    if m:
        n = int(m.group(1)) if m.group(1) else 1
        order = sorted(chapters, key=lambda c: (
            (1, 0) if c.get('num') is None else (0, -(float(c.get('num') or 0)))))
        return order[:n], '%d bab terbaru' % n
    nums, texts = set(), []
    for tok in re.split(r'[,\s]+', s):
        if not tok:
            continue
        rm = re.match(
            r'^([0-9]+(?:\.[0-9]+)?)\s*[-.]+?\s*([0-9]+(?:\.[0-9]+)?)$', tok)
        if rm:
            a, b = float(rm.group(1)), float(rm.group(2))
            if a > b:
                a, b = b, a
            for c in chapters:
                n = c.get('num')
                if n is not None and a <= float(n) <= b:
                    nums.add(round(float(n), 6))
            continue
        fm = re.match(r'^[0-9]+(?:\.[0-9]+)?$', tok)
        if fm:
            nums.add(round(float(tok), 6))
            continue
        texts.append(tok)
    out = []
    for c in chapters:
        n = c.get('num')
        if n is not None and round(float(n), 6) in nums:
            out.append(c)
            continue
        if texts:
            hay = ('%s %s' % (c.get('slug') or '', c.get('title') or '')).lower()
            if any(t in hay for t in texts):
                out.append(c)
    if not nums and texts:
        return out, 'bab berisi "%s"' % ', '.join(texts)
    return out, 'bab terpilih: %s' % spec
def refresh_series_images(adapter=None):
    """Segarkan ulang URL gambar seluruh bab dari halaman sumbernya.

    Mode rutin (`--images`) bersifat incremental: bab yang sudah punya `images`
    TIDAK di-fetch ulang. Bila URL CDN/mirror sumber berubah atau mati, mode ini
    dipakai untuk mencari ulang (re-parse) dan MENIMPA `images` tiap bab dengan
    URL terbaru dari `external`. Bab yang gagal dimuat tetap mempertahankan URL
    lamanya (tidak dihapus).

    Usage:
      python %s --refresh-images              # interaktif: pilih seri & bab
      python %s --refresh-images all          # semua seri (konfirmasi)
      python %s --refresh-images <slug> ...   # seri tertentu (non-interaktif)
      python %s --refresh-images <slug> --chapters 1,3,5   # hanya bab 1,3,5
      python %s --refresh-images <slug> --chapters 2-10    # rentang bab 2-10
      python %s --refresh-images <slug> --chapters latest:3  # 3 bab terbaru

    Lewati konfirmasi dengan --force atau env SCRAPE_FORCE=1.
    Batasi jumlah bab yang disegarkan per run dengan env MAX_IMAGE_CHAPTERS.
    Setelah selesai, jalankan `python build.py`.
    """ % (_script_name(), _script_name(), _script_name(), _script_name(),
           _script_name(), _script_name())
    files = list_series_files()
    if not files:
        print('Tidak ada seri untuk disegarkan (site-content/series/ kosong).')
        return 0
    metas = [read_series_meta(p) for p in files]

    # --- target dari argumen baris perintah (+ opsi --chapters) ---
    try:
        ri = sys.argv.index('--refresh-images')
    except ValueError:
        ri = -1
    targets, chapter_spec = [], ''
    want_all = False
    if ri >= 0:
        rest, chapter_spec = _parse_refresh_args(sys.argv, ri + 1)
        if 'all' in [a.lower() for a in rest]:
            want_all = True
        elif rest:
            lower_rest = set(a.lower() for a in rest)
            targets = [m for m in metas
                       if m['slug'].lower() in lower_rest
                       or m['title'].lower() in lower_rest]

    # --- mode interaktif: pilih dari daftar ---
    if not want_all and not targets and interactive():
        print('Seri di katalog (%d):' % len(metas))
        for i, m in enumerate(metas, 1):
            print('  %2d. %s  (%d bab)' % (i, m['title'], m['chapters']))
        print('  all. Segarkan SEMUA seri')
        sel = ask_text(
            '\nPilih nomor seri yang disegarkan (pisahkan koma/space, atau '
            '"all" untuk semua, kosongkan untuk batal):\n> ')
        if not sel:
            print('Dibatalkan; tidak ada yang disegarkan.')
            return 0
        if sel.strip().lower() == 'all':
            want_all = True
        else:
            chosen = []
            for p in re.split(r'[,\s]+', sel.strip()):
                if not p:
                    continue
                if p.isdigit():
                    idx = int(p)
                    if 1 <= idx <= len(metas):
                        chosen.append(idx)
                    else:
                        print('   ! nomor %s di luar jangkauan; dilewati.' % p)
                else:
                    for m in metas:
                        if (m['slug'].lower() == p.lower()
                                or m['title'].lower() == p.lower()):
                            chosen.append(metas.index(m) + 1)
            targets = [metas[i - 1] for i in sorted(set(chosen))]

    if want_all:
        targets = metas

    if not targets:
        print('Tidak ada seri yang dipilih; tidak ada yang disegarkan.')
        return 0

    # --- pemilihan bab (interaktif bila terminal & belum ada --chapters) ---
    if interactive() and not chapter_spec:
        first = targets[0]
        d0 = load_json_file(first['path']) or {}
        ch0 = d0.get('chapters') or []
        print('\nDaftar bab %s (%d):' % (first['title'], len(ch0)))
        for c in ch0[:20]:
            num = c.get('num')
            lbl = '' if num is None else str(
                int(num) if float(num).is_integer() else num)
            print('  %-6s %s' % (lbl, (c.get('title') or '').strip()))
        if len(ch0) > 20:
            print('  ... (+%d bab lainnya)' % (len(ch0) - 20))
        spec = ask_text(
            '\nPilih bab: [enter = SEMUA | contoh: 1,3,5 | 2-10 | '
            'latest:3 | batal]\n> ')
        if spec.strip().lower() in ('batal', 'cancel', 'x'):
            print('Dibatalkan.')
            return 0
        chapter_spec = spec

    # deskripsi seleksi bab (dari seri pertama, untuk ringkasan konfirmasi)
    d0 = load_json_file(targets[0]['path']) or {}
    _, filter_desc = select_chapters(d0.get('chapters') or [], chapter_spec)

    # --- konfirmasi ---
    force = ('--force' in sys.argv
             or os.environ.get('SCRAPE_FORCE', '').strip() not in ('', '0'))
    if not force:
        summary = '\n'.join('  - %s (%d bab)' % (t['title'], t['chapters'])
                            for t in targets)
        print('Akan disegarkan ulang gambarnya [%s]:\n%s'
              % (filter_desc, summary))
        if not ask_yes_no('\nLanjutkan? [y/tidak]: '):
            print('Dibatalkan.')
            return 0

    return _refresh_execute(targets, chapter_spec, adapter=adapter)
def _refresh_execute(targets, chapter_spec='', adapter=None):
    """Eksekusi penyegaran gambar untuk daftar target seri (dipakai
    oleh refresh_series_images). Hanya bab TERPILIH (chapter_spec) yang
    ber-`external` di-fetch ulang; `images` ditimpa URL terbaru; bab yang gagal
    mempertahankan URL lama (tidak dihapus).

    Bila `adapter` None (mis. dari scraper.py multi-sumber), adaptor dipilih
    per-seri lewat source_url (lihat adapter_for_url)."""
    t_all = time.time()
    grand_fetched = grand_failed = 0
    for m in targets:
        t_series = time.time()
        print('[refresh] %s (%d bab) [%s]'
              % (m.get('title') or m.get('slug'), m.get('chapters') or 0, ts()))
        data = load_json_file(m['path']) or {}
        a = adapter or adapter_for_url(data.get('source_url') or '')
        if a is None:
            print('   ! seri %s tidak dikenal sumbernya; dilewati.'
                  % (m.get('slug') or m.get('title')))
            continue
        chs = data.get('chapters') or []
        sel, desc = select_chapters(chs, chapter_spec)
        before = sum(1 for c in sel if c.get('images'))
        fetched = failed = skipped = 0
        consec = 0
        for c in sel:
            ext = (c.get('external') or '').strip()
            if not ext:
                continue
            if MAX_IMAGE_CHAPTERS and fetched >= MAX_IMAGE_CHAPTERS:
                skipped += 1
                continue
            t1 = time.time()
            try:
                res = a.refresh_chapter(data, c)
                if not res:
                    failed += 1
                    print('   ! tidak ada gambar baru dari sumber untuk %s '
                          '(URL lama dipertahankan).'
                          % (chapter_label(c) or ext))
                else:
                    imgs = res[0] or []
                    cdate = res[1] if len(res) > 1 else None
                    if imgs:
                        c['images'] = imgs
                        if not c.get('date') and cdate:
                            c['date'] = cdate
                        fetched += 1
                        consec = 0
                        logv('  [refresh] %s: %d gambar baru (%s)'
                             % (chapter_label(c), len(imgs), secs(t1)))
                    else:
                        failed += 1
                        print('   ! gambar tak ditemukan untuk %s (URL lama '
                              'dipertahankan).' % ext)
            except Exception as ex:
                consec += 1
                failed += 1
                print('   ! bab `%s` gagal disegarkan, URL lama dipertahankan: %s'
                      % (c.get('slug') or c.get('title'), ex))
                consec = pause_on_failures(consec)
            polite_delay()
        with open(m['path'], 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        after = sum(1 for c in sel if c.get('images'))
        cap_note = ' (%d dilewati MAX_IMAGE_CHAPTERS)' % skipped if skipped else ''
        print('-> %s: bab terpilih %d/%d [%s]; disegarkan %d, gagal %d, '
              'ber-gambar %d -> %d%s (%s)'
              % (m['title'], len(sel), len(chs), desc, fetched, failed,
                 before, after, cap_note, secs(t_series)))
        grand_fetched += fetched
        grand_failed += failed
    print('\nselesai: %d seri diproses, %d bab disegarkan, %d gagal (total %s).'
          % (len(targets), grand_fetched, grand_failed, secs(t_all)))
    print('Jalankan `python build.py` agar halaman bab memakai URL '
          'gambar terbaru.')
    return 0
# ------------------------------------------------------------------ jalankan


def run(adapter, auto_build=None):
    """Jalankan pipeline scraper penuh untuk SATU adaptor sumber.

    Membaca site-content/sources.json, site-content/manual-batch.txt, dan
    `--seed-sitemap`/env SCRAPE_SITEMAP_URL, lalu hanya memproses entri milik
    `adapter`. Pagination + resume checkpoint, paralel bab/seri, dedupe, dan
    auto-build semuanya ditangani mesin ini.

    `auto_build` (opsional) mengesampingkan keputusan auto-build per run:
      True  -> selalu jalankan build di akhir run;
      False -> tidak pernah (dipakai scraper.py agar build hanya 1x per
               run multi-sumber, yaitu di adaptor terakhir);
      None  -> ikuti env SCRAPE_AUTO_BUILD (auto bila ada seri baru).
    """
    if adapter is None:
        print('   ! run() butuh adaptor sumber; lewati.')
        return 1
    want_images = ('--images' in sys.argv
                   or os.environ.get('SCRAPE_IMAGES', '').strip()
                   not in ('', '0'))
    want_dates = (not want_images
                  and ('--dates' in sys.argv
                       or os.environ.get('SCRAPE_DATES', '').strip()
                       not in ('', '0')))
    # MODE UPDATE-ONLY (SCRAPE_UPDATE_ONLY=1, 'true', 'yes', 'on'):
    # Hanya memeriksa & memperbarui seri yang SUDAH ADA di katalog
    # (site-content/series/*.json). Tidak scan halaman daftar / sitemap,
    # tidak menambah seri baru, tidak import manual-batch.
    update_only = os.environ.get('SCRAPE_UPDATE_ONLY', '').strip().lower() \
        in ('1', 'true', 'yes', 'on')
    # MODE SMART-UPDATE (SCRAPE_SMART_UPDATE=1, 'true', 'yes', 'on'):
    # 1) scan halaman daftar "terbaru" dari sources.json (mis. komik-terbaru);
    # 2) filter HANYA seri yang SUDAH ADA di katalog; 3) scrape cuma seri itu.

    smart_update = os.environ.get('SCRAPE_SMART_UPDATE', '').strip().lower() \
        in ('1', 'true', 'yes', 'on')
    # Seed dari sitemap (opsional): import daftar seri LENGKAP dari sitemap
    # (mis. https://komikindo.ch/sitemap.xml). Argumen `--seed-sitemap <url>`
    # ATAU env SCRAPE_SITEMAP_URL (beberapa URL dipisah koma). Entri sitemap
    # ditaruh PALING DEPAN sehingga BATCH_LIMIT/MAX_NEW_SERIES berlaku untuk
    # seri dari sitemap lebih dulu; pagination daftar dimatikan pada mode ini.
    # MODE UPDATE-ONLY: daftar entri diambil dari SERI YANG SUDAH ADA di
    # katalog (site-content/series/*.json) milik adaptor ini. Sumber dari
    # sources.json (listing halaman) / sitemap / manual-batch TIDAK dipakai,
    # sehingga tidak ada seri baru yang ditambahkan & tidak ada scan halaman
    # daftar. Setiap seri di-scrape ulang dari halaman serinya (source_url).
    if update_only:
        seri_existing = list_series_files()
        entries = []
        for _p in seri_existing:
            d = load_json_file(_p) or {}
            u = (d.get('source_url') or '').strip()
            if not u or not adapter.match_url(u):
                continue
            entries.append({'url': u, 'slug': d.get('slug'),
                            'title': d.get('title'),
                            'auto_title': True})
        seed_sitemap = ''
        print('[scraper] MODE UPDATE-ONLY: %d seri tersimpan milik %s akan '
              'diperbarui (tanpa scan daftar / seri baru).'
              % (len(entries), adapter.name))
        if not entries:
            print('   ! belum ada seri tersimpan milik %s; jalankan scrape '
                  'penuh dulu (tanpa SCRAPE_UPDATE_ONLY).' % adapter.name)
            return 0
    elif smart_update:
        # MODE SMART-UPDATE: scan halaman daftar "terbaru" (mis. komik-terbaru)
        # dari sources.json, lalu HANYA memproses seri yang SUDAH ADA di katalog.

        seed_sitemap = ''
        entries = []
        src_data = load_json_file(SRC) or []
        for e in src_data:
            u = (e.get('url') or '').strip()
            if not u or not adapter.match_url(u):
                continue
            entries.append(e)
        print('[scraper] MODE SMART-UPDATE: scan halaman daftar terbaru, '
              'lalu update seri yang sudah ada di katalog (tanpa seri baru.)')
        if not entries:
            print('   ! tidak ada sumber daftar terbaru milik %s di sources.json '
                  '(dilewati).' % adapter.name)
            return 0
    else:
        seed_sitemap = os.environ.get('SCRAPE_SITEMAP_URL', '').strip()
        entries = []
        if seed_sitemap:
            for u in [x.strip() for x in seed_sitemap.split(',') if x.strip()]:
                try:
                    entries += adapter.sitemap_series_entries(u)
                except Exception as ex:
                    print('   ! gagal baca sitemap %s: %s' % (u, ex))
            if not entries:
                print('   ! sitemap tidak menghasilkan seri; lanjut ke sumber lain.')
        src_data = load_json_file(SRC)
        has_sources = bool(src_data)
        if src_data is not None:
            my_src = 0
            for e in src_data:
                if adapter.matches(e):
                    entries.append(e)
                    my_src += 1
            if my_src:
                print('[scraper] %d entri sumber milik %s diproses.' %
                      (my_src, adapter.name))
            elif has_sources:
                print('[scraper] tidak ada entri milik %s di sources.json '
                      '(dilewati).' % adapter.name)
        manual_txt = os.path.join(ROOT, 'site-content', 'manual-batch.txt')
        if os.path.exists(manual_txt):
            with open(manual_txt, encoding='utf-8-sig') as fh:
                for line in fh:
                    u = url_to_entry(line)
                    if u and adapter.match_url(u.get('url') or ''):
                        entries.append(u)
                    elif u:
                        print('   ! URL manual %s bukan milik %s; dilewati.'
                              % (u.get('url'), adapter.name))
    # Terminal interaktif & sumber kosong: tawarkan menempel URL langsung.
    if not entries and interactive():
        u = ask_text(
            'Tidak ada sumber milik %s. Tempel URL halaman daftar / seri '
            '(kosongkan untuk keluar):\n> ' % adapter.name)
        if u:
            if adapter.match_url(u):
                e0 = url_to_entry(u)
                if e0:
                    entries.append(e0)
            else:
                print('   ! URL %s bukan milik %s; diabaikan.' % (u, adapter.name))
        print()
    if not entries:
        print('Tidak ada sumber milik %s (sources.json kosong untuk sumber ini '
              '& tanpa URL manual).' % adapter.name)
        print('Isi sources.json, atau tempel URL halaman seri / halaman daftar '
              'lewat form Action.')
        return 0

    if seed_sitemap:
        print('[scraper] sumber: %d entri dari sitemap (+sources.json)' %
              len(entries))
    # Bersihkan duplikat yang sudah terlanjur ada di katalog (mis. satu seri
    # dengan dua slug berbeda dari run lama) sebelum memproses sumber baru.
    cleanup_duplicate_series()

    # Expand halaman daftar -> daftar manga (scan sesuai batch nanti).
    expanded, seen_q = [], set()
    for e in entries:
        sub = adapter.expand_seed(e, want_images)
        for s in sub:
            u = s.get('url') or ''
            if u and u not in seen_q:
                seen_q.add(u)
                expanded.append(s)
    if not expanded:
        print('Tidak ada manga untuk diproses setelah scan.')
        return 0
    if smart_update:
        # Filter: HANYA seri yang SUDAH ADA di katalog (URL source_url sama).
        existing_urls = set()
        for _p in list_series_files():
            _d = load_json_file(_p) or {}
            _u = (_d.get('source_url') or '').rstrip('/')
            if _u:
                existing_urls.add(_u)
        before = len(expanded)
        expanded = [s for s in expanded
                    if (s.get('url') or '').rstrip('/') in existing_urls]
        print('[scraper] smart-update: %d seri di daftar terbaru, %d sudah ada '
              'di katalog → di-update.' % (before, len(expanded)))
        if not expanded:
            print('   ! tidak ada seri terbaru yang sudah ada di katalog; selesai.')
            return 0
    print('[scraper] seri siap diproses: %d' % len(expanded))

    limit = None
    b = os.environ.get('BATCH_LIMIT', '')
    if b.isdigit():
        limit = int(b)
    mode = ('gambar' if want_images else
            'tanggal' if want_dates else 'link')
    pm = os.environ.get('SCRAPE_MAX_PAGES', '')
    max_pages = int(pm) if pm.isdigit() else 200
    print('[scraper] %s | sumber: %s | mode: %s | delay: %.1fs | batch: %s '
          '| max_pages: %d | max_image_chapters: %d | max_new: %d | '
          'workers: %d | parallel_min: %d | series_workers: %d | '
          'auto_build: %s | resume: %s | verbose: %s'
          % (ts(), adapter.name, mode, DELAY, b or '-', max_pages,
             MAX_IMAGE_CHAPTERS, MAX_NEW_SERIES, SCRAPE_WORKERS,
             PARALLEL_MIN_CHAPTERS, SERIES_WORKERS,
             AUTO_BUILD_ENV or 'auto',
             'on' if SCRAPE_RESUME else 'off', VERBOSE))
    total_ch = 0      # jumlah bab yang ditulis pada run ini
    processed = 0     # jumlah seri yang diproses (termasuk halaman lanjutan)
    # --- Halaman daftar: seri diproses satu per satu (SERIES_WORKERS=1) atau ---
    # --- beberapa seri bersamaan (SERIES_WORKERS>1, paralel lintas-seri). ---
    new_count = 0     # jumlah seri BARU (belum pernah ada di katalog) yang ditulis
    done_url = set()  # URL seri yang sudah benar-benar diproses
    done_slug = set() # slug seri yang sudah diproses run ini (anti-duplikat)

    # Kunci bersama untuk status run (processed/total_ch/new_count/done_url/
    # done_slug) supaya aman saat beberapa thread memproses seri bersamaan.
    process_lock = threading.Lock()

    def _process_one(e):
        """Proses SATU entri seri: scrape -> dedupe -> tulis ke file.

        Kembalikan 'stop' bila batas BATCH_LIMIT/MAX_NEW_SERIES tercapai
        (pemanggil harus berhenti memproses entri berikutnya), False bila
        URL/slug sudah diproses (duplikat), selain itu True (berhasil/gagal).

        Aman dipanggil dari beberapa thread sekaligus (paralel lintas-seri)."""
        nonlocal processed, total_ch, new_count
        u = e.get('url') or ''
        with process_lock:
            if u in done_url:
                return False
            done_url.add(u)
            if limit and processed >= limit:
                print('   (batas BATCH_LIMIT=%d tercapai; sisa dilewati).'
                      % limit)
                return 'stop'
            if new_count >= MAX_NEW_SERIES:
                print('   (batas MAX_NEW_SERIES=%d seri BARU tercapai; sisa '
                      'dilewati).' % MAX_NEW_SERIES)
                return 'stop'
            processed += 1
            n_disp = processed
        print('[%d%s] %s [%s]'
              % (n_disp, '/%d' % limit if limit else '', u, ts()))
        try:
            t_series = time.time()
            m = adapter.scrape_series(e, want_images=want_images,
                                      want_dates=want_dates)
        except Exception as ex:
            print('   ! gagal: %s [%s]' % (ex, ts()))
            polite_delay()
            return True
        slug = slugify(m['slug']) or slugify(m.get('title') or '')
        slug = resolve_canonical_slug(slug)
        m['slug'] = slug
        existing = find_existing_series(m)
        is_new = existing is None
        if existing:
            eslug = (existing.get('slug') or slug).strip()
            if eslug != slug:
                print('  -> seri "%s" DETEKSI DUPLIKAT: sama dengan "%s"; '
                      'digabung ke seri yang sudah ada.'
                      % (slug, eslug))
                slug = eslug
                m['slug'] = slug
        # Dedupe slug + tulis file di dalam kunci: dua URL berbeda untuk seri
        # yang sama tidak boleh menulis file yang sama secara bersamaan.
        with process_lock:
            if slug in done_slug:
                print('  -> seri "%s" sudah diproses run ini; dilewati.'
                      % slug)
                return True
            done_slug.add(slug)
            n = write_series(slug, m)
            total_ch += n
            if is_new:
                new_count += 1
        logv('   -> seri %s (%s), %d bab (mode %s, %s)'
             % (slug,
                'BARU %d/%d' % (new_count, MAX_NEW_SERIES)
                if is_new else 'update',
                n, mode, secs(t_series)))
        polite_delay()
        return True

    def process_series(queue):
        """Proses satu daftar entri seri; hormati budget BATCH_LIMIT dan
        batas MAX_NEW_SERIES, dedupe berdasarkan URL.
        Memperbarui processed/total_ch/new_count/done_url/done_slug.
        Bila SERIES_WORKERS>1, beberapa seri diproses BERSAMAAN (paralel)."""
        if SERIES_WORKERS > 1 and len(queue) > 1:
            _process_series_parallel(queue)
        else:
            for e in queue:
                if _process_one(e) == 'stop':
                    break

    def _process_series_parallel(queue):
        """Versi paralel process_series: jadwalkan tiap seri ke pool berisi
        SERIES_WORKERS thread. Begitu sebuah job melaporkan 'stop' (batas
        batch/seri baru tercapai), job yang belum sempat jalan di-skip. Catatan:
        karena sifat paralel, MAX_NEW_SERIES bisa terlewati paling banyak
        SERIES_WORKERS-1 seri."""
        stop = [False]

        def _job(e):
            if stop[0]:
                return
            if _process_one(e) == 'stop':
                stop[0] = True

        with ThreadPoolExecutor(max_workers=SERIES_WORKERS) as ex:
            futures = [ex.submit(_job, e) for e in queue]
            for f in futures:
                f.result()

    process_series(expanded)
# --- Lanjut halaman: interaktif (atau SCRAPE_NEXT_URL untuk non-TTY) ---
    # Prioritas URL lanjutan: 1) env SCRAPE_NEXT_URL, 2) resume otomatis dari
    # state (bila run sebelumnya sudah pernah mengisi halaman), 3) prompt.
    # Mode --seed-sitemap mematikan pagination (sitemap sudah berisi semua seri).
    skip_paging = seed_sitemap or smart_update
    cont_url = ('' if skip_paging
                else os.environ.get('SCRAPE_NEXT_URL', '' .strip()))
    pages_done = set()
    n_pages = 0
    state = load_state() if SCRAPE_RESUME else {}
    if not cont_url and SCRAPE_RESUME and not skip_paging:
        for e in entries:
            u = e.get('url') or ''
            if not u or not adapter.is_listing_url(u):
                continue
            bk = listing_key(u)
            rec = (state.get('listings') or {}).get(bk) or {}
            pages_done.update(rec.get('pages_done') or [])
            np = (rec.get('next_page') or '').strip()
            if np and listing_key(np) == bk and np != u:
                cont_url = np
                print('\n[resume] run sebelumnya sudah pernah mengisi halaman; '
                      'lanjut otomatis ke halaman tersimpan: %s' % np)
                break
    if not cont_url and interactive() and not skip_paging:
        try:
            if ask_yes_no(
                    '\nSelesai scrape dari URL sumber. Lanjutkan ke halaman '
                    'berikutnya? [y/tidak]: '):
                cont_url = ask_text(
                    'Tempel URL halaman berikutnya (mis. '
                    'https://sumber/komik-terbaru/page/2/):\n> ')
        except KeyboardInterrupt:
            cont_url = ''
        print()

    # Halaman dasar (base) dari sources.json baru saja diproses di atas;
    # tandai & simpan posisinya di state resume (tanpa menyentuh next_page
    # yang sudah tersimpan dari run sebelumnya).
    if SCRAPE_RESUME and not skip_paging:
        for e in entries:
            u = e.get('url') or ''
            if u and adapter.is_listing_url(u):
                pages_done.add(u)
                record_resume_page(state, listing_key(u), u)

    while cont_url:
        if not cont_url.startswith('http'):
            print('   ! URL halaman tidak valid: %r; berhenti.' % cont_url)
            break
        if cont_url in pages_done:
            # Sudah pernah diproses (run sebelumnya); loncat ke berikutnya.
            print('-> Halaman %s sudah pernah diproses; loncat ke berikutnya.'
                  % cont_url)
            cont_url = next_page_after(cont_url)
            continue
        pages_done.add(cont_url)
        n_pages += 1
        if n_pages > max_pages:
            print('-> Mencapai batas %d halaman (SCRAPE_MAX_PAGES); berhenti '
                  '(posisi tersimpan, run berikutnya lanjut otomatis).'
                  % max_pages)
            break
        ck = listing_key(cont_url) if adapter.is_listing_url(cont_url) else ''
        print('\n--- Halaman %s ---' % cont_url)
        try:
            links = adapter.expand_seed({'url': cont_url, 'listing': True},
                                        want_images)
        except Exception as ex:
            print('   ! gagal buka halaman %s: %s' % (cont_url, ex))
            break
        polite_delay()
        if len(links) == 1 and (links[0].get('url') or '') == cont_url:
            # expand_seed tidak menemukan seri apa pun di halaman ini ->
            # akhir daftar tercapai; bersihkan next_page agar run berikutnya
            # mulai lagi dari base (tidak mengulang halaman kosong).
            print('-> Halaman %s tidak memuat seri; berhenti (akhir daftar).'
                  % cont_url)
            if SCRAPE_RESUME:
                clear_resume_next(state, ck)
            break
        fresh = [s for s in links if (s.get('url') or '') not in done_url]
        if not fresh:
            print('-> Halaman %s tidak punya seri baru; berhenti (akhir daftar).'
                  % cont_url)
            if SCRAPE_RESUME:
                clear_resume_next(state, ck)
            break
        process_series(fresh)
        nxt = next_page_after(cont_url)
        # Simpan URL halaman berikutnya: resume run berikutnya dari posisi ini.
        if SCRAPE_RESUME and adapter.is_listing_url(cont_url):
            record_resume_page(state, listing_key(cont_url), cont_url, nxt)
        if limit and processed >= limit:
            print('   (batas BATCH_LIMIT=%d tercapai; posisi disimpan untuk '
                  'lanjut run berikutnya).' % limit)
            break
        if new_count >= MAX_NEW_SERIES:
            print('-> Batas %d seri BARU tercapai; berhenti scan halaman '
                  'berikutnya (posisi disimpan untuk lanjut run berikutnya).'
                  % MAX_NEW_SERIES)
            break
        cont_url = nxt

    print('\nselesai (sumber %s, mode %s): %d seri diproses (+%d BARU), '
          '%d bab ditulis ke site-content/series/ (total %s)'
          % (adapter.name, mode, processed, new_count, total_ch, secs(T0)))

    # --- Auto-build: langsung bangun ulang situs statis setelah scrape ---
    # Default: otomatis bila run ini menambahkan seri BARU. Atur
    # SCRAPE_AUTO_BUILD=1 untuk selalu menjalankan, atau =0 untuk mematikan.
    # Parameter `auto_build` (dari pemanggil) menimpa keputusan ini; scraper.py
    # multi-sumber memakainya agar build hanya terjadi SEKALI di adaptor terakhir.
    if auto_build is None:
        ab = AUTO_BUILD_ENV
        if ab in ('0', 'false', 'no', 'off'):
            do_build = False
        elif ab in ('1', 'true', 'yes', 'on'):
            do_build = True
        else:
            do_build = new_count > 0
    elif auto_build is True:
        do_build = True
    else:
        do_build = False
    if do_build:
        rc = run_build()
        if rc:
            print('  [build] peringatan: `python build.py` keluar '
                  'dengan kode %s.' % rc)
    else:
        print('(build statis dilewati: SCRAPE_AUTO_BUILD=%r, seri baru=%d)'
              % (AUTO_BUILD_ENV or 'auto', new_count))
    return 0