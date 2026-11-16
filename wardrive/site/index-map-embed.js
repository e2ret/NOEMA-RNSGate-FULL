// Extracted from /var/www/rns.moscow/htdocs/index.html -- the block that drives the
// embedded coverage map (theme and label language). Kept here so the map side and the
// parent side of the postMessage contract are reviewable together; the site itself is
// not otherwise in this repo.

  // ── Theme ─────────────────────────────────────────────────
  (function () {
    function applyTheme(t) {
      document.documentElement.setAttribute('data-theme', t);
      localStorage.setItem('theme', t);
      var btn = document.getElementById('theme-toggle');
      if (btn) btn.textContent = t === 'dark' ? '☀' : '☽';
      var meta = document.querySelector('meta[name="theme-color"]');
      if (meta) meta.setAttribute('content', t === 'dark' ? '#0a0e0a' : '#f2f5f2');
      var mapFrame = document.querySelector('.map-frame-wrapper iframe');
      if (mapFrame) mapFrame.contentWindow.postMessage(t, '*');
    }
    window.toggleTheme = function () {
      applyTheme((document.documentElement.getAttribute('data-theme') || 'dark') === 'dark' ? 'light' : 'dark');
    };
    applyTheme(document.documentElement.getAttribute('data-theme') || 'dark');

    var mapFrame = document.querySelector('.map-frame-wrapper iframe');
    if (mapFrame) {
      mapFrame.addEventListener('load', function () {
        var t = document.documentElement.getAttribute('data-theme') || 'dark';
        mapFrame.contentWindow.postMessage(t, '*');
        // setLang usually runs before the lazy iframe exists, so resend the language
        // once it is actually there.
        if (window.getLang) {
          mapFrame.contentWindow.postMessage({ type: 'lang', lang: window.getLang() },
                                             'https://map.rns.moscow');
        }
      });
    }
  })();
