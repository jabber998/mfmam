# -*- coding: utf-8 -*-
'''
Mirror chapter images from doujindesu to Blogger.

Doujindesu image URLs are signed and expire within about 24 hours.
This module downloads chapter images, uploads them to a Blogger DRAFT
post (image host), and rewrites images[] in the series json file with
permanent blogger.googleusercontent.com URLs instead of the expiring ones.

Usage:
  python scraper_doujindesu.py --images
  python scraper_blogger.py <slug> [--chapters BAB] [--max-images N] [--force]
  python scraper_blogger.py --info
  python scraper_blogger.py --ensure-post [--force]
  python scraper_blogger.py --test
  python scraper_blogger.py --test-upload [URL1,URL2]
  python scraper_blogger.py --auth | --auth-manual
  python build.py

Two upload mechanisms (automatic fallback:
  A) resumable via docs.google.com/upload/blogger/photos/resumable
  B) classic via add-image.g (multipart, needs postID}

The host post is a DRAFT. Run --test-upload to verify public access of
image URLs inside a DRAFT post (HTTP 200 anonymous).

Credentials (not committed; see .gitignore:
  site-content/blogger-creds.json or env BLOGGER_*
    {
      "client_id":     "...apps.googleusercontent.com",
      "client_secret": "...",
      "refresh_token": "...",
      "blog_id":       "1234567890123"
    }
  Generate with: python scraper_blogger.py --auth
'''
import io
import json
import os
import re
import sys
import time
from urllib.request import Request, urlopen

import requests

from scraper_common import (
    ROOT, SERIES_DIR, UA, _SSL_CTX,
    chapter_label, load_json_file, logv, select_chapters,
)

CONTENT = os.path.join(ROOT, 'site-content')

CREDS_PATH = os.path.join(CONTENT, 'blogger-creds.json')
META_PATH = os.path.join(CONTENT, 'blogger-meta.json')
IMAGE_MAP_PATH = os.path.join(CONTENT, 'blogger-image-map.json')

SCOPE = 'https://www.googleapis.com/auth/blogger'
OAUTH_TOKEN_URL = 'https://oauth2.googleapis.com/token'
BLOGGER_API = 'https://www.googleapis.com/blogger/v3'
UPLOAD_RESUMABLE = 'https://docs.google.com/upload/blogger/photos/resumable'
UPLOAD_ADDIMAGE = 'https://www.blogger.com/add-image.g'

IMAGE_POST_TITLE = 'mf-mam image host'
IMAGE_POST_LABEL = 'mf-mam-images'

_MAX_ENV = os.environ.get('BLOGGER_MAX_IMAGES_PER_RUN', '').strip()
MAX_IMAGES_PER_RUN = int(_MAX_ENV) if _MAX_ENV.isdigit() else 300

_COMPRESS = os.environ.get('BLOGGER_COMPRESS', '').strip().lower() \
    not in ('0', 'false', 'no', 'off')
MAX_DIM = 1200
JPEG_QUALITY = 82

DELAY = 0.7


def _g(k):
    v = os.environ.get(k, '')
    return v.strip()


def load_creds():
    """Credentials from file or env."""
    env = {
        'client_id': _g('BLOGGER_CLIENT_ID'),
        'client_secret': _g('BLOGGER_CLIENT_SECRET'),
        'refresh_token': _g('BLOGGER_REFRESH_TOKEN'),
        'blog_id': _g('BLOGGER_BLOG_ID'),
        'blog_url': _g('BLOGGER_BLOG_URL'),
    }
    if any(v for v in env.values())and env['client_id']:
        return {k: v for k, v in env.items() if v}
    return load_json_file(CREDS_PATH) or {}


def save_creds(creds):
    os.makedirs(CONTENT, exist_ok=True)
    with open(CREDS_PATH, 'w', encoding='utf-8') as fh:
        json.dump(creds, fh, ensure_ascii=False, indent=2)


def load_meta():
    return load_json_file(META_PATH) or {}


def save_meta(meta):
    os.makedirs(CONTENT, exist_ok=True)
    with open(META_PATH, 'w', encoding='utf-8')as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)


def load_image_map():
    return load_json_file(IMAGE_MAP_PATH) or {}


def save_image_map(mapping):
    os.makedirs(CONTENT, exist_ok=True)
    with open(IMAGE_MAP_PATH, 'w', encoding='utf-8')as fh:
        json.dump(mapping, fh, ensure_ascii=False, indent=2)


def chid_from_external(ext):
    # chapter id from the external reader url
    if not ext:
        return ''
    return ext.rstrip('/').rsplit('/', 1)[-1]


def is_permanent_images(imgs):
    # true if all urls are already permanent on google cdn
    imgs = imgs or []
    if not imgs:
        return False
    for u in imgs:
        if not isinstance(u, str):
            return False
        low = u.lower()
        if not ('googleusercontent.com' in low and low.startswith('http')):
            return False
    return True


def is_permanent_single(url):
    low = url or ''
    low = low.lower()
    return bool(low.startswith('http')and 'googleusercontent.com' in low)


def _auth_headers(token):
    return {
        'Authorization': 'Bearer %s' % token,
        'Content-Type': 'application/json',
    }

class BloggerClient:
    # blogger client: oauth2, blog id, draft post, image upload
    def __init__(self, creds):
        self.creds = creds or {}
        self._access = None
        self._access_exp = 0.0

    def access_token(self, force=False):
        now = time.time()
        if self._access and now < self._access_exp and not force:
            return self._access
        rt = self.creds.get('refresh_token') or ''
        cid = self.creds.get('client_id') or ''
        csec = self.creds.get('client_secret') or ''
        if not (rt and cid and csec):
            raise RuntimeError('kredensial belum lengkap di %s' % CREDS_PATH)
        r = requests.post(OAUTH_TOKEN_URL, data={
            'grant_type': 'refresh_token',
            'refresh_token': rt,
            'client_id': cid,
            'client_secret': csec,
        }, timeout=30)
        if r.status_code != 200:
            raise RuntimeError('gagal refresh token %s: %s' % (r.status_code, r.text[:200]))
        j = r.json()
        self._access = j['access_token']
        self._access_exp = now + int(j.get('expires_in',3600)) - 60
        return self._access

    def refresh(self):
        self._access = None
        return self.access_token(force=True)

    def blog_id(self):
        v = self.creds.get('blog_id')
        if v:
            return str(v).strip()
        burl = self.creds.get('blog_url')
        if burl:
            burl = str(burl).strip()
            tok = self.access_token()
            r = requests.get(BLOGGER_API + '/blogs/byurl',
                             params={'url': burl},
                             headers=_auth_headers(tok), timeout=30)
            r.raise_for_status()
            return r.json()['id']
        tok = self.access_token()
        r = requests.get(BLOGGER_API + '/users/self/blogs',
                         headers=_auth_headers(tok), timeout=30)
        if r.status_code == 200:
            items = (r.json().get('items') or [])
            if len(items) == 1:
                return items[0]['id']
            if len(items)> 1:
                names = ['  - %s (id=%s)' % (b.get('name'), b.get('id'))
                         for b in items]
                raise RuntimeError('banyak blog; set blog_id di %s' % CREDS_PATH)
        raise RuntimeError('blog_id tidak ditemukan di %s' % CREDS_PATH)

    def ensure_image_post(self, force_create=False):
        meta = load_meta()
        bid = self.blog_id()
        tok = self.access_token()
        pid = meta.get('post_id')
        if pid and not force_create:
            pid = str(pid).strip()
            r = requests.get(BLOGGER_API + '/blogs/%s/posts/%s' % (bid, pid),
                             headers=_auth_headers(tok), timeout=30)
            if r.status_code == 200:
                meta['blog_id'] = bid
                return meta
            if r.status_code not in (404, 410):
                # post host mungkin masih ada; jangan wipe cache
                # hanya karena respons API error/belum stabil.
                raise RuntimeError('cek post host %s: %s'
                                   % (r.status_code, r.text[:200]))
            meta.pop('post_id', None)
            save_meta(meta)
        r = requests.get(BLOGGER_API + '/blogs/%s/posts' % bid,
                         headers=_auth_headers(tok),
                         params={'labels': IMAGE_POST_LABEL, 'maxResults': 50,
                                 'fetchBodies': 'false'},
                         timeout=30)
        if r.status_code == 200:
            for item in r.json().get('items') or []:
                if item.get('status') == 'DRAFT'and item.get('labels') \
                        and IMAGE_POST_LABEL in item.get('labels', []):
                    meta['post_id'] = item['id']
                    meta['post_url'] = item.get('url') or ''
                    meta['blog_id'] = bid
                    save_meta(meta)
                    return meta
        body = {
            'kind': 'blogger#post',
            'title': IMAGE_POST_TITLE,
            'content': '<p>penampung gambar bab</p>',
            'labels': [IMAGE_POST_LABEL],
        }
        r = requests.post(BLOGGER_API + '/blogs/%s/posts' % bid,
                          headers=_auth_headers(tok),
                          params={'isDraft': 'true'},
                          json=body, timeout=30)
        if r.status_code not in (200, 201):
            raise RuntimeError('gagal buat post draft %s: %s'
                               % (r.status_code, r.text[:300]))
        j = r.json()
        meta['post_id'] = j['id']
        meta['post_url'] = j.get('url') or ''
        meta['blog_id'] = bid
        meta['post_mode'] = 'draft'
        save_meta(meta)
        return meta

    # -------------------------------------------------------------- upload
    def upload_resumable(self, data, filename, content_type):
        # mechanism A: resumable upload to blogger photo storage
        size = len(data)
        payload = {
            'protocolVersion': '0.8',
            'createSessionRequest': {
                'fields': [
                    {'external': {'name': 'file', 'filename': filename,
                                  'put': {}, 'size': size}},
                    {'inlined': {'name': 'title', 'content': filename,
                                 'contentType': 'text/plain'}},
                    {'inlined': {'name': 'addtime',
                                 'content': str(int(time.time() * 1000)),
                                 'contentType': 'text/plain'}},
                    {'inlined': {'name': 'onepick_version', 'content': 'v2',
                                 'contentType': 'text/plain'}},
                    {'inlined': {'name': 'onepick_host_id', 'content': '10',
                                 'contentType': 'text/plain'}},
                    {'inlined': {'name': 'album_mode', 'content': 'permanent',
                                 'contentType': 'text/plain'}},
                    {'inlined': {'name': 'silo_id', 'content': '3',
                                 'contentType': 'text/plain'}},
                ]
            },
        }
        tok = self.access_token()
        headers = {
            'authorization': 'Bearer %s' % tok,
            'content-type': 'application/x-www-form-urlencoded',
            'x-goog-upload-command': 'start',
            'x-goog-upload-protocol': 'resumable',
            'x-goog-upload-header-content-length': str(size),
            'x-goog-upload-header-content-type': content_type,
            'x-client-pctx': 'CgcSBWjtl_cu',
        }
        r = requests.post(UPLOAD_RESUMABLE, params={'authuser': '0',
                                                    'opi': '98421741'},
                          data=json.dumps(payload), headers=headers, timeout=60)
        if r.status_code != 200:
            raise RuntimeError('resumable start %s: %s'
                               % (r.status_code, r.text[:200]))
        up_url = r.headers.get('x-goog-upload-url') or ''
        if not up_url:
            raise RuntimeError('resumable start tanpa x-goog-upload-url: %s'
                               % r.text[:200])
        return self._resumable_finalize(up_url, data, content_type)

    def _resumable_finalize(self, up_url, data, content_type):
        up_headers = {
            'accept': '*/*',
            'content-type': content_type,
            'origin': 'https://docs.google.com',
            'referer': 'https://docs.google.com/',
            'user-agent': UA,
            'x-client-pctx': 'CgcSBWjtl_cu',
            'x-goog-upload-command': 'upload, finalize',
            'x-goog-upload-offset': '0',
        }
        r = requests.post(up_url, headers=up_headers, data=data, timeout=120)
        if r.status_code != 200:
            raise RuntimeError('resumable upload %s: %s'
                               % (r.status_code, r.text[:200]))
        try:
            info = (r.json()['sessionStatus']['additionalInfo']
                    ['uploader_service.GoogleRupioAdditionalInfo']
                    ['completionInfo']['customerSpecificInfo'])
            url = info['url'] or ''
        except (KeyError, ValueError) as ex:
            raise RuntimeError('respons resumable tak dikenal: %s' % ex)
        if not url.startswith('http'):
            raise RuntimeError('URL hasil upload tidak valid: %s' % (url[:120]))
        # s0 means original size
        parts = url.rstrip('/').split('/')
        return '/'.join(parts[:-1]) + '/s0/' + parts[-1]

    def upload_add_image(self, post_id, data, filename, content_type):
        # mechanism B: classic multipart add-image.g
        tok = self.access_token()
        fields = {
            'blogID': self.blog_id(),
            'postID': str(post_id),
            'secure': 'true',
            'storage_type': '1',
        }
        files = {'image': (filename, data, content_type)}
        msg = ''
        for scheme in ('Bearer', 'OAuth'):
            headers = {'authorization': '%s %s' % (scheme, tok),
                       'user-agent': UA}
            r = requests.post(UPLOAD_ADDIMAGE, data=fields, files=files,
                              headers=headers, timeout=120)
            if r.status_code in (200, 201):
                return self._parse_addimage_url(r)
            msg = '%s %s' % (r.status_code, r.text[:250])
        raise RuntimeError('add-image gagal: ' + msg)

    @staticmethod
    def _parse_addimage_url(r):
        txt = r.text or ''
        try:
            j = r.json()
        except ValueError:
            j = None
        if j:
            if j.get('status') == 'ok'and j.get('url'):
                return j['url']
            for k in ('url', 'href', 'imageUrl'):
                if j.get(k):
                    return j[k]
        pat = re.compile(
            r'https?://[^\s]*googleusercontent\.com[^\s]*')
        m = pat.search(txt)
        if m:
            return m.group(0)
        raise RuntimeError('URL tidak ditemukan di respons add-image: %s'
                           % txt[:250])

    def upload_image(self, data, filename, content_type, post_id=None):
        # single image: try resumable, then fallback to add-image
        last = None
        try:
            return self.upload_resumable(data, filename, content_type)
        except Exception as ex:
            last = ex
            print('   upload resumable gagal %s; coba add-image.g...' % ex)
        if not post_id:
            print('   add-image.g butuh post_id')
            raise last or RuntimeError('semua mekanisme upload gagal')
        try:
            return self.upload_add_image(post_id, data, filename, content_type)
        except Exception as ex2:
            last = ex2
        raise last or RuntimeError('semua mekanisme upload gagal')

    @staticmethod
    def verify_public(url):
        # anonymous GET; returns (status, type, bytes
        try:
            r = requests.get(url, timeout=30, allow_redirects=True,
                             headers={'user-agent': UA})
            ct = r.headers.get('Content-Type', '')
            return (r.status_code, ct, len(r.content))
        except Exception as ex:
            return (-1, str(ex), 0)

# ------------------------------------------------------- image processing
def download_image(url):
    # download one chapter image; returns (bytes, content_type
    req = Request(url, headers={
        'User-Agent': UA,
        'Referer': 'https://doujin.desu.xxx/',
        'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
    })
    with urlopen(req, timeout=90, context=_SSL_CTX) as r:
        ct = r.headers.get('Content-Type') or ''
        return r.read(), ct


def prepare_image(data, source_ct, base_name):
    # compress when enabled: resize to max 1200px and save as jpeg
    if not _COMPRESS:
        ext = source_ct.split('/')[-1] or 'jpg'
        ext = ext.lower()
        if ext in ('jpeg', 'jpg'):
            ext = 'jpg'
        return data, '%s.%s' % (base_name, ext), source_ct or 'image/jpeg'
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return data, '%s.jpg' % base_name, 'image/jpeg'
    if img.mode not in ('RGB', 'L'):
        try:
            img = img.convert('RGB')
        except Exception:
            pass
    if max(img.size) > MAX_DIM:
        img.thumbnail((MAX_DIM , MAX_DIM), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue(), '%s.jpg' % base_name, 'image/jpeg'


def fmt_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return '%.1f %s' % (n, unit)
        n /= 1024.0
    return '%.1f GB' % n

def refresh_stale_doujindesu_series(slug):
    """Refresh URL gambar bab doujindesu yang BELUM permanen (googleusercontent).

    Kembalikan jumlah bab yang berhasil di-refresh. Bab yang sudah permanen
    dilewati (tidak di-fetch & tidak ditimpa). Menggunakan API doujindesu
    (_doujin_api_get /api/chapters/<chid> -> content_urls)."""
    try:
        from scraper_doujindesu import _doujin_api_get
    except Exception as ex:
        print('   ! scraper_doujindesu tidak bisa diimpor: %s' % ex)
        return 0
    path = os.path.join(SERIES_DIR, slug + '.json')
    data = load_json_file(path)
    if not data:
        return 0
    chs = data.get('chapters') or []
    nref = 0
    for c in chs:
        ext = (c.get('external') or '').strip()
        chid = ext.rstrip('/').rsplit('/', 1)[-1] if ext else ''
        imgs = c.get('images') or []
        if chid and not is_permanent_images(imgs):
            try:
                d = _doujin_api_get('/chapters/' + chid)
                urls = []
                if isinstance(d, dict):
                    urls = [u for u in (d.get('content_urls') or [])
                            if isinstance(u, str) and u.strip().startswith('http')]
                if urls:
                    c['images'] = urls
                    nref += 1
                    logv('  [refresh] %s: %d url segar' % (chapter_label(c), len(urls)))
                else:
                    print('   ! bab %s: API tak memberi content_urls (URL lama '
                          'dipertahankan).' % chapter_label(c))
            except Exception as ex:
                print('   ! bab %s gagal di-refresh (URL lama dipertahankan): %s'
                      % (chapter_label(c), ex))
    if nref:
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    return nref


def mirror_series(slug, chapter_spec='', max_images=None, force=False,
                  refresh=False):
    # mirror chapter images of a doujindesu series to blogger
    max_images = max_images or MAX_IMAGES_PER_RUN
    creds = load_creds()
    if not creds.get('client_id'):
        print('kredensial belum diisi; jalankan --auth dulu')
    path = os.path.join(SERIES_DIR, slug + '.json')
    data = load_json_file(path)
    if not data:
        print('   seri %r tidak ditemukan' % slug)
        return 1
    src = data.get('source_url') or ''
    src = src.lower()
    if 'doujin.desu.xxx' not in src:
        print('   %s bukan seri doujindesu' % slug)
        return 1

    if refresh:
        # URL gambar doujindesu bertanda tangan & basi ~24 jam. Refresh hanya
        # bab yang URL-nya BELUM permanen (amz/expired) agar mirror dapat
        # benih URL segar; bab yang sudah permanent tidak disentuh.
        try:
            nref = refresh_stale_doujindesu_series(slug)
        except Exception as ex:
            print('   ! refresh gagal: %s' % ex)
            return 1
        if nref:
            print('   refresh %d bab ke URL segar' % nref)
        data = load_json_file(path) or {}

    mapping = load_image_map()
    chs = data.get('chapters') or []
    sel, desc = select_chapters(chs, chapter_spec)
    if not sel:
        print('   tidak ada bab yang cocok dengan %r' % chapter_spec)
        return 1

    client = BloggerClient(creds)
    try:
        meta = client.ensure_image_post()
    except Exception as ex:
        print('   gagal pastikan post host: %s' % ex)
        return 1
    post_id = meta.get('post_id')

    print('[mirror] seri: %s | bab: %d/%d [%s] (post %s)'
          % (data.get('title'), len(sel), len(chs), desc, post_id))
    uploaded = skipped = failed =0
    used_bytes =0
    per_series = mapping.setdefault(slug, {})
    t_all = time.time()

    for c in sel:
        ext = c.get('external') or ''
        chid = chid_from_external(ext)
        old_imgs = c.get('images') or []
        if is_permanent_images( old_imgs):
            per_series[chid] = old_imgs
            skipped +=1
            continue
        if not old_imgs:
            print('   bab %s belum punya url gambar' % chapter_label(c))
            failed +=1
            continue
        urls = []
        part_fail =0
        perm_skip =0
        uploaded_here =0
        for i, u in enumerate(old_imgs, 1):
            if uploaded >= max_images:
                # JANGAN buang sisa: pertahankan url asli yang belum diproses
                urls.extend(old_imgs[i - 1:])
                print('   capai batas %d gambar; berhenti' % max_images)
                break
            if is_permanent_single( u):
                urls.append(u)
                perm_skip +=1
                continue
            ok_g = False
            for attempt in (1, 2):
                try:
                    raw,sct = download_image(u)
                    fname = '%s-%s-%03d' % (slug[:24], chid[:8] or 'x', i)
                    pdata,pname,pct = prepare_image(raw,sct,fname)
                    url = client.upload_image(pdata,pname,pct,post_id=post_id)
                    urls.append(url)
                    used_bytes += len(pdata)
                    uploaded +=1
                    uploaded_here +=1
                    ok_g = True
                    break
                except Exception as ex:
                    if attempt ==1:
                        print('   gambar %s gagal; coba lagi' % chapter_label(c))
                        time.sleep(2.0)
                    else:
                        print('   gambar %s gagal tetap: %s' % (chapter_label(c), ex))
            if not ok_g:
                urls.append(u)
                part_fail +=1
            time.sleep(DELAY + (i % 3) * 0.2)
        if chid:
            per_series[chid] = urls
            c['images'] = urls
            save_image_map(mapping)
        print('   -> %s: %d url (baru +%d, perm %d, gagal %d)'
              % (chapter_label(c), len(urls), uploaded_here, perm_skip, part_fail))
        if part_fail:
            failed +=1
        elif urls:
            skipped +=1

    with open(path, 'w', encoding='utf-8')as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    save_image_map(mapping)
    print('selesai: %d bab, %d gambar (%s), %d lewati, %d gagal (%ds).'
          % (len(sel), uploaded, fmt_size(used_bytes), skipped, failed,
             round(time.time() - t_all)))
    if uploaded:
        print('URL gambar kini permanen')

# ---------------------------------------------------------------------- CLI
def cmd_auth():
    """Create refresh token via local OAuth flow."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    creds = load_creds()
    cid = creds.get('client_id') or os.environ.get('BLOGGER_CLIENT_ID', '')
    csec = creds.get('client_secret') or os.environ.get('BLOGGER_CLIENT_SECRET', '')
    if not (cid and csec):
        print('client_id/client_secret belum ada di %s' % CREDS_PATH)
        return 1
    cfile = os.path.join(ROOT, '.blogger-client.json')
    client = {
        'installed': {
            'client_id': cid,
            'client_secret': csec,
            'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
            'token_uri': 'https://oauth2.googleapis.com/token',
            'redirect_uris': ['http://localhost'],
        }
    }
    with open(cfile, 'w', encoding='utf-8')as fh:
        json.dump(client, fh)
    flow = InstalledAppFlow.from_client_secrets_file(
        cfile, [SCOPE], redirect_uri='http://localhost')
    print('Membuka browser untuk otorisasi Google...')
    try:
        cred = flow.run_local_server(port=0, open_browser=True)
    except Exception as ex:
        print('  Otentikasi dibatalkan/gagal: %s' % ex)
    finally:
        try:
            os.remove(cfile)
        except OSError:
            pass
    creds.setdefault('client_id', cid)
    creds.setdefault('client_secret', csec)
    creds['refresh_token'] = cred.refresh_token or ''
    if not creds['refresh_token']:
        print('  Tidak dapat refresh token; coba lagi.')
    save_creds(creds)
    print('  refresh_token tersimpan ke %s' % CREDS_PATH)


def cmd_auth_manual():
    """Manual OAuth (PKCE): copy the redirect url back to a file."""
    import base64
    import hashlib
    import random
    import webbrowser
    from urllib.parse import unquote, urlencode

    creds = load_creds()
    cid = creds.get('client_id') or ''
    csec = creds.get('client_secret') or ''
    if not (cid and csec):
        print('client_id/client_secret belum ada di %s' % CREDS_PATH)
        return 1

    port = random.randint(40000, 65000)
    verifier = base64.urlsafe_b64encode(os.urandom(48)).rstrip(b'=').decode()
    digest = hashlib.sha256(verifier.encode('ascii')).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b'=').decode()
    state = ''.join(random.choices('abcdef0123456789', k=24))
    redirect_uri = 'http://localhost:%d/' % port

    params = {
        'response_type': 'code',
        'client_id': cid,
        'redirect_uri': redirect_uri,
        'scope': SCOPE,
        'state': state,
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'access_type': 'offline',
        'prompt': 'consent',
    }
    url = 'https://accounts.google.com/o/oauth2/auth?' + urlencode(params)
    auth_url_path = os.path.join(CONTENT, 'blogger-auth-url.txt')
    code_path = os.path.join(CONTENT, 'blogger-auth-code.txt')
    state_path = os.path.join(CONTENT, 'blogger-auth-state.json')
    with open(auth_url_path, 'w', encoding='utf-8')as fh:
        fh.write(url + '\n')
    with open(state_path, 'w', encoding='utf-8')as fh:
        json.dump({
            'port': port, 'state': state, 'verifier': verifier,
            'redirect_uri': redirect_uri, 'client_id': cid,
        }, fh, ensure_ascii=False, indent=2)

    print('1) Buka URL berikut di browser, login and izinkan:')
    print('   %s' % url)
    print('2) Browser akan mencoba membuka %s' % redirect_uri)
    print('   Halaman tidak bisa diakses itu NORMAL.')
    print('3) Salin SELURUH URL (diawali http://localhost:%d/...) ke file:' % port)
    print('       %s' % code_path)
    print('   Script menunggu hingga 5 menit...')
    try:
        webbrowser.open(url)
    except Exception:
        pass

    deadline = time.time() + 300
    while time.time() < deadline:
        if os.path.exists(code_path):
            txt = open(code_path, 'r', encoding='utf-8',
                       errors='ignore').read().strip()
            if txt and len(txt) > 20:
                break
        time.sleep(2)
    else:
        print('  Waktu habis (5 menit) tanpa kode diterima.')

    txt = open(code_path, 'r', encoding='utf-8',
               errors='ignore').read().strip()
    m = re.search(r'[?&]code=([^&\s]+)', txt)
    if not m:
        print('  Tidak menemukan parameter code= di input: %s' % txt[:180])
    code = unquote(m.group(1))

    r = requests.post(OAUTH_TOKEN_URL, data={
        'grant_type': 'authorization_code',
        'client_id': cid,
        'client_secret': csec,
        'code': code,
        'redirect_uri': redirect_uri,
        'code_verifier': verifier,
    }, timeout=30)
    if r.status_code != 200:
        print('  Gagal tukar kode (%s): %s' % (r.status_code, r.text[:300]))
    j = r.json()
    rt = j.get('refresh_token') or ''
    if not rt:
        print('Tidak ada refresh_token di respons: %s' % list(j.keys()))
    creds['client_id'] = cid
    creds['client_secret'] = csec
    creds['refresh_token'] = rt
    save_creds(creds)
    for p in (auth_url_path, code_path, state_path):
        try:
            os.remove(p)
        except OSError:
            pass
    print('refresh_token tersimpan ke %s' % CREDS_PATH)
    print('Verifikasi: `python scraper_blogger.py --info`.')

# ------------------------------------------------------------------ ensure
def cmd_ensure_post(force=False):
    """Pastikan post host (draft) ada; tulis post_id/url ke meta."""
    creds = load_creds()
    if not creds.get('client_id'):
        print('   kredensial belum diisi; jalankan --auth dulu')
        return 1
    client = BloggerClient(creds)
    try:
        meta = client.ensure_image_post(force_create=force)
    except Exception as ex:
        print('   gagal pastikan post host: %s' % ex)
        return 1
    print('   post_id : %s' % meta.get('post_id'))
    print('   post_url: %s' % (meta.get('post_url') or ''))
    print('   mode    : %s' % (meta.get('post_mode') or 'draft'))
    return 0


# ------------------------------------------------------------------ info
def cmd_info():
    """Info blog host + post host + statistik image-map."""
    creds = load_creds()
    if not creds.get('client_id'):
        print('   kredensial belum terisi; jalankan --auth dulu')
        return 1
    client = BloggerClient(creds)
    try:
        bid = client.blog_id()
    except Exception as ex:
        print('   blog_id gagal diambil: %s' % ex)
        return 1
    print('   blog_id   : %s' % bid)
    meta = load_meta()
    pid = meta.get('post_id') or ''
    if pid:
        print('   post_id   : %s' % pid)
        print('   post_url  : %s' % (meta.get('post_url') or ''))
        print('   post_mode : %s' % (meta.get('post_mode') or 'draft'))
    else:
        print('   post_id   : belum ada (jalankan --ensure-post atau <slug>)')
    mapping = load_image_map()
    n = 0
    for chs in mapping.values():
        if isinstance(chs, dict):
            for imgs in chs.values():
                if isinstance(imgs, list):
                    n += len(imgs)
    print('   image-map : %d seri, %d url' % (len(mapping), n))
    return 0


# ------------------------------------------------------------ test-upload
def cmd_test_upload(url=''):
    """Uji akses publik (anonim) URL gambar googleusercontent.

    Tanpa argumen: ambil contoh url dari blogger-image-map.json.
    Dengan argumen: uji url yang diberikan (pisahkan dengan koma).
    """
    urls = []
    if url:
        urls = [u.strip() for u in url.split(',')
                if u.strip().startswith('http')]
    else:
        mapping = load_image_map()
        for chs in mapping.values():
            if not isinstance(chs, dict):
                continue
            for imgs in chs.values():
                if isinstance(imgs, list):
                    urls.extend(u for u in imgs
                                if isinstance(u, str) and u.startswith('http'))
    if not urls:
        print('   tidak ada URL gambar untuk diuji (image-map kosong?)')
        return 2
    sample = urls[:12]
    print('   uji %d url (dari %d ter-mapping):' % (len(sample), len(urls)))
    ok = 0
    for u in sample:
        status, ct, n = BloggerClient.verify_public(u)
        if status == 200:
            ok += 1
            print('   OK  %s %s (%s)' % (status, ct or '-', fmt_size(n)))
        else:
            print('   XX  %s %s' % (status, u))
    print('   hasil: %d/%d dapat diakses publik (anonim)' % (ok, len(sample)))
    if ok < len(sample):
        print('   gambar di post DRAFT mungkin tak terbaca anonim;')
        print('   coba publish post host (ubah status post menjadi LIVE).')
        return 1
    return 0


# --------------------------------------------------------------- test
def cmd_test():
    """Uji lokal kompresi (pakai test_img.bin); bila kredensial ada,
    lanjut upload 1 gambar uji lalu cek akses publiknya."""
    src = os.path.join(ROOT, 'test_img.bin')
    if not os.path.exists(src):
        print('   file uji %s tidak ditemukan' % src)
        return 1
    raw = open(src, 'rb').read()
    source_ct = 'image/webp' if raw[:4] == b'RIFF' else 'image/jpeg'
    pdata, pname, pct = prepare_image(raw, source_ct, 'test')
    print('   kompresi: %s -> %s (%s, %s)' %
          (fmt_size(len(raw)), fmt_size(len(pdata)), pname, pct))
    creds = load_creds()
    if not creds.get('client_id'):
        print('   kredensial kosong; uji lokal selesai')
        return 0
    client = BloggerClient(creds)
    try:
        meta = client.ensure_image_post()
        pid = meta.get('post_id')
        url = client.upload_image(pdata, pname, pct, post_id=pid)
        status, ct, n = client.verify_public(url)
    except Exception as ex:
        print('   upload uji gagal: %s' % ex)
        return 1
    print('   url    : %s' % url)
    print('   publik : HTTP %s (%s) %s' % (status, fmt_size(n), ct))
    return 0 if status == 200 else 1


# ---------------------------------------------------------------- main
USAGE = """Pemakaian:
  python scraper_blogger.py <slug> [--chapters BAB] [--max-images N] [--force] [--refresh]
  python scraper_blogger.py --info
  python scraper_blogger.py --ensure-post [--force]
  python scraper_blogger.py --test
  python scraper_blogger.py --test-upload [URL1,URL2]
  python scraper_blogger.py --auth | --auth-manual

  --refresh  refresh dulu URL gambar bab yang basi (amz) via API doujindesu,
             lalu mirror ke Blogger. Bab yang sudah permanen dilewati.
"""


def main():
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print(USAGE)
        sys.exit(0)
    cmd = args[0]
    rest = args[1:]
    if cmd == '--auth':
        sys.exit(cmd_auth())
    if cmd == '--auth-manual':
        sys.exit(cmd_auth_manual())
    if cmd == '--info':
        sys.exit(cmd_info())
    if cmd == '--ensure-post':
        sys.exit(cmd_ensure_post(force='--force' in rest))
    if cmd == '--test':
        sys.exit(cmd_test())
    if cmd == '--test-upload':
        sys.exit(cmd_test_upload(rest[0] if rest else ''))
    if cmd.startswith('-'):
        print('perintah tidak dikenal: %s' % cmd)
        print(USAGE)
        sys.exit(2)
    chapter_spec = ''
    max_images = None
    force = False
    refresh = False
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ('-c', '--chapters'):
            if i + 1 >= len(rest):
                print('--chapters butuh nilai')
                sys.exit(2)
            chapter_spec = rest[i + 1]
            i += 2
        elif a == '--max-images':
            if i + 1 >= len(rest):
                print('--max-images butuh nilai')
                sys.exit(2)
            max_images = int(rest[i + 1])
            i += 2
        elif a == '--force':
            force = True
            i += 1
        elif a == '--refresh':
            refresh = True
            i += 1
        else:
            print('argumen tak dikenal: %s' % a)
            sys.exit(2)
    sys.exit(mirror_series(cmd, chapter_spec, max_images, force,
                           refresh=refresh))


if __name__ == '__main__':
    main()
