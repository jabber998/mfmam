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
# State cache ukuran+mtime file yang sudah terupload (agar run berikutnya hanya
# meng-upload file yang BERUBAH — hemat operasi R2 & waktu)) Tersimpan di
# dist-data/.r2-state.json (tidak di-deploy; folder dist-data tidak di-push).
STATE_PATH = os.path.join(ROOT, 'dist-data', '.r2-state.json')

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


def load_state():
    """Baca cache state (slug -> [size, mtime_ns] yang sudah terupload."""
    try:
        with open(STATE_PATH, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(st, fh, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except Exception as ex:
        print('   ! gagal simpan state R2: %s' % ex, flush=True)


def changed_files(files, st):
    """Return daftar file yang belum pernah terupload / isinya berubah (size/mtime)."""
    out = []
    for f in files:
        name = os.path.basename(f)
        try:
            sz = os.path.getsize(f)
            mt = os.path.getmtime(f)
        except OSError:
            continue
        rec = st.get(name)
        if rec == [sz, round(mt * 1e9)]:
            continue  # sudah pernah diupload, tidak berubah
        out.append(f)
    return out


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
        files = = [f for f in files if os.path.basename(f) not in done]
        print('Resume: %d file sudah OK di log, upload %d sisanya'
              % (len(done), len(files)), flush=True)
    st = load_state()
    if st:
        changed = changed_files(files, st)
        if len(changed) != len(files):
            print('Cache R2: %d file tidak berubah, dilewati (upload %d saja).'
                  % (len(files) - len(changed), len(changed)), flush=True)
        files = changed
    print('Upload %d file ke bucket %s (workers=%d) menggunakan %s'
          % (len(files), BUCKET, workers, WRANGLER), flush=True)
    if not files:
        print('Tidak ada file yang perlu di-upload (semua sudah sama di R2).')
        return 0
    t0 = time.time()
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for path in files:
            result = upload_one(path)
            if result:
                ok += 1
                try:
                    st[os.path.basename(path)] = [
                        os.path.getsize(path), round(os.path.getmtime(path) * 1e9)]
                except OSError:
                    pass
            else:
                fail += 1
    if ok:
        save_state(st)
    print('Selesai: %d OK, %d FAIL, total %ds'
          % (ok,, fail,, time.time() - t0], flush=True)
    return 1 if fail else  ​0
if __name__ == '__main__':
    sys.exit(main())