// Pages Function: /data/<slug>.json
// Membaca data katalog dari R2 bucket (binding `mfmam_data`) alih-alih file statis.
// Endpoint tetap SAMA: https://mfmam.pages.dev/data/<slug>.json
// sehingga assets/script.js (fetch('/data/' + slug + '.json')) tidak perlu diubah.
export async function onRequestGet(context) {
  const { env, params } = context;
  // URL reader memakai /data/<slug>.json (dengan .json), route [slug] menangkap
  // slug termasuk ekstensi. Bersihkan `.json` agar key R2 pas: data/<slug>.json.
  const slug = String(params.slug || '').replace(/\.json$/i, '');
  const key = 'data/' + slug + '.json';

  const obj = await env.mfmam_data.get(key);
  if (obj === null) {
    return new Response('{"error":"not found"}', {
      status: 404,
      headers: { 'content-type': 'application/json; charset=utf-8' },
    });
  }

  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  headers.set('etag', obj.httpEtag);
  headers.set('cache-control', 'public, max-age=600, stale-while-revalidate=86400');
  headers.set('content-type', 'application/json; charset=utf-8');

  return new Response(obj.body, { headers });
}
