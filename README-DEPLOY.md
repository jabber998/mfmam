# Deploy situs statis Mfmam

Folder ini adalah hasil ekspor STATIS situs WordPress Mfmam
(migrasi dari InfinityFree ke **Netlify** / Vercel, sesuai keputusan
pindah ke Netlify karena situs ini besar ±1,3 GB).

## Isi
- Setiap bab      -> /<slug>/index.html
- Setiap seri     -> /manga/<slug>/
- Halaman daftar, genre, 404, sitemap, pencarian client-side
- Gambar bab memakai URL CDN eksternal (zero-storage); sampul di /covers/

## Cara deploy — Netlify (direkomendasikan untuk situs 1,3 GB)

Cara termudah: double-klik `deploy-netlify.cmd` di folder induk project ini
(satu folder di atas). Script otomatis:
  1) pasang Netlify CLI        ->  `npm install -g netlify-cli`
  2) login (buka browser)      ->  `netlify login`
  3) deploy produksi           ->  `netlify deploy --prod --dir .`

Manual:
```powershell
cd "C:\Users\ss\Local Sites\infinityfree\static-export"
npm install -g netlify-cli     # sekali saja
netlify login                  # membuka browser utk login akun
netlify deploy --prod --dir .  # publish ke produksi (27rb file, ~1,3 GB)
```
Jika PowerShell memblokir `*.ps1` (ExecutionPolicy), pakai `npx.cmd`,
`netlify.cmd`, atau buka `deploy-netlify.cmd` lewat `cmd`.

URL cantik (`/<slug-bab>/`) otomatis aktif lewat `netlify.toml` + `_redirects`.

## Cara deploy — Vercel (cadangan, khusus SUBSET)
Vercel hanya menerima upload statis 100 MB (Hobby) / 1 GB (Pro), dan folder
ini 1,3 GB, jadi full upload DITOLAK. Vercel hanya cocok bila Anda menyusun
**subset**: index, 404, manga/, genre/, daftar-komik/, assets/, covers/,
search.json, sitemap.xml, robots.txt, vercel.json + beberapa bab terbaru.
```powershell
cd "C:\Users\ss\Local Sites\infinityfree\static-export"
npm install -g vercel   # atau npx.cmd vercel
vercel login
vercel deploy --prod
```

## Ukuran & catatan
- Total: **±1,3 GB / 27.345 file**. ~1,28 GB adalah HTML bab (banyak `<img>`
  dengan URL CDN mirror eksternal). Netlify menangani ukuran ini dengan baik.
- Bandwidth gratis Netlify 100 GB/bulan; gambar tampil dari CDN eksternal,
  jadi beban bandwidth situs ini kecil.
- Setelah deploy, cek: `/`, `/daftar-komik/`, `/manga/eleceed/`,
  `/<slug-bab>/`, `/genre/action/`, `/sitemap.xml`, `/404.html`.

Ganti domain sesuai keinginan -> SSL otomatis (Vercel).

Lihat juga PANDUAN-MIGRASI.md di folder induk untuk langkah manual lengkap.
