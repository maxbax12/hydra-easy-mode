/* Hydra market maker — web panel. Vanilla JS, no third-party code.
 *
 * Access: the link `hydra-mm gui` prints ends in #t=<token>. The token is moved into
 * sessionStorage, removed from the address bar and sent as X-Hydra-Token on every API call.
 * Everything from the server is rendered as text nodes (textContent), never parsed as HTML.
 */
'use strict';

(() => {
  const TOKEN_KEY = 'hydraGuiToken';
  const TABS = ['dashboard', 'controls', 'volume', 'setup'];
  const STAGES = ['node', 'backup', 'plan', 'telegram', 'funding', 'done'];
  const REFRESH_MS = 15000;
  const NS = 'http://www.w3.org/2000/svg';
  const CHAIN_NAME = { bitcoin: 'Bitcoin', ethereum: 'Ethereum', arbitrum: 'Arbitrum One' };
  const S = {
    tab: null, memToken: null, overview: null, setup: null, setupInfo: null, openStage: null, userPicked: false,
    sigs: {}, planSeq: 0, planTimer: null, planResult: null, planBody: null, markets: null, capacity: null,
    leases: null, msSelected: null, volume: null, volTimer: null, tgTimer: null, watching: {}, fees: null,
    homeDecided: false, navigated: false,
  };

  // ================================================================ helpers
  const $ = (id) => document.getElementById(id);

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') el.className = v;
        else if (k === 'text') el.textContent = v;
        else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
        else if (v === true) el.setAttribute(k, '');
        else el.setAttribute(k, String(v));
      }
    }
    return append(el, kids);
  }
  function append(el, kids) {
    for (const kid of [].concat(kids).flat(Infinity)) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  function fill(el, ...kids) {
    el.replaceChildren();
    return append(el, kids);
  }
  function icon(name, cls) {
    const s = document.createElementNS(NS, 'svg');
    s.setAttribute('class', 'i' + (cls ? ' ' + cls : ''));
    s.setAttribute('aria-hidden', 'true');
    const u = document.createElementNS(NS, 'use');
    u.setAttribute('href', '#i-' + name);
    s.append(u);
    return s;
  }
  const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);
  const isNum = (x) => typeof x === 'number' && isFinite(x);
  const pill = (text, kind, extra) => h('span', { class: `pill pill-${kind}${extra ? ' ' + extra : ''}`, text });
  const skeleton = (...widths) => h('div', { class: 'skeleton' }, widths.map((w) => h('div', { class: 'sk' + (w ? ' ' + w : '') })));
  function emptyState(iconName, title, text, action) {
    return h('div', { class: 'empty' }, h('div', { class: 'empty-icon' }, icon(iconName)),
      h('div', { class: 'empty-title', text: title }), text ? h('p', { text }) : null, action || null);
  }
  function callout(kind, iconName, title, ...body) {
    return h('div', { class: `callout callout-${kind}` }, icon(iconName), h('div', null, title ? h('b', { text: title }) : null, body));
  }
  function stackTable(headers, rows) {
    const t = h('table', { class: 'table stack' },
      h('thead', null, h('tr', null, headers.map(([label, cls]) => h('th', { class: cls || null, text: label })))),
      h('tbody', null, rows));
    for (const tr of t.tBodies[0].rows) {
      [...tr.cells].forEach((td, i) => { if (headers[i] && headers[i][0]) td.setAttribute('data-label', headers[i][0]); });
    }
    return t;
  }

  function fmtAmt(x) {
    if (!isNum(x)) return '–';
    if (Math.abs(x) >= 100) return x.toLocaleString('en-US', { maximumFractionDigits: 2 });
    if (x === 0) return '0';
    return x.toLocaleString('en-US', { maximumSignificantDigits: 6 });
  }
  const fmtSigned = (x) => (!isNum(x) ? '–' : (x > 0 ? '+' : x < 0 ? '−' : '') + fmtAmt(Math.abs(x)));
  function fmtUsd(x, signed) {
    if (!isNum(x)) return '–';
    const sign = x < 0 ? '−' : (signed && x > 0 ? '+' : '');
    return sign + '$' + Math.abs(x).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  const fmtUsd0 = (x) => (!isNum(x) ? '–' : '$' + Math.round(x).toLocaleString('en-US'));
  function fmtPrice(x) {
    if (!isNum(x)) return '–';
    return x >= 1000 ? x.toLocaleString('en-US', { maximumFractionDigits: 2 }) : x.toLocaleString('en-US', { maximumSignificantDigits: 6 });
  }
  function fmtWhen(ts) {
    if (!isNum(ts) || ts <= 0) return '–';
    return new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
  const fmtClock = (ts) => new Date((ts || Date.now() / 1000) * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  function fmtLeft(secs) {
    if (!isNum(secs)) return '';
    if (secs <= 0) return 'ended';
    const d = Math.floor(secs / 86400), hr = Math.floor((secs % 86400) / 3600), m = Math.floor((secs % 3600) / 60);
    if (d > 0) return `in ${d} d ${hr} h`;
    if (hr > 0) return `in ${hr} h ${m} min`;
    return `in ${m} min`;
  }
  function fmtDuration(secs) {
    secs = Math.max(0, Math.round(secs));
    const m = Math.floor(secs / 60), s = secs % 60;
    return m ? `${m} min ${s} s` : `${s} s`;
  }
  const signClass = (x) => (isNum(x) && x > 0 ? 'pos' : isNum(x) && x < 0 ? 'neg' : '');

  let toastTimer = null;
  function toast(msg, bad) {
    const el = $('toast');
    fill(el, icon(bad ? 'alert' : 'check-circle'), h('span', { text: msg }));
    el.className = 'toast' + (bad ? ' bad' : '');
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 7000 : 4000);
  }
  function showError(el, msg) {
    if (!el) return;
    if (el.classList.contains('callout')) fill(el, icon('alert'), h('div', { text: msg || '' }));
    else el.textContent = msg || '';
    el.hidden = !msg;
  }
  async function busy(btn, fn) {
    if (!btn) return fn();
    btn.disabled = true;
    btn.classList.add('busy');
    try { return await fn(); } finally { btn.disabled = false; btn.classList.remove('busy'); }
  }
  async function copyText(text, btn) {
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch (e) {
      const ta = h('textarea', { class: 'sr-copy', readonly: true });
      ta.value = text;
      document.body.append(ta);
      ta.select();
      try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
      ta.remove();
    }
    if (btn) {
      const label = btn.querySelector('span');
      const old = label.textContent;
      label.textContent = ok ? 'Copied' : 'Copy failed';
      setTimeout(() => { label.textContent = old; }, 1600);
    }
  }
  function copyBtn(text, label) {
    const b = h('button', { class: 'btn btn-sm', type: 'button', 'aria-label': `Copy ${label || ''}`.trim() }, icon('copy', 'i-sm'), h('span', { text: 'Copy' }));
    b.addEventListener('click', () => copyText(text, b));
    return b;
  }
  const copyField = (text, label) => h('div', { class: 'copy-field' }, h('code', { text }), copyBtn(text, label));

  // ================================================================ token & API
  function getToken() {
    try { return sessionStorage.getItem(TOKEN_KEY) || S.memToken; } catch (e) { return S.memToken; }
  }
  function setToken(t) {
    S.memToken = t;
    try { sessionStorage.setItem(TOKEN_KEY, t); } catch (e) { /* private mode: memory only */ }
  }
  function clearToken() {
    S.memToken = null;
    try { sessionStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ }
  }
  function takeTokenFromHash() {
    const raw = location.hash.replace(/^#/, '');
    if (!raw.includes('t=')) return;
    const params = new URLSearchParams(raw);
    const t = params.get('t');
    if (t) setToken(t);
    const tab = [...params.keys()].find((k) => TABS.includes(k));          // #t=…&setup opens that tab
    history.replaceState(null, '', location.pathname + location.search + (tab ? '#' + tab : ''));   // the token leaves the address bar
  }

  class ApiError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }
  async function api(path, body) {
    const opts = { method: body === undefined ? 'GET' : 'POST', headers: { 'X-Hydra-Token': getToken() || '' },
                   credentials: 'omit', cache: 'no-store', redirect: 'error' };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      setPill('bad', 'Offline');
      throw new ApiError(0, 'The panel can’t reach the server. Is the SSH tunnel still open?');
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (res.status === 401) {
      lock((data && data.error) || 'The access key is missing or wrong.');
      throw new ApiError(401, 'Locked');
    }
    if (!res.ok) throw new ApiError(res.status, (data && data.error) || `The server answered ${res.status}.`);
    return data;
  }

  // ================================================================ modal
  function confirmModal({ title, body, confirmLabel = 'Confirm', danger = false, typeWord = null }) {
    return new Promise((resolve) => {
      const dlg = $('modal'), ok = $('modal-ok'), input = $('modal-type-input');
      fill($('modal-title'), title);
      fill($('modal-body'), body);
      ok.textContent = confirmLabel;
      ok.className = 'btn ' + (danger ? 'btn-danger' : 'btn-primary');
      $('modal-type').hidden = !typeWord;
      input.value = '';
      if (typeWord) {
        $('modal-type-label').textContent = `Type ${typeWord} to confirm`;
        ok.disabled = true;
        input.oninput = () => { ok.disabled = input.value.trim().toUpperCase() !== typeWord; };
      } else {
        ok.disabled = false;
        input.oninput = null;
      }
      let settled = false;
      const finish = (v) => {
        if (settled) return;
        settled = true;
        dlg.removeEventListener('close', onClose);
        $('modal-form').onsubmit = null;
        $('modal-cancel').onclick = null;
        if (dlg.open) dlg.close();
        resolve(v);
      };
      const onClose = () => finish(false);
      dlg.addEventListener('close', onClose);
      $('modal-form').onsubmit = (e) => { e.preventDefault(); if (!ok.disabled) finish(true); };
      $('modal-cancel').onclick = () => finish(false);
      dlg.showModal();
      (typeWord ? input : $('modal-cancel')).focus();
    });
  }

  // ================================================================ shell: lock, tabs, pill, banner
  function lock(msg) {
    clearToken();
    $('nav').hidden = true;
    $('banner').hidden = true;
    for (const t of TABS) $('tab-' + t).hidden = true;
    $('locked').hidden = false;
    showError($('unlock-error'), msg && msg !== 'Locked' ? msg : '');
    setPill('muted', 'Locked');
  }
  function unlock() {
    $('locked').hidden = true;
    $('nav').hidden = false;
  }

  function showTab(name) {
    if (!TABS.includes(name)) name = 'dashboard';
    const changed = S.tab !== name;
    S.tab = name;
    for (const t of TABS) $('tab-' + t).hidden = t !== name;
    for (const b of document.querySelectorAll('.nav-item')) {
      if (b.dataset.tab === name) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
    }
    history.replaceState(null, '', location.pathname + location.search + '#' + name);
    if (changed) window.scrollTo({ top: 0 });
    renderBanner();
    loadTab(name);
  }
  function loadTab(name) {
    if (name === 'dashboard') loadDashboard();
    else if (name === 'controls') loadControls();
    else if (name === 'setup') loadSetup();
    else if (name === 'volume') loadVolume();
  }
  function refreshTick() {
    if (document.hidden || !getToken() || !$('locked').hidden) return;
    if (S.tab === 'dashboard') loadDashboard();
    else {
      loadOverview();
      if (S.tab === 'controls') loadSwitches();
      else if (S.tab === 'volume') loadVolume();
      else if (S.tab === 'setup') loadSetup();
    }
  }

  function setPill(kind, text) {
    const p = $('state-pill');
    p.className = 'pill pill-' + kind;
    p.textContent = text;
  }

  function renderShell(o) {
    const st = o.setup || {};
    const d = o.doctor;
    if (d.pending) setPill('muted', 'Checking');
    else if (st.node === 'offline' || st.node === 'error') setPill('bad', 'Node offline');
    else if (!st.complete) setPill('warn', 'Setting up');
    else if (o.paused_all) setPill('warn', 'Paused');
    else setPill('ok', 'Running');
    $('nav-setup-label').textContent = st.complete ? 'Setup' : 'Get started';
    $('nav-dot').hidden = !!st.complete || d.pending;
    renderBanner();
  }

  function renderBanner() {
    const b = $('banner');
    const att = S.overview && S.overview.setup && S.overview.setup.attention;
    if (!att || S.tab === 'setup' || !$('locked').hidden) { b.hidden = true; return; }
    b.dataset.level = att.level;
    $('banner-icon').setAttribute('href', att.level === 'info' ? '#i-info' : '#i-alert');
    $('banner-text').textContent = att.text;
    const btn = $('banner-action');
    btn.className = 'btn btn-sm ' + (att.level === 'bad' ? 'btn-danger' : 'btn-primary');
    fill(btn, h('span', { text: att.label }), icon('arrow', 'i-sm'));
    btn.onclick = () => runAction(att, btn);
    b.hidden = false;
  }
  function runAction(att, btn) {
    if (att.action === 'resume') return busy(btn, () => doPause(false, null));
    if (att.action === 'recheck') return busy(btn, () => loadOverview(true).then(() => (S.tab === 'setup' ? loadSetup(true) : null)));
    openStage(att.stage || null);
    showTab('setup');
    return null;
  }

  // ================================================================ dashboard
  function loadDashboard() {
    return Promise.allSettled([loadOverview(), loadMarkets(), loadCapacity(), loadLeases(), loadWallet(), loadFills()]);
  }

  let overviewRetry = null;
  async function loadOverview(fresh) {
    try {
      const o = await api('/api/overview' + (fresh ? '?fresh=1' : ''));
      S.overview = o;
      renderShell(o);
      renderOverview(o);
      if (!S.homeDecided && !o.doctor.pending) {          // home: the setup until it is complete
        S.homeDecided = true;
        if (o.setup && !o.setup.complete && !S.navigated && S.tab === 'dashboard') showTab('setup');
      }
      clearTimeout(overviewRetry);
      if (o.doctor.pending) overviewRetry = setTimeout(() => loadOverview(), 4000);
      return o;
    } catch (e) {
      if (e.status === 401) return null;
      $('hero-title').textContent = 'Can’t reach the server';
      $('hero-sub').textContent = e.message;
      return null;
    }
  }

  function setKpi(id, value, sub, tone, valueClass) {
    const k = $(id);
    k.dataset.tone = tone || '';
    const v = k.querySelector('.kpi-value');
    v.textContent = value;
    v.className = 'kpi-value' + (valueClass ? ' ' + valueClass : '');
    const s = k.querySelector('.kpi-sub');
    s.textContent = sub || '';
    s.title = sub || '';
  }

  function renderOverview(o) {
    const d = o.doctor, t = o.totals, st = o.setup || {};
    const title = $('hero-title'), sub = $('hero-sub');
    if (d.pending) { title.textContent = 'Overview'; sub.textContent = 'Running the health checks…'; }
    else if (o.paused_all) { title.textContent = 'Paused'; sub.textContent = 'None of your quotes are on the order book. Resume in Controls when you are ready.'; }
    else if (st.complete) {
      title.textContent = t.markets ? `Quoting on ${t.quoting} of ${t.markets} market${t.markets === 1 ? '' : 's'}` : 'Running';
      sub.textContent = 'All health checks pass. Prices: BTC ' + (o.prices ? fmtUsd0(o.prices.BTC) : '–') + ', ETH ' + (o.prices ? fmtUsd0(o.prices.ETH) : '–') + '.';
    } else { title.textContent = 'Not running yet'; sub.textContent = 'Finish the setup to start quoting.'; }
    $('updated').textContent = 'Updated ' + fmtClock();
    const qs = Object.entries(t.pnl_by_quote || {}).map(([q, v]) => `${fmtSigned(v)} ${q}`).join(', ');
    setKpi('kpi-pnl', t.markets ? fmtUsd(t.pnl_usd, true) : '–', t.markets ? (qs || 'No trades yet') : 'Starts with your first trade',
      t.pnl_usd > 0 ? 'ok' : t.pnl_usd < 0 ? 'bad' : '', signClass(t.pnl_usd));
    setKpi('kpi-markets', t.markets ? `${t.quoting} of ${t.markets}` : '–',
      o.paused_all ? 'Everything is paused' : (t.markets ? `${t.fills.toLocaleString('en-US')} trades so far` : 'No markets yet'),
      o.paused_all ? 'warn' : (t.markets && t.quoting === t.markets ? 'ok' : t.markets ? 'warn' : ''));
    renderDoctor(d);
  }

  function renderDoctor(d) {
    const list = $('doctor');
    if (d.pending) { fill(list, h('li', null, skeleton('w-90', 'w-60'))); return; }
    const firstBad = (d.steps || []).findIndex((s) => !s.ok);
    fill(list, (d.steps || []).map((s, i) => h('li', null,
      h('span', { class: 'dot-icon ' + (s.ok ? 'ok' : (i === firstBad ? 'warn' : '')) }, icon(s.ok ? 'check' : (i === firstBad ? 'alert' : 'dot'))),
      h('div', { class: 'grow' }, h('div', { text: cap(s.what) }),
        i === firstBad && s.hint ? h('div', { class: 'sub' }, 'On the server: ', h('code', { text: s.hint })) : null))));
  }

  function marketStatus(m) {
    if (!m.enabled) return pill('Off', 'muted');
    if (m.paused === 'volume') return pill('Volume test', 'info');
    if (m.paused) return pill('Paused', 'warn');
    if (m.bids === null || m.bids === undefined) return pill('Starting', 'muted');
    if (m.bids + m.asks > 0) return pill('Quoting', 'ok', 'pill-live');
    return pill('No quotes', 'bad');
  }

  async function loadMarkets() {
    try {
      const r = await api('/api/markets');
      S.markets = r;
      $('markets-note').textContent = r.node_error ? 'Order book unavailable right now' : '';
      if (!r.markets.length) {
        fill($('markets'), emptyState('swap', 'No markets yet', 'Your markets appear here once your plan is saved and funded.',
          h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: 'Go to setup', onclick: () => showTab('setup') })));
        return r;
      }
      const rows = r.markets.map((m) => h('tr', null,
        h('td', null, h('span', { class: 'cell-strong', text: m.pair })),
        h('td', null, marketStatus(m)),
        h('td', { class: 'r num' }, fmtPrice(m.fair),
          isNum(m.best_bid) || isNum(m.best_ask) ? h('span', { class: 'sub', text: `book ${fmtPrice(m.best_bid)} / ${fmtPrice(m.best_ask)}` }) : null),
        h('td', { class: 'r num' }, isNum(m.bids) ? `${m.bids} / ${m.asks}` : '–', h('span', { class: 'sub', text: 'buy / sell' })),
        h('td', { class: 'r num' }, isNum(m.position) ? `${fmtSigned(m.position)} ${m.base}` : '–',
          m.position_pct ? h('span', { class: 'sub', text: `${m.position_pct} of limit` }) : null),
        h('td', { class: 'r num', text: isNum(m.fills) ? m.fills.toLocaleString('en-US') : '–' }),
        h('td', { class: 'r num ' + signClass(m.pnl) }, isNum(m.pnl) ? `${fmtSigned(m.pnl)} ${m.quote}` : '–')));
      fill($('markets'), stackTable([['Market'], ['Status'], ['Price', 'r'], ['Our quotes', 'r'], ['Position', 'r'], ['Trades', 'r'], ['P&L', 'r']], rows));
      return r;
    } catch (e) {
      if (e.status !== 401) fill($('markets'), callout('warn', 'alert', 'Markets unavailable', e.message));
      return null;
    }
  }

  async function loadCapacity() {
    const el = $('capacity');
    try {
      const r = await api('/api/capacity');
      S.capacity = r;
      if (!r.assets.length) {
        fill(el, emptyState('gauge', 'No channels yet', 'Channels are opened in the last setup step, once your funds have arrived.'));
        setKpi('kpi-capacity', '–', 'No channels yet', '');
        return;
      }
      let worst = null;
      for (const a of r.assets) {
        for (const [side, total, need] of [['send', a.send, a.need_send], ['receive', a.recv, a.need_recv]]) {
          if (isNum(need) && need > 0) {
            const ratio = total / need;
            if (!worst || ratio < worst.ratio) worst = { ratio, label: `${a.asset} ${side}` };
          }
        }
      }
      if (worst) {
        const tone = worst.ratio >= 0.7 ? 'ok' : worst.ratio >= 0.5 ? 'warn' : 'bad';
        setKpi('kpi-capacity', tone === 'ok' ? 'Healthy' : tone === 'warn' ? 'Getting low' : 'Low',
          tone === 'ok' ? 'Every side has room for its quotes' : `${worst.label} at ${Math.round(worst.ratio * 100)}% of what it needs`, tone);
      } else setKpi('kpi-capacity', `${r.assets.length} assets`, 'In your channels', 'info');
      fill(el, h('div', { class: 'cap-grid' }, r.assets.map((a) => h('div', { class: 'cap-asset' },
        h('h3', null, a.asset, h('span', { text: a.chain ? `on ${CHAIN_NAME[a.chain] || a.chain}` : '' })),
        capRow('Send', a.send, a.send_free, a.need_send, a.asset),
        capRow('Receive', a.recv, a.recv_free, a.need_recv, a.asset)))));
    } catch (e) {
      if (e.status !== 401) { fill(el, callout('warn', 'alert', 'Capacity unavailable', e.message)); setKpi('kpi-capacity', '–', 'Node not reachable', ''); }
    }
  }
  function capRow(label, total, free, need, asset) {
    const frac = isNum(need) && need > 0 ? Math.min(1, total / need) : (total > 0 ? free / total : 0);
    const ratio = isNum(need) && need > 0 ? total / need : 1;
    const tone = ratio < 0.5 ? ' bad' : ratio < 0.7 ? ' warn' : '';
    const text = `${label}: ${fmtAmt(total)} ${asset}, ${fmtAmt(free)} free` + (isNum(need) ? `; your plan needs about ${fmtAmt(need)}` : '');
    const bar = h('div', { class: 'bar' + tone, role: 'img', 'aria-label': text, title: text }, h('span'));
    requestAnimationFrame(() => { bar.firstChild.style.width = Math.round(frac * 100) + '%'; });
    return h('div', { class: 'cap-row' },
      h('div', { class: 'cap-line' }, h('span', { text: label }), h('span', { text: `${fmtAmt(total)} (${fmtAmt(free)} free)` })),
      bar, tone ? h('div', { class: 'cap-low', text: `Below ${tone === ' bad' ? 'half' : '70%'} of the ~${fmtAmt(need)} your quotes need` }) : null);
  }

  async function loadLeases() {
    const el = $('leases');
    try {
      const r = await api('/api/leases');
      S.leases = r;
      const soon = r.leases.filter((l) => l.liquidity_end - r.now < 48 * 3600);
      if (!r.leases.length) setKpi('kpi-leases', 'None', 'No running leases', '');
      else setKpi('kpi-leases', soon.length ? `${soon.length} ending soon` : 'All good',
        `Next ends ${fmtLeft(Math.min(...r.leases.map((l) => l.liquidity_end)) - r.now)}`, soon.length ? 'warn' : 'ok');
      if (!r.leases.length) { fill(el, emptyState('clock', 'No running leases', 'Leases start when your channels open.')); return; }
      fill(el, h('ul', { class: 'list' }, r.leases.map((l) => {
        const left = l.liquidity_end - r.now;
        return h('li', null, h('span', { class: 'dot-icon ' + (left < 24 * 3600 ? 'warn' : 'ok') }, icon('clock')),
          h('div', { class: 'grow' }, h('div', null, h('b', { text: l.asset }), ` on ${CHAIN_NAME[l.chain] || l.chain}`),
            h('div', { class: 'sub', text: `Ends ${fmtWhen(l.lease_end)} (${fmtLeft(l.lease_end - r.now)})` }),
            l.liquidity_end > l.lease_end ? h('div', { class: 'sub', text: `The hub keeps it until ${fmtWhen(l.liquidity_end)}` }) : null));
      })));
    } catch (e) {
      if (e.status !== 401) { fill(el, callout('warn', 'alert', null, e.message)); setKpi('kpi-leases', '–', 'Node not reachable', ''); }
    }
  }

  async function loadWallet() {
    const el = $('wallet');
    try {
      const r = await api('/api/wallet');
      const rows = Object.entries(r.onchain || {}).filter(([, v]) => v > 0);
      const gas = Object.entries(r.gas || {}).filter(([, v]) => v > 0);
      fill(el, rows.length ? h('dl', { class: 'kv' }, rows.map(([a, v]) => [h('dt', { text: a }), h('dd', { text: fmtAmt(v) })]))
        : emptyState('wallet', 'Nothing on-chain', 'Everything is in your channels.'),
      gas.length ? h('p', { class: 'help', text: 'Gas: ' + gas.map(([c, v]) => `${fmtAmt(v)} ETH on ${CHAIN_NAME[c] || c}`).join(', ') }) : null);
    } catch (e) {
      if (e.status !== 401) fill(el, callout('warn', 'alert', null, e.message));
    }
  }

  async function loadFills() {
    const el = $('fills');
    try {
      const r = await api('/api/fills?n=30');
      if (!r.fills.length) { fill(el, emptyState('book', 'No trades yet', 'Trades appear here as soon as someone trades with your quotes.')); return; }
      fill(el, stackTable([['When'], ['Market'], ['Trade'], ['Position after', 'r']], r.fills.map((f) => h('tr', null,
        h('td', { class: 'num', text: (f.at || '').slice(5, 16) }),
        h('td', { class: 'cell-strong', text: f.pair }),
        h('td', null, h('span', { class: f.side === 'buy' ? 'pos' : '', text: f.side === 'buy' ? 'Bought ' : 'Sold ' }),
          `${fmtAmt(f.amount)} at ${fmtPrice(f.price)}`, f.how ? h('span', { class: 'sub', text: f.how }) : null),
        h('td', { class: 'r num', text: f.position === null || f.position === undefined ? '–' : fmtSigned(f.position) })))));
    } catch (e) {
      if (e.status !== 401) fill(el, callout('warn', 'alert', null, e.message));
    }
  }

  // ================================================================ controls
  function loadControls() {
    return Promise.allSettled([loadOverview(), loadSwitches(), loadMarketSettings(), loadAlertSettings()]);
  }

  async function doPause(on, market) {
    try {
      const r = await api(on ? '/api/pause' : '/api/resume', market ? { market } : {});
      toast(r.message);
    } catch (e) {
      if (e.status !== 401) toast(e.message, true);
    }
    await Promise.allSettled([loadOverview(), S.tab === 'controls' ? loadSwitches() : loadMarkets()]);
  }

  async function loadSwitches() {
    let r = null;
    try { r = await api('/api/markets'); S.markets = r; } catch (e) { r = null; }
    const pausedAll = r ? r.paused_all : (S.overview && S.overview.paused_all);
    $('master-title').textContent = pausedAll ? 'Everything is paused' : 'Quoting';
    $('master-text').textContent = pausedAll
      ? 'None of your quotes are on the order book. Your funds stay in the channels. Resume when you are ready.'
      : 'Pausing takes every quote off the order book within seconds. Your funds stay in the channels.';
    const mi = $('master-icon');
    mi.className = 'master-icon' + (pausedAll ? ' paused' : '');
    fill(mi, icon(pausedAll ? 'pause' : 'play'));
    $('pause-all').disabled = !!pausedAll;
    $('resume-all').disabled = !pausedAll && !(r && r.markets.some((m) => m.paused === 'market'));
    const list = $('market-switches');
    if (!r) return;
    const rows = r.markets.filter((m) => m.configured);
    if (!rows.length) { fill(list, h('li', null, emptyState('swap', 'No markets yet', 'Save a plan in Setup to add markets.'))); return; }
    fill(list, rows.map((m) => {
      const paused = !!m.paused;
      const why = m.paused === 'volume' ? 'Paused by the volume test' : m.paused === 'all' ? 'Paused with everything' :
        m.paused ? 'Paused' : (!m.enabled ? 'Switched off in its settings' : (isNum(m.bids) ? `${m.bids} buy and ${m.asks} sell quotes` : 'Waiting for its first status'));
      const b = h('button', { class: 'btn btn-sm ' + (paused ? 'btn-primary' : ''), type: 'button',
        disabled: m.paused === 'all' || m.paused === 'volume' || !m.enabled }, icon(paused ? 'play' : 'pause', 'i-sm'), h('span', { text: paused ? 'Resume' : 'Pause' }));
      b.addEventListener('click', () => busy(b, () => doPause(!paused, m.name)));
      return h('li', null, marketStatus(m), h('div', { class: 'grow' }, h('b', { text: m.pair }), h('span', { class: 'sub', text: why })), b);
    }));
  }

  async function loadMarketSettings() {
    try {
      const r = await api('/api/settings/markets');
      const picker = $('ms-picker');
      if (!r.markets.length) {
        fill(picker);
        picker.hidden = true;
        fill($('ms-form'), h('div', { class: 'field-wide' }, emptyState('sliders', 'Nothing to tune yet', 'Save a plan in Setup first.')));
        return;
      }
      picker.hidden = false;
      if (!S.msSelected || !r.markets.some((m) => m.name === S.msSelected)) S.msSelected = r.markets[0].name;
      fill(picker, r.markets.map((m) => {
        const b = h('button', { type: 'button', role: 'radio', 'aria-checked': String(m.name === S.msSelected), text: m.pair });
        b.addEventListener('click', () => { S.msSelected = m.name; loadMarketSettings(); });
        return b;
      }));
      renderMarketForm(r.markets.find((m) => m.name === S.msSelected), r.fields);
    } catch (e) {
      if (e.status !== 401) fill($('ms-form'), callout('bad', 'alert', null, e.message));
    }
  }

  function numberField(id, label, unit, value, help, attrs) {
    const input = h('input', Object.assign({ id, type: 'number', inputmode: 'decimal', step: 'any' }, attrs || {}));
    input.value = value === null || value === undefined ? '' : String(value);
    return { input, el: h('div', { class: 'field' }, h('label', { for: id, text: label }),
      unit ? h('div', { class: 'with-unit' }, input, h('span', { text: unit })) : input, help ? h('p', { class: 'help', text: help }) : null) };
  }
  function switchField(id, label, checked, help) {
    const input = h('input', { id, type: 'checkbox' });
    input.checked = !!checked;
    return { input, el: h('div', { class: 'field' }, h('label', { class: 'switch' }, input, h('span', { class: 'switch-track' }), h('span', { text: label })),
      help ? h('p', { class: 'help', text: help }) : null) };
  }

  function renderMarketForm(m, fields) {
    const form = $('ms-form');
    const inputs = {};
    const kids = fields.map((f) => {
      const fld = numberField('ms-' + f.key, f.label, (f.unit || '').replace('{base}', m.base), m.values[f.key], f.help,
        f.kind === 'int' ? { step: '1', min: f.min, max: f.max } : { min: f.min, max: f.max });
      inputs[f.key] = fld.input;
      return fld.el;
    });
    const enabled = switchField('ms-enabled', 'Market on', m.enabled !== false,
      'Switching a market off cancels its quotes until you switch it on again.');
    kids.push(enabled.el);
    const err = h('div', { class: 'callout callout-bad', hidden: true });
    const save = h('button', { class: 'btn btn-primary', type: 'submit' }, h('span', { text: `Save ${m.pair}` }));
    fill(form, kids, h('div', { class: 'actions' }, save), err);
    form.onsubmit = async (e) => {
      e.preventDefault();
      showError(err, '');
      for (const i of Object.values(inputs)) i.removeAttribute('aria-invalid');
      const changes = {};
      for (const f of fields) {
        const raw = inputs[f.key].value.trim();
        if (raw === '') { inputs[f.key].setAttribute('aria-invalid', 'true'); showError(err, `${f.label}: enter a number.`); return; }
        if (Number(raw) !== m.values[f.key]) changes[f.key] = raw;
      }
      if (enabled.input.checked !== (m.enabled !== false)) changes.enabled = enabled.input.checked;
      if (!Object.keys(changes).length) { toast('Nothing changed.'); return; }
      if (changes.enabled === false && !(await confirmModal({ title: `Switch ${m.pair} off?`,
        body: h('p', { text: 'Its quotes are cancelled and it stops trading until you switch it on again.' }), confirmLabel: 'Switch off' }))) return;
      await busy(save, async () => {
        try {
          const r = await api('/api/settings/markets', { name: m.name, changes });
          await loadMarketSettings();
          toast(r.message);
        } catch (ex) {
          if (ex.status === 401) return;
          showError(err, ex.message);
          for (const f of fields) if (ex.message.startsWith(f.label)) inputs[f.key].setAttribute('aria-invalid', 'true');
        }
      });
    };
  }

  async function loadAlertSettings() {
    const form = $('alerts-form');
    try {
      const r = await api('/api/settings/alerts');
      if (!r.configured) { fill(form, h('div', { class: 'field-wide' }, emptyState('send', 'Not set up yet', 'Alert settings are created with your plan.'))); return; }
      const inputs = {};
      const kids = r.fields.map((f) => {
        const id = 'al-' + f.key.replace(/\W/g, '-');
        const v = r.values[f.key];
        if (f.kind === 'bool') { const s = switchField(id, f.label, v, f.help); inputs[f.key] = s.input; return s.el; }
        if (f.kind === 'hours') {
          const i = h('input', { id, type: 'text', inputmode: 'numeric', autocomplete: 'off' });
          i.value = Array.isArray(v) ? v.join(', ') : String(v ?? '');
          inputs[f.key] = i;
          return h('div', { class: 'field' }, h('label', { for: id, text: f.label }), h('div', { class: 'with-unit' }, i, h('span', { text: f.unit })), h('p', { class: 'help', text: f.help }));
        }
        const fld = numberField(id, f.label, f.unit, v, f.help, f.kind === 'int' ? { step: '1', min: f.min, max: f.max } : { min: f.min, max: f.max });
        inputs[f.key] = fld.input;
        return fld.el;
      });
      const err = h('div', { class: 'callout callout-bad', hidden: true });
      const save = h('button', { class: 'btn btn-primary', type: 'submit' }, h('span', { text: 'Save alert settings' }));
      fill(form, kids, h('div', { class: 'actions' }, save), err);
      form.onsubmit = async (e) => {
        e.preventDefault();
        showError(err, '');
        const changes = {};
        for (const f of r.fields) {
          const i = inputs[f.key], cur = r.values[f.key];
          i.removeAttribute('aria-invalid');
          if (f.kind === 'bool') { if (i.checked !== !!cur) changes[f.key] = i.checked; continue; }
          const raw = i.value.trim();
          if (f.kind === 'hours') { if (raw !== (Array.isArray(cur) ? cur.join(', ') : String(cur))) changes[f.key] = raw; continue; }
          if (raw === '' || Number(raw) !== cur) changes[f.key] = raw;
        }
        if (!Object.keys(changes).length) { toast('Nothing changed.'); return; }
        await busy(save, async () => {
          try {
            const res = await api('/api/settings/alerts', { changes });
            await loadAlertSettings();
            toast(res.message);
          } catch (ex) {
            if (ex.status === 401) return;
            showError(err, ex.message);
            for (const f of r.fields) if (ex.message.startsWith(f.label)) inputs[f.key].setAttribute('aria-invalid', 'true');
          }
        });
      };
    } catch (e) {
      if (e.status !== 401) fill(form, callout('bad', 'alert', null, e.message));
    }
  }

  // ================================================================ jobs: a readable progress view
  function parseSteps(output, kind, running) {
    const lines = (output || '').split('\n').map((l) => l.replace(/\s+$/, ''));
    const items = [];
    if (kind === 'fund-go') {
      let cur = null;
      for (const raw of lines) {
        const l = raw.trim();
        const head = l.match(/^(bitcoin|ethereum|arbitrum):$/);
        if (head) { cur = { title: CHAIN_NAME[head[1]], lines: [] }; items.push(cur); continue; }
        if (!l) continue;
        if (cur) cur.lines.push(l);
        else if (/^Send these|Nothing done yet|already hold/.test(l)) items.push({ title: 'Before opening', lines: [l] });
      }
      items.forEach((it, i) => {
        const text = it.lines.join('\n');
        it.state = /❌/.test(text) ? 'bad' : /✅/.test(text) ? 'ok' : (running && i === items.length - 1 ? 'run' : 'idle');
        it.text = (it.lines.slice().reverse().find((x) => !/^Stopping/.test(x)) || (running ? 'Working…' : '')).replace(/^[✅❌]\s*/, '');
      });
      const last = lines.filter((l) => l.trim()).slice(-1)[0];
      if (!running && last && !/^\s/.test(last) && !/:$/.test(last)) items.push({ title: '', text: last.trim(), state: 'info' });
      return items;
    }
    for (const raw of lines.filter((l) => l.trim()).slice(-8)) {
      const l = raw.trim();
      items.push({ title: '', text: l.replace(/^[✅❌]\s*/, ''), state: /^✅/.test(l) ? 'ok' : /^❌|^Error|Traceback/.test(l) ? 'bad' : 'idle' });
    }
    return items;
  }

  function watchJob(job, container, onDone) {
    const head = h('div', { class: 'job-head' });
    const steps = h('ul', { class: 'job-steps' });
    const pre = h('pre', { tabindex: '0', 'aria-label': 'Full output' });
    fill(container, h('div', { class: 'job', 'data-running': 'true' }, head, steps,
      h('details', null, h('summary', { text: 'Full log' }), pre)));
    S.watching[job.id] = true;
    const render = (j) => {
      const running = j.status === 'running';
      const elapsed = ((j.ended || Date.now() / 1000) - j.started);
      fill(head, running ? h('span', { class: 'spinner' }) : h('span', { class: 'dot-icon ' + (j.status === 'done' ? 'ok' : 'bad') }, icon(j.status === 'done' ? 'check' : 'x')),
        h('b', { text: j.title }), h('span', { class: 'muted', text: fmtDuration(elapsed) }),
        running ? pill('Running', 'info', 'pill-live') : j.status === 'done' ? pill('Finished', 'ok') : pill(`Failed (code ${j.exit_code})`, 'bad'));
      const items = parseSteps(j.output, j.kind, running);
      fill(steps, items.length ? items.map((it) => h('li', null,
        it.state === 'run' ? h('span', { class: 'spinner' }) :
          h('span', { class: 'dot-icon ' + ({ ok: 'ok', bad: 'bad', info: '' }[it.state] || '') }, icon({ ok: 'check', bad: 'x', info: 'info' }[it.state] || 'dot')),
        h('div', { class: 'grow' }, it.title ? h('b', { text: it.title }) : null, h('span', { text: it.text })))) :
        h('li', null, h('span', { class: 'spinner' }), h('span', { class: 'grow', text: 'Starting…' })));
      const nearBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
      pre.textContent = j.output || '(no output yet)';
      if (nearBottom) pre.scrollTop = pre.scrollHeight;
      container.firstChild.dataset.running = String(running);
    };
    render(job);
    const tick = async () => {
      try {
        const j = await api('/api/jobs/' + job.id);
        render(j);
        if (j.status === 'running') { setTimeout(tick, 1000); return; }
        delete S.watching[job.id];
        if (onDone) onDone(j);
      } catch (e) {
        if (e.status === 401) return;
        if (e.status === 404) { delete S.watching[job.id]; return; }
        setTimeout(tick, 3000);
      }
    };
    setTimeout(tick, 600);
  }
  const jobRunningIn = (el) => !!(el && el.querySelector('.job[data-running="true"]'));

  // ================================================================ setup: the stepper
  function openStage(stage) {
    if (stage) { S.openStage = stage; S.userPicked = true; }
  }

  async function loadSetup(fresh) {
    let st;
    try {
      st = await api('/api/setup/state' + (fresh ? '?fresh=1' : ''));
    } catch (e) {
      if (e.status !== 401) toast(e.message, true);
      return null;
    }
    S.setup = st;
    if (!S.setupInfo) await loadSetupInfo();
    renderStepper(st);
    if (st.pending) setTimeout(() => { if (S.tab === 'setup') loadSetup(); }, 4000);
    return st;
  }

  const STAGE_PILL = {
    done: ['Done', 'ok'], action: ['Action needed', 'warn'], checking: ['Checking', 'muted'], todo: ['To do', 'muted'],
    waiting: ['Waiting', 'warn'], optional: ['Optional', 'muted'], skipped: ['Skipped', 'muted'], starting: ['Starting', 'info'],
  };

  function renderStepper(st) {
    const done = st.progress.done, total = st.progress.total;
    $('setup-meter').style.width = Math.round(done / total * 100) + '%';
    $('setup-meter-bar').setAttribute('aria-valuenow', String(done));
    fill($('setup-count'), h('b', { text: String(done) }), ` of ${total} done`);
    $('setup-title').textContent = st.complete ? 'You’re all set' : 'Get your market maker running';
    $('setup-sub').textContent = st.complete
      ? 'Your market maker is running. Come back here to change your plan, add funds or connect Telegram.'
      : 'Six short steps. Each one checks itself, so you always know what comes next.';
    if (!S.userPicked) S.openStage = st.complete ? null : st.current;
    for (const k of STAGES) {
      const li = $('stage-' + k), status = st.stages[k];
      const isOpen = S.openStage === k;
      li.className = 'stage' + (status === 'done' ? ' is-done' : '') + (status === 'skipped' ? ' is-skipped' : '') +
        (k === st.current && !st.complete ? ' is-current' : '') + (status === 'action' || status === 'waiting' ? ' is-action' : '') + (isOpen ? ' is-open' : '');
      const marker = li.querySelector('.stage-marker use');
      const base = { node: 'server', backup: 'shield', plan: 'layers', telegram: 'send', funding: 'wallet', done: 'flag' }[k];
      marker.setAttribute('href', '#i-' + (status === 'done' ? 'check' : base));
      const [label, kind] = k === st.current && !st.complete && status === 'todo' ? ['Next', 'info'] : (STAGE_PILL[status] || ['', 'muted']);
      fill(li.querySelector('.stage-pill'), pill(label, kind, 'no-dot'));
      li.querySelector('.stage-summary').textContent = stageSummary(k, st);
      const headBtn = li.querySelector('.stage-head');
      headBtn.setAttribute('aria-expanded', String(isOpen));
      headBtn.onclick = () => { S.userPicked = true; S.openStage = S.openStage === k ? null : k; renderStepper(S.setup); if (S.openStage === k) li.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); };
      const body = $(`stage-${k}-body`);
      body.hidden = !isOpen;
      if (isOpen) renderStageBody(k, st, body);
    }
  }

  function stageSummary(k, st) {
    const p = st.plan;
    switch (k) {
      case 'node': return { ok: 'Running and admitted to mainnet', offline: 'Your node isn’t answering', invite: 'Waiting for an invite code',
        not_admitted: 'Not admitted to mainnet yet', hub: 'Connecting to the Hydranet hub', checking: 'Checking…', error: 'The checks could not run' }[st.node] || '';
      case 'backup': return st.acks.backup ? `Confirmed on ${fmtWhen(st.acks.backup.at)}` : 'Write down your recovery phrase';
      case 'plan': return p ? `${fmtUsd0(p.budget)}, ${p.preset}, ${p.markets.length} market${p.markets.length === 1 ? '' : 's'}` : 'Budget, style and markets';
      case 'telegram': return { connected: 'Alerts go to your chat', waiting: 'Waiting for your /start message', unpaired: 'Bot set, no chat paired', off: st.acks.telegram_skipped ? 'Skipped for now' : 'Alerts and control from your phone' }[st.telegram.state] || '';
      case 'funding': {
        const f = st.steps.find((s) => s.key === 'funding');
        return f ? cap(f.what) : (st.configured ? 'Send funds, then open your channels' : 'After your plan is saved');
      }
      case 'done': return st.complete ? 'Your market maker is running' : st.stages.done === 'starting' ? 'Placing the first quotes' : 'Finish the steps above';
      default: return '';
    }
  }

  function renderStageBody(k, st, body) {
    if (k === 'plan') { syncPlanStage(st); return; }
    if (k === 'funding') { renderFundingStage(st, body); return; }
    const sig = JSON.stringify([k, stageSig(k, st)]);
    if (S.sigs[k] === sig && body.childNodes.length) return;
    if (body.contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return;
    if (jobRunningIn(body)) return;
    S.sigs[k] = sig;
    ({ node: renderNodeStage, backup: renderBackupStage, telegram: renderTelegramStage, done: renderDoneStage })[k](st, body);
  }
  function stageSig(k, st) {
    if (k === 'node') return [st.node, st.identity, st.steps.filter((s) => ['node', 'access', 'hub', 'error'].includes(s.key)).map((s) => s.what)];
    if (k === 'backup') return [st.acks.backup];
    if (k === 'telegram') return [st.telegram, st.acks.telegram_skipped];
    if (k === 'done') return [st.complete, st.stages.done, st.paused];
    return [];
  }

  const recheckBtn = (label) => {
    const b = h('button', { class: 'btn', type: 'button' }, icon('refresh', 'i-sm'), h('span', { text: label || 'Check again' }));
    b.addEventListener('click', () => busy(b, async () => { S.sigs = {}; await loadSetup(true); loadOverview(); }));
    return b;
  };

  // ---- a. node & access
  function renderNodeStage(st, body) {
    const step = (key) => st.steps.find((s) => s.key === key);
    if (st.node === 'checking') { fill(body, skeleton('w-90', 'w-75', 'w-40')); return; }
    if (st.node === 'offline' || st.node === 'error') {
      const s = step('node') || step('error');
      fill(body,
        callout('bad', 'alert', st.node === 'offline' ? 'Your node isn’t answering yet' : 'The checks could not run',
          h('p', { text: st.node === 'offline'
            ? 'Right after installing, the node needs a few minutes to start. If it stays like this, look at it on the server:'
            : (s ? cap(s.what) : 'Try again in a minute.') })),
        h('ol', { class: 'steps-list' }, h('li', null, 'Is it running? ', h('code', { text: 'docker compose ps' })),
          h('li', null, 'What is it doing? ', h('code', { text: 'docker compose logs --tail 80 node' })),
          h('li', null, 'Start it: ', h('code', { text: 'docker compose up -d' }))),
        h('div', { class: 'stage-actions' }, recheckBtn()));
      return;
    }
    const identity = st.identity ? h('div', { class: 'field' }, h('span', { class: 'field-label', text: 'Your node’s identity' }), copyField(st.identity, 'identity'),
      h('p', { class: 'help', text: 'Share this (it’s public) with the Hydra team to get whitelisted, or with whoever sends you an invite.' })) : null;
    if (st.node === 'invite' || st.node === 'not_admitted') {
      const input = h('input', { id: 'invite-code', type: 'password', autocomplete: 'off', spellcheck: 'false', required: true });
      const btn = h('button', { class: 'btn btn-primary', type: 'submit' }, icon('key', 'i-sm'), h('span', { text: 'Redeem invite' }));
      const err = h('div', { class: 'callout callout-bad', hidden: true });
      const jobBox = h('div');
      const form = h('form', { class: 'inline-form', autocomplete: 'off', novalidate: true },
        h('div', { class: 'field' }, h('label', { for: 'invite-code', text: 'Invite code' }), input), btn);
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        showError(err, '');
        const code = input.value.trim();
        if (!code) { showError(err, 'Paste your invite code first.'); return; }
        busy(btn, async () => {
          try {
            const r = await api('/api/invite', { code });
            input.value = '';
            watchJob(r.job, jobBox, () => { S.sigs.node = null; loadSetup(true); loadOverview(); });
          } catch (ex) { if (ex.status !== 401) showError(err, ex.message); }
        });
      });
      fill(body,
        h('p', { class: 'lead', text: st.node === 'invite'
          ? 'Your node is running. Hydra mainnet is invite-only: enter the invite code an existing Hydra user gave you, and your node joins within a few minutes.'
          : 'Your node is running but isn’t admitted to mainnet yet. Redeem an invite code, or ask the Hydra team to whitelist the identity below.' }),
        form, err, jobBox, identity);
      if (st.job && st.job.kind === 'invite') watchJob(st.job, jobBox, () => { S.sigs.node = null; loadSetup(true); });
      return;
    }
    if (st.node === 'hub') {
      const s = step('hub');
      fill(body, callout('warn', 'alert', 'Connecting to the Hydranet hub',
        h('p', { text: 'Your node is admitted, but the hub isn’t reachable on every chain yet. This usually fixes itself; every check retries the connection.' }),
        s ? h('p', { class: 't-sm muted', text: s.what }) : null), h('div', { class: 'stage-actions' }, recheckBtn()), identity);
      return;
    }
    fill(body, h('ul', { class: 'list' }, st.steps.filter((s) => ['node', 'access', 'hub'].includes(s.key)).map((s) =>
      h('li', null, h('span', { class: 'dot-icon ok' }, icon('check')), h('div', { class: 'grow', text: cap(s.what) })))),
    identity ? h('details', null, h('summary', { class: 'muted t-sm', text: 'Show your node’s identity' }), identity) : null);
  }

  // ---- b. wallet backup
  function renderBackupStage(st, body) {
    const box = h('input', { type: 'checkbox', id: 'backup-ack' });
    box.checked = !!st.acks.backup;
    box.addEventListener('change', async () => {
      box.disabled = true;
      try {
        await api('/api/setup/ack', { item: 'backup', value: box.checked });
        toast(box.checked ? 'Thanks. Keep that paper somewhere safe.' : 'Marked as not backed up.');
        S.userPicked = false;
        await loadSetup();
        loadOverview();
      } catch (e) { if (e.status !== 401) { toast(e.message, true); box.checked = !box.checked; } }
      box.disabled = false;
    });
    fill(body,
      h('p', { class: 'lead', text: 'Your wallet has a 24-word recovery phrase. Whoever has it has your funds, and it’s the only way to get them back if this server is lost.' }),
      callout('info', 'shield', 'Where it is',
        h('p', null, 'On your server, in ', h('code', { text: '~/hydra-mm/easy/node/.env' }), ', on the line that starts with ', h('code', { text: 'MNEMONIC=' }),
          '. The file is readable only by you. This panel never shows it.')),
      h('ol', { class: 'steps-list' },
        h('li', null, 'On the server, open the file: ', h('code', { text: 'cat ~/hydra-mm/easy/node/.env' }), ' (make sure no one can see your screen).'),
        h('li', { text: 'Write the 24 words on paper, in order. Check each one twice.' }),
        h('li', { text: 'Keep the paper somewhere safe and offline. Never put it in a chat, an email, a photo or a cloud note.' })),
      h('label', { class: 'check' }, box, h('span', null, h('b', { text: 'I’ve written down my recovery phrase and stored it safely offline.' }),
        h('span', { class: 'help', text: st.acks.backup ? ` Confirmed on ${fmtWhen(st.acks.backup.at)}.` : ' Saved on the server, so you’re only asked once.' }))));
  }

  // ---- d. Telegram
  function renderTelegramStage(st, body) {
    const tg = st.telegram;
    clearTimeout(S.tgTimer);
    const tokenForm = () => {
      const input = h('input', { id: 'tg-token', type: 'password', autocomplete: 'off', spellcheck: 'false', placeholder: '123456789:AAE…' });
      const btn = h('button', { class: 'btn btn-primary', type: 'submit' }, icon('send', 'i-sm'), h('span', { text: 'Connect' }));
      const err = h('div', { class: 'callout callout-bad', hidden: true });
      const form = h('form', { class: 'inline-form', autocomplete: 'off', novalidate: true },
        h('div', { class: 'field' }, h('label', { for: 'tg-token', text: 'Bot token' }), input), btn);
      form.addEventListener('submit', (e) => {
        e.preventDefault();
        showError(err, '');
        busy(btn, async () => {
          try {
            await api('/api/telegram', { token: input.value.trim() });
            input.value = '';
            if (st.acks.telegram_skipped) await api('/api/setup/ack', { item: 'telegram_skipped', value: false });
            S.sigs.telegram = null;
            await loadSetup();
          } catch (ex) { if (ex.status !== 401) showError(err, ex.message); }
        });
      });
      return [form, err];
    };
    if (tg.state === 'connected') {
      fill(body, callout('ok', 'check-circle', 'Telegram is connected', h('p', { text: 'Alerts and the daily report go to your chat. Send /help to your bot to see the commands.' })),
        h('details', null, h('summary', { class: 'muted t-sm', text: 'Use a different bot' }), h('p', { class: 'help', text: 'Paste the new bot’s token; you’ll pair a chat again.' }), tokenForm()));
      return;
    }
    if (tg.state === 'waiting') {
      const cmd = `/start ${tg.waiting_code}`;
      const again = h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Get a new code' });
      again.addEventListener('click', () => busy(again, async () => { await api('/api/telegram', { token: '' }); S.sigs.telegram = null; await loadSetup(); }));
      fill(body,
        h('p', { class: 'lead', text: 'Open your bot in Telegram (or add it to a group) and send this message:' }),
        h('div', { class: 'copy-field' }, h('code', { class: 'code-big', text: cmd }), copyBtn(cmd, 'command')),
        h('p', { class: 'live', text: 'Waiting for your message. This page notices on its own.' }),
        h('div', { class: 'stage-actions' }, again, h('span', { class: 'muted t-sm', text: 'The code works once, for an hour.' })));
      S.tgTimer = setTimeout(pollTelegram, 4000);
      return;
    }
    const skip = h('button', { class: 'btn btn-ghost', type: 'button', text: st.acks.telegram_skipped ? 'Keep skipping' : 'Skip for now' });
    skip.addEventListener('click', () => busy(skip, async () => {
      await api('/api/setup/ack', { item: 'telegram_skipped', value: true });
      S.userPicked = false;
      S.sigs.telegram = null;
      await loadSetup();
    }));
    if (tg.state === 'unpaired') {
      const pair = h('button', { class: 'btn btn-primary', type: 'button' }, h('span', { text: 'Get a pairing code' }));
      pair.addEventListener('click', () => busy(pair, async () => { await api('/api/telegram', { token: '' }); S.sigs.telegram = null; await loadSetup(); }));
      fill(body, h('p', { class: 'lead', text: 'Your bot is set up, but no chat is paired with it yet.' }), h('div', { class: 'stage-actions' }, pair, skip));
      return;
    }
    fill(body,
      h('p', { class: 'lead', text: 'Optional, and recommended: alerts on your phone (trades, low capacity, leases, problems), a daily report, and /status, /pause and /resume from anywhere.' }),
      h('ol', { class: 'steps-list' },
        h('li', null, 'In Telegram, open ', h('b', { text: '@BotFather' }), ', send ', h('code', { text: '/newbot' }), ' and pick a name.'),
        h('li', { text: 'BotFather replies with a token. Paste it here.' })),
      tokenForm(), h('div', { class: 'stage-actions' }, skip));
  }
  async function pollTelegram() {
    if (S.tab !== 'setup' || S.openStage !== 'telegram') return;
    try {
      const t = await api('/api/telegram');
      if (t.state === 'connected') { toast('Telegram is connected.'); S.userPicked = false; S.sigs.telegram = null; await loadSetup(); loadOverview(); return; }
    } catch (e) { if (e.status === 401) return; }
    S.tgTimer = setTimeout(pollTelegram, 4000);
  }

  // ---- f. done
  function renderDoneStage(st, body) {
    if (st.complete) {
      fill(body, h('div', { class: 'success' }, h('div', { class: 'success-icon' }, icon('check')),
        h('h3', { text: 'Your market maker is running' }),
        h('p', { text: st.paused ? 'It’s paused right now: resume it from the dashboard or Controls.' : 'It quotes on your markets around the clock and earns the spread when people trade with it. Keep an eye on it from the dashboard.' }),
        h('button', { class: 'btn btn-primary btn-lg', type: 'button', onclick: () => showTab('dashboard') }, h('span', { text: 'Go to the dashboard' }), icon('arrow', 'i-sm'))));
      return;
    }
    if (st.stages.done === 'starting') {
      fill(body, h('div', { class: 'success' }, h('span', { class: 'spinner' }), h('h3', { text: 'Almost there' }),
        h('p', { text: 'Your channels are funded. The bot places its first quotes within a minute or two after the channels are ready.' }), recheckBtn()));
      return;
    }
    fill(body, emptyState('flag', 'Finish the steps above', 'When your node is funded, this is where you’ll see it start.'));
  }

  // ---- c. plan (static form + live preview)
  async function loadSetupInfo() {
    try {
      S.setupInfo = await api('/api/setup/info');
      buildPlanForm(S.setupInfo);
    } catch (e) {
      if (e.status !== 401) showError($('plan-error'), e.message);
    }
  }

  function buildPlanForm(info) {
    const cur = info.current || {};
    const budget = cur.budget || 1000;
    $('plan-budget').min = String(info.min_budget);
    $('plan-budget').value = String(budget);
    $('plan-budget-range').value = String(Math.min(budget, 25000));
    const preset = cur.preset || 'balanced';
    fill($('plan-presets'), info.presets.map((p) => {
      const r = h('input', { type: 'radio', name: 'preset', value: p.name });
      r.checked = p.name === preset;
      return h('label', { class: 'choice' }, r, h('span', { class: 'choice-card' },
        h('span', { class: 'choice-top' }, h('span', { class: 'choice-name', text: p.name }), h('span', { class: 'choice-mark' }, icon('check'))),
        h('span', { class: 'choice-text', text: p.text }),
        h('ul', { class: 'facts' },
          h('li', null, 'Quotes per side', h('b', { text: String(p.levels) })),
          h('li', null, 'First quote', h('b', { text: `${p.half_spread_pct}%` })),
          isNum(p.stable_half_spread_pct) ? h('li', { class: 'sub' }, 'on USDC/USDC', h('b', { text: `${p.stable_half_spread_pct}%` })) : null,
          h('li', null, 'Arbitrage', h('b', { text: p.arbitrage_stable ? 'USDC/USDC' : 'Off' })),
          h('li', null, 'Budget in use', h('b', { text: `${p.usage_pct}%` })))));
    }));
    const chosen = cur.markets || info.markets.map((m) => m.pair);
    fill($('plan-markets'), info.markets.map((m) => {
      const c = h('input', { type: 'checkbox', name: 'market', value: m.pair });
      c.checked = chosen.includes(m.pair);
      return h('label', { class: 'choice' }, c, h('span', { class: 'choice-card' },
        h('span', { class: 'choice-top' }, h('span', { class: 'choice-name pair', text: m.pair }), h('span', { class: 'choice-mark square' }, icon('check'))),
        h('span', { class: 'choice-text', text: m.text }),
        h('ul', { class: 'facts' },
          h('li', null, 'Share of the budget', h('b', { text: `${m.share_pct}%` })),
          h('li', null, 'Your funds on', h('b', { text: (m.chains || []).map((c) => CHAIN_NAME[c] || c).join(' + ') })))));
    }));
    schedulePreview(0);
  }
  function planBody() {
    return {
      budget: $('plan-budget').value.trim(),
      preset: (document.querySelector('input[name="preset"]:checked') || {}).value || 'balanced',
      markets: [...document.querySelectorAll('input[name="market"]:checked')].map((c) => c.value),
    };
  }
  function schedulePreview(delay) {
    clearTimeout(S.planTimer);
    $('plan-save').disabled = true;
    $('plan-preview').classList.add('is-loading');
    S.planTimer = setTimeout(previewPlan, delay === undefined ? 450 : delay);
  }

  async function previewPlan() {
    const body = planBody(), seq = ++S.planSeq, el = $('plan-preview');
    showError($('plan-error'), '');
    const budget = Number(body.budget);
    if (!body.markets.length || !isFinite(budget) || budget <= 0) {
      S.planResult = null;
      el.classList.remove('is-loading');
      fill(el, emptyState('layers', !body.markets.length ? 'Pick at least one market' : 'Enter a budget', 'The preview appears here.'));
      updateSaveState();
      return;
    }
    if (!el.childNodes.length) fill(el, h('h3', { text: 'Your plan' }), skeleton('tall', 'w-90', 'w-75', 'w-60', 'w-90'));
    try {
      const r = await api('/api/plan', body);
      if (seq !== S.planSeq) return;
      S.planResult = r.plan;
      S.planBody = body;
      renderPreview(r.plan);
    } catch (e) {
      if (seq !== S.planSeq || e.status === 401) return;
      S.planResult = null;
      fill(el, callout('warn', 'alert', 'No preview', e.message));
    }
    el.classList.remove('is-loading');
    updateSaveState();
  }

  function renderPreview(p) {
    const byChain = {};
    for (const a of p.assets) (byChain[a.chain] = byChain[a.chain] || []).push(a);
    fill($('plan-preview'),
      h('h3', null, h('span', { text: `Plan for ${fmtUsd0(p.budget_usd)}` }), pill(cap(p.preset), 'info', 'no-dot')),
      h('dl', { class: 'preview-figs' },
        h('div', null, h('dt', { text: 'You deposit' }), h('dd', { text: fmtUsd0(p.deposit_usd) })),
        h('div', null, h('dt', { text: 'Leased room to receive' }), h('dd', { text: fmtUsd0(p.inbound_usd) })),
        h('div', null, h('dt', { text: 'Lease cost per week' }), h('dd', { text: fmtUsd(p.lease_cost_week_usd) })),
        h('div', null, h('dt', { text: 'Markets' }), h('dd', { text: String(p.markets.length) }))),
      h('div', { class: 'preview-section' }, h('h4', { text: 'What you deposit, per network' }),
        Object.entries(byChain).map(([chain, assets]) => h('div', { class: 'chain-row' },
          h('b', { text: CHAIN_NAME[chain] || chain }), h('span', { class: 'r', text: fmtUsd0(assets.reduce((s, a) => s + a.own_usd, 0)) }),
          h('span', { class: 'sub wide', text: 'Deposit ' + assets.map((a) => `${fmtAmt(a.own)} ${a.asset}`).join(' + ') }),
          h('span', { class: 'sub wide', text: 'Leased ' + assets.map((a) => `${fmtAmt(a.inbound)} ${a.asset}`).join(' + ') })))),
      p.markets.length ? h('div', { class: 'preview-section' }, h('h4', { text: 'Quotes' }),
        p.markets.map((m) => h('div', { class: 'chain-row' }, h('b', { text: m.pair }), h('span', { class: 'r', text: `from ${m.half_spread_pct}%` }),
          h('span', { class: 'sub wide', text: `${m.levels} quotes per side of ${fmtAmt(m.size)} ${m.base}${m.arbitrage ? ', arbitrage on' : ''}` })))) : null,
      p.warnings.length ? h('div', { class: 'preview-section' }, h('ul', { class: 'warn-list' }, p.warnings.map((w) => h('li', null, icon('alert', 'i-sm'), h('span', { text: w }))))) : null);
  }

  function updateSaveState() {
    const st = S.setup, info = S.setupInfo, p = S.planResult;
    const btn = $('plan-save'), hint = $('plan-save-hint');
    const nodeUp = st && !['offline', 'checking', 'error'].includes(st.node);
    const budget = Number($('plan-budget').value);
    let why = '';
    if (!p) why = 'Waiting for the preview.';
    else if (!p.markets.length) why = 'Nothing to quote with this plan: raise the budget or pick fewer markets.';
    else if (info && budget < info.min_budget) why = `The minimum budget is ${fmtUsd0(info.min_budget)}.`;
    else if (!nodeUp) why = 'Your node needs to be running to save (step 1).';
    else if (jobRunningIn($('plan-job'))) why = 'Saving…';
    btn.disabled = !!why;
    hint.textContent = why || (st && st.configured ? 'Saving replaces your current plan; the old settings are kept as a backup.' : 'No money moves yet. Funding is step 5.');
    $('plan-save').textContent = st && st.configured ? 'Save new plan' : 'Save plan';
  }

  function syncPlanStage(st) {
    if (!S.setupInfo) return;
    updateSaveState();
    if (st.job && st.job.kind === 'setup' && !S.watching[st.job.id]) watchJob(st.job, $('plan-job'), afterSave);
  }

  async function savePlan() {
    const p = S.planBody, plan = S.planResult;
    if (!p || !plan) return;
    const yes = await confirmModal({
      title: S.setup && S.setup.configured ? 'Save your new plan?' : 'Save this plan?',
      body: [h('ul', null, h('li', { text: `Budget: ${fmtUsd0(Number(p.budget))}, ${p.preset}` }), h('li', { text: `Markets: ${p.markets.join(', ')}` }),
        h('li', { text: `You deposit about ${fmtUsd0(plan.deposit_usd)}; leases cost about ${fmtUsd(plan.lease_cost_week_usd)} a week` })),
      h('p', { text: S.setup && S.setup.configured ? 'This replaces your current market settings; the old file is kept as a backup. No money moves.' : 'No money moves yet: funding is a separate step.' })],
      confirmLabel: 'Save plan',
    });
    if (!yes) return;
    await busy($('plan-save'), async () => {
      try {
        const r = await api('/api/setup', p);
        watchJob(r.job, $('plan-job'), afterSave);
      } catch (ex) {
        if (ex.status !== 401) showError($('plan-error'), ex.message);
      }
    });
  }
  async function afterSave(j) {
    if (j.status === 'done') toast('Your plan is saved.');
    S.userPicked = false;
    S.sigs = {};
    S.setupInfo = await api('/api/setup/info').catch(() => S.setupInfo);
    await loadSetup(true);
    loadOverview();
  }

  // ---- e. funding
  function renderFundingStage(st, body) {
    if (!st.configured) {
      S.sigs.funding = 'none';
      fill(body, emptyState('wallet', 'Save your plan first', 'Then this step shows exactly what to send, and where.'));
      return;
    }
    if (S.sigs.funding !== 'live' || !$('funding-live')) {
      S.sigs.funding = 'live';
      fill(body,
        h('p', { class: 'lead', text: 'Send these amounts to your node, each on exactly the network shown. This page checks every 15 seconds and ticks them off as they arrive.' }),
        h('div', { id: 'funding-live', class: 'stack-sm' }, skeleton('tall', 'w-90', 'w-75')),
        h('div', { id: 'funding-open' }),
        h('div', { id: 'fund-job' }));
      if (st.job && (st.job.kind === 'fund-go' || st.job.kind === 'fund')) watchJob(st.job, $('fund-job'), afterFund);
    }
    loadFunding();
  }

  let fundingBusy = false;
  async function loadFunding() {
    const live = $('funding-live');
    if (!live || fundingBusy) return;
    fundingBusy = true;
    try {
      const f = await api('/api/funding');
      if (!f.configured) return;
      renderFunding(f);
    } catch (e) {
      if (e.status !== 401) fill(live, callout('warn', 'alert', 'Can’t check the funding right now', e.message));
    } finally { fundingBusy = false; }
  }

  const FUND_STATE = { waiting: ['Waiting', 'warn'], arrived: ['Arrived', 'ok'], in_channels: ['In your channels', 'ok'] };
  function renderFunding(f) {
    const live = $('funding-live');
    fill(live,
      h('div', { class: 'chains' }, f.chains.map((c) => {
        const [label, kind] = FUND_STATE[c.state] || ['', 'muted'];
        return h('div', { class: 'chain-card', 'data-state': c.state },
          h('div', { class: 'chain-head' }, h('h4', { text: CHAIN_NAME[c.chain] || c.chain }), pill(label, kind, c.state === 'waiting' ? 'pill-live' : '')),
          c.items.map((i) => i.state === 'waiting'
            ? h('div', { class: 'chain-item' }, h('div', null, h('div', { class: 'amt', text: `${fmtAmt(i.missing)} ${i.asset}` }),
              h('div', { class: 'sub', text: i.in_wallet > 0 ? `${fmtAmt(i.in_wallet)} of ${fmtAmt(i.planned)} arrived so far` : `about ${fmtUsd0(i.missing_usd)}` })), pill('To send', 'muted', 'no-dot'))
            : h('div', { class: 'chain-item done' }, h('div', null, h('div', { class: 'amt' }, icon('check', 'i-sm'), ` ${fmtAmt(i.planned)} ${i.asset}`),
              h('div', { class: 'sub', text: i.state === 'in_channels' ? 'In your channels' : 'Arrived in the node wallet' })))),
          c.state !== 'in_channels' && c.address ? copyField(c.address, `${c.chain} address`) : null,
          isNum(c.gas) && c.chain === 'ethereum' && c.items.some((i) => i.asset === 'ETH' && i.state !== 'in_channels')
            ? h('p', { class: 'help', text: `Gas in the wallet: ${fmtAmt(c.gas)} ETH (keep about 0.003 ETH extra).` }) : null);
      })),
      h('p', { class: 'live', text: `Checks every 15 seconds. Last check ${fmtClock(f.checked_at)}.` }),
      f.notes.length && !f.funded ? h('ul', { class: 'warn-list note-list' }, f.notes.map((n) => h('li', null, icon('info', 'i-sm'), h('span', { text: n })))) : null);
    renderFundingOpen(f);
  }

  function renderFundingOpen(f) {
    const box = $('funding-open');
    if (!box || jobRunningIn($('fund-job'))) return;
    const sig = JSON.stringify([f.funded, f.all_arrived, f.waiting]);
    if (box.dataset.sig === sig && box.childNodes.length) return;
    box.dataset.sig = sig;
    if (f.funded) {
      fill(box, callout('ok', 'check-circle', 'Your channels are funded',
        h('p', { text: `They hold ${fmtUsd0(f.have_usd)} of the ${fmtUsd0(f.plan_usd)} your plan needs. To add funds later, raise the budget in your plan, save it, and come back here.` })));
      return;
    }
    const open = h('button', { class: 'btn btn-danger btn-lg', type: 'button', disabled: !f.all_arrived }, icon('wallet', 'i-sm'), h('span', { text: 'Open channels' }));
    open.addEventListener('click', () => fundGo(open));
    if (!f.all_arrived) {
      fill(box, h('div', { class: 'stage-actions' }, open,
        h('span', { class: 'muted t-sm', text: `Waiting for ${f.waiting} deposit${f.waiting === 1 ? '' : 's'}. The button unlocks when ${f.waiting === 1 ? 'it has' : 'they have'} arrived (Bitcoin needs a few confirmations).` })));
      return;
    }
    const fees = h('div', null, skeleton('w-90', 'w-75'));
    fill(box, callout('ok', 'check-circle', 'Everything has arrived', h('p', { text: 'Review the fees below, then open your channels. This deposits your funds and leases room to receive for 7 days (renewed automatically).' })),
      fees, h('div', { class: 'stage-actions' }, open));
    loadFees(fees);
  }

  async function loadFees(el) {
    try {
      const r = await api('/api/funding/fees');
      S.fees = r;
      if (r.nothing_to_do) { fill(el, callout('ok', 'check', null, 'The channels already hold what your plan needs.')); return; }
      fill(el, h('div', { class: 'table-flat' }, stackTable([['Network'], ['What happens'], ['Fee', 'r']], r.requests.map((q) => h('tr', null,
        h('td', { class: 'cell-strong', text: CHAIN_NAME[q.chain] || q.chain }),
        h('td', null, q.assets.map((a) => h('div', { text: [a.deposit > 0 ? `deposit ${fmtAmt(a.deposit)} ${a.asset}` : '',
          a.lease > 0 ? `lease ${fmtAmt(a.lease)} ${a.asset} of room to receive` : ''].filter(Boolean).join(', then ').replace(/^./, (c) => c.toUpperCase()) })),
        h('span', { class: 'sub', text: `Leases run ${Math.round(q.hours / 24)} days and renew automatically` })),
        h('td', { class: 'r' }, q.error ? h('span', { class: 'neg', text: `Not possible yet: ${q.error}` })
          : h('span', { class: 'num', text: q.fee === null ? 'after the approval' : `${fmtAmt(q.fee)} ${q.fee_asset}` }),
        q.error ? null : h('span', { class: 'sub', text: q.how })))))));
    } catch (e) {
      if (e.status !== 401) fill(el, callout('warn', 'alert', 'No fee estimate right now', e.message));
    }
  }

  async function fundGo(btn) {
    const fees = (S.fees && S.fees.requests) || [];
    const yes = await confirmModal({
      title: 'Open and fund your channels?',
      body: [h('p', { text: 'This moves your money: the node deposits your funds into channels with the Hydra hub and leases room to receive.' }),
        fees.length ? h('ul', null, fees.map((q) => h('li', { text: `${CHAIN_NAME[q.chain] || q.chain}: fee ${q.fee === null ? 'after the approval' : `${fmtAmt(q.fee)} ${q.fee_asset}`}` }))) : null,
        h('p', { text: 'It can take several minutes. You can watch every step here; it keeps going if you close the page.' })],
      confirmLabel: 'Open channels', danger: true, typeWord: 'OPEN',
    });
    if (!yes) return;
    await busy(btn, async () => {
      try {
        const r = await api('/api/fund/go', { confirm: true });
        watchJob(r.job, $('fund-job'), afterFund);
      } catch (e) {
        if (e.status !== 401) toast(e.message, true);
      }
    });
  }
  async function afterFund(j) {
    if (j.status === 'done') toast('Done. Channels activate after a few confirmations.');
    S.userPicked = false;
    const box = $('funding-open');
    if (box) box.dataset.sig = '';
    await loadSetup(true);
    loadOverview();
  }

  // ================================================================ volume
  async function loadVolume() {
    clearTimeout(S.volTimer);
    let r;
    try { r = await api('/api/volume'); } catch (e) {
      if (e.status !== 401) showError($('vol-unavailable'), e.message);
      return;
    }
    S.volume = r;
    if (!r.available) {
      fill($('vol-unavailable'), icon('alert'), h('div', { text: `Volume mode isn’t available on this installation${r.reason ? ` (${r.reason})` : ''}.` }));
      $('vol-unavailable').hidden = false;
      $('vol-body').hidden = true;
      return;
    }
    $('vol-unavailable').hidden = true;
    $('vol-body').hidden = false;
    renderVolume(r);
    if (r.status.running && S.tab === 'volume') S.volTimer = setTimeout(loadVolume, 3000);
  }

  function costFor(market, usd) {
    const c = ((S.volume && S.volume.status.cost_per_1000) || {})[market];
    return isNum(c) ? { per1000: c, est: c * usd / 1000 } : null;
  }
  function resultText(res) {
    if (!res) return '';
    if (res === 'done') return 'finished';
    if (res === 'stopped') return 'stopped by you';
    if (res.startsWith('aborted')) return 'stopped early: ' + res.replace(/^aborted:\s*/, '');
    return res;
  }

  function renderVolume(r) {
    const st = r.status, run = st.running, last = st.last_run, cfg = st.config || {}, mk = cfg.markets || {};
    const today = Object.values(st.today || {});
    const tv = today.reduce((s, x) => s + (x.volume_usd || 0), 0), tc = today.reduce((s, x) => s + (x.cost_usd || 0), 0);
    const target = Object.values(mk).reduce((s, x) => s + (x.daily_usd || 0), 0);
    const cheapest = Object.entries(st.cost_per_1000 || {}).sort((a, b) => a[1] - b[1])[0];
    fill($('vol-kpis'),
      kpiTile('bars', 'Volume today', fmtUsd(tv), `${today.reduce((s, x) => s + (x.rounds || 0), 0)} rounds`, tv > 0 ? 'info' : ''),
      kpiTile('wallet', 'Fees today', fmtUsd(tc), tv > 0 ? `${fmtUsd(tc / tv * 1000)} per $1,000` : 'Nothing yet', ''),
      kpiTile('clock', 'Daily target', target > 0 ? fmtUsd0(target) : 'Off', cfg.enabled ? `bursts every ${cfg.burst_every_min} min` : 'Targets are off', cfg.enabled && target > 0 ? 'ok' : ''),
      kpiTile('trend', 'Cheapest market', cheapest ? cheapest[0] : '–', cheapest ? `${fmtUsd(cheapest[1])} per $1,000` : '', '', 'sm'));
    const runEl = $('vol-running');
    runEl.hidden = !run;
    if (run) {
      const frac = run.target_usd > 0 ? Math.min(1, run.volume_usd / run.target_usd) : 0;
      const bar = h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(Math.round(frac * 100)) }, h('span'));
      requestAnimationFrame(() => { bar.firstChild.style.width = Math.round(frac * 100) + '%'; });
      const stop = h('button', { class: 'btn btn-warn', type: 'button' }, icon('pause', 'i-sm'), h('span', { text: 'Stop after this round' }));
      stop.addEventListener('click', () => busy(stop, async () => {
        try { toast((await api('/api/volume/stop', {})).message); } catch (e) { if (e.status !== 401) toast(e.message, true); }
        loadVolume();
      }));
      fill(runEl, h('div', { class: 'card-head' }, h('h2', { class: 'card-title' }, h('span', { class: 'spinner' }), `Running on ${run.market}`), stop),
        bar, h('dl', { class: 'stats' },
          h('div', null, h('dt', { text: 'Volume' }), h('dd', { text: `${fmtUsd(run.volume_usd)} of ${fmtUsd0(run.target_usd)}` })),
          h('div', null, h('dt', { text: 'Rounds' }), h('dd', { text: String(run.rounds ?? 0) })),
          h('div', null, h('dt', { text: 'Fees so far' }), h('dd', { text: fmtUsd(run.cost_usd) })),
          h('div', null, h('dt', { text: 'Started' }), h('dd', { text: fmtWhen(run.started_at) })),
          h('div', null, h('dt', { text: 'Started by' }), h('dd', { text: run.source === 'daily' ? 'Daily target' : 'You' }))),
        run.message ? h('p', { class: 'muted t-sm', text: run.message }) : null);
    }
    const lastEl = $('vol-last');
    if (last && !run) {
      const aborted = (last.result || '').startsWith('aborted');
      fill(lastEl, callout(aborted ? 'warn' : 'ok', aborted ? 'alert' : 'check-circle', `Last run ${resultText(last.result)}`,
        h('p', { class: 't-sm', text: `${last.market}: ${fmtUsd(last.volume_usd)} in ${last.rounds} rounds, fees ${fmtUsd(last.cost_usd)}${last.ended_at ? `, ${fmtWhen(last.ended_at)}` : ''}.` })));
      lastEl.hidden = false;
    } else lastEl.hidden = true;
    const sel = $('vol-market'), prev = sel.value;
    fill(sel, r.markets.map((m) => h('option', { value: m, text: m })));
    if (prev && r.markets.includes(prev)) sel.value = prev;
    $('vol-start').disabled = !!run;
    updateVolCost();
    fill($('vol-today'), stackTable([['Market'], ['Volume', 'r'], ['Fees', 'r'], ['Rounds', 'r'], ['Per $1,000', 'r'], ['Daily target', 'r']], r.markets.map((m) => {
      const t = (st.today || {})[m] || {};
      const c = (st.cost_per_1000 || {})[m];
      return h('tr', null, h('td', { class: 'cell-strong', text: m }), h('td', { class: 'r num', text: fmtUsd(t.volume_usd || 0) }),
        h('td', { class: 'r num', text: fmtUsd(t.cost_usd || 0) }), h('td', { class: 'r num', text: String(t.rounds || 0) }),
        h('td', { class: 'r num', text: isNum(c) ? fmtUsd(c) : '–' }),
        h('td', { class: 'r num', text: (mk[m] && mk[m].daily_usd > 0) ? `${fmtUsd0(mk[m].daily_usd)}${cfg.enabled ? '' : ' (off)'}` : '–' }));
    })));
    if (!$('vol-cfg-form').contains(document.activeElement)) renderVolumeConfig(r);
    const hist = (st.history || []).slice().reverse();
    fill($('vol-history'), hist.length ? stackTable([['Date'], ['Market'], ['Volume', 'r'], ['Fees', 'r'], ['Rounds', 'r']],
      hist.map((x) => h('tr', null, h('td', { class: 'num', text: x.date }), h('td', { text: x.market }),
        h('td', { class: 'r num', text: fmtUsd(x.volume_usd) }), h('td', { class: 'r num', text: fmtUsd(x.cost_usd) }),
        h('td', { class: 'r num', text: String(x.rounds) })))) : emptyState('trend', 'No volume yet', 'Runs and daily targets show up here.'));
  }
  function kpiTile(iconName, label, value, sub, tone, size) {
    return h('article', { class: 'kpi', 'data-tone': tone || '' }, h('div', { class: 'kpi-label' }, icon(iconName, 'i-sm'), label),
      h('div', { class: 'kpi-value' + (size ? ' ' + size : ''), text: value, title: value }), h('div', { class: 'kpi-sub', text: sub || '' }));
  }

  function updateVolCost() {
    const m = $('vol-market').value, usd = Number($('vol-usd').value);
    const c = isNum(usd) && usd > 0 ? costFor(m, usd) : null;
    $('vol-cost').textContent = c ? `Estimated fees: about ${fmtUsd(c.est)} (${fmtUsd(c.per1000)} per $1,000 on this market).`
      : (costFor(m, 1) ? 'Enter an amount to see the estimated fees.' : 'No fee estimate for this market yet.');
  }

  async function startVolume(e) {
    e.preventDefault();
    showError($('vol-run-error'), '');
    const market = $('vol-market').value, usdRaw = $('vol-usd').value.trim(), sizeRaw = $('vol-size').value.trim();
    const usd = Number(usdRaw);
    if (!usdRaw || !isFinite(usd) || usd <= 0) { showError($('vol-run-error'), 'Enter how many dollars of volume to create.'); return; }
    const c = costFor(market, usd);
    const strat = (S.volume.strategies || {})[market];
    const yes = await confirmModal({
      title: 'Start a volume run?',
      body: [h('ul', null, h('li', { text: `Market: ${market}` }), h('li', { text: `Volume: ${fmtUsd0(usd)}` }),
        h('li', { text: c ? `Estimated fees: about ${fmtUsd(c.est)}` : 'Fee estimate: not available yet' })),
      h('p', { text: strat ? `The market maker on ${market} pauses until the run ends.` : 'Your node trades with itself; you can stop it any time.' })],
      confirmLabel: 'Start run',
    });
    if (!yes) return;
    await busy($('vol-start'), async () => {
      try {
        const body = { market, usd: usdRaw, confirm: true };
        if (sizeRaw) body.size = sizeRaw;
        toast((await api('/api/volume/start', body)).message);
      } catch (ex) {
        if (ex.status !== 401) showError($('vol-run-error'), ex.message);
      }
    });
    loadVolume();
  }

  function renderVolumeConfig(r) {
    const form = $('vol-cfg-form');
    const cfg = JSON.parse(JSON.stringify(r.status.config || {}));
    cfg.markets = cfg.markets || {};
    const enabled = switchField('vc-enabled', 'Daily targets on', cfg.enabled);
    const pauseMm = switchField('vc-pause', 'Pause the market maker during a run', cfg.pause_market_maker !== false);
    const burst = numberField('vc-burst', 'A burst every', 'minutes', cfg.burst_every_min, null, { step: '1', min: 5, max: 240 });
    const hours = Array.isArray(cfg.active_hours) ? cfg.active_hours : [0, 24];
    const from = h('input', { id: 'vc-from', type: 'number', step: '1', min: '0', max: '23', 'aria-label': 'From hour' });
    const to = h('input', { id: 'vc-to', type: 'number', step: '1', min: '1', max: '24', 'aria-label': 'To hour' });
    from.value = String(hours[0]); to.value = String(hours[1]);
    const rowInputs = {};
    const rows = r.markets.map((m) => {
      const v = cfg.markets[m] || {};
      const daily = h('input', { type: 'number', step: 'any', min: '0', inputmode: 'decimal', 'aria-label': `Daily target for ${m}` });
      daily.value = String(v.daily_usd ?? 0);
      const size = h('input', { type: 'number', step: 'any', min: '0', inputmode: 'decimal', 'aria-label': `Largest round for ${m}` });
      size.value = v.max_size === undefined || v.max_size === null ? '' : String(v.max_size);
      rowInputs[m] = { daily, size };
      return h('tr', null, h('td', { class: 'cell-strong', text: m }), h('td', null, h('div', { class: 'with-unit' }, daily, h('span', { text: 'USD a day' }))),
        h('td', null, h('div', { class: 'with-unit' }, size, h('span', { text: m.split('/')[0] }))));
    });
    const err = h('div', { class: 'callout callout-bad', hidden: true });
    const save = h('button', { class: 'btn btn-primary', type: 'submit' }, h('span', { text: 'Save daily targets' }));
    fill(form,
      h('div', { class: 'vol-cfg-head' }, enabled.el, pauseMm.el, burst.el,
        h('div', { class: 'field' }, h('label', { for: 'vc-from', text: 'Active between (UTC hours)' }),
          h('div', { class: 'hours' }, from, h('span', { class: 'muted', text: 'and' }), to))),
      h('div', { class: 'table-flat' }, stackTable([['Market'], ['Daily target'], ['Largest round']], rows)),
      h('div', { class: 'actions' }, save), err);
    form.onsubmit = async (e) => {
      e.preventDefault();
      showError(err, '');
      const num = (el, label) => {
        const raw = el.value.trim(), v = Number(raw);
        if (raw === '' || !isFinite(v)) throw new Error(`${label}: enter a number.`);
        return v;
      };
      let out;
      try {
        out = Object.assign({}, cfg, { enabled: enabled.input.checked, pause_market_maker: pauseMm.input.checked,
          burst_every_min: num(burst.input, 'Burst interval'), active_hours: [num(from, 'Active from'), num(to, 'Active until')],
          markets: Object.assign({}, cfg.markets) });
        for (const m of r.markets) {
          const cur = Object.assign({}, cfg.markets[m] || {});
          cur.daily_usd = num(rowInputs[m].daily, `${m} daily target`);
          if (rowInputs[m].size.value.trim() !== '') cur.max_size = num(rowInputs[m].size, `${m} largest round`);
          out.markets[m] = cur;
        }
      } catch (ex) { showError(err, ex.message); return; }
      await busy(save, async () => {
        try {
          toast((await api('/api/volume/config', { config: out })).message);
          if (document.activeElement) document.activeElement.blur();
          loadVolume();
        } catch (ex) {
          if (ex.status !== 401) showError(err, ex.message);
        }
      });
    };
  }

  // ================================================================ start
  function wire() {
    for (const b of document.querySelectorAll('.nav-item')) b.addEventListener('click', () => { S.navigated = true; showTab(b.dataset.tab); });
    $('brand').addEventListener('click', (e) => { e.preventDefault(); showTab(S.overview && S.overview.setup && !S.overview.setup.complete ? 'setup' : 'dashboard'); });
    $('unlock-form').addEventListener('submit', (e) => {
      e.preventDefault();
      const v = $('unlock-key').value.trim();
      if (!v) return;
      setToken(v);
      $('unlock-key').value = '';
      start();
    });
    $('refresh').addEventListener('click', (e) => busy(e.currentTarget, () => loadDashboard()));
    $('doctor-recheck').addEventListener('click', (e) => busy(e.currentTarget, () => loadOverview(true)));
    $('pause-all').addEventListener('click', (e) => busy(e.currentTarget, () => doPause(true, null)));
    $('resume-all').addEventListener('click', (e) => busy(e.currentTarget, () => doPause(false, null)));
    // plan: every change re-plans (debounced)
    $('plan-form').addEventListener('input', (e) => {
      if (e.target.id === 'plan-budget-range') $('plan-budget').value = e.target.value;
      if (e.target.id === 'plan-budget') $('plan-budget-range').value = String(Math.min(Number(e.target.value) || 0, 25000));
      schedulePreview();
    });
    $('plan-form').addEventListener('change', (e) => {
      if (e.target.type === 'radio' || e.target.type === 'checkbox') schedulePreview(150);
    });
    $('plan-form').addEventListener('submit', (e) => e.preventDefault());
    $('plan-save').addEventListener('click', savePlan);
    $('vol-run-form').addEventListener('submit', startVolume);
    $('vol-market').addEventListener('change', updateVolCost);
    $('vol-usd').addEventListener('input', updateVolCost);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshTick(); });
    setInterval(refreshTick, REFRESH_MS);
  }

  async function start() {
    if (!getToken()) { lock(); return; }
    unlock();
    const want = location.hash.replace(/^#/, '');
    if (TABS.includes(want)) { S.homeDecided = true; showTab(want); if (want !== 'dashboard') loadOverview(); return; }
    const o = await loadOverview();                     // decides home once the checks have run
    if (o === null && !getToken()) return;
    showTab(o && o.setup && !o.setup.complete && !o.doctor.pending ? 'setup' : 'dashboard');
  }

  takeTokenFromHash();
  wire();
  start();
})();
