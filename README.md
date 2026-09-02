# Mfmam — Panduan Perintah

Situs statis baca manhwa berbahasa Indonesia. Konten dikelola sebagai JSON di
folder `site-content/`, lalu **dibangun menjadi halaman HTML statis** oleh
`build.py`. Katalog diperbarui oleh scraper (bisa manual / terjadwal
via GitHub Actions), dan dipublikasikan ke Cloudflare Pages.

Sejak refactor, scraper dipecah **satu file per sumber** (`scraper_common.py`
berisi mesin bersama: network/retry, dedupe, state pagination, CLI), dan
`scraper.py` tinggal menjalankan ketiganya sekaligus:

- `scraper_komikindo.py` → situs HTML tema WordPress/Madara (mis. komikindo.ch)
- `scraper_mikoroku.py` → katalog JSON publik + feed Blogger (mikoroku.com)
- `scraper_doujindesu.py` → API terenkripsi doujin.desu.xxx

Semua perintah dijalankan dari **folder root repo ini**.

---

## Daftar Perintah Singkat

| Perintah | Fungsi |
|---|---|
| `python scraper.py` | Update katalog SEMUA sumber: judul bab + link sumber (mode LINK) |
| `python scraper_komikindo.py` | Hanya sumber KomikIndo (HTML WordPress/Madara) |
| `python scraper_mikoroku.py` | Hanya sumber Mikoroku (JSON + feed Blogger) |
| `python scraper_doujindesu.py` | Hanya sumber Doujindesu (API terenkripsi) |
| `python scraper.py --images` | Update + gambar bab + tanggal tiap bab (incremental) |
| `python scraper.py --dates` | Hanya tanggal tiap bab (opsional; umumnya tak perlu) |
| `python scraper.py --test` | Uji parser tanpa menyentuh jaringan (semua sumber) |
| `python scraper.py --seed-sitemap <url>` | Import seluruh daftar seri dari sitemap, lalu scrape |
| `python scraper.py --delete` | Hapus seri (interaktif: pilih) |
| `python scraper.py --delete all` | Hapus SEMUA seri (ada konfirmasi) |
| `python scraper.py --delete <slug>...` | Hapus seri tertentu (non-interaktif) |
| `python scraper.py --refresh-images` | Perbaiki URL gambar bab yg rusak/berubah (interaktif: pilih seri) |
| `python scraper.py --refresh-images <slug>...` | Perbaiki URL gambar bab utk seri tertentu (non-interaktif) |
| `python scraper.py --refresh-images <slug> --chapters <spec>` | Hanya bab tertentu (contoh: `1,3,5` / `2-10` / `latest:3`) |
| `python build.py` | Bangun halaman statis dari `site-content/` ke `dist/`; data katalog disiapkan di `dist-data/` (untuk R2) |
| `python _restore_data.py` | Ambil data katalog dari situs yang live (incremental, tanpa git) |
| `python _upload_r2.py` | Upload `dist-data/data/*.json` ke bucket R2 `mfmam-data` (paralel, jalankan ulang untuk resume) |
| `python _upload_r2.py --resume` | Lanjutkan upload dari log (lewati yang sudah OK) |
| `npx wrangler pages deploy dist --project-name=mfmam` | Deploy `dist/` + Pages Function + binding R2 ke Cloudflare Pages |
| `python -m http.server 8000` | Pratinjau situs hasil build secara lokal |

> `scraper.py` (orkestrator) meneruskan perintah yang sama ke KETIGA scraper
> secara berurutan. Setiap `scraper_*.py` juga bisa dijalankan sendiri — berguna
> untuk meng-scrape satu sumber saja (mis. hanya komikindo) tanpa menyentuh
> sumber lain.

---

## 1. Scraper Katalog

Membaca daftar sumber lalu menulis hasil ke `site-content/series/<slug>.json`.
Skema satu seri:
```json
{
  "id": "kt79zj",
  "slug": "eleceed",
  "title": "Eleceed",
  "desc": "...",
  "genres": ["Action", "Comedy"],
  "cover_url": "https://...",
  "status": "Berjalan",
  "author": "...",
  "last_updated": "2026-09-02",
  "chapters": [ { "slug": "kt79zj-chapter-1", "title": "Chapter 1", "external": "https://...", "images": [], "date": "2026-01-01" } ]
}
```

### Pembagian file (refactor per-sumber)

| File | Isi |
|---|---|
| `scraper_common.py` | Mesin bersama: fetch/retry, jeda & circuit-breaker, slug & dedupe, tulis seri, state pagination (`scrape-state.json`), CLI (`--delete`, `--refresh-images`, `--seed-sitemap`, auto-build), kontrak `SourceAdapter` + registry adaptor |
| `scraper_komikindo.py` | Adaptor #1 — halaman seri/daftar HTML tema WordPress/Madara, gambar bab dari `<div id="chimg-..">`, sitemap (mis. komikindo.ch) |
| `scraper_mikoroku.py` | Adaptor #2 — katalog `kind:"json"`, daftar bab & gambar dari feed Blogger (`mikodrive.my.id`) |
| `scraper_doujindesu.py` | Adaptor #3 — klien API terenkripsi doujin.desu.xxx (`_enc_resp_`, salt, device-id) |
| `scraper.py` | Orkestrator: mengimpor & menjalankan ketiga adaptor di atas secara berurutan |

Tiap adaptor menyeleksi entri miliknya (`matches()`), sehingga `sources.json`
boleh berisi campuran ketiga sumber sekaligus; `scraper.py` memproses semuanya,
sedangkan `scraper_*.py` hanya sumbernya sendiri.

### Sumber yang di-scrape
Prioritas:
1. **`site-content/sources.json`** — daftar URL halaman seri **atau** halaman
   daftar (listing, mis. `https://situssumber/komik/`). URL listing otomatis
   di-scan menjadi daftar manga.
2. **`site-content/manual-batch.txt`** — satu URL per baris (dibuat otomatis oleh
   GitHub Actions saat manual run; dihapus setelah run).

Contoh `sources.json`:
```json
[
  { "url": "https://komikindo.ch/komik/" },
  { "url": "https://komikindo.ch/komik/eleceed/", "title": "Eleceed" },
  {
    "url": "https://raw.githubusercontent.com/moemaomao/mymangadata/main/all-manga.json",
    "kind": "json",
    "detail_prefix": "https://mikoroku.com/detail?slug="
  }
]
```

#### Sumber berkas JSON katalog (`kind: "json"`)
Beberapa situs SPA (mis. **mikoroku.com**) memuat daftar serinya dari **berkas
JSON publik** (sering disimpan di GitHub raw), bukan halaman HTML. Untuk
impornya, tambahkan entri dengan `"kind": "json"`:

- `url` → berkas JSON (array seri, atau objek berisi key `items`/`data`/`list`/`manga`/`series`).
- `detail_prefix` (opsional) → membuat URL referensi detail tiap seri
  (mis. `https://mikoroku.com/detail?slug=<slUG>`).
- Tiap item JSON: `title` (wajib), optional `slug`, `desc`, `genres`, `img`/`cover`,
  `status`, `type`, `author`, `artist`, `altTitle`, `chapters[]`.

Scraper membangun metadata seri **langsung dari JSON tanpa fetch HTML**
(halaman detail sumbernya SPA yang tidak punya konten server-side), lalu
menulisnya ke `site-content/series/`. `BATCH_LIMIT`/`SCRAPE_MAX_NEW` tetap
berlaku. Untuk mikoroku.com, katalog publiknya ada di repo GitHub
`moemaomao/mymangadata` (`all-manga.json`, saat ini **81 seri**).

##### Daftar bab mikoroku (feed Blogger)
Selain metadata, mikoroku menyimpan **daftar bab + gambar bab** di feed Blogger
(`https://www.mikodrive.my.id/feeds/posts/default`), tempat tiap bab jadi satu
artikel berlabel judul seri dengan konten berisi URL gambar `data-original`.
Aktifkan di entri JSON dengan:

```json
{
  "url": "https://raw.githubusercontent.com/moemaomao/mymangadata/main/all-manga.json",
  "kind": "json",
  "detail_prefix": "https://mikoroku.com/detail?slug=",
  "reader_prefix": "https://mikoroku.com/reader?slug=",
  "blogger_feed": "https://www.mikodrive.my.id"
}
```

Dengan `blogger_feed` terisi, scraper memanggil `fetch_blogger_chapters()` per
seri untuk mengisi daftar bab (judul, nomor, tanggal, `external` = link reader,
dan **gambar** bila mode `--images`). Catatan: kelengkapan daftar bab bergantung
pada hasil pencarian feed Blogger (`?q=`), jadi bila feed lambat/tidak lengkap
di suatu jaringan (mis. perlu VPN), run di **CI/GitHub Actions** umumnya lebih
stabil. Feed yang gagal/kosong tidak mematikan proses (dilewati).

> ℹ️ **doujindesu.xxx** — situs SPA dengan **API terenkripsi** (`_enc_resp_`).
> Adaptornya (`scraper_doujindesu.py`) membaca metadata & daftar bab dari
> `/api/manga/<slug>` dan gambar dari `/api/chapters/<id>`. Sertifikat SSL
> situs ini **cert mismatch** di sebagian jaringan/ISP → aktifkan
> `SCRAPE_INSECURE_SSL=1` (percaya sumber Anda) agar request tidak gagal
> verifikasi. URL gambar bab bertanda tangan & basi ~24 jam, jadi di mode
> `--images` gambar diambil saat itu juga dan tidak dipertahankan antarrun.
> Catatan: bila API situs sedang kosong/sulit diakses (mis. diblokir
> challenge), run akan melewati seri doujindesu tanpa mematikan sumber lain.

### Mode & contoh pemakaian
```powershell
python scraper.py             # mode LINK: judul bab + link sumber (default)
python scraper.py --images    # + ambil gambar bab + tanggal tiap bab
python scraper.py --dates     # (opsional) hanya tanggal tiap bab, tanpa gambar
python scraper.py --test      # uji parser dengan HTML contoh, tanpa network
```

> **Catatan**: `--images` sudah mengisi **gambar + tanggal tiap bab** otomatis,
> jadi umumnya tidak perlu `--dates`. Mode `--dates` disediakan bila Anda hanya
> ingin tanggal (tanpa gambar). Gambar & tanggal bersifat **incremental** — bab
> yang sudah punya data tidak di-fetch ulang, jadi run rutin cepat.

### Batas seri baru & build otomatis
Scraper **berhenti otomatis setelah menambahkan 200 seri BARU** (seri yang
belum pernah ada di katalog) dalam sekali run. Proteksi ini mencegah run tidak
sengaja membanjiri katalog ratusan seri sekaligus. Nilai batas bisa diubah
dengan env `SCRAPE_MAX_NEW`:

```powershell
$env:SCRAPE_MAX_NEW="300"
python scraper.py --images
```

Setelah run selesai, bila ada seri baru yang ditambahkan, scraper **langsung
menjalankan `python build.py`** secara otomatis agar halaman statis
ikut ter-update:

| Variabel | Fungsi | Default |
|---|---|---|
| `SCRAPE_MAX_NEW` | Berhenti setelah N seri BARU ditambahkan | `200` |
| `SCRAPE_AUTO_BUILD` | `0`=jangan build; `1`=selalu build; kosong=otomatis bila ada seri baru | otomatis |
| `SCRAPE_RESUME` | `0`=matikan resume pagination (jangan simpan/gunakan posisi halaman berikutnya) | aktif |
| `SCRAPE_NEXT_URL` | URL halaman lanjutan untuk run non-interaktif (menindih resume) | tidak ada |
| `SCRAPE_INSECURE_SSL` | `1`=bypass verifikasi SSL (untuk sumber bernama `doujin*`/`mikoroku` dsb. yang cert-nya bermasalah) | mati |

```powershell
$env:SCRAPE_AUTO_BUILD="0"   # matikan build otomatis
python scraper.py --images
```

### Perbaiki link gambar yang mati/berubah — `--refresh-images`
Karena sifatnya incremental, bab yang **sudah punya `images` tidak pernah
di-fetch ulang** oleh `--images`. Bila URL CDN/mirror gambar berubah atau mati,
gunakan mode ini untuk mencari ulang dan **menimpa** URL gambar tiap bab dari
halaman sumbernya (`external`):

```powershell
python scraper.py --refresh-images                    # interaktif: pilih seri & bab
python scraper.py --refresh-images all                # SEMUA seri (ada konfirmasi)
python scraper.py --refresh-images <slug>...          # seri tertentu, mis. one-piece-ace-story
python scraper.py --refresh-images <slug> --force     # tanpa konfirmasi (bisa juga SCRAPE_FORCE=1)
python scraper.py --refresh-images <slug> --chapters 1,3,5   # hanya bab 1, 3, 5
python scraper.py --refresh-images <slug> --chapters 2-10    # rentang bab 2 s.d. 10
python scraper.py --refresh-images <slug> --chapters latest:3  # 3 bab terbaru
```

Format `--chapters`: `all`/kosong = semua; nomor pisah koma (`1,3,5`); rentang
(`2-10` / `2..10`); `latest:N` / `terbaru:N` = N bab terbaru; atau potongan
judul/slug bab. Saat dijalankan interaktif, tersedia daftar bab untuk dipilih.

Bab yang gagal dimuat **mempertahankan URL lama** (tidak dihapus). Batasi jumlah
bab per run dengan env `MAX_IMAGE_CHAPTERS`. Setelah selesai, jalankan
`python build.py` agar halaman bab memakai URL gambar terbaru.

### Log & verbose
Scraper kini menampilkan log yang lebih detail: timestamp per event, status/
ukuran/durasi tiap request (`[fetch]`), kemajuan per bab (`[gambar]`,
`[tanggal]`, `[refresh]`), metadata seri yang terparsing (`[seri]`), dan
rincian penulisan file (`[write]`). Set `SCRAPE_VERBOSE=0` bila hanya ingin
ringkasan (log per-request/per-bab disembunyikan, ringkasan tetap tampil):

```powershell
$env:SCRAPE_VERBOSE="0"
python scraper.py --images
```

### Anti-duplikat seri
Scraper menurunkan **slug kanonik dari URL halaman seri** (bukan dari tebakan
entry) dan menyimpan `source_url` di `site-content/series/<slug>.json`. Slug
dari URL **bab** juga dinormalisasi ke slug seri (`one-piece-ace-story-chapter-4`
→ `one-piece-ace-story`). Saat sebuah URL sumber menunjuk seri yang **sudah
ada**, hasil scrape **digabung ke seri yang sudah ada** — tidak membuat entri
duplikat. Dua URL berbeda untuk seri yang sama (mis. halaman daftar vs URL bab)
juga dipakai sekali: baris `DETEKSI DUPLIKAT` muncul di log.

Selain pencocokan slug, seri juga dianggap **sama** bila:
- `source_url` sama, atau
- **overlap URL bab ≥ 2**, atau
- judul ternormalisasi sama **dan** ada ≥ 1 bab yang sama.

Di awal tiap run, scraper otomatis menjalankan **pembersihan duplikat** yang
sudah terlanjur ada di katalog (`cleanup_duplicate_series`): dua file seri yang
ternyata sama digabung (bab terbanyak sebagai file utama), file duplikat
dihapus, dan log `[dedup]` tercetak.

### Skip bab yang sudah benar — kecuali URL gambar salah
Mode `--images` bersifat **incremental**: bab yang sudah punya gambar **benar**
dilewati (tidak di-fetch ulang). Namun bab yang URL gambarnya **salah/rusak**
(berisi favicon/logo/thumbnail, ekstensi salah, atau spasi mentah) otomatis
**di-fetch ulang** sampai mendapat URL yang benar. Jadi perbaikan URL berlaku
otomatis pada run rutin, tanpa perlu mode khusus.

### Paralel untuk seri besar — `SCRAPE_WORKERS` & `SCRAPE_PARALLEL_MIN`
Scraper otomatis memproses bab secara **paralel** (log `MODE PARALEL`) saat
jumlah bab yang perlu diambil/diisi melebihi ambang `PARALLEL_MIN_CHAPTERS`
(default `50`). Kedua nilai diatur lewat variabel lingkungan:

| Variabel | Fungsi | Default |
|---|---|---|
| `SCRAPE_WORKERS` | Jumlah worker (request yang berjalan bersamaan) | `4` |
| `SCRAPE_PARALLEL_MIN` | Ambang jumlah bab untuk mengaktifkan mode paralel | `50` |
| `SCRAPE_DELAY` | Jeda dasar antar request (detik, dengan jitter) | `3.0` |

```powershell
# 6 request paralel, mode paralel mulai dari >30 bab yang butuh proses
$env:SCRAPE_WORKERS = "6"
$env:SCRAPE_PARALLEL_MIN = "30"
$env:SCRAPE_DELAY = "1.5"        # lebih agresif (sesuaikan agar tidak diblokir)
python scraper.py --images
```

Untuk **cmd** (bukan PowerShell): `set SCRAPE_WORKERS=6` lalu `python scraper.py --images`.

Catatan:
- Mode paralel hanya aktif bila **`SCRAPE_WORKERS > 1`**.
- `SCRAPE_WORKERS=1` memaksa proses **berurutan** (paling aman terhadap rate-limit).
- Header log run menampilkan nilai aktif: `workers: N | parallel_min: M | series_workers: N`.

### Beberapa seri sekaligus — paralel lintas-seri (`SCRAPE_SERIES_WORKERS`)

`SCRAPE_WORKERS` di atas hanya mempercepat bab **dalam satu seri**. Secara
default seri itu sendiri diproses **satu per satu**. Untuk memproses **beberapa
seri secara bersamaan** dalam satu run, atur `SCRAPE_SERIES_WORKERS` (>1):

| Variabel | Fungsi | Default |
|---|---|---|
| `SCRAPE_SERIES_WORKERS` | Jumlah seri yang di-scrape bersamaan (paralel lintas-seri) | `1` (serial) |

```powershell
# 3 seri di-scrape bareng, tiap seri tetap 4 worker bab (jika bab > 50)
$env:SCRAPE_SERIES_WORKERS = "3"
python scraper.py --images
```

Catatan:
- Berlaku untuk semua sumber (sources.json, manual-batch, dan halaman lanjutan).
- Parallel berarti ritme request ke sumber juga naik ≈ N×; pakai nilai kecil
  (mis. `2`–`3`) dan perhatikan rate-limit/`SCRAPE_DELAY`.
- Perhatian: `MAX_NEW_SERIES` bisa terlewati paling banyak `SCRAPE_SERIES_WORKERS-1`
  karena seri yang sudah berjalan tidak dihentikan di tengah.
- Bisa dikombinasikan dengan `SCRAPE_WORKERS` (paralel antar-bab).

### Lanjut halaman otomatis (pagination interaktif)
Saat run selesai dan terminal interaktif, scraper menawarkan lanjut ke halaman
berikutnya:

```
Selesai scrape dari URL sumber. Lanjutkan ke halaman berikutnya? [y/tidak]: y
Tempel URL halaman berikutnya (mis. https://sumber/komik-terbaru/page/2/):
> https://komikindo.ch/komik-terbaru/page/2/
```

Setelah URL ber-halaman dimasukkan, scraper **otomatis menelusuri** `page/3`,
`page/4`, ... sampai menemukan halaman yang **tidak memuat seri** (atau hanya
seri duplikat dari halaman sebelumnya). Path daftar seperti `komik-terbaru`,
`manga-terbaru`, `komik-update`, dll. dikenali sebagai halaman daftar.

Di CI (non-interaktif) prompt tidak muncul — gunakan `SCRAPE_NEXT_URL`.

#### Resume otomatis — simpan URL halaman berikutnya (+checkpoint)

Setiap kali satu halaman daftar selesai diproses, scraper **menyimpan URL
halaman berikutnya** ke `site-content/scrape-state.json`:

```json
{
  "listings": {
    "https://komikindo.ch/komik-terbaru/": {
      "next_page": "https://komikindo.ch/komik-terbaru/page/3/",
      "pages_done": [
        "https://komikindo.ch/komik-terbaru/",
        "https://komikindo.ch/komik-terbaru/page/2/"
      ],
      "last_scan": "2026-09-01T04:35:36"
    }
  }
}
```

Pada run berikutnya, bila run sebelumnya **sudah pernah mengisi** halaman
tersebut, scraper otomatis **melanjutkan dari URL tersimpan** — tanpa perlu
prompt ulang atau `SCRAPE_NEXT_URL`. Halaman yang sudah pernah diproses
(`pages_done`) tidak diulang, dan posisi diperbarui setelah setiap halaman
sehingga run yang terpotong (jaringan drop, `BATCH_LIMIT`, `SCRAPE_MAX_PAGES`,
atau batas seri baru) bisa dilanjutkan di batch berikutnya.

Ketika mencapai akhir daftar (halaman tidak memuat seri / seri baru),
`next_page` dikosongkan sehingga run berikutnya kembali memulai dari halaman
pertama (base dari `sources.json`). Matikan fitur ini dengan `SCRAPE_RESUME=0`:

### Bootstrap katalog dari sitemap — `--seed-sitemap`

Untuk mengimpor **seluruh daftar seri** sekaligus (mis. 9.173 seri komikindo.ch)
tanpa menelusuri halaman daftar satu per satu, gunakan sitemap:

```powershell
python scraper.py --seed-sitemap https://komikindo.ch/sitemap.xml
# atau via env (beberapa URL dipisah koma)
$env:SCRAPE_SITEMAP_URL = "https://komikindo.ch/sitemap.xml"
python scraper.py --images
```

Cara kerjanya:
- Sitemap index (`sitemap.xml`) diikuti ke sub-sitemap-nya (sub-sitemap bertema
  `manga/series/komik/manhwa` diutamakan), lalu hanya **URL halaman seri**
  (`…/komik/<slug>/`) yang diambil — URL bab/genre/artikel dibuang.
- Entri sitemap ditaruh **paling depan** antrean, jadi `BATCH_LIMIT`
  (form manual) dan `SCRAPE_MAX_NEW` berlaku untuk seri dari sitemap dulu.
- Pagination daftar **dimatikan** pada mode ini. Duplikat vs `sources.json`
  otomatis dilewati (dedupe berdasarkan URL).
- Di form GitHub Action tersedia input **`sitemap_url`** untuk menjalankan mode
  ini tanpa harus memegang terminal.

### Hapus seri — `--delete`

Menghapus seri dari katalog (`site-content/series/<slug>.json`) **beserta
artefak hasil build-nya** (halaman detail `manga/<slug>/` dan folder bab
`<id>-...` di root). Ada 3 mode:

```powershell
python scraper.py --delete                # interaktif: pilih seri (all = semua)
python scraper.py --delete all            # hapus SEMUA seri
python scraper.py --delete eleceed        # hapus seri tertentu (boleh beberapa slug)
```

```powershell
# lewati konfirmasi (untuk otomatisasi)
python scraper.py --delete all --force
# atau via env
$env:SCRAPE_FORCE = "1"; python scraper.py --delete eleceed
```

Setiap mode meminta konfirmasi ya/tidak kecuali diberi `--force` / `SCRAPE_FORCE=1`.
Setelah dihapus, jalankan `python build.py` untuk membersihkan sisa
halaman / index yang sudah tidak terpakai (search.json, sitemap, beranda).

### Variabel lingkungan (opsional)
| Variabel | Fungsi | Contoh |
|---|---|---|
| `SCRAPE_IMAGES` | Aktifkan mode gambar (sama dgn `--images`) | `$env:SCRAPE_IMAGES="1"` |
| `SCRAPE_DATES` | Aktifkan mode tanggal | `$env:SCRAPE_DATES="1"` |
| `SCRAPE_DELAY` | Jeda antar request (detik, default 3.0; ada jitter acak) | `$env:SCRAPE_DELAY="5"` |
| `BATCH_LIMIT` | Batas jumlah manga yang diproses per run | `$env:BATCH_LIMIT="10"` |
| `MAX_IMAGE_CHAPTERS` | Batas bab yang diambil gambarnya per run (0=tanpa batas) | `$env:MAX_IMAGE_CHAPTERS="50"` |
| `MAX_CHAPTER_DATES` | Batas bab yang diisi tanggalnya per run | `$env:MAX_CHAPTER_DATES="50"` |
| `SCRAPE_NEXT_URL` | URL halaman awal untuk dilanjutkan otomatis (tanpa prompt) | `$env:SCRAPE_NEXT_URL="https://.../page/2/"` |
| `SCRAPE_MAX_PAGES` | Batas aman jumlah halaman yang ditelusuri (default 200) | `$env:SCRAPE_MAX_PAGES="50"` |
| `SCRAPE_FORCE` | Lewati konfirmasi saat `--delete` | `$env:SCRAPE_FORCE="1"` |

Contoh memakai env di PowerShell:
```powershell
$env:SCRAPE_IMAGES = "1"; $env:BATCH_LIMIT = "20"; python scraper.py
```

### Output
Tiap seri ditulis ke `site-content/series/<slug>.json`:
```json
{
  "slug": "eleceed", "title": "Eleceed", "desc": "...", "genres": ["Action"],
  "status": "Ongoing", "cover_url": "...", "last_updated": "2026-08-28",
  "chapters": [
    { "slug": "ab12cd-chapter-1", "title": "Eleceed Chapter 1",
      "num": 1, "external": "https://...", "date": "2026-01-05", "images": ["https://cdn/..."] }
  ]
}
```
---

## 2. Build Situs Statis — `build.py`

Membaca semua JSON di `site-content/`, lalu **menulis halaman statis ke `dist/`**
(folder ter-*gitignore*):

```
python build.py
```

Yang dihasilkan:
- `index.html` — beranda (daftar manga, urut update terbaru)
- `daftar-komik/index.html` — daftar manhwa + kolom pencarian bab
- `manga/<slug>/index.html` — halaman detail seri (daftar bab **urutan baca**
  dari chapter 1, bukan dari terbaru)
- `data/<slug>.json` — data LENGKAP per seri (termasuk `images[]`), dibaca
  **reader dinamis** client-side (bab dibuka lewat `#bab/N`, TANPA membuat
  1 file per bab — inilah yang membuat jumlah file tetap < 20.000, muat di
  Cloudflare Pages Free)
- `genre/*/index.html`, `genre/index.html` — halaman genre
- `search.json`, `chapters-index.json`, `sitemap.xml`, `404.html`
- `data/manifest.json` — daftar slug (dipakai `_restore_data.py`)

> Perintah ini juga dijalankan otomatis oleh GitHub Actions setelah scrape
> (`scrape.yml`), lalu hasilnya di-deploy ke Cloudflare Pages via wrangler.

### Pratinjau hasil build secara lokal
```powershell
python build.py
python -m http.server 8000
# buka http://localhost:8000/
```

---

## 3. Deploy ke Cloudflare Pages (CLI / Actions)

Deploy `dist/` ke Cloudflare Pages pakai wrangler:

```powershell
npx wrangler pages deploy dist --project-name=mfmam
```

Agar bisa deploy, set dulu di repo (Settings → Secrets and variables):
- `CLOUDFLARE_API_TOKEN` — token API Cloudflare dengan izin **Pages:Edit**
- `CLOUDFLARE_ACCOUNT_ID` — ID akun Cloudflare

> Kredensial ini TIDAK perlu di commit; hanya dipakai oleh GitHub Actions.

---

## 4. Otomatisasi — GitHub Actions

Tidak perlu perintah manual; workflow sudah aktif di repo. Anda tetap bisa
menjalankannya manual dari tab **Actions**.

### `scrape.yml` — update katalog + deploy ke Cloudflare Pages (tiap 6 jam)
- **Jadwal**: cron `35 3,9,15,21 * * *` UTC (4×/hari).
- **Alur**: checkout (tanpa riwayat) → `python _restore_data.py` (ambil data
  katalog dari situs yang live, supaya scrape tetap incremental) → `python
  scraper.py` dengan `SCRAPE_IMAGES=1` → `python build.py` (tulis ke `dist/`)
  → `npx wrangler pages deploy dist`.
- **Data TIDAK di-commit ke git** — jadi repo tetap kecil & GitHub tidak
  membengkak meski katalog besar.
- **Persyaratan**: set Secrets `CLOUDFLARE_API_TOKEN` & `CLOUDFLARE_ACCOUNT_ID`;
  opsional `vars.SITE_URL` (default `https://mfmam.pages.dev`) dan
  `vars.PAGES_PROJECT` (default `mfmam`).
- **Jalankan manual**: *Actions → Scraper Otomatis Mfmam (Cloudflare Pages) →
  Run workflow*, lalu isi `urls` / `batch` / `sitemap_url`.

---

## 5. Admin Web — Decap CMS

Tanpa command line; cukup akses browser:

```
https://<domain-situs>/admin/
```

Login (git-gateway), lalu edit:
- **Pengaturan Situs** → `site-content/settings.json`
- **Halaman Info** → `site-content/pages/*.json`
- **Katalog Seri** → `site-content/series/*.json`

Setiap simpan = commit ke repo → workflow deploy otomatis.

---

## 7. Struktur Berkas Penting

```
.
├── scraper_common.py       # mesin bersama: fetch/retry, dedupe, state, CLI
├── scraper_komikindo.py    # scraper #1 — KomikIndo (HTML WordPress/Madara)
├── scraper_mikoroku.py     # scraper #2 — Mikoroku (katalog JSON + feed Blogger)
├── scraper_doujindesu.py   # scraper #3 — Doujindesu (API terenkripsi)
├── scraper.py              # orkestrator multi-sumber (jalankan ketiganya)
├── build.py                # bangun halaman statis dari site-content/ ke dist/
├── _restore_data.py        # ambil data katalog dari situs live (incremental)
├── site-content/
│   ├── settings.json       # nama situs & tagline
│   ├── sources.json        # daftar URL sumber untuk scraper
│   ├── pages/*.json        # halaman info (mis. kontak)
│   └── series/*.json       # katalog seri (dihasilkan scraper, TIDAK di-git)
├── admin/                  # Decap CMS (admin web)
└── .github/workflows/      # scrape.yml (scrape+build+deploy ke Cloudflare Pages)
```

> Folder hasil build (`dist/`) dan data katalog (`site-content/series/`)
> ter-*gitignore* dan diregenerasi tiap run. Jangan diedit manual.

### Paginasi halaman daftar
Semua daftar seri (beranda `/`, `daftar-komik/`, dan halaman genre) hanya
menampilkan maksimal **30 seri per halaman** (`PAGE_SIZE`, ubah dengan env
`PAGE_SIZE=banyak`). Halaman berikutnya adalah halaman statis:

- Beranda: `/` → `/page/2/` → `/page/3/` → …
- Daftar manhwa: `/daftar-komik/` → `/daftar-komik/page/2/` → …
- Genre: `/genre/action/` → `/genre/action/page/2/` → …

Navigasi `‹ Sebelumnya | 1 … N | Berikutnya ›` otomatis dibuat oleh
`build.py`; daftar halaman ini ikut masuk `sitemap.xml`.

---

## 8. Urutan Pemakaian Umum

1. Isi `site-content/sources.json` dengan URL sumber.
2. `python scraper.py --images` → katalog masuk ke `site-content/series/`.
3. `python build.py` → halaman statis ter-generate di `dist/`.
4. `python -m http.server 8000` → cek hasilnya di browser.
5. `npx wrangler pages deploy dist --project-name=mfmam` → publikasikan.
6. Untuk pemeliharaan harian, biarkan GitHub Actions berjalan (`scrape.yml`
   4×/hari; otomatis scrape + build + deploy ke Cloudflare Pages).

---

## 9. Troubleshooting

| Masalah | Solusi |
|---|---|
| Scrape gagal / diblokir sumber | Naikkan `SCRAPE_DELAY` (mis. `5`); cek koneksi ke situs sumber |
| Bab tidak bertambah | Pastikan halaman sumber berisi link bab "chapter"; cek log scraper |
| Manga tidak muncul di beranda | Pastikan ada `site-content/series/*.json` lalu jalankan `python build.py` |
| Gambar rusak | Gambar memakai CDN sumber (hotlink); cek referrer/URL masih valid |
| Urutan bab tampak mundur | Fitur baru: daftar bab & reader memakai **urutan baca** (bab 1 dulu). Jalankan ulang build agar halaman lama tergantikan |
| Halaman daftar terlalu panjang | Hasil build ter-paginasi maks. 30 seri/halaman; ubah dengan `PAGE_SIZE` (mis. `48`) |
| PowerShell menolak perintah | Pakai `cmd` / `npx.cmd` |