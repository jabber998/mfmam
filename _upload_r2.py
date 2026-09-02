# -*- coding: utf-8 -*-
"""
Upload data katalog (dist-data/data/*.json) ke bucket R2 `mfmam-data`
key: data/<slug>.json -- HANYA file yang BERUBAH.

State riwayat upload disimpan DI DALAM bucket R2 itu sendiri
(key: .r2-state.json) sehingga bertahan antar-run (lokal maupun CI).
Setiap file dibandingkan pakai MD5; bila isinya sama dengan yang sudah
pernah terupload, file dilewati (hemat operasi R2 + waktu).

Menemukan wrangler:
  - Lokal: pakai OAuth (wrangler.cmd hasil login).
  - CI/GitHub Actions: set env WRANGLER_CMD, contoh: "npx wrangler@4"
    (token API lewat env CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID).

Jalankan:
  python _upload_r2.py                # upload file yang berubah
  python _upload_r2.py --force        # upload SEMUA (abaikan state)
  python _upload_r2.py --workers 8

Setelah selesai mencetak baris:
  CHANGED:<n>
agar workflow bisa melewati deploy bila n = 0 (tidak ada perubahan).
"""
import os
import sys
import json
import glob
import time
import hashlib
import subprocess
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, 'dist-data', 'data')
BUCKET = 'mfmam-data'
PREFIX = 'data/'
STATE_KEY = '.r2-state.json'
STATE_TMP = os.path.join(ROOT, 'dist-data', '_r2-state-tmp.json')
STATE_LOCAL = os.path.join(ROOT, 'dist-data', '_r2-state-local.json')

# Perintah wrangler. Bisa dioverride lewat env (CI memakai npx).
WRANGLER = os.environ.get('WRANGLER_CMD', '') or None


def _wrangler_cmd():
    if WRANGLER:
        return WRANGLER.split()
    for cand in (
        os.path.join(os.environ.get('APPDATA', ''), 'npm', 'wrangler.cmd'),
        os.path.join(os.environ.get('APPDATA', ''), 'npm', 'wrangler'),
        'wrangler',
    ):
        if cand and os.path.exists(cand):
            return [cand]
    return ['wrangler']


def _run(cmd, timeout=240):
    """Jalankan subprocess; kembalikan (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout)
        return r.returncode, r.stdout or '', r.stderr or ''
    except Exception as ex:
        return -1, '', '%s: %s' % (type(ex).__name__, ex)


def fetch_state():
    """Ambil state riwayat dari R2 (key .r2-state.json). {} bila belum ada."""
    if not os.path.isdir(os.path.join(ROOT, 'dist-data')):
        return {}
    rc, out, err = _run(_wrangler_cmd() + [
        'r2', 'object', 'get', '%s/%s' % (BUCKET, STATE_KEY),
        '--remote', '--file', STATE_TMP], timeout=120)
    if rc != 0 or not os.path.exists(STATE_TMP):
        return {}
    try:
        with open(STATE_TMP, encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return {}


def push_state(st):
    """Simpan state riwayat kembali ke R2."""
    try:
        with open(STATE_LOCAL, 'w', encoding='utf-8') as fh:
            json.dump(st, fh, ensure_ascii=False)
    except Exception as ex:
        print('   ! gagal simpan state lokal: %s' % ex, flush=True)
        return False
    rc, out, err = _run(_wrangler_cmd() + [
        'r2', 'object', 'put', '%s/%s' % (BUCKET, STATE_KEY),
        '--file', STATE_LOCAL, '--remote',
        '--content-type', 'application/json; charset=utf-8'], timeout=120)
    if rc != 0:
        print('   ! gagal upload state ke R2: %s' %
              (err or out)[-180:].replace('\n', ' '), flush=True)
        return False
    return True


def md5_of(path):
    h = hashlib.md5()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1048576), b''):
            h.update(chunk)
    return h.hexdigest()


def changed_files(files, st):
    """File yang belum terupload / isinya berbeda (berdasar MD5)."""
    out = []
    for f in files:
        name = os.path.basename(f)
        try:
            h = md5_of(f)
        except OSError:
            continue
        if st.get(name) == h:
            continue
        out.append(f)
    return out


def upload_one(path):
    name = os.path.basename(path)
    cmd = _wrangler_cmd() + [
        'r2', 'object', 'put', '%s/%s' % (BUCKET, PREFIX + name),
        '--file', path, '--remote',
        '--content-type', 'application/json; charset=utf-8']
    t0 = time.time()
    last_err = ''
    ok = False
    for attempt in range(1, 4):  # retry 3x
        rc, out, err = _run(cmd, timeout=240)
        if rc == 0:
            ok = True
            break
        last_err = (err or out)[-200:]
        time.sleep(2 * attempt)
    mb = os.path.getsize(path) / 1048576
    detail = last_err.replace('\n', ' ')[:120] if not ok else ''
    print('[%s] %-55s %6.2f MB  %5.1fs  %s'
          % ('OK ' if ok else 'FAIL', name, mb, time.time() - t0, detail),
          flush=True)
    return ok
def main():
    workers = 8
    force = '--force' in sys.argv
    if '--workers' in sys.argv:
        try:
            workers = int(sys.argv[sys.argv.index('--workers') + 1])
        except (IndexError, ValueError):
            pass
    files = sorted(glob.glob(os.path.join(DATA_DIR, '*.json')))
    st = fetch_state() if not force else {}
    if not force:
        changed = changed_files(files, st)
        skipped = len(files) - len(changed)
        if skipped:
            print('Cache R2: %d file sama, dilewati (upload %d saja).'
                  % (skipped, len(changed)), flush=True)
        files = changed
    print('Upload %d file ke bucket %s (workers=%d) -- command %s'
          % (len(files), BUCKET, workers, ' '.join(_wrangler_cmd())), flush=True)
    if not files:
        print('Tidak ada file yang perlu di-upload (semua sudah sama di R2).',
              flush=True)
        print('CHANGED:0')
        return 0
    t0 = time.time()
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for path in files:
            if upload_one(path):
                ok += 1
                try:
                    st[os.path.basename(path)] = md5_of(path)
                except OSError:
                    pass
            else:
                fail += 1
    if ok:
        push_state(st)
    print('Selesai: %d OK, %d FAIL, total %.0fs' % (ok, fail, time.time() - t0),
          flush=True)
    print('CHANGED:%d' % ok)
    return 1 if fail else 0


if __name__ == '__main__':
    sys.exit(main())