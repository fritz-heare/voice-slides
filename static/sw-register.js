"use strict";
// Registration and the update prompt, shared by both views.
//
// An installed PWA can hold a worker for days, so a deploy is invisible until
// three things happen: the browser re-fetches sw.js (the server answers it
// no-cache), the bytes differ (the VERSION substitution moved), and the waiting
// worker activates. The last one is the user's call — a reload mid-talk is
// worse than an old deck — so a waiting worker raises a toast instead.
//
// The visible version badge is filled from <meta name="app-version">, which is
// the only place the number appears in a page.

(function () {
  var meta = document.querySelector('meta[name="app-version"]');
  var version = meta ? meta.getAttribute('content') : '';

  document.addEventListener('DOMContentLoaded', function () {
    Array.prototype.forEach.call(document.querySelectorAll('[data-app-version]'), function (el) {
      el.textContent = 'v' + version;
    });
  });

  if (!('serviceWorker' in navigator)) return;

  var swHadController = !!navigator.serviceWorker.controller;
  var swReloading = false;
  var swToastEl = null;

  function swToast(reg) {
    if (swToastEl) return;
    var el = document.createElement('div');
    el.className = 'swtoast';
    el.setAttribute('role', 'status');
    el.innerHTML = '<span>Update available</span>'
      + '<button type="button" class="swgo">Reload</button>'
      + '<button type="button" class="swdismiss" aria-label="Dismiss">\u00D7</button>';
    document.body.appendChild(el);
    swToastEl = el;
    el.querySelector('.swgo').addEventListener('click', function () {
      var w = reg.waiting || reg.installing;
      el.querySelector('.swgo').textContent = 'Updating';
      if (w) w.postMessage({ type: 'SKIP_WAITING' });
      else if (!swReloading) { swReloading = true; location.reload(); }
    });
    el.querySelector('.swdismiss').addEventListener('click', function () {
      el.parentNode.removeChild(el);
      swToastEl = null;
    });
  }

  // One reload per controller change, and none at all on the first install,
  // where clients.claim() would otherwise bounce a page just opened.
  navigator.serviceWorker.addEventListener('controllerchange', function () {
    if (swReloading || !swHadController) return;
    swReloading = true;
    location.reload();
  });

  window.addEventListener('load', function () {
    navigator.serviceWorker.register('sw.js', { scope: './' }).then(function (reg) {
      if (reg.waiting && navigator.serviceWorker.controller) swToast(reg);
      reg.addEventListener('updatefound', function () {
        var nw = reg.installing;
        if (!nw) return;
        nw.addEventListener('statechange', function () {
          if (nw.state === 'installed' && navigator.serviceWorker.controller) swToast(reg);
        });
      });
      // An open presenter window can sit untouched for a whole talk.
      document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible') reg.update();
      });
      setInterval(function () { reg.update(); }, 20 * 60 * 1000);
    }).catch(function (e) {
      console.warn('sw registration failed', e);
    });
  });
})();
