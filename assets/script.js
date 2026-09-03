// Mfmam static: tema, nav, pencarian, hero
(function(){
  var root = document.documentElement;
  var btn = document.getElementById('theme-btn');
  function apply(light){ root.classList.toggle('light', light);
    if (btn) btn.textContent = light ? '\u2600' : '\uD83C\uDF19';
    try { localStorage.setItem('mfmam-theme', light ? 'light' : 'dark'); } catch(e){} }
  var saved = null;
  try { saved = localStorage.getItem('mfmam-theme'); } catch(e){}
  if (saved === 'light') apply(true);
  else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) apply(true);
  if (btn) btn.addEventListener('click', function(){ apply(!root.classList.contains('light')); });

  // ---- tombol Blur: tampilkan/sembunyikan seri sumber dewasa ----
  // ON  -> seri Mikoroku & Doujindesu ikut ditampilkan
  // OFF -> kedua sumber disembunyikan (default).
  // Pencarian global & cari bab ikut menyaring lewat event 'blur-change'.
  var blurBtn = document.getElementById('blur-btn');
  var blurOn = false;
  try { blurOn = localStorage.getItem('mfmam-blur') === '1'; } catch(e){}
  function applyBlur(on){
    blurOn = !!on;
    document.body.classList.toggle('blur-on', blurOn);
    if (blurBtn){
      blurBtn.textContent = blurOn ? 'Blur: Nyala' : 'Blur: Mati';
      blurBtn.classList.toggle('on', blurOn);
      blurBtn.setAttribute('aria-pressed', blurOn ? 'true' : 'false');
      blurBtn.title = blurOn
        ? 'Sembunyikan seri dari Mikoroku & Doujindesu'
        : 'Tampilkan seri dari Mikoroku & Doujindesu';
    }
    try { localStorage.setItem('mfmam-blur', blurOn ? '1' : '0'); } catch(e){}
    window.dispatchEvent(new Event('blur-change'));
  }
  if (blurBtn) blurBtn.addEventListener('click', function(){ applyBlur(!blurOn); });
  [].slice.call(document.querySelectorAll('[data-blur-enable]')).forEach(function(b){
    b.addEventListener('click', function(){ applyBlur(true); });
  });
  applyBlur(blurOn);

  var nt = document.getElementById('nav-toggle'), mn = document.getElementById('main-nav');
  if (nt && mn) nt.addEventListener('click', function(){ mn.classList.toggle('open'); });

  // hero carousel
  var slides = [].slice.call(document.querySelectorAll('.hero-slide'));
  if (slides.length) {
    slides[0].classList.add('active');
    var i = 0;
    setInterval(function(){
      slides[i].classList.remove('active');
      i = (i + 1) % slides.length;
      slides[i].classList.add('active');
    }, 4500);
  }

  // pencarian
  var sb = document.getElementById('search-btn'),
      md = document.getElementById('search-modal'),
      inp = document.getElementById('search-input'),
      res = document.getElementById('search-results');
  var index = [];
  if (sb && md) {
    fetch('/search.json').then(function(r){ return r.json(); }).then(function(d){ index = d; }).catch(function(){});
    sb.addEventListener('click', function(){ md.hidden = false; setTimeout(function(){ if (inp) inp.focus(); }, 30); });
    md.addEventListener('click', function(e){ if (e.target === md) md.hidden = true; });
    function renderGlobalSearch(){
      var q = inp.value.trim().toLowerCase();
      res.innerHTML = '';
      if (q.length < 2) return;
      var out = index.filter(function(d){
        if (d.b && !blurOn) return false; // seri dewasa disembunyikan saat Blur mati
        return d.t.toLowerCase().indexOf(q) !== -1 ||
               (d.g || []).some(function(g){ return g.toLowerCase().indexOf(q) !== -1; });
      });
      out.slice(0, 30).forEach(function(d){
        var a = document.createElement('a');
        a.className = 'sr'; a.href = d.u;
        var s = document.createElement('strong'); s.textContent = d.t;
        var sp = document.createElement('span'); sp.className = 'sg'; sp.textContent = (d.g || []).join(', ');
        a.appendChild(s); a.appendChild(sp);
        res.appendChild(a);
      });
      if (!out.length){ var e = document.createElement('div'); e.className = 'sr'; e.textContent = 'Tidak ditemukan.'; res.appendChild(e); }
    }
    inp.addEventListener('input', renderGlobalSearch);
    window.addEventListener('blur-change', renderGlobalSearch);
  }

  // cari bab: temukan bab yang ingin dibaca
  var cfx = document.getElementById('chap-search-form'),
      csec = document.getElementById('chapter-search'),
      cix = document.getElementById('chap-search-input'),
      crx = document.getElementById('chap-search-results');
  if (cfx && cix && crx) {
    var seriesFilter = (csec && csec.getAttribute('data-series')) || '';
    // --- MODE HALAMAN SERI: filter langsung daftar bab pada halaman ---
    var chList = document.querySelector('nav.ch-list');
    if (seriesFilter && chList) {
      var rows = [].slice.call(chList.querySelectorAll('a.ch-row'));
      var secTitle = null;
      var titles = document.querySelectorAll('.seri-page .sec-title');
      for (var i = 0; i < titles.length; i++){
        if (/Daftar Bab/.test(titles[i].textContent)){ secTitle = titles[i]; break; }
      }
      function rowNumber(row){
        var n = row.querySelector('.ch-no');
        return n ? (n.textContent || '').trim() : '';
      }
      function rowTitle(row){
        var t = row.querySelector('.ch-ti');
        return t ? (t.textContent || '').trim() : '';
      }
      function rowHit(row, tokens){
        var num = rowNumber(row);
        var hay = (num + ' ' + rowTitle(row)).toLowerCase();
        return tokens.every(function(t){
          // angka: cocokkan NOMOR BAB secara persis (exact),
          // mis. ketik "10" -> hanya chapter 10 (bukan 100-109, 110, dst).
          if (/^\d+(\.\d+)?$/.test(t)){
            return num === t;
          }
          return hay.indexOf(t) !== -1;
        });
      }
      function filterSeriesRows(q){
        var tokens = (q || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
        if (!tokens.length){
          rows.forEach(function(r){ r.style.display = ''; });
          if (secTitle) secTitle.textContent = 'Daftar Bab (' + rows.length + ')';
          crx.hidden = true; crx.innerHTML = '';
          return;
        }
        var visible = 0;
        rows.forEach(function(r){
          if (rowHit(r, tokens)){ r.style.display = ''; visible++; }
          else { r.style.display = 'none'; }
        });
        if (secTitle) secTitle.textContent = 'Daftar Bab (' + visible + ' / ' + rows.length + ')';
        crx.innerHTML = '';
        var msg = document.createElement('div');
        msg.className = visible ? 'cs-count' : 'cs-empty';
        msg.textContent = visible
          ? visible + ' bab cocok untuk \u201c' + q + '\u201d.'
          : 'Tidak ada bab yang cocok untuk \u201c' + q + '\u201d. Coba ketik nomor lain seperti 120 atau 50.';
        crx.appendChild(msg);
        crx.hidden = false;
      }
      cix.addEventListener('input', function(){ filterSeriesRows(cix.value); });
      cfx.addEventListener('submit', function(e){ e.preventDefault(); filterSeriesRows(cix.value); });
    } else {
      // --- MODE HALAMAN DAFTAR (lintas semua manhwa): kotak hasil ---
      var chapterIndex = [];
      fetch('/chapters-index.json').then(function(r){ return r.json(); })
        .then(function(d){ chapterIndex = d || []; })
        .catch(function(){});
      function tokenHit(token, d){
        if (!token) return true;
        var ts = d.n != null ? String(d.n) : '';
        // angka: cocokkan NOMOR BAB secara persis (exact),
        // mis. ketik "10" -> hanya chapter 10 (bukan 100-109, 110, dst).
        if (/^\d+(\.\d+)?$/.test(token)) return ts === token;
        var hay = (d.s + ' ' + d.t).toLowerCase();
        return hay.indexOf(token) !== -1;
      }
      function renderChap(q){
        var tokens = (q || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
        crx.innerHTML = '';
        if (!tokens.length){ crx.hidden = true; return; }
        var res = chapterIndex.filter(function(d){
          if (d.b && !blurOn) return false; // seri dewasa disembunyikan saat Blur mati
          return tokens.every(function(t){ return tokenHit(t, d); });
        });
        res.sort(function(a, b){
          var as = a.s.toLowerCase().indexOf(q) === 0 ? 0
                 : (a.s.toLowerCase().indexOf(q) !== -1 ? 1 : 2);
          var bs = b.s.toLowerCase().indexOf(q) === 0 ? 0
                 : (b.s.toLowerCase().indexOf(q) !== -1 ? 1 : 2);
          if (as !== bs) return as - bs;
          return (b.n || 0) - (a.n || 0);
        });
        var count = document.createElement('div');
        count.className = 'cs-count';
        count.textContent = res.length + ' bab ditemukan untuk \u201c' + q + '\u201d.';
        crx.appendChild(count);
        res.slice(0, 50).forEach(function(d){
          var a = document.createElement('a');
          a.className = 'cs-row'; a.href = d.u;
          var n = document.createElement('span'); n.className = 'cs-n';
          n.textContent = d.n != null ? d.n : '';
          var t = document.createElement('span'); t.className = 'cs-t'; t.textContent = d.t;
          var s = document.createElement('span'); s.className = 'cs-s'; s.textContent = d.s;
          a.appendChild(n); a.appendChild(t); a.appendChild(s);
          crx.appendChild(a);
        });
        if (!res.length){
          var e = document.createElement('div');
          e.className = 'cs-empty';
          e.textContent = 'Tidak ada bab yang cocok. Coba nama manhwa lain atau nomor bab (mis. 120).';
          crx.appendChild(e);
        }
        crx.hidden = false;
      }
      cfx.addEventListener('submit', function(e){ e.preventDefault(); renderChap(cix.value); });
      cix.addEventListener('input', function(){ renderChap(cix.value); });
      window.addEventListener('blur-change', function(){ renderChap(cix.value); });
    }
  }

  // Tanggal rilis relatif: bab baru tampil "5 jam yang lalu";
  // bab lama tampil tanggal absolut (mis. "26 Agu 2026").
  function agoText(dateStr){
    var m = /^(\d{4})-(\d{1,2})-(\d{1,2})/.exec(dateStr || '');
    if (!m) return null;
    var d = new Date(+m[1], +m[2] - 1, +m[3]);
    var diff = Date.now() - d.getTime();
    if (diff < 0) diff = 0;
    var HOUR = 3600000, DAY = 86400000;
    if (diff < HOUR) return 'kurang dari 1 jam yang lalu';
    if (diff < 24 * HOUR) return Math.floor(diff / HOUR) + ' jam yang lalu';
    if (diff < 7 * DAY) return Math.floor(diff / DAY) + ' hari yang lalu';
    if (diff < 30 * DAY) return Math.floor(diff / (7 * DAY)) + ' minggu yang lalu';
    return null; // terlalu lama -> gunakan tanggal absolut (data-fmt)
  }
  function applyDates(){
    var els = document.querySelectorAll('.ch-dt[data-date]');
    for (var i = 0; i < els.length; i++){
      var el = els[i], ds = el.getAttribute('data-date');
      if (!el.getAttribute('data-fmt')) el.setAttribute('data-fmt', el.textContent);
      var t = agoText(ds);
      el.textContent = t || el.getAttribute('data-fmt');
    }
  }
  applyDates();
  setInterval(applyDates, 60000);

  // ====== READER DINAMIS (model Cloudflare Pages) ======
  // Halaman bab TIDAK lagi dibuat satu file per bab; bab dibuka lewat hash
  // `#bab/<kunci>` pada halaman seri, lalu data dibaca dari /data/<slug>.json
  // (memuat images[]) secara client-side. Prev/Next memakai urutan baca (naik).
  var seriPage = document.querySelector('.seri-page[data-slug]');
  if (seriPage){
    var slug = seriPage.getAttribute('data-slug');
    var readerShell = document.getElementById('reader');
    // Halaman seri lama / build lama mungkin tak punya elemen reader.
    if (!readerShell){ return; }
    var dataCache = {};
    var sortedChs = null;

    function escA(s){
      return String(s == null ? '' : s).replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;').replace(/</g, '&lt;');
    }
    function numText(n){
      if (n == null || n === '') return null;
      var f = Number(n);
      return String(Number.isInteger(f) ? f : f);
    }
    function keyOf(c){ return c.num != null ? numText(c.num) : (c.slug || 'chapter'); }

    function fromHash(){
      var m = /#bab\/([^/#?]+)/.exec(location.hash);
      return m ? decodeURIComponent(m[1]) : null;
    }
    function fmtDt(s){
      var m = /^(\d{4})-(\d{1,2})-(\d{1,2})/.exec(s || '');
      if (!m) return s || '';
      var bln = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun',
                 'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des'];
      return parseInt(m[3], 10) + ' ' + (bln[+m[2]] || m[2]) + ' ' + m[1];
    }
    function el(tag, cls, txt){
      var e = document.createElement(tag);
      if (cls) e.className = cls;
      if (txt != null) e.textContent = txt;
      return e;
    }
    function loadData(cb){
      if (dataCache[slug]) return cb(dataCache[slug]);
      fetch('/data/' + slug + '.json')
        .then(function(r){ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(function(d){ dataCache[slug] = d; cb(d); })
        .catch(function(){ readerMsg('Data bab gagal dimuat.'); });
    }
    function asc(a, b){
      if (a.num == null) return 1;
      if (b.num == null) return -1;
      return a.num - b.num;
    }
    function showBox(box){
      readerShell.innerHTML = '';
      readerShell.appendChild(box);
      seriPage.hidden = true;
      readerShell.hidden = false;
      window.scrollTo(0, 0);
    }
    function readerMsg(text){
      var box = el('div', 'reader');
      box.appendChild(el('p', null, text));
      var back = el('a', 'baca-btn', '\u2190 Kembali ke Detail');
      back.href = '/manga/' + slug + '/';
      box.appendChild(back);
      showBox(box);
    }
    function navLink(c, label){
      var a = el('a', 'nav-btn', label);
      a.href = '#bab/' + keyOf(c);
      return a;
    }
    function showChapter(key){
      loadData(function(d){
        sortedChs = (d.chapters || []).slice().sort(asc);
        var idx = -1;
        for (var i = 0; i < sortedChs.length; i++){
          if (String(keyOf(sortedChs[i])) === String(key)){ idx = i; break; }
        }
        if (idx < 0){ readerMsg('Bab tidak ditemukan.'); return; }
        var c = sortedChs[idx];
        var box = el('div', 'reader');
        // baris crumb + nav atas
        var top = el('div', 'reader-top');
        var crumb = el('div', 'reader-crumb');
        var link = el('a', null, d.title || slug);
        link.href = '/manga/' + slug + '/';
        crumb.appendChild(link);
        crumb.appendChild(document.createTextNode(
          ' \u2014 ' + (c.title || 'Chapter ' + (numText(c.num) || ''))));
        top.appendChild(crumb);
        var navTop = el('div', 'reader-nav');
        if (idx > 0){ navTop.appendChild(navLink(sortedChs[idx - 1], '\u2190 Sebelumnya')); }
        else navTop.appendChild(el('span', 'nav-btn disabled', '\u2190 Sebelumnya'));
        top.appendChild(navTop);
        box.appendChild(top);
        // judul
        box.appendChild(el('h1', 'reader-title',
          c.title || ('Chapter ' + (numText(c.num) || ''))));
        if (c.date){
          var dline = el('div', 'reader-date');
          dline.appendChild(document.createTextNode('Dipublikasikan: '));
          var sp = el('span', 'ch-dt', fmtDt(c.date));
          sp.setAttribute('data-date', c.date);
          sp.setAttribute('data-fmt', fmtDt(c.date));
          dline.appendChild(sp);
          box.appendChild(dline);
        }
        var content = el('div', 'reader-content');
        var imgs = (c.images && c.images.length) ? c.images : [];
        if (imgs.length){
          var wrap = el('div', 'reader-images');
          var broken = 0;
          var fallbackAdded = false;
          function addBacaSumber(){
            if (fallbackAdded || !c.external) return;
            fallbackAdded = true;
            var srcdiv = el('div', 'reader-src');
            srcdiv.appendChild(el('p', null,
              'Gambar tidak dapat dimuat dari CDN. Baca di sumber sebagai gantinya.'));
            var srcA = el('a', 'baca-btn', 'Baca di Sumber \u2192');
            srcA.href = c.external; srcA.target = '_blank'; srcA.rel = 'nofollow noopener';
            srcdiv.appendChild(srcA);
            content.appendChild(srcdiv);
          }
          imgs.forEach(function(u){
            var img = document.createElement('img');
            img.src = u; img.alt = c.title || 'Bab';
            img.loading = 'lazy'; img.decoding = 'async';
            img.setAttribute('referrerpolicy', 'no-referrer');
            img.addEventListener('error', function(){
              // Gambar CDN rusak/kedaluwarsa: sembunyikan; bila SEMUA gambar
              // gagal, tampilkan tombol fallback ke halaman sumber.
              this.style.display = 'none';
              broken++;
              if (broken === imgs.length) addBacaSumber();
            });
            wrap.appendChild(img);
          });
          content.appendChild(wrap);
          if (c.external){
            var info = el('p', 'reader-info');
            info.innerHTML = 'Gambar ditayangkan dari CDN sumber. Bila rusak, ' +
              'baca langsung di <a target="_blank" rel="nofollow noopener" ' +
              'href="' + escA(c.external) + '">sumber resmi</a>.';
            content.appendChild(info);
          }
        } else if (c.external){
          var srcdiv = el('div', 'reader-src');
          srcdiv.appendChild(el('p', null, 'Halaman gambar belum tersedia di sini.'));
          var srcA = el('a', 'baca-btn', 'Baca di Sumber \u2192');
          srcA.href = c.external; srcA.target = '_blank'; srcA.rel = 'nofollow noopener';
          srcdiv.appendChild(srcA);
          content.appendChild(srcdiv);
        } else {
          content.appendChild(el('p', null, 'Konten kosong.'));
        }
        box.appendChild(content);
        // nav bawah
        var navBot = el('div', 'reader-nav');
        if (idx < sortedChs.length - 1){ navBot.appendChild(navLink(sortedChs[idx + 1], 'Berikutnya \u2192')); }
        else navBot.appendChild(el('span', 'nav-btn disabled', 'Berikutnya \u2192'));
        box.appendChild(navBot);
        showBox(box);
        try {
          var base = (document.title || '').split(' - ').pop() || 'Mfmam';
          document.title = (c.title || 'Chapter') + ' - ' + (d.title || slug) + ' - ' + base;
        } catch(e){}
      });
    }
    function showSeries(){
      sortedChs = null;
      readerShell.hidden = true;
      readerShell.innerHTML = '';
      seriPage.hidden = false;
    }
    function routeReader(){
      var key = fromHash();
      if (key != null){ showChapter(key); }
      else { showSeries(); }
    }
    window.addEventListener('hashchange', routeReader);
    routeReader();
  }
})();