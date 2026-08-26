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
    inp.addEventListener('input', function(){
      var q = inp.value.trim().toLowerCase();
      res.innerHTML = '';
      if (q.length < 2) return;
      var out = index.filter(function(d){
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
    });
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
          if (/^\d+$/.test(t)){
            return num === t || (num.length > t.length && num.indexOf(t) === 0);
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
        if (/^\d+$/.test(token)) return ts === token || (ts.length > token.length && ts.indexOf(token) === 0);
        if (/^\d+\.\d+$/.test(token)) return d.n != null && String(d.n) === token;
        var hay = (d.s + ' ' + d.t).toLowerCase();
        return hay.indexOf(token) !== -1;
      }
      function renderChap(q){
        var tokens = (q || '').trim().toLowerCase().split(/\s+/).filter(Boolean);
        crx.innerHTML = '';
        if (!tokens.length){ crx.hidden = true; return; }
        var res = chapterIndex.filter(function(d){
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
    }
  }
})();