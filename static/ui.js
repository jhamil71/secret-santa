/* Shared page behaviour: autosaving fields, in-place updates, toasts.
 *
 *  - <form data-autosave> saves by itself while you type / pick (JSON reply), showing a toast.
 *  - Any other POST form inside <main> is sent in the background and the page content is swapped in
 *    place, keeping scroll position and open <details id=...> panels. data-refocus="#sel" refocuses a field.
 *  - Server flash messages become toasts. Without JavaScript everything still works as plain forms.
 */
(function () {
  var main = document.querySelector('main'), toast = document.getElementById('toast'), tt;
  if (!main || !toast) return;

  function say(msg, bad) {
    toast.textContent = msg;
    toast.className = 'toast show' + (bad ? ' bad' : '');
    clearTimeout(tt);
    tt = setTimeout(function () { toast.className = 'toast'; }, bad ? 4500 : Math.min(1500 + msg.length * 30, 6000));
  }
  window.UI = { say: say };

  function flashesToToasts() {
    var f = main.querySelectorAll('.flash'), last = null, i;
    for (i = 0; i < f.length; i++) { last = f[i]; }
    if (last) { say(last.textContent.trim(), last.classList.contains('error')); }
    for (i = 0; i < f.length; i++) { f[i].remove(); }
  }
  flashesToToasts();

  function swap(html, refocus) {
    var doc = new DOMParser().parseFromString(html, 'text/html'), nm = doc.querySelector('main');
    if (!nm) { location.reload(); return; }
    var open = [].map.call(main.querySelectorAll('details[open][id]'), function (d) { return d.id; });
    main.innerHTML = nm.innerHTML;
    open.forEach(function (id) { var d = document.getElementById(id); if (d) d.open = true; });
    flashesToToasts();
    if (refocus) { var el = main.querySelector(refocus); if (el) el.focus(); }
    document.dispatchEvent(new CustomEvent('swapped'));
  }

  function saveNow(f) {
    fetch(f.action, {
      method: 'POST', body: new FormData(f), credentials: 'same-origin',
      headers: { 'X-Requested-With': 'fetch', 'Accept': 'application/json' }
    }).then(function (r) {
      if (r.status === 400 && (r.headers.get('Content-Type') || '').indexOf('json') < 0) {
        say('Your session expired. Refresh the page.', true); return;
      }
      if (r.status === 404) { say('That item is gone. Refresh the page.', true); return; }
      return r.json().then(function (d) {
        say(d.ok ? 'Saved ✓' : (d.error || 'Could not save.'), !d.ok);
        document.dispatchEvent(new CustomEvent('autosaved', { detail: { form: f, data: d } }));
      });
    }).catch(function () { say('Could not save. Check your connection.', true); });
  }

  var timers = new WeakMap();
  function queue(f, delay) { clearTimeout(timers.get(f)); timers.set(f, setTimeout(function () { saveNow(f); }, delay)); }
  function autosaveForm(el) { var f = el.form; return f && f.hasAttribute('data-autosave') ? f : null; }

  document.addEventListener('input', function (e) {          // typing: save shortly after you pause
    var f = autosaveForm(e.target);
    if (f && /^(text|tel|url)$/.test(e.target.type)) queue(f, 700);
  });
  document.addEventListener('change', function (e) {         // picking / ticking / leaving a field: save now
    var f = autosaveForm(e.target);
    if (f) queue(f, 0);
  });

  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (e.defaultPrevented || !f.closest || !f.closest('main')) return;   // e.g. confirm() was cancelled
    e.preventDefault();
    if (f.hasAttribute('data-autosave')) { clearTimeout(timers.get(f)); saveNow(f); return; }
    fetch(f.action, { method: 'POST', body: new FormData(f), credentials: 'same-origin',
                      headers: { 'X-Requested-With': 'fetch' } })
      .then(function (r) {
        if (r.redirected && /\/admin\/login/.test(r.url)) { location.href = r.url; return; }
        if (r.status === 400) { say('Your session expired. Refresh the page.', true); return; }
        return r.text().then(function (t) { swap(t, f.getAttribute('data-refocus')); });
      })
      .catch(function () { say('Connection problem. Try again.', true); });
  });
})();
