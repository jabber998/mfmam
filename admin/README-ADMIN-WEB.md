# Admin di Web — Decap CMS untuk Mfmam

Fitur ini membuat **halaman admin yang bisa diakses dari alamat situs**:
`https://mfmam.netlify.app/admin/` — login lewat browser, edit konten, lalu
Netlify otomatis build & deploy. (Jalur "repo + Git" yang dipilih: admin di
alamat web, bukan admin lokal.)

## Cara kerjanya
1. **Decap CMS** (berjalan di browser) melayani UI dari file `admin/index.html`
   + konfigurasi `admin/config.yml`.
2. Anda login (lewat Netlify Identity / git-gateway) dan mengedit:
   - **Pengaturan Situs** → `site-content/settings.json`
   - **Halaman Info** → `site-content/pages/*.json`
   - **Katalog Seri** → `site-content/series/*.json` (bab berupa **link ke
     sumber**, tanpa menyimpan gambar)
3. Setiap perubahan tersimpan sebagai **commit di repo Git** Anda.
4. Netlify mendeteksi commit → menjalankan `python _netlify_build.py`
   (netlify.toml) → menghasilkan halaman statis → publikasi otomatis.

## Langkah yang HARUS Anda lakukan (butuh akun Anda — tidak bisa dari sini)
1. **Buat repo Git** (mis. GitHub) dan unggah isi folder `static-export/`
   (admin/, site-content/, _netlify_build.py, netlify.toml, aset, dan konten).
   > Catatan: konten arsip besar (26.000+ halaman bab) ikut masuk repo bila
   > ingin tetap online; itu berat untuk GitHub. Untuk uji coba, cukup unggah
   > admin/, site-content/, netlify.toml, _netlify_build.py, assets/, dan 404.html.
2. **Di Netlify**: buka site → **Site configuration → Build & deploy** →
   hubungkan **Continuous Deployment** ke repo itu (branch `main`). Netlify
   memakai `netlify.toml` (command & publish) otomatis.
3. **Aktifkan Identity + Git Gateway** di Netlify:
   - Site → **Identity** → **Enable Identity**.
   - Site → **Identity → Services** → aktifkan **Git Gateway**.
   - Invite diri sendiri sebagai user Identity (atau daftar).
4. Buka `https://mfmam.netlify.app/admin/`, login, edit → commit → build otomatis.

## Uji build lokal (sebelum push)
```
cd static-export
python _netlify_build.py     # baca site-content/ -> tulis halaman di folder ini
```
Diuji: membangun index, info, seri, bab-link, search.json, sitemap, 404.

## Catatan
- **Admin lokal** (CMS Python) tetap ada untuk operasi besar/lama; **Decap CMS**
  ini adalah admin yang tampil di web sesuai keinginan Anda.
- `media_folder` diarahkan ke `static/covers` — file diunggah via admin masuk
  ke repo juga.
- Jika Anda hanya ingin mode drag-and-drop (tanpa Git), abaikan `command`
  di netlify.toml; deploy folder statis tetap jalan seperti sekarang.