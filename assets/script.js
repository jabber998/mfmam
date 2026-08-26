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

  // cari bab (halaman daftar manhwa): temukan bab yang ingin dibaca
  var cfx = document.getElementById('chap-search-form'),
      cix = document.getElementById('chap-search-input'),
      crx = document.getElementById('chap-search-results');
  if (cfx && cix && crx) {
    var chapterIndex = [];
    fetch('/chapters-index.json').then(function(r){ return r.json(); })
      .then(function(d){ chapterIndex = d || []; })
      .catch(function(){});
    function tokenHit(token, d){
      if (!token) return true;
      if (/^\d+$/.test(token)){
        var ts = d.n != null ? String(d.n) : '';
        return ts === token || (ts.length > token.length && ts.indexOf(token) === 0);
      }
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
})();