// ==UserScript==
// @name         Pomocnik rezerwacji – Auschwitz-Birkenau
// @namespace    visit-auschwitz-autofill
// @version      4.0.1
// @description  Szablony formularza „Zwiedzanie grupowe”, dziennik zgłoszeń i automatyczne ponowne wypełnienie po odrzuceniu (współpracuje z programem Pomocnik rezerwacji). CAPTCHA i „Wyślij” zostają dla człowieka.
// @match        https://visit.auschwitz.org/formularz.html*
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_xmlhttpRequest
// @grant        GM_notification
// @connect      127.0.0.1
// @updateURL    http://127.0.0.1:47631/pomocnik.user.js
// @downloadURL  http://127.0.0.1:47631/pomocnik.user.js
// ==/UserScript==

(function () {
  'use strict';

  const $ = window.jQuery || (typeof unsafeWindow !== 'undefined' && unsafeWindow.jQuery);
  const form = document.getElementById('form_zgloszenie');
  if (!form || !$) return;

  const P = 'form_zgloszenie_';
  const TPL_KEY = 'szablony';
  const TPL_AUTO_KEY = 'szablon_automat';   // szablon używany, gdy dla odrzuconego dnia nie ma zapisanej kopii formularza
  const LOG_KEY = 'zgloszenia';             // { 'RRRR-MM-DD': { status, proba, szablon, godzina, wyslano, formularz } }
  const LAST_DATE_KEY = 'ostatnia_data';
  const DONE_KEY = 'obsluzone_zdarzenia';   // { idZdarzenia: znacznikCzasu }
  const CLAIM_KEY = 'zajete_zdarzenia';     // { idZdarzenia: { karta, czas } } – żeby dwie karty nie wypełniały tego samego
  const MIN_KEY = 'panel_zwiniety';
  const HELPER = 'http://127.0.0.1:47631';
  const TAB_ID = Math.random().toString(36).slice(2);

  const STATUS = {
    wyslane:      { nazwa: 'Czeka',        klasa: 'wait' },
    przydzielone: { nazwa: 'Przydzielone', klasa: 'ok' },
    odrzucone:    { nazwa: 'Odrzucone',    klasa: 'err' },
  };

  // Pola, których nie zapisujemy w szablonie (data jest wybierana osobno, reszta to CAPTCHA/techniczne).
  const SKIP = new Set([
    P + 'data', P + 'data2', P + 'wartosc', P + 'submit', P + 'submit_type',
    P + 'idgrupywydarzen', 'g-recaptcha-response',
  ]);
  // Pola wpływające na cenę/kształt formularza – ustawiamy je najpierw.
  const FIRST = [P + 'id_tematu', P + 'id_jezyka', P + 'grupa_szkolna', P + 'kraj'];

  const load = (k, d) => { try { return JSON.parse(GM_getValue(k, JSON.stringify(d))); } catch { return d; } };
  const save = (k, v) => GM_setValue(k, JSON.stringify(v));
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const norm = s => String(s).toLowerCase().replace(/\s+/g, ' ').trim();
  const addDays = (iso, n) => { const d = new Date(iso + 'T12:00:00'); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); };
  const dayDiff = (a, b) => Math.round((new Date(b + 'T12:00:00') - new Date(a + 'T12:00:00')) / 864e5);
  const plDate = iso => iso.split('-').reverse().join('.');
  const weekday = iso => ['nd', 'pn', 'wt', 'śr', 'cz', 'pt', 'sb'][new Date(iso + 'T12:00:00').getDay()];
  const today = () => new Date().toISOString().slice(0, 10);

  const ICON = {
    check: '<svg viewBox="0 0 16 16" width="14" height="14"><path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    x: '<svg viewBox="0 0 16 16" width="14" height="14"><path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
    trash: '<svg viewBox="0 0 16 16" width="14" height="14"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    min: '<svg viewBox="0 0 16 16" width="14" height="14"><path d="M4 8h8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
    max: '<svg viewBox="0 0 16 16" width="14" height="14"><path d="M4 10l4-4 4 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  };

  let biezaceZdarzenie = null; // odrzucenie, które właśnie obsługujemy w tej karcie

  // ---------- Dziennik zgłoszeń ----------

  const getLog = () => load(LOG_KEY, {});
  function setEntry(date, patch) {
    const log = getLog();
    if (patch === null) delete log[date];
    else log[date] = { ...(log[date] || {}), ...patch };
    save(LOG_KEY, log);
    refreshAll();
  }

  // Zapis „wysłane” w momencie kliknięcia „Wyślij” – tylko gdy formularz przejdzie walidację i CAPTCHA jest zaznaczona.
  form.addEventListener('submit', () => {
    const date = form[P + 'data'].value;
    const captchaOk = (form['g-recaptcha-response'] || {}).value;
    const valid = typeof $(form).valid === 'function' ? $(form).valid() : true;
    const blad = $('#blad-formularza').html();
    if (!date || !captchaOk || !valid || blad) return;

    const log = getLog();
    const cur = log[date];
    // Nowy dzień albo ponowne zapytanie po odrzuceniu. Wpisów „czeka”/„przydzielone” nie nadpisujemy.
    if (!cur || cur.status === 'odrzucone') {
      log[date] = {
        ...(cur || {}),
        status: 'wyslane',
        proba: ((cur && cur.proba) || 0) + 1,
        szablon: biezaceZdarzenie ? 'po odrzuceniu' : (selTpl.value || ''),
        godzina: form[P + 'godzina'].value,
        wyslano: new Date().toISOString(),
        formularz: readForm(), // kopia wszystkich pól – użyjemy jej przy ewentualnym kolejnym odrzuceniu
      };
      save(LOG_KEY, log);
    }
    save(LAST_DATE_KEY, date);
    if (biezaceZdarzenie) markDone(biezaceZdarzenie.id);
  });

  // ---------- Odczyt / zapis formularza ----------

  function readForm() {
    const fields = {};
    for (const el of form.elements) {
      if (!el.name || SKIP.has(el.name) || el.type === 'submit' || el.type === 'button') continue;
      fields[el.name] = el.type === 'checkbox' ? el.checked : el.value;
    }
    const kontrahenci = [...document.querySelectorAll('.faktura-dane')].map(box => ({
      typ: box.dataset.typ,
      id: $(box).find('.kontrahent.selected').data('id') ?? null,
    })).filter(k => k.id != null);

    const d1 = form[P + 'data'].value, d2 = form[P + 'data2'].value;
    const offset2 = d1 && d2 ? dayDiff(d1, d2) : 1;
    return { fields, kontrahenci, offset2 };
  }

  function setField(name, value) {
    const el = form.elements[name];
    if (!el) return;
    if (el.type === 'checkbox') {
      if (el.checked !== !!value) { el.checked = !!value; $(el).trigger('change'); }
    } else {
      $(el).val(value).trigger('change').trigger('blur');
    }
  }

  // Wartość opcji w liście rozwijanej po jej tekście (np. „angielski” → "2").
  function optionByText(name, text) {
    const el = form.elements[name];
    if (!el || !text) return null;
    const exact = [...el.options].find(o => norm(o.dataset.orig || o.text) === norm(text));
    return exact ? exact.value : null;
  }

  async function fillForm(tpl, date, silent) {
    const f = tpl.fields;
    for (const name of FIRST) if (name in f) setField(name, f[name]);
    await sleep(600); // czas na przeliczenie ceny / pokazanie pól dnia drugiego
    for (const [name, value] of Object.entries(f)) if (!FIRST.includes(name)) setField(name, value);

    if (f[P + 'faktura']) {
      for (const k of tpl.kontrahenci || []) {
        const el = $(`.faktura-dane[data-typ="${k.typ}"] .kontrahent[data-id="${k.id}"]`);
        if (el.length && !el.hasClass('selected')) el.trigger('click');
      }
    }

    const dSel = form[P + 'data'];
    if (![...dSel.options].some(o => o.value === date)) dSel.add(new Option(plDate(date), date));
    setField(P + 'data', date);
    const d2 = form[P + 'data2'];
    if (d2 && $(d2).is(':visible')) {
      const second = addDays(date, tpl.offset2 || 1);
      if ([...d2.options].some(o => o.value === second)) setField(P + 'data2', second);
    }

    save(LAST_DATE_KEY, date);
    await sleep(400);
    if (typeof window.PrzeliczWartosc === 'function') window.PrzeliczWartosc();

    const missing = checkMissing();
    const cap = document.querySelector('.g-recaptcha') || form[P + 'submit'];
    cap.scrollIntoView({ behavior: 'smooth', block: 'center' });
    if (!silent) status(missing.length
      ? 'Brakuje: ' + missing.join(', ')
      : `Wypełniono na ${plDate(date)}. Zaznacz CAPTCHA i kliknij „Wyślij”.`, missing.length ? 'err' : 'ok');
    return missing;
  }

  function checkMissing() {
    const labels = { id_tematu: 'temat', id_jezyka: 'język', data: 'data', godzina: 'godzina',
      data2: 'data (dzień 2)', godzina2: 'godzina (dzień 2)', liczba_osob_doroslych: 'liczba osób powyżej 26 lat',
      liczba_osob_mlodziez: 'liczba osób poniżej 26 lat', kraj: 'kraj', akceptacja2: 'akceptacja regulaminu' };
    return Object.keys(labels).filter(k => {
      const el = form.elements[P + k];
      if (!el || !$(el).is(':visible')) return false;
      return el.type === 'checkbox' ? !el.checked : el.value === '';
    }).map(k => labels[k]);
  }

  // ---------- Współpraca z programem Pomocnik rezerwacji ----------

  function helper(path) {
    return new Promise(resolve => {
      if (typeof GM_xmlhttpRequest !== 'function') return resolve(null);
      GM_xmlhttpRequest({
        method: 'GET', url: HELPER + path, timeout: 3000,
        onload: r => { try { resolve(JSON.parse(r.responseText)); } catch { resolve(null); } },
        onerror: () => resolve(null), ontimeout: () => resolve(null),
      });
    });
  }

  function markDone(id) {
    const done = load(DONE_KEY, {});
    done[id] = Date.now();
    const keys = Object.keys(done).sort((a, b) => done[a] - done[b]);
    keys.slice(0, Math.max(0, keys.length - 300)).forEach(k => delete done[k]);
    save(DONE_KEY, done);
  }

  async function checkHelper() {
    const s = await helper('/status');
    const el = q('.pr-conn');
    if (!s) {
      el.className = 'pr-conn off';
      el.innerHTML = '<i></i><span>Program nie działa – uruchom Pomocnika rezerwacji</span>';
    } else if (!s.polaczono) {
      el.className = 'pr-conn warn';
      el.innerHTML = `<i></i><span>Brak połączenia z pocztą${s.blad ? ': ' + esc(s.blad) : ''}</span>`;
    } else {
      const t = s.ostatnie_sprawdzenie ? new Date(s.ostatnie_sprawdzenie).toLocaleTimeString('pl-PL') : '–';
      el.className = 'pr-conn ok';
      el.innerHTML = `<i></i><span>Czuwa nad ${esc(s.login)} · ${t}</span>`;
    }
  }

  // Po otwarciu strony: czy jest nieobsłużone odrzucenie? Jeśli tak – wypełnij formularz tym samym terminem.
  async function autoProcess() {
    const res = await helper('/zdarzenia');
    if (!res) return;
    const done = load(DONE_KEY, {});
    const claims = load(CLAIM_KEY, {});
    const now = Date.now();
    const wanted = (location.hash.match(/pomocnik=([\w-]+)/) || [])[1];

    const inne = res.zdarzenia.filter(e => e.typ === 'inny' && !done[e.id]);
    if (inne.length) showInfo(inne);

    const open = res.zdarzenia.filter(e =>
      e.typ === 'odrzucenie' && !done[e.id] && now - Date.parse(e.czas) < 12 * 3600e3);
    const free = e => !claims[e.id] || claims[e.id].karta === TAB_ID || now - claims[e.id].czas > 10 * 60e3;
    const ev = (wanted && open.find(e => e.id === wanted)) || open.find(free);
    if (!ev) return;

    claims[ev.id] = { karta: TAB_ID, czas: now };
    save(CLAIM_KEY, claims);
    history.replaceState(null, '', location.pathname + location.search);
    await handleRejection(ev, open.length - 1);
  }

  async function handleRejection(ev, pozostale) {
    biezaceZdarzenie = ev;
    const log = getLog();
    const cur = log[ev.data] || {};
    setEntry(ev.data, { ...cur, status: 'odrzucone', proba: cur.proba || 1, odrzucono: ev.czas, godzina: cur.godzina || ev.godzina });

    // Źródło danych: kopia formularza z pierwotnego zapytania → albo szablon automatu → albo pierwszy szablon.
    const tpls = load(TPL_KEY, {});
    // Bez szablonu i tak wypełniamy to, co wiadomo z maila (temat, język, data, godzina).
    const base = cur.formularz || tpls[load(TPL_AUTO_KEY, '')] || Object.values(tpls)[0];
    const tpl = base ? JSON.parse(JSON.stringify(base)) : { fields: {}, kontrahenci: [], offset2: 1 };
    const f = tpl.fields;
    const temat = optionByText(P + 'id_tematu', ev.rodzaj);
    const jezyk = optionByText(P + 'id_jezyka', ev.jezyk);
    if (temat) f[P + 'id_tematu'] = temat;
    if (jezyk) f[P + 'id_jezyka'] = jezyk;
    f[P + 'godzina'] = ev.godzina;

    const missing = await fillForm(tpl, ev.data, true);
    const uwagi = [];
    if (!base) uwagi.push('brak zapisanego szablonu – uzupełnij brakujące pola, a po wysłaniu zapisz formularz jako szablon');
    if (ev.rodzaj && !temat) uwagi.push(`nie znaleziono rodzaju „${ev.rodzaj}” – sprawdź temat`);
    if (!jezyk) uwagi.push(`nie znaleziono języka „${ev.jezyk}” – sprawdź język`);
    if (missing.length) uwagi.push('brakuje: ' + missing.join(', '));
    if (pozostale > 0) uwagi.push(`czekają jeszcze inne odrzucenia (${pozostale}) – po wysłaniu odśwież stronę`);
    banner(ev, uwagi.join('; '));
    alarm(ev);
  }

  function banner(ev, uwaga) {
    let b = document.getElementById('pr-banner');
    if (!b) {
      b = document.createElement('div');
      b.id = 'pr-banner';
      document.body.prepend(b);
    }
    b.innerHTML = `
      <div class="pr-b-body">
        <div class="pr-b-title">Odrzucono termin ${plDate(ev.data)}, ${esc(ev.godzina)} · ${esc(ev.jezyk)}</div>
        <div class="pr-b-text">Formularz wypełniono ponownie tym samym terminem. Zaznacz „Nie jestem robotem” i kliknij „Wyślij”.</div>
        ${uwaga ? `<div class="pr-b-note">${esc(uwaga)}</div>` : ''}
      </div>
      <button type="button" class="pr-b-skip">Pomiń</button>`;
    panel.style.top = (b.offsetHeight + 16) + 'px'; // panel pod paskiem, żeby nie zasłaniał „Pomiń”
    b.querySelector('.pr-b-skip').onclick = () => {
      markDone(ev.id);
      biezaceZdarzenie = null;
      b.remove();
      panel.style.top = '';
      status(`Pominięto odrzucenie z ${plDate(ev.data)}.`, 'ok');
    };
  }

  function alarm(ev) {
    // Krótki sygnał w przeglądarce (może być zablokowany – główny alarm gra program na komputerze).
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      [0, 0.35, 0.7].forEach(t => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
        g.gain.setValueAtTime(0.2, ctx.currentTime + t);
        o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.18);
      });
    } catch { /* bez dźwięku */ }
    if (typeof GM_notification === 'function') {
      GM_notification({ title: `Odrzucono ${plDate(ev.data)}, ${ev.godzina}`,
        text: 'Formularz jest gotowy – zaznacz CAPTCHA i wyślij.', onclick: () => window.focus() });
    }
    const orig = document.title;
    let n = 0;
    const t = setInterval(() => {
      document.title = n++ % 2 ? orig : 'Odrzucenie – wyślij ponownie';
      if (n > 60 || document.hasFocus()) { clearInterval(t); document.title = orig; }
    }, 800);
  }

  function showInfo(inne) {
    const el = q('.pr-info');
    el.hidden = false;
    el.innerHTML = '<div class="pr-info-h">Nowe wiadomości z muzeum</div>' + inne.map(e =>
      `<div class="pr-info-row"><span>${esc(e.temat_maila)}</span><button type="button" class="pr-icon" data-id="${esc(e.id)}" title="Ukryj">${ICON.x}</button></div>`).join('');
    el.onclick = e => {
      const b = e.target.closest('button[data-id]');
      if (!b) return;
      markDone(b.dataset.id);
      b.parentElement.remove();
      if (!el.querySelector('.pr-info-row')) el.hidden = true;
    };
  }

  // Podtrzymanie sesji: co 10 min ciche zapytanie do strony; jeśli sesja wygasła – ostrzeżenie.
  setInterval(async () => {
    try {
      const r = await fetch('/formularz.html', { credentials: 'same-origin', cache: 'no-store' });
      if (/login/.test(r.url)) {
        status('Sesja wygasła – odśwież stronę i zaloguj się ponownie.', 'err');
        if (typeof GM_notification === 'function')
          GM_notification({ title: 'Sesja wygasła', text: 'Zaloguj się ponownie na visit.auschwitz.org.' });
      }
    } catch { /* brak sieci */ }
  }, 10 * 60e3);

  // ---------- Wygląd ----------

  const css = document.createElement('style');
  css.textContent = `
    #pr, #pr-banner { --pr-bg:#fff; --pr-text:#1d1d1b; --pr-muted:#6b6b66; --pr-line:#e3e2de; --pr-soft:#f5f5f3;
      --pr-accent:#1d1d1b; --pr-accent-text:#fff; --pr-gold:#a8843f;
      --pr-ok:#2f7d4f; --pr-ok-bg:#e7f3ec; --pr-wait:#9a6b12; --pr-wait-bg:#fbf1dc; --pr-err:#b3372f; --pr-err-bg:#fbe9e7;
      font:13px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; color:var(--pr-text); }
    #pr *, #pr-banner * { box-sizing:border-box; font-family:inherit; }
    #pr { position:fixed; top:16px; right:16px; z-index:99999; width:340px; max-height:calc(100vh - 32px); overflow:auto;
      background:var(--pr-bg); border:1px solid var(--pr-line); border-radius:12px; box-shadow:0 10px 30px rgba(0,0,0,.12), 0 2px 6px rgba(0,0,0,.06); }
    #pr .pr-head { display:flex; align-items:center; justify-content:space-between; padding:12px 14px; border-bottom:1px solid var(--pr-line); }
    #pr .pr-title { font-weight:600; font-size:14px; letter-spacing:-.01em; }
    #pr .pr-title small { color:var(--pr-gold); font-weight:500; margin-left:6px; }
    #pr.min .pr-body { display:none; }
    #pr.min .pr-head { border-bottom:0; }
    #pr .pr-body { padding:12px 14px 14px; }
    #pr .pr-conn { display:flex; gap:8px; align-items:center; font-size:12px; color:var(--pr-muted); padding:8px 10px; background:var(--pr-soft); border-radius:8px; margin-bottom:10px; }
    #pr .pr-conn i { width:8px; height:8px; border-radius:50%; background:var(--pr-muted); flex:none; }
    #pr .pr-conn.ok i { background:var(--pr-ok); box-shadow:0 0 0 3px var(--pr-ok-bg); }
    #pr .pr-conn.warn i { background:var(--pr-wait); box-shadow:0 0 0 3px var(--pr-wait-bg); }
    #pr .pr-conn.off i { background:var(--pr-err); box-shadow:0 0 0 3px var(--pr-err-bg); }
    #pr .pr-info { font-size:12px; padding:8px 10px; border:1px solid var(--pr-line); border-radius:8px; margin-bottom:10px; }
    #pr .pr-info-h { font-weight:600; margin-bottom:4px; }
    #pr .pr-info-row { display:flex; gap:8px; align-items:center; padding:3px 0; }
    #pr .pr-info-row span { flex:1; }
    #pr .pr-seg { display:flex; background:var(--pr-soft); border-radius:8px; padding:3px; margin-bottom:12px; }
    #pr .pr-seg button { flex:1; border:0; background:none; padding:6px; border-radius:6px; font-size:12px; font-weight:500; color:var(--pr-muted); cursor:pointer; }
    #pr .pr-seg button.on { background:var(--pr-bg); color:var(--pr-text); box-shadow:0 1px 2px rgba(0,0,0,.08); }
    #pr label { display:block; font-size:12px; font-weight:500; margin:10px 0 4px; }
    #pr select, #pr input[type=date] { width:100%; padding:7px 9px; border:1px solid var(--pr-line); border-radius:8px; background:var(--pr-bg); color:var(--pr-text); font-size:13px; }
    #pr select:focus, #pr input:focus { outline:none; border-color:var(--pr-gold); box-shadow:0 0 0 3px rgba(168,132,63,.2); }
    #pr .pr-hint { font-size:11px; color:var(--pr-muted); margin-top:4px; }
    #pr .pr-btn { display:inline-flex; align-items:center; justify-content:center; gap:6px; width:100%; padding:8px 10px; margin-top:8px;
      border:1px solid var(--pr-line); border-radius:8px; background:var(--pr-bg); color:var(--pr-text); font-size:12px; font-weight:500; cursor:pointer; }
    #pr .pr-btn:hover { border-color:var(--pr-muted); }
    #pr .pr-btn.primary { background:var(--pr-accent); color:var(--pr-accent-text); border-color:var(--pr-accent); font-size:13px; padding:10px; margin-top:12px; }
    #pr .pr-btn.primary:hover { opacity:.9; }
    #pr .pr-row { display:flex; gap:6px; }
    #pr .pr-row .pr-btn { width:auto; flex:1; }
    #pr .pr-icon { display:inline-grid; place-items:center; width:26px; height:26px; padding:0; border:1px solid transparent; border-radius:6px; background:none; color:var(--pr-muted); cursor:pointer; flex:none; }
    #pr .pr-icon:hover { background:var(--pr-soft); color:var(--pr-text); }
    #pr .pr-status { font-size:12px; margin-top:10px; min-height:16px; }
    #pr .pr-status.ok { color:var(--pr-ok); } #pr .pr-status.err { color:var(--pr-err); }
    #pr .pr-sum { display:flex; gap:6px; margin-bottom:10px; }
    #pr .pr-sum div { flex:1; text-align:center; padding:8px 4px; border-radius:8px; background:var(--pr-soft); }
    #pr .pr-sum b { display:block; font-size:16px; }
    #pr .pr-sum span { font-size:11px; color:var(--pr-muted); }
    #pr .pr-list { margin-top:8px; border:1px solid var(--pr-line); border-radius:8px; max-height:300px; overflow:auto; }
    #pr .pr-item { display:flex; align-items:center; gap:8px; padding:7px 8px; border-bottom:1px solid var(--pr-line); }
    #pr .pr-item:last-child { border-bottom:0; }
    #pr .pr-item .d { width:86px; font-variant-numeric:tabular-nums; }
    #pr .pr-item .d small { color:var(--pr-muted); }
    #pr .pr-item .s { flex:1; min-width:0; }
    #pr .pr-item .meta { display:block; font-size:11px; color:var(--pr-muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    #pr .pr-item .pr-icon.on.ok { color:var(--pr-ok); background:var(--pr-ok-bg); }
    #pr .pr-item .pr-icon.on.err { color:var(--pr-err); background:var(--pr-err-bg); }
    #pr .pr-tag { display:inline-block; font-size:11px; font-weight:600; padding:1px 7px; border-radius:999px; }
    #pr .pr-tag.wait { color:var(--pr-wait); background:var(--pr-wait-bg); }
    #pr .pr-tag.ok { color:var(--pr-ok); background:var(--pr-ok-bg); }
    #pr .pr-tag.err { color:var(--pr-err); background:var(--pr-err-bg); }
    #pr .pr-empty { padding:14px; text-align:center; color:var(--pr-muted); }
    #pr .pr-sep { height:1px; background:var(--pr-line); margin:14px 0 4px; }
    #pr [hidden] { display:none !important; }
    #pr-banner { position:sticky; top:0; z-index:99998; display:flex; gap:16px; align-items:center; padding:14px 20px;
      background:#7f2620; color:#fff; box-shadow:0 2px 12px rgba(0,0,0,.25); font-size:14px; }
    #pr-banner .pr-b-body { flex:1; }
    #pr-banner .pr-b-title { font-weight:600; font-size:15px; }
    #pr-banner .pr-b-text { opacity:.9; }
    #pr-banner .pr-b-note { margin-top:6px; display:inline-block; padding:3px 8px; border-radius:6px; background:rgba(255,255,255,.15); font-size:13px; }
    #pr-banner .pr-b-skip { border:1px solid rgba(255,255,255,.5); background:none; color:#fff; padding:7px 14px; border-radius:8px; font-weight:500; cursor:pointer; }
    #pr-banner .pr-b-skip:hover { background:rgba(255,255,255,.12); }
    @media (prefers-color-scheme: dark) {
      #pr { --pr-bg:#1e1e1c; --pr-text:#ecebe7; --pr-muted:#a19f98; --pr-line:#34332f; --pr-soft:#282826;
        --pr-accent:#ecebe7; --pr-accent-text:#141413; --pr-gold:#cfae6c;
        --pr-ok:#6cc596; --pr-ok-bg:#1d3327; --pr-wait:#e2b458; --pr-wait-bg:#3a2f18; --pr-err:#f08a80; --pr-err-bg:#3d1f1c; }
    }`;
  document.head.appendChild(css);

  // ---------- Panel ----------

  const panel = document.createElement('div');
  panel.id = 'pr';
  panel.innerHTML = `
    <div class="pr-head">
      <div class="pr-title">Pomocnik rezerwacji<small id="pr-badge"></small></div>
      <button type="button" class="pr-icon" id="pr-min" title="Zwiń / rozwiń">${ICON.min}</button>
    </div>
    <div class="pr-body">
      <div class="pr-conn off"><i></i><span>Sprawdzanie programu…</span></div>
      <div class="pr-info" hidden></div>
      <div class="pr-seg">
        <button type="button" data-tab="fill" class="on">Wypełnianie</button>
        <button type="button" data-tab="log">Zgłoszenia</button>
      </div>

      <div data-pane="fill">
        <label for="pr-tpl">Szablon</label>
        <select id="pr-tpl"></select>
        <div class="pr-hint">Używany też po odrzuceniu, gdy nie ma kopii pierwotnego zapytania.</div>
        <label for="pr-date">Data zwiedzania</label>
        <select id="pr-date"></select>
        <button type="button" class="pr-btn primary" id="pr-fill">Wypełnij formularz</button>
        <div class="pr-row">
          <button type="button" class="pr-btn" id="pr-save">Zapisz jako szablon</button>
          <button type="button" class="pr-btn" id="pr-del" style="flex:0 0 auto">Usuń</button>
        </div>
        <div class="pr-status" id="pr-status"></div>
      </div>

      <div data-pane="log" hidden>
        <div class="pr-sum" id="pr-sum"></div>
        <select id="pr-filter">
          <option value="future">Nadchodzące</option>
          <option value="wyslane">Czekają na odpowiedź</option>
          <option value="przydzielone">Przydzielone</option>
          <option value="odrzucone">Odrzucone</option>
          <option value="all">Wszystkie</option>
        </select>
        <div class="pr-list" id="pr-list"></div>
        <div class="pr-sep"></div>
        <label for="pr-add-date">Dodaj ręcznie (np. wysłane z innego komputera)</label>
        <div class="pr-row">
          <input type="date" id="pr-add-date" style="flex:1">
          <button type="button" class="pr-btn" id="pr-add" style="flex:0 0 auto;margin-top:0">Dodaj</button>
        </div>
        <div class="pr-row">
          <button type="button" class="pr-btn" id="pr-csv">Eksport</button>
          <button type="button" class="pr-btn" id="pr-backup">Kopia</button>
          <button type="button" class="pr-btn" id="pr-restore">Wczytaj</button>
        </div>
        <input type="file" id="pr-file" accept=".json" hidden>
      </div>
    </div>`;
  document.body.appendChild(panel);

  const q = s => panel.querySelector(s);
  const selTpl = q('#pr-tpl'), selDate = q('#pr-date'), statusEl = q('#pr-status');
  const status = (msg, typ) => { statusEl.textContent = msg; statusEl.className = 'pr-status ' + (typ || ''); };

  // Zwijanie panelu (zapamiętywane)
  const setMin = m => { panel.classList.toggle('min', m); q('#pr-min').innerHTML = m ? ICON.max : ICON.min; save(MIN_KEY, m); };
  q('#pr-min').onclick = () => setMin(!panel.classList.contains('min'));
  setMin(load(MIN_KEY, false));

  // Zakładki
  panel.querySelectorAll('.pr-seg button').forEach(b => b.onclick = () => {
    panel.querySelectorAll('.pr-seg button').forEach(x => x.classList.toggle('on', x === b));
    panel.querySelectorAll('[data-pane]').forEach(p => p.hidden = p.dataset.pane !== b.dataset.tab);
  });

  function renderTemplates(selectName) {
    const names = Object.keys(load(TPL_KEY, {}));
    selTpl.innerHTML = names.length
      ? names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join('')
      : '<option value="">Brak – wypełnij formularz i zapisz jako szablon</option>';
    const pick = selectName || load(TPL_AUTO_KEY, '');
    if (pick && names.includes(pick)) selTpl.value = pick;
    save(TPL_AUTO_KEY, selTpl.value || '');
  }
  selTpl.onchange = () => save(TPL_AUTO_KEY, selTpl.value);

  const statusLabel = e => {
    if (!e) return '';
    const proba = e.proba > 1 ? `, próba ${e.proba}` : '';
    return ` · ${STATUS[e.status].nazwa.toLowerCase()}${proba}`;
  };

  // Lista dat w panelu + dopiski statusu w oryginalnym polu „Data” na stronie.
  function renderDates() {
    const log = getLog();
    const src = form[P + 'data'];
    const keep = selDate.value;
    const opts = [...src.options].filter(o => o.value);
    selDate.innerHTML = opts.map(o =>
      `<option value="${o.value}">${plDate(o.value)} (${weekday(o.value)})${statusLabel(log[o.value])}</option>`).join('');

    for (const o of opts) {
      if (!o.dataset.orig) o.dataset.orig = o.text;
      o.text = o.dataset.orig + statusLabel(log[o.value]);
    }

    if (keep && !log[keep]) { selDate.value = keep; return; }
    // Domyślnie: pierwszy dzień bez zgłoszenia, licząc od ostatnio użytej daty (albo od początku listy).
    const last = load(LAST_DATE_KEY, '');
    const free = opts.map(o => o.value).filter(v => !log[v]);
    selDate.value = free.find(v => v > last) || free[0] || (opts[0] && opts[0].value) || '';
  }

  function renderLog() {
    const log = getLog();
    const dates = Object.keys(log).sort();
    const count = s => dates.filter(d => log[d].status === s).length;
    q('#pr-sum').innerHTML = Object.entries(STATUS)
      .map(([k, s]) => `<div><b>${count(k)}</b><span>${s.nazwa}</span></div>`).join('');
    const waiting = count('wyslane');
    q('#pr-badge').textContent = waiting ? `${waiting} czeka` : '';

    const f = q('#pr-filter').value;
    const shown = dates.filter(d =>
      f === 'all' ? true : f === 'future' ? d >= today() : log[d].status === f);

    q('#pr-list').innerHTML = shown.length ? shown.map(d => {
      const e = log[d], s = STATUS[e.status];
      const meta = [e.godzina, e.proba > 1 ? `próba ${e.proba}` : '', e.szablon].filter(Boolean).join(' · ');
      return `<div class="pr-item" data-date="${d}">
        <span class="d">${plDate(d)} <small>${weekday(d)}</small></span>
        <span class="s"><span class="pr-tag ${s.klasa}">${s.nazwa}</span><span class="meta">${esc(meta)}</span></span>
        <button type="button" class="pr-icon ok ${e.status === 'przydzielone' ? 'on' : ''}" data-act="przydzielone" title="Oznacz jako przydzielone">${ICON.check}</button>
        <button type="button" class="pr-icon err ${e.status === 'odrzucone' ? 'on' : ''}" data-act="odrzucone" title="Oznacz jako odrzucone">${ICON.x}</button>
        <button type="button" class="pr-icon" data-act="usun" title="Usuń wpis">${ICON.trash}</button>
      </div>`;
    }).join('') : '<div class="pr-empty">Brak zgłoszeń w tym widoku.</div>';
  }

  function refreshAll() { renderDates(); renderLog(); }

  // Akcje w dzienniku
  q('#pr-list').onclick = e => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const date = btn.closest('.pr-item').dataset.date;
    const cur = getLog()[date];
    if (btn.dataset.act === 'usun') {
      if (confirm(`Usunąć wpis z ${plDate(date)}? Dzień znów będzie oznaczony jako wolny.`)) setEntry(date, null);
    } else {
      // Ponowne kliknięcie cofa do „czeka”.
      setEntry(date, { status: cur.status === btn.dataset.act ? 'wyslane' : btn.dataset.act });
    }
  };
  q('#pr-filter').onchange = renderLog;

  q('#pr-add').onclick = () => {
    const d = q('#pr-add-date').value;
    if (!d) return alert('Wybierz datę.');
    if (getLog()[d]) return alert('Na ten dzień już jest wpis.');
    setEntry(d, { status: 'wyslane', proba: 1, szablon: 'dodane ręcznie', wyslano: new Date().toISOString() });
  };

  q('#pr-csv').onclick = () => {
    const log = getLog();
    const rows = [['Data', 'Dzień', 'Status', 'Próba', 'Godzina', 'Szablon', 'Wysłano']].concat(
      Object.keys(log).sort().map(d => [plDate(d), weekday(d), STATUS[log[d].status].nazwa, log[d].proba || 1,
        log[d].godzina || '', log[d].szablon || '', log[d].wyslano ? new Date(log[d].wyslano).toLocaleString('pl-PL') : '']));
    const csv = '﻿' + rows.map(r => r.map(c => `"${String(c).replace(/"/g, '""')}"`).join(';')).join('\r\n');
    download(csv, `zgloszenia-${today()}.csv`, 'text/csv');
  };

  q('#pr-backup').onclick = () => download(
    JSON.stringify({ szablony: load(TPL_KEY, {}), zgloszenia: getLog() }, null, 2),
    `pomocnik-kopia-${today()}.json`, 'application/json');

  q('#pr-restore').onclick = () => q('#pr-file').click();
  q('#pr-file').onchange = async e => {
    const file = e.target.files[0];
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      if (!confirm('Wczytać kopię? Wpisy z kopii zostaną dodane do obecnych (przy tych samych datach wygrywa kopia).')) return;
      save(TPL_KEY, { ...load(TPL_KEY, {}), ...(data.szablony || {}) });
      save(LOG_KEY, { ...getLog(), ...(data.zgloszenia || {}) });
      renderTemplates(); refreshAll();
      alert('Wczytano.');
    } catch { alert('To nie jest poprawny plik kopii.'); }
    e.target.value = '';
  };

  function download(text, name, type) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type }));
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  // Wypełnianie / szablony
  q('#pr-fill').onclick = () => {
    const tpl = load(TPL_KEY, {})[selTpl.value];
    if (!tpl) return status('Najpierw wypełnij formularz i zapisz go jako szablon.', 'err');
    const date = selDate.value;
    const e = getLog()[date];
    // Po odrzuceniu wolno wysłać ponownie – ostrzegamy tylko przy „czeka” i „przydzielone”.
    if (e && e.status !== 'odrzucone' &&
        !confirm(`Na ${plDate(date)} już jest zgłoszenie (${STATUS[e.status].nazwa.toLowerCase()}).\nMożna wysłać tylko jedno zapytanie na dzień. Mimo to wypełnić?`)) return;
    fillForm(tpl, date);
  };

  q('#pr-save').onclick = () => {
    const name = prompt('Nazwa szablonu (np. „Grupa PL 3,5 h 9:00”):', selTpl.value || '');
    if (!name) return;
    const tpls = load(TPL_KEY, {});
    tpls[name] = readForm();
    save(TPL_KEY, tpls);
    renderTemplates(name);
    status(`Zapisano szablon „${name}”.`, 'ok');
  };

  q('#pr-del').onclick = () => {
    const name = selTpl.value;
    if (!name || !confirm(`Usunąć szablon „${name}”?`)) return;
    const tpls = load(TPL_KEY, {});
    delete tpls[name];
    save(TPL_KEY, tpls);
    renderTemplates();
    status('Usunięto szablon.', 'ok');
  };

  // Ostrzeżenie, gdy ktoś ręcznie wybierze w formularzu dzień, na który już wysłano zapytanie.
  $(form[P + 'data']).on('change', function () {
    const e = getLog()[this.value];
    if (e && e.status !== 'odrzucone') status(`Na ${plDate(this.value)} już jest zgłoszenie (${STATUS[e.status].nazwa.toLowerCase()}).`, 'err');
  });

  renderTemplates();
  refreshAll();
  checkHelper();
  setInterval(checkHelper, 30e3);
  autoProcess();
})();
