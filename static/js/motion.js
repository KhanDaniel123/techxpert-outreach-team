/* ==========================================================================
   TechXpert Outreach - motion layer (progressive enhancement)
   --------------------------------------------------------------------------
   Vanilla JS, no dependencies. Enhances what the server already rendered:
   staggered entrances, count-up stats, an animated discovery progress bar,
   and scroll-reveal for timeline/activity rows. If anything here throws,
   the page simply stays as the server rendered it.
   ========================================================================== */
(function () {
  'use strict';

  var doc = document.documentElement;

  function unpre() { doc.classList.remove('mx-pre'); }

  var reduced = false;
  try {
    reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) { /* matchMedia unavailable: assume motion is fine */ }
  if (reduced) { unpre(); return; }

  unpre();
  doc.classList.add('mx-anim');

  /* ---- 1. staggered entrances: nav, hero, stats, cards, in DOM order ------ */
  try {
    var seq = doc.querySelectorAll('nav, .hero, .stats .stat, body > .card, .row .card, .authwrap');
    var n = 0;
    for (var i = 0; i < seq.length; i++) {
      var el = seq[i];
      if (el.classList.contains('mx-stagger')) continue;
      el.classList.add('mx-stagger');
      el.style.setProperty('--mx-d', String(Math.min(n, 14)));
      n++;
    }
  } catch (e) { /* never break the page for an animation */ }

  /* ---- 2. animated stat counters ------------------------------------------ */
  function countUp(el) {
    var raw = (el.textContent || '').trim().replace(/,/g, '');
    if (!/^\d+$/.test(raw)) return;          /* only plain integers */
    var target = parseInt(raw, 10);
    if (target <= 0) return;
    var dur = 900, t0 = null;
    function frame(t) {
      if (t0 === null) t0 = t;
      var p = Math.min(1, (t - t0) / dur);
      var eased = 1 - Math.pow(1 - p, 3);    /* easeOutCubic */
      el.textContent = String(Math.round(target * eased));
      if (p < 1) {
        window.requestAnimationFrame(frame);
      } else {
        el.textContent = String(target);     /* land exactly */
      }
    }
    window.requestAnimationFrame(frame);
  }
  try {
    var nums = doc.querySelectorAll('.stat .n');
    for (var i = 0; i < nums.length; i++) countUp(nums[i]);
  } catch (e) {}

  /* ---- 3. discovery progress bar from the "today: X/Y" pill --------------- */
  try {
    var pills = doc.querySelectorAll('.pill');
    for (var i = 0; i < pills.length; i++) {
      var m = /today:\s*(\d+)\s*\/\s*(\d+)/i.exec(pills[i].textContent || '');
      if (!m) continue;
      var done = parseInt(m[1], 10);
      var total = parseInt(m[2], 10);
      if (!total) break;
      var pct = Math.max(0, Math.min(100, Math.round(done / total * 100)));
      var host = pills[i].closest('p');
      if (host && host.parentNode && !host.parentNode.querySelector('.mx-pbar')) {
        var bar = doc.createElement('div');
        bar.className = 'mx-pbar';
        bar.setAttribute('role', 'progressbar');
        bar.setAttribute('aria-valuenow', String(pct));
        bar.setAttribute('aria-valuemin', '0');
        bar.setAttribute('aria-valuemax', '100');
        bar.setAttribute('aria-label', 'New leads discovered today');
        var track = doc.createElement('div');
        track.className = 'mx-track';
        var fill = doc.createElement('div');
        fill.className = 'mx-fill';
        track.appendChild(fill);
        var label = doc.createElement('div');
        label.className = 'mx-label';
        label.textContent = done + ' of ' + total + ' new leads today (' + pct + '%)';
        bar.appendChild(track);
        bar.appendChild(label);
        host.parentNode.insertBefore(bar, host.nextSibling);
        /* two rAFs so the CSS transition runs from 0 to the target width */
        window.requestAnimationFrame(function () {
          window.requestAnimationFrame(function () { fill.style.width = pct + '%'; });
        });
      }
      break; /* one progress bar per page */
    }
  } catch (e) {}

  /* ---- 4. timeline / activity rows reveal on scroll ------------------------ */
  try {
    var cards = doc.querySelectorAll('.card');
    var rows = [];
    for (var i = 0; i < cards.length; i++) {
      var h = cards[i].querySelector('h2');
      if (!h) continue;
      var t = (h.textContent || '').trim().toLowerCase();
      if (t !== 'timeline' && t !== 'recent activity') continue;
      var divs = cards[i].querySelectorAll('div[style*="display:flex"]');
      for (var j = 0; j < divs.length; j++) rows.push(divs[j]);
    }
    if (rows.length && 'IntersectionObserver' in window) {
      for (var k = 0; k < rows.length; k++) {
        rows[k].classList.add('mx-row');
        rows[k].style.transitionDelay = (Math.min(k % 8, 8) * 60) + 'ms';
      }
      var io = new IntersectionObserver(function (entries) {
        for (var i = 0; i < entries.length; i++) {
          if (entries[i].isIntersecting) {
            entries[i].target.classList.add('mx-in');
            io.unobserve(entries[i].target);
          }
        }
      }, { threshold: 0.08 });
      for (var k = 0; k < rows.length; k++) io.observe(rows[k]);
    }
  } catch (e) {}
})();
