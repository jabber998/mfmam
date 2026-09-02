# -*- coding: utf-8 -*-
"""
Upload dist-data/data/*.json ke bucket R2 mfmam-data (key: data/<slug>.json).

Pakai `wrangler r2 object put` (OAuth sudah login, tanpa perlu S3 Access Key),
dengan beberapa proses paralel agar lebih cepat.

Jalankan:
  python _upload_r2.py            # upload semua file di dist-data/data/
  python _upload_r2.py --workers 8
  python _upload_r2.py --resume   # lewati file yang sudah tercatat OK di log
"""
import os
import sys
import glob
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, 'dist-data', 'data')
BUCKET = 'mfmam-data'
PREFIX = 'data/'
LOG_PATH = os.path.join(ROOT, 'r2-upload.log')

# cari wrangler.cmd
WRANGLER = None
for cand in (
    os.path.join(os.environ.get('APPDATA', ''), 'npm', 'wrangler.cmd'),
    os.path.join(os.environ.get('APPDATA', ''), 'npm', 'wrangler'),
    'wrangler',
):
    if cand and os.path.exists(cand):
        WRANGLER = cand
        break
if not WRANGLER:
    WRANGLER = 'wrangler'


def done_from_log():
    """Kumpulkan nama file yang sudah tercatat OK di log (mode resume)."""
    done = set()
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    if line.startswith('[OK'):
                        parts = line.split(']', 1)
                        if len(parts) > 1:
                            name = parts[1].strip().split()[0]
                            done.add(name)
        except Exception:
            pass
    return done


def upload_one(path):
    name = os.path.basename(path)
    key = PREFIX + name
    cmd = [WRANGLER, 'r2', 'object', 'put',
           '%s/%s' % (BUCKET, key),
           '--file', path,
           '--remote',
           '--content-type', 'application/json; charset=utf-8']
    t0 = time.time()
    last_err = ''
    ok = False
    for attempt in range(1, 4):  # retry 3x
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding='utf-8', errors='replace',
                               timeout=240)
            dt = time.time() - t0
            if r.returncode == 0:
                ok = True
                break
            last_err = (r.stderr or r.stdout or '')[-200:]
            time.sleep(2 * attempt)
        except Exception as e:
            dt = time.time() - t0
            last_err = '%s: %s' % (type(e).__name__, e)
            time.sleep(2 * attempt)
    mb = os.path.getsize(path) / 1048576
    status = 'OK ' if ok else 'FAIL'
    detail = last_err.replace('\n', ' ')[:120] if not ok else ''
    print('[%s] %-55s %6.2f MB  %5.1fs  %s'
          % (status, name, mb, time.time() - t0, detail), flush=True)
    return ok


def main():
    workers = 8
    resume = '--resume' in sys.argv
    if '--workers' in sys.argv:
        try:
            workers = int(sys.argv[sys.argv.index('--workers') + 1])
        except (IndexError, ValueError):
            pass
    files = sorted(glob.glob(os.path.join(DATA_DIR, '*.json')))
    if resume:
        done = done_from_log()
        files = [f for f in files if os.path.basename(f) not in done]
        print('Resume: %d file sudah OK di log, upload %d sisanya'
              % (len(done), len(files)), flush=True)
    print('Upload %d file ke bucket %s (workers=%d) menggunakan %s'
          % (len(files), BUCKET, workers, WRANGLER), flush=True)
    if not files:
        print('Tidak ada file untuk di-upload.')
        return 0
    t0 = time.time()
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for result in ex.map(upload_one, files):
            if result:
                ok += 1
            else:
                fail += 1
    print('Selesai: %d OK, %d FAIL, total %ds'
          % (ok, fail, time.time() - t0), flush=True)
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())