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
})();