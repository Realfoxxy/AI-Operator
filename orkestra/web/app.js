/* Orkestra görsel çalışma alanı — saf JavaScript, bağımlılık yok (DOKUMANTASYON.md bölüm 10).
 *
 * Bölümler:
 *   (0) DOM yardımcıları
 *   (1) API istemcisi ve token yönetimi
 *   (2) SSE istemcisi (üstel geri çekilmeyle yeniden bağlanma)
 *   (3) Durum deposu: olay → durum indirgeyici (saf fonksiyon)
 *   (4) Oluşturucular: üst çubuk, ofis, bağlantılar, sohbet, pano, grafik, dosyalar, doğrulama, rapor, modal
 *   (5) Güvenli Markdown dönüştürücü (yalnızca düğüm üretir)
 *   (6) Başlatma ve kullanıcı etkileşimleri
 *
 * Güvenlik (kural 24): model ve kullanıcı metinleri DOM'a yalnızca textContent veya
 * document.createElement ile kurulan düğümlerle eklenir. HTML dizgesi hiçbir yerde ayrıştırılmaz.
 * Dürüst görselleştirme (10.1): her animasyon ve durum, sunucudan gelen gerçek bir olaydan türetilir.
 */
'use strict';

(function () {
  // ===================================================================== (0) DOM yardımcıları

  const $ = (id) => document.getElementById(id);
  const ATTRS = new Set(['role', 'type', 'id', 'title', 'tabindex', 'for', 'value', 'name', 'lang',
    'aria-label', 'aria-current', 'aria-expanded', 'aria-selected', 'aria-controls', 'aria-hidden']);

  function el(tag, opts, ...children) {
    const n = document.createElement(tag);
    if (opts) {
      for (const [k, v] of Object.entries(opts)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') n.className = v;
        else if (k === 'text') n.textContent = String(v);
        else if (k === 'agent') n.style.setProperty('--agent', v);
        else if (k.startsWith('data-') || ATTRS.has(k)) n.setAttribute(k, v === true ? '' : String(v));
      }
    }
    for (const c of children) add(n, c);
    return n;
  }

  function add(n, c) {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) { c.forEach((x) => add(n, x)); return; }
    n.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
  }

  function svgEl(tag, attrs) {
    const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, String(v));
    return n;
  }

  const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); };
  const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function fmtDuration(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const pad = (x) => String(x).padStart(2, '0');
    return h ? `${h}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
  }

  function parseTime(z) {
    const t = Date.parse(z || '');
    return Number.isFinite(t) ? t : Date.now();
  }

  function announce(text) {
    const live = $('live');
    live.textContent = '';
    window.setTimeout(() => { live.textContent = text; }, 30);
  }

  // ===================================================================== (1) API istemcisi ve token

  const TOKEN_KEY = 'orkestra-token';

  function initToken() {
    const url = new URL(window.location.href);
    let t = url.searchParams.get('t');
    if (t) {
      try { sessionStorage.setItem(TOKEN_KEY, t); } catch (e) { /* depolama kapalı olabilir */ }
      url.searchParams.delete('t');
      history.replaceState(null, '', url.pathname + url.search + url.hash);   // token adres çubuğundan silinir
    } else {
      try { t = sessionStorage.getItem(TOKEN_KEY); } catch (e) { t = null; }
    }
    return t || '';
  }

  const TOKEN = initToken();

  async function api(method, path, body) {
    const opts = { method, headers: { 'X-Orkestra-Token': TOKEN }, cache: 'no-store', credentials: 'same-origin' };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const err = new Error((data && data.hata) || `İstek başarısız (HTTP ${res.status}).`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // ===================================================================== (2) SSE istemcisi

  const sse = { es: null, delay: 1000, timer: null };

  function setConn(kind) {
    const c = $('conn');
    c.classList.toggle('ok', kind === 'ok');
    c.classList.toggle('bad', kind === 'bad');
    $('conn-text').textContent = kind === 'ok' ? 'Bağlı' : kind === 'bad' ? 'Bağlantı koptu, yeniden deneniyor…' : 'Bağlanıyor…';
  }

  function connect(sonId) {
    if (sse.es) sse.es.close();
    const es = new EventSource(`/api/olaylar?t=${encodeURIComponent(TOKEN)}&son=${encodeURIComponent(sonId)}`);
    sse.es = es;
    es.addEventListener('open', () => { sse.delay = 1000; setConn('ok'); });
    es.addEventListener('orkestra', (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch (err) { return; }
      dispatch(ev, true);
    });
    es.addEventListener('error', () => {
      if (sse.es !== es) return;
      es.close();
      sse.es = null;
      setConn('bad');
      scheduleResync();
    });
  }

  function scheduleResync() {
    window.clearTimeout(sse.timer);
    sse.timer = window.setTimeout(resync, sse.delay);
    sse.delay = Math.min(sse.delay * 2, 15000);      // 1, 2, 4 … en fazla 15 sn
  }

  async function resync() {
    try {
      const st = await api('GET', '/api/durum');
      loadStatus(st);                                // önce durum yeniden kurulur
      connect(st.son_id);                            // sonra olaylar kaldığı yerden devam eder
    } catch (e) {
      setConn('bad');
      if (e.status === 401) { showFatal('Erişim belirteci geçersiz. Konsolda yazan adresi yeniden aç.'); return; }
      scheduleResync();
    }
  }

  // ===================================================================== (3) Durum deposu

  const FINISHED = new Set(['bitti', 'hata', 'durduruldu', 'iptal']);
  const MAX_CHAT = 2000;
  const LIVE_CHARS = 400;

  function emptyRun() {
    return {
      info: null, status: '', running: false, paused: false, mode: 'tek',
      phase: { no: 0, title: '', status: '' }, budget: null,
      agentState: {}, tasks: {}, taskOrder: [], plans: {}, plansVersion: 0,
      chat: [], chatSeq: 0, files: {}, verifs: [], report: '',
      consults: [], pending: null, violations: [], endedAt: 0,
    };
  }

  function initialState() {
    return Object.assign(emptyRun(), { lastId: 0, agents: [], defaults: {} });
  }

  // Yardımcılar (saf): yeni nesneler üretir, girdiyi değiştirmez.
  function withChat(s, item) {
    const chat = s.chat.concat([Object.assign({ seq: s.chatSeq + 1 }, item)]);
    if (chat.length > MAX_CHAT) chat.splice(0, chat.length - MAX_CHAT);
    return Object.assign({}, s, { chat, chatSeq: s.chatSeq + 1 });
  }

  function taskKey(s, id, phase) { return `${phase || s.phase.no || 1}:${id}`; }

  function patchTask(s, key, patch) {
    if (!s.tasks[key]) return s;
    const tasks = Object.assign({}, s.tasks);
    tasks[key] = Object.assign({}, tasks[key], typeof patch === 'function' ? patch(tasks[key]) : patch);
    return Object.assign({}, s, { tasks });
  }

  function patchAgent(s, name, patch) {
    const agentState = Object.assign({}, s.agentState);
    const cur = agentState[name] || { status: 'bosta', live: '', done: 0, asked: 0, answered: 0, active: 0 };
    agentState[name] = Object.assign({}, cur, typeof patch === 'function' ? patch(cur) : patch);
    return Object.assign({}, s, { agentState });
  }

  const short = (t, n) => { t = String(t || ''); return t.length > n ? t.slice(0, n) + '…' : t; };

  /** Saf indirgeyici: (durum, olay) → yeni durum. ev._at, olayın istemciye ulaştığı zamandır. */
  function reduce(state, ev) {
    let s = state;
    if (typeof ev.id === 'number') {
      if (ev.id <= s.lastId) return s;                       // tekrar oynatılan olay
      s = Object.assign({}, s, { lastId: ev.id });
    }
    const at = ev._at || parseTime(ev.zaman);
    switch (ev.tur) {
      case 'baslangic': {
        s = Object.assign({}, s, emptyRun(), {
          info: { hedef: ev.hedef, calisma_klasoru: ev.calisma_klasoru, mod: ev.mod, mock: ev.mock, baslangic: at / 1000 },
          mode: ev.mod || 'tek', running: true, status: 'basladi',
        });
        return withChat(s, { kind: 'info', who: 'Orkestra', text: ev.devam ? 'Kayıtlı çalıştırma sürdürülüyor.' : 'Çalıştırma başladı.' });
      }
      case 'durum': {
        const running = !FINISHED.has(ev.durum);
        const paused = ev.durum === 'duraklatildi' ? true : (ev.durum === 'devam_ediyor' ? false : s.paused && running);
        s = Object.assign({}, s, { status: ev.durum, running, paused, endedAt: running ? 0 : at });
        const labels = { duraklatildi: '⏸ Duraklatıldı.', devam_ediyor: '▶ Devam ediyor.', durduruldu: '⏹ Durduruldu. Durum kaydedildi.',
          bitti: '✔ Çalıştırma tamamlandı.', hata: '✖ Çalıştırma hata ile bitti.' };
        if (labels[ev.durum]) s = withChat(s, { kind: ev.durum === 'bitti' ? 'ok' : (ev.durum === 'hata' ? 'bad' : 'info'), who: 'Orkestra', text: labels[ev.durum] });
        if (!running) s = Object.assign({}, s, { pending: null });
        return s;
      }
      case 'asama': {
        s = Object.assign({}, s, { mode: 'asamali', phase: { no: ev.no, title: ev.baslik || s.phase.title, status: ev.durum } });
        const d = { planlaniyor: 'planlanıyor', calisiyor: 'çalışıyor', bitti: 'bitti' }[ev.durum] || ev.durum;
        return withChat(s, { kind: 'info', who: 'Orkestra', text: `Aşama ${ev.no}${ev.baslik ? ': ' + ev.baslik : ''} — ${d}` });
      }
      case 'plan': {
        const phase = ev.asama || 1;
        const plans = Object.assign({}, s.plans); plans[phase] = ev.plan;
        const tasks = Object.assign({}, s.tasks);
        const taskOrder = s.taskOrder.slice();
        for (const t of ev.plan.tasks || []) {
          const key = `${phase}:${t.id}`;
          if (!tasks[key]) taskOrder.push(key);
          tasks[key] = { key, phase, id: t.id, title: t.title, assignee: t.assignee, deps: t.depends_on || [],
            write: !!t.needs_write, files: t.files || [], verify: t.verify || [], instructions: t.instructions,
            expected: t.expected_output, status: 'bekliyor', output: '', error: '', warnings: [], reviews: [],
            verifyRes: [], verifying: false, lastReview: null };
        }
        s = Object.assign({}, s, { plans, tasks, taskOrder, plansVersion: s.plansVersion + 1,
          phase: Object.assign({}, s.phase, { no: phase, title: ev.plan.phase_title || s.phase.title }) });
        const n = (ev.plan.tasks || []).length;
        return withChat(s, { kind: 'plan', who: plannerName(s), agent: plannerName(s),
          text: `Planı hazırladım${ev.asama ? ` (aşama ${ev.asama})` : ''}: ${n} görev. ${ev.plan.summary || ''}` });
      }
      case 'gorev_durumu': {
        const key = taskKey(s, ev.gorev);
        s = patchTask(s, key, (t) => ({ status: ev.durum, verifying: ev.durum === 'dogrulamada' ? true : t.verifying && ev.durum === 'dogrulamada' }));
        if (ev.durum === 'atandi') {
          const t = s.tasks[key];
          s = withChat(s, { kind: 'info', who: plannerName(s), agent: plannerName(s), text: `[${ev.gorev}] ${t ? t.title : ''} → ${ev.ajan}` });
        }
        return s;
      }
      case 'ajan_durumu': {
        const fresh = ['dusunuyor', 'dosya_yaziyor', 'denetliyor'].includes(ev.durum);
        return patchAgent(s, ev.ajan, (a) => ({
          status: ev.durum, role: ev.rol || '', task: ev.gorev || '', detail: ev.detay || '', active: ev.aktif || 0,
          since: (a.status === ev.durum && a.task === (ev.gorev || '')) ? a.since : at,
          live: fresh ? '' : a.live, errorAt: ev.durum === 'hata' ? at : a.errorAt,
        }));
      }
      case 'parca':
        return patchAgent(s, ev.ajan, (a) => ({ live: (a.live + (ev.metin || '')).slice(-LIVE_CHARS) }));
      case 'yanit':
        if (ev.rol === 'işçi') {
          const key = taskKey(s, ev.gorev);
          s = patchTask(s, key, { output: ev.metin || '' });
        }
        return s;
      case 'danisma_basladi': {
        const consults = s.consults.concat([{ id: ev.id, task: ev.gorev, from: ev.soran, to: ev.hedef, q: ev.soru, a: null, at, answeredAt: 0 }]);
        s = Object.assign({}, s, { consults: consults.slice(-50) });
        s = patchAgent(s, ev.soran, (a) => ({ asked: (a.asked || 0) + 1 }));
        return withChat(s, { kind: 'ask', who: `${ev.soran} → ${ev.hedef}`, agent: ev.soran, text: ev.soru, long: true });
      }
      case 'danisma': {
        const consults = s.consults.slice();
        for (let i = consults.length - 1; i >= 0; i--) {
          const c = consults[i];
          if (c.from === ev.soran && c.to === ev.hedef && c.task === ev.gorev && c.a === null) {
            consults[i] = Object.assign({}, c, { a: ev.cevap, answeredAt: at });
            break;
          }
        }
        s = Object.assign({}, s, { consults });
        s = patchAgent(s, ev.hedef, (a) => ({ answered: (a.answered || 0) + 1 }));
        return withChat(s, { kind: 'answer', who: ev.hedef, agent: ev.hedef, text: ev.cevap, long: true });
      }
      case 'denetim': {
        const key = taskKey(s, ev.gorev);
        s = patchTask(s, key, (t) => ({ reviews: t.reviews.concat([{ verdict: ev.karar, feedback: ev.geri_bildirim, auto: !!ev.otomatik }]),
          lastReview: { verdict: ev.karar, at } }));
        const ok = ev.karar === 'approve';
        const text = ev.otomatik ? `[${ev.gorev}] doğrulama başarısız → otomatik REVİZYON ↺`
          : `[${ev.gorev}] ${ok ? 'ONAY ✓' : 'REVİZYON ↺'}${ev.geri_bildirim ? ' — ' + ev.geri_bildirim : ''}`;
        return withChat(s, { kind: ok ? 'ok' : 'warn', who: ev.otomatik ? 'Orkestra' : reviewerName(s), agent: ev.otomatik ? null : reviewerName(s), text, long: true });
      }
      case 'dogrulama': {
        const v = { task: ev.gorev, cmd: ev.komut, code: ev.cikis_kodu, dur: ev.sure_sn, out: ev.cikti, ok: !!ev.basarili, at };
        s = Object.assign({}, s, { verifs: s.verifs.concat([v]).slice(-300) });
        s = patchTask(s, taskKey(s, ev.gorev), (t) => ({ verifyRes: t.verifyRes.concat([v]), verifying: false }));
        return withChat(s, { kind: v.ok ? 'ok' : 'bad', who: 'doğrulama', text: `[${ev.gorev}] ${ev.komut}: ${v.ok ? '✓ başarılı' : '✗ başarısız'} (${ev.sure_sn} sn)` });
      }
      case 'gorev_sonucu': {
        const key = taskKey(s, ev.gorev);
        const status = { 'atlandı': 'atlandi' }[ev.durum] || ev.durum;
        const t = s.tasks[key];
        s = patchTask(s, key, { status, error: ev.hata || '', warnings: ev.uyarilar || [] });
        if (status === 'tamam' && t) s = patchAgent(s, t.assignee, (a) => ({ done: (a.done || 0) + 1 }));
        if (status !== 'tamam') s = withChat(s, { kind: 'bad', who: 'Orkestra', text: `[${ev.gorev}] ${ev.durum}: ${ev.hata || ''}` });
        else if ((ev.uyarilar || []).length) s = withChat(s, { kind: 'warn', who: 'Orkestra', text: `[${ev.gorev}] uyarılar: ${(ev.uyarilar || []).join('; ')}` });
        return s;
      }
      case 'dosya_degisikligi': {
        const files = Object.assign({}, s.files);
        const unrep = new Set(ev.bildirilmemis || []);
        const put = (p, kind) => { files[p] = { path: p, kind, task: ev.gorev, agent: ev.ajan, unreported: unrep.has(p) }; };
        (ev.eklenen || []).forEach((p) => put(p, 'yeni'));
        (ev.degisen || []).forEach((p) => put(p, 'değişti'));
        (ev.silinen || []).forEach((p) => put(p, 'silindi'));
        s = Object.assign({}, s, { files });
        if (unrep.size) s = withChat(s, { kind: 'warn', who: 'Orkestra', text: `Bildirilmemiş değişiklik (${ev.ajan}): ${[...unrep].slice(0, 10).join(', ')}` });
        return s;
      }
      case 'korumali_ihlal': {
        s = Object.assign({}, s, { violations: s.violations.concat([{ agents: String(ev.ajan || '').split(', '), at }]) });
        return withChat(s, { kind: 'bad', who: '⛔ İhlal', text: `${ev.ajan} (${ev.gorev}): ${ev.neden}\n${(ev.yollar || []).join('\n')}`, long: true });
      }
      case 'onay_istegi':
        return Object.assign({}, s, { pending: { istek_id: ev.istek_id, tur: ev.istek_turu, veri: ev.veri || {} } });
      case 'onay_cevabi': {
        if (s.pending && s.pending.istek_id === ev.istek_id) s = Object.assign({}, s, { pending: null });
        const labels = { onay: 'Onaylandı.', ret: 'Reddedildi.', devam: 'İhlale rağmen devam edildi.', durdur: 'İhlal nedeniyle durduruldu.' };
        return withChat(s, { kind: ev.karar === 'onay' || ev.karar === 'devam' ? 'info' : 'warn', who: 'Sen', text: labels[ev.karar] || ev.karar });
      }
      case 'kullanici_notu':
        return withChat(s, { kind: 'note', who: `Not → ${ev.hedef || 'tüm ekip'}`, text: ev.metin, long: true });
      case 'sentez':
        s = Object.assign({}, s, { report: ev.metin || '' });
        return withChat(s, { kind: 'ok', who: plannerName(s), agent: plannerName(s), text: 'Sentez raporu hazır (Rapor sekmesi).' });
      case 'butce':
        return Object.assign({}, s, { budget: { used: ev.kullanilan, limit: ev.sinir, vused: ev.dogrulama_kullanilan, vlimit: ev.dogrulama_sinir } });
      case 'hata':
        return withChat(s, { kind: ev.uyari ? 'warn' : 'bad', who: 'Orkestra', text: ev.mesaj, long: true });
      case 'iptal':
        return withChat(s, { kind: 'warn', who: 'Orkestra', text: `İptal edildi: ${ev.neden}` });
      case 'devam':
        return withChat(s, { kind: 'info', who: 'Orkestra', text: `Kaldığı yerden devam: önceki çağrı ${ev.onceki_calls}, kalan görev ${ev.kalan_gorevler}` });
      case '_bilgi':
        return Object.assign({}, s, { info: Object.assign({}, s.info || {}, ev.bilgi || {}) });
      case '_yerel':
        return withChat(s, { kind: ev.kind || 'bad', who: 'Arayüz', text: ev.metin });
      default:
        return s;
    }
  }

  function plannerName(s) { const p = s.agents.find((a) => a.planlayici); return p ? p.ad : 'planlayıcı'; }
  function reviewerName(s) { const p = s.agents.find((a) => a.denetci); return p ? p.ad : 'denetçi'; }

  let state = initialState();
  const ui = { screen: 'start', forceStart: false, dismissedApproval: null, modal: null, chatRendered: 0, chatFirst: 0,
    plansRendered: -1, selectedPhase: null, selectedFile: null, filesKey: '', verifsRendered: -1, reportRendered: null };

  function dispatch(ev, live) {
    const before = state;
    if (live) ev._at = Date.now();
    state = reduce(state, ev);
    if (state === before) return;
    if (live) effects(ev, before);
    if (ev.tur === 'baslangic') { ui.forceStart = false; resetViews(); }
    scheduleRender();
  }

  function loadStatus(st) {
    const base = initialState();
    base.agents = st.ajanlar || [];
    base.defaults = { workspace: st.varsayilan_workspace, memory: st.hafiza_ozeti, mock: st.mock, maxGoal: st.max_hedef };
    let s = base;
    for (const ev of st.olaylar || []) s = reduce(s, ev);
    if (st.calistirma) {
      s = Object.assign({}, s, { info: Object.assign({}, s.info || {}, st.calistirma) });
      if (!s.status) s = Object.assign({}, s, { running: !!st.calisiyor });
    }
    s = Object.assign({}, s, { running: !!st.calisiyor, paused: !!st.duraklatildi, pending: st.bekleyen_onay ? {
      istek_id: st.bekleyen_onay.istek_id, tur: st.bekleyen_onay.istek_turu, veri: st.bekleyen_onay.veri || {} } : null });
    const agentState = Object.assign({}, s.agentState);
    for (const [name, text] of Object.entries(st.canli || {})) {
      agentState[name] = Object.assign({}, agentState[name] || { status: 'bosta', done: 0, asked: 0, answered: 0 }, { live: text });
    }
    s = Object.assign({}, s, { agentState, lastId: Math.max(s.lastId, st.son_id || 0) });
    state = s;
    resetViews();
    renderStartDefaults();
    scheduleRender();
  }

  function resetViews() {
    ui.chatRendered = 0; ui.chatFirst = 0; ui.plansRendered = -1; ui.filesKey = ''; ui.verifsRendered = -1; ui.reportRendered = null;
    ui.selectedPhase = null; ui.boardTasks = null; ui.graphTasks = null;
    clear($('chat-list'));
  }

  // ===================================================================== (4) Oluşturucular

  let renderQueued = false;
  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    window.requestAnimationFrame(() => { renderQueued = false; render(); });
  }

  function render() {
    const hasRun = !!state.info;
    const showStart = ui.forceStart || !hasRun;
    $('start-screen').hidden = !showStart;
    $('run-screen').hidden = showStart;
    renderModal();                       // önce modal: üst çubuktaki "Bekleyen onay" düğmesi doğru görünsün
    renderTop(showStart);
    if (showStart) return;
    renderDesks();
    renderLinks();
    renderChat();
    renderBoard();
    renderGraph();
    renderFiles();
    renderVerify();
    renderReport();
    renderNoteTargets();
  }

  // ---------------------------------------------------------------- üst çubuk

  function renderTop(showStart) {
    const s = state;
    const info = s.info;
    const onRun = !showStart && !!info;
    $('goal-btn').hidden = !onRun;
    if (onRun) $('goal-text').textContent = (info.hedef || '').split('\n')[0];
    if (!onRun) $('goal-full').hidden = true;
    const phase = $('phase-info');
    phase.hidden = !onRun;
    if (onRun) {
      phase.textContent = s.mode === 'asamali'
        ? `Aşamalı · Aşama ${s.phase.no || 1}/${info.max_asama || '?'}${s.phase.title ? ' · ' + s.phase.title : ''}`
        : 'Tek plan';
    }
    const b = s.budget;
    $('budget').hidden = !onRun || !b;
    if (b && onRun) {
      const ratio = b.limit ? Math.min(1, b.used / b.limit) : 0;
      $('budget-fill').style.transform = `scaleX(${ratio})`;
      $('budget').classList.toggle('high', ratio >= 0.85);
      $('budget-text').textContent = `${b.used}/${b.limit}` + (b.vlimit ? ` · doğrulama ${b.vused}/${b.vlimit}` : '');
    }
    $('elapsed').hidden = !onRun || !info.baslangic;
    const rs = $('run-state');
    rs.hidden = !onRun;
    if (onRun) {
      const labels = { basladi: 'çalışıyor', duraklatildi: '⏸ duraklatıldı', devam_ediyor: 'çalışıyor', durduruldu: '⏹ durduruldu', bitti: '✔ bitti', hata: '✖ hata', iptal: 'iptal' };
      rs.textContent = s.paused ? '⏸ duraklatıldı' : (labels[s.status] || (s.running ? 'çalışıyor' : '—'));
    }
    const pause = $('pause-btn');
    pause.hidden = !onRun || !s.running;
    pause.setAttribute('aria-label', s.paused ? 'Devam ettir' : 'Duraklat');
    pause.firstChild.textContent = s.paused ? '▶ ' : '⏸ ';
    pause.querySelector('.btn-text').textContent = s.paused ? 'Devam' : 'Duraklat';
    $('stop-btn').hidden = !onRun || !s.running;
    $('new-btn').hidden = !onRun || s.running;
    $('approval-btn').hidden = !(s.pending && ui.modal === null);
    updateTimers();
  }

  function updateTimers() {
    const info = state.info;
    if (info && info.baslangic && !$('elapsed').hidden) {
      const end = state.running || !state.endedAt ? Date.now() : state.endedAt;
      $('elapsed').textContent = '⏱ ' + fmtDuration(end - info.baslangic * 1000);
    }
    for (const t of document.querySelectorAll('.desk-timer[data-since]')) {
      t.textContent = fmtDuration(Date.now() - Number(t.getAttribute('data-since')));
    }
  }

  // ---------------------------------------------------------------- ofis ve masalar

  const SHAPES = ['●', '▲', '■', '★', '⬟', '◗'];
  const STATUS = {
    bosta: ['○', 'boşta'], sirada: ['⏳', 'sırada'], dusunuyor: ['💭', 'düşünüyor'], yaziyor: ['✍', 'yazıyor'],
    dosya_yaziyor: ['✎', 'dosya yazıyor'], danisiyor: ['💬', 'danışıyor'], denetliyor: ['🔍', 'denetliyor'], hata: ['⚠', 'hata'],
  };

  function agentColor(name) { const a = state.agents.find((x) => x.ad === name); return a ? a.renk : 'var(--idle)'; }

  function effectiveStatus(a) {
    if (!a) return 'bosta';
    if (a.status === 'hata' && Date.now() - (a.errorAt || 0) > 5000) return 'bosta';   // hata 5 sn sonra boşta görünür
    if (!state.running && a.status !== 'hata') return 'bosta';
    return a.status || 'bosta';
  }

  function renderDesks() {
    const top = $('planner-row'), row = $('desk-row');
    let others = 0;
    state.agents.forEach((ag, i) => {
      const parent = ag.planlayici ? top : row;
      let desk = document.getElementById(`desk-${ag.ad}`);
      if (!desk) { desk = el('article', { class: 'desk', id: `desk-${ag.ad}`, 'data-agent': ag.ad }); parent.appendChild(desk); }
      const shape = ag.planlayici ? '◆' : SHAPES[(others++) % SHAPES.length];
      fillDesk(desk, ag, shape, i);
    });
  }

  function fillDesk(desk, ag, shape) {
    const a = state.agentState[ag.ad] || {};
    const st = effectiveStatus(a);
    desk.className = `desk st-${st}${ag.planlayici ? ' planner' : ''}`;
    desk.style.setProperty('--agent', ag.renk);
    const role = ag.planlayici ? 'Baş Planlayıcı' + (ag.denetci ? ' · Denetçi' : '') : (ag.denetci ? 'Denetçi' : 'Ekip üyesi');
    const [icon, label] = STATUS[st] || STATUS.bosta;
    const task = a.task && st !== 'bosta' ? findTaskTitle(a.task) : '';
    const statusRow = el('div', { class: 'desk-status' },
      el('span', { 'aria-hidden': 'true', text: icon }), el('span', { text: label }),
      st === 'dusunuyor' ? el('span', { class: 'dots', 'aria-hidden': 'true' }, el('span'), el('span'), el('span')) : null,
      st !== 'bosta' && a.since ? el('span', { class: 'desk-timer', 'data-since': a.since, text: fmtDuration(Date.now() - a.since) }) : null);
    if (st === 'hata' && a.detail) statusRow.appendChild(el('span', { class: 'sr-only', text: a.detail }));
    const liveText = st === 'yaziyor' || st === 'dosya_yaziyor' || (st !== 'bosta' && a.live) ? (a.live || '') : '';
    const liveBox = el('pre', { class: 'desk-live', 'aria-label': `${ag.ad} canlı çıktı` });
    if (liveText) { liveBox.appendChild(document.createTextNode(liveText)); if (st === 'yaziyor' || st === 'dosya_yaziyor') liveBox.appendChild(el('span', { class: 'cursor', 'aria-hidden': 'true' })); }
    const kids = [
      el('div', { class: 'desk-head' },
        el('div', { class: 'avatar', 'aria-hidden': 'true' }, (ag.ad[0] || '?').toUpperCase(), el('span', { class: 'avatar-shape', text: shape })),
        el('div', null, el('div', { class: 'desk-name', text: ag.ad }), el('div', { class: 'desk-role', text: role, title: ag.gorunen })),
        a.active > 1 && st !== 'bosta' ? el('span', { class: 'desk-badge', title: 'Eşzamanlı çağrı sayısı', text: `×${a.active}` }) : null),
      statusRow,
      el('div', { class: 'desk-task', text: task ? `${a.task} · ${task}` : (st === 'danisiyor' && a.detail ? `→ ${a.detail}` : (a.task && st !== 'bosta' ? a.task : '')) }),
      st === 'dosya_yaziyor' && a.detail ? el('div', { class: 'desk-files', text: '✎ ' + a.detail }) : null,
      st === 'hata' && a.detail ? el('div', { class: 'desk-files', text: short(a.detail, 160) }) : null,
      liveBox,
      el('div', { class: 'desk-counters' },
        el('span', { title: 'Tamamlanan görev', text: `✔ ${a.done || 0}` }),
        el('span', { title: 'Yapılan danışma', text: `💬→ ${a.asked || 0}` }),
        el('span', { title: 'Alınan danışma', text: `💬← ${a.answered || 0}` })),
    ];
    const flash = desk.querySelector('.flash');
    clear(desk);
    add(desk, kids);
    if (flash) desk.appendChild(flash);
    if (liveText) liveBox.scrollTop = liveBox.scrollHeight;
  }

  function findTaskTitle(tag) {
    const id = String(tag).split('-')[0];
    const t = state.tasks[taskKey(state, id)];
    return t ? t.title : '';
  }

  function deskCenter(name, officeRect) {
    const d = document.getElementById(`desk-${name}`);
    if (!d) return null;
    const r = d.getBoundingClientRect();
    return { x: r.left - officeRect.left + r.width / 2, y: r.top - officeRect.top + r.height / 2, w: r.width, h: r.height, top: r.top - officeRect.top };
  }

  function renderLinks() {
    const layer = $('links');
    clear(layer);
    if (!state.running) return;
    const office = layer.parentElement.getBoundingClientRect();
    const now = Date.now();
    for (const c of state.consults) {
      const open = c.a === null;
      if (!open && now - c.answeredAt > 4000) continue;
      const a = deskCenter(open ? c.from : c.to, office), b = deskCenter(open ? c.to : c.from, office);
      if (!a || !b) continue;
      const mx = (a.x + b.x) / 2, my = Math.min(a.y, b.y) - 60;
      layer.appendChild(svgEl('path', { d: `M${a.x},${a.y} Q${mx},${my} ${b.x},${b.y}`, class: `link-path${open ? '' : ' answer'}` }));
    }
  }

  // ---------------------------------------------------------------- canlı efektler (yalnızca gerçek olaylara bağlı)

  function effects(ev, before) {
    if (reducedMotion()) {
      if (ev.tur === 'korumali_ihlal') announce(`İhlal: ${ev.ajan}`);
      return;
    }
    window.requestAnimationFrame(() => {
      const fx = $('fx-layer');
      if (!fx || $('run-screen').hidden) return;
      const office = fx.getBoundingClientRect();
      if (ev.tur === 'gorev_durumu' && ev.durum === 'atandi') {
        const from = deskCenter(plannerName(state), office), to = deskCenter(ev.ajan, office);
        if (!from || !to) return;
        const t = state.tasks[taskKey(state, ev.gorev)];
        const card = el('div', { class: 'fly', agent: agentColor(ev.ajan), text: `${ev.gorev} · ${t ? short(t.title, 30) : ''}` });
        fx.appendChild(card);
        card.style.transform = `translate(${from.x - 40}px, ${from.y}px)`;
        card.getBoundingClientRect();
        window.requestAnimationFrame(() => {
          card.style.transform = `translate(${to.x - 40}px, ${to.top + 8}px)`;
          card.style.opacity = '0';
        });
        window.setTimeout(() => card.remove(), 1200);
      } else if (ev.tur === 'danisma_basladi' || ev.tur === 'danisma') {
        const ask = ev.tur === 'danisma_basladi';
        const anchor = deskCenter(ask ? ev.soran : ev.hedef, office);
        if (!anchor) return;
        const text = short(ask ? ev.soru : ev.cevap, 120);
        const bubble = el('div', { class: `bubble${ask ? '' : ' answer'}`, text: (ask ? '💬 ' : '↩ ') + text });
        // Konum left/top ile verilir; açılış animasyonu yalnızca transform (ölçek) ve opacity kullanır.
        let left = anchor.x - 120, top = anchor.top - 48;
        if (top < 0) { left = anchor.x + anchor.w / 2 + 12; top = anchor.top + 8; }   // üstte yer yoksa masanın sağı
        bubble.style.left = `${Math.max(0, Math.min(left, office.width - 250))}px`;
        bubble.style.top = `${Math.max(0, top)}px`;
        fx.appendChild(bubble);
        window.setTimeout(() => bubble.classList.add('fade'), ask ? 5000 : 4000);
        window.setTimeout(() => bubble.remove(), ask ? 5800 : 4800);
        window.setTimeout(scheduleRender, 4100);    // cevap oku 4 sn sonra kaldırılır
      } else if (ev.tur === 'korumali_ihlal') {
        for (const name of String(ev.ajan || '').split(', ')) {
          const desk = document.getElementById(`desk-${name}`);
          if (!desk) continue;
          const old = desk.querySelector('.flash');
          if (old) old.remove();
          const f = el('div', { class: 'flash', 'aria-hidden': 'true' });
          desk.appendChild(f);
          window.setTimeout(() => f.remove(), 1700);
        }
        announce(`Korumalı yol ihlali: ${ev.ajan}`);
      } else if (ev.tur === 'denetim') {
        window.setTimeout(scheduleRender, 2600);    // damga 2,5 sn sonra kaldırılır
      }
    });
    if (ev.tur === 'gorev_sonucu') announce(`${ev.gorev} ${ev.durum}`);
    if (ev.tur === 'onay_istegi') announce('Onay bekleniyor.');
    if (ev.tur === 'durum' && FINISHED.has(ev.durum)) announce(`Çalıştırma ${ev.durum}.`);
    if (before.status !== state.status && ev.tur === 'durum' && ev.durum === 'duraklatildi') announce('Duraklatıldı.');
  }

  // ---------------------------------------------------------------- sohbet

  function renderChat() {
    const list = $('chat-list');
    const chat = state.chat;
    const first = chat.length ? chat[0].seq : 0;
    if (first !== ui.chatFirst && ui.chatRendered) { clear(list); ui.chatRendered = 0; }
    ui.chatFirst = first;
    const startIdx = chat.findIndex((m) => m.seq > ui.chatRendered);
    if (startIdx === -1) return;
    const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 60;
    for (let i = startIdx; i < chat.length; i++) list.appendChild(chatItem(chat[i]));
    ui.chatRendered = chat[chat.length - 1].seq;
    while (list.childElementCount > MAX_CHAT) list.removeChild(list.firstChild);
    if (nearBottom) list.scrollTop = list.scrollHeight;
  }

  function chatItem(m) {
    const kindClass = { ok: 'k-ok', warn: 'k-warn', bad: 'k-bad', note: 'k-note' }[m.kind] || '';
    const body = el('div', { class: 'msg-body', text: m.text || '' });
    const li = el('li', { class: `msg ${kindClass}`, agent: m.agent ? agentColor(m.agent) : null },
      el('div', { class: 'msg-head' }, el('span', { class: 'msg-who', text: m.who || '' })), body);
    if (m.long && (m.text || '').length > 220) {
      body.classList.add('clamped');
      const more = el('button', { type: 'button', class: 'msg-more', 'aria-expanded': 'false', text: 'genişlet' });
      more.addEventListener('click', () => {
        const open = body.classList.toggle('clamped');
        more.textContent = open ? 'genişlet' : 'daralt';
        more.setAttribute('aria-expanded', String(!open));
      });
      li.appendChild(more);
    }
    return li;
  }

  // ---------------------------------------------------------------- pano

  const COLUMNS = [
    ['Bekliyor', ['bekliyor', 'atandi']], ['Çalışıyor', ['calisiyor']], ['Denetimde / Doğrulamada', ['denetimde', 'dogrulamada']],
    ['Tamam', ['tamam']], ['Hata / Atlandı', ['hata', 'atlandi']],
  ];

  function renderBoard() {
    // Yalnızca görevler değiştiğinde veya bir damga kaldırılacaksa yeniden çizilir (klavye odağı korunur).
    if (ui.boardTasks === state.tasks && !ui.boardStamp) return;
    ui.boardTasks = state.tasks;
    ui.boardStamp = false;
    const board = $('board');
    const focusedKey = document.activeElement && document.activeElement.getAttribute('data-key');
    clear(board);
    const now = Date.now();
    const multi = state.mode === 'asamali';
    for (const [title, statuses] of COLUMNS) {
      const items = state.taskOrder.map((k) => state.tasks[k]).filter((t) => t && statuses.includes(t.status));
      const col = el('section', { class: 'col', 'aria-label': title }, el('h3', null, el('span', { text: title }), el('span', { text: String(items.length) })));
      for (const t of items) {
        const vr = t.verifyRes[t.verifyRes.length - 1];
        const card = el('button', { type: 'button', class: 'card', agent: agentColor(t.assignee), 'aria-label': `${t.id} ${t.title}, ${t.assignee}, durum ${t.status}` },
          el('span', { class: 'card-id', text: (multi ? `A${t.phase}·` : '') + t.id }),
          el('span', { class: 'card-title', text: t.title }),
          el('span', { class: 'card-meta' },
            el('span', { text: t.assignee }),
            t.write ? el('span', { title: 'Dosya yazar', text: '✎' }) : null,
            t.status === 'dogrulamada' ? el('span', { class: 'badge', text: '⏳ doğrulama' }) : null,
            vr && t.status !== 'dogrulamada' ? el('span', { class: `badge ${vr.ok ? 'ok' : 'bad'}`, text: vr.ok ? '✓ test' : '✗ test' }) : null,
            t.warnings.length ? el('span', { class: 'badge warn', title: t.warnings.join('\n'), text: `⚠ ${t.warnings.length}` }) : null));
        card.setAttribute('data-key', t.key);
        if (t.lastReview && now - t.lastReview.at < 2500) {
          ui.boardStamp = true;
          const ok = t.lastReview.verdict === 'approve';
          card.appendChild(el('span', { class: `stamp ${ok ? 'ok' : 'rev'}`, 'aria-hidden': 'true', text: ok ? 'ONAY ✓' : 'REVİZYON ↺' }));
        }
        card.addEventListener('click', () => openTaskModal(t.key));
        col.appendChild(card);
        if (focusedKey === t.key) window.setTimeout(() => card.focus(), 0);
      }
      board.appendChild(col);
    }
  }

  // ---------------------------------------------------------------- plan grafiği (en uzun yol katmanlama)

  function renderGraph() {
    const phases = Object.keys(state.plans).map(Number).sort((a, b) => a - b);
    const sel = $('phase-select');
    if (ui.plansRendered !== state.plansVersion) {
      clear(sel);
      phases.forEach((p) => sel.appendChild(el('option', { value: p, text: state.mode === 'asamali' ? `Aşama ${p}` : 'Plan' })));
      if (ui.selectedPhase === null || !phases.includes(ui.selectedPhase)) ui.selectedPhase = phases[phases.length - 1] || null;
      if (ui.selectedPhase !== null) sel.value = String(ui.selectedPhase);
      ui.plansRendered = state.plansVersion;
    }
    sel.parentElement.hidden = phases.length < 2;
    if (ui.graphTasks === state.tasks && ui.graphPhase === ui.selectedPhase) return;
    ui.graphTasks = state.tasks;
    ui.graphPhase = ui.selectedPhase;
    const g = $('graph');
    clear(g);
    const phase = ui.selectedPhase;
    if (phase === null) { g.setAttribute('width', 300); g.setAttribute('height', 60); g.appendChild(svgText(10, 30, 'Henüz plan yok.')); return; }
    const tasks = (state.plans[phase].tasks || []).map((t) => state.tasks[`${phase}:${t.id}`]).filter(Boolean);
    const layer = {};
    const depth = (t, seen) => {
      if (layer[t.id] !== undefined) return layer[t.id];
      if (seen.has(t.id)) return 0;
      seen.add(t.id);
      let d = 0;
      for (const dep of t.deps) { const dt = tasks.find((x) => x.id === dep); if (dt) d = Math.max(d, depth(dt, seen) + 1); }
      layer[t.id] = d;
      return d;
    };
    tasks.forEach((t) => depth(t, new Set()));
    const W = 180, H = 50, GX = 70, GY = 22, P = 16;
    const cols = {};
    const pos = {};
    tasks.forEach((t) => { const l = layer[t.id]; cols[l] = (cols[l] || 0) + 1; pos[t.id] = { x: P + l * (W + GX), y: P + (cols[l] - 1) * (H + GY) }; });
    const maxRows = Math.max(1, ...Object.values(cols));
    const maxLayer = Math.max(0, ...Object.values(layer));
    g.setAttribute('width', P * 2 + (maxLayer + 1) * W + maxLayer * GX);
    g.setAttribute('height', P * 2 + maxRows * H + (maxRows - 1) * GY);
    const defs = svgEl('defs');
    const marker = svgEl('marker', { id: 'ok-ucu', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' });
    marker.appendChild(svgEl('path', { d: 'M0,0 L10,5 L0,10 z', class: 'g-arrow' }));
    defs.appendChild(marker);
    g.appendChild(defs);
    for (const t of tasks) {
      for (const dep of t.deps) {
        const a = pos[dep], b = pos[t.id];
        if (!a || !b) continue;
        const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x, y2 = b.y + H / 2, mx = (x1 + x2) / 2;
        g.appendChild(svgEl('path', { d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 2},${y2}`, class: 'g-edge', 'marker-end': 'url(#ok-ucu)' }));
      }
    }
    for (const t of tasks) {
      const p = pos[t.id];
      const node = svgEl('g', { class: `g-node s-${t.status}`, transform: `translate(${p.x},${p.y})`, tabindex: 0, role: 'button', 'aria-label': `${t.id} ${t.title} ${t.status}` });
      node.appendChild(svgEl('rect', { width: W, height: H, rx: 8 }));
      const bar = svgEl('rect', { width: 5, height: H, rx: 2 });
      bar.style.fill = agentColor(t.assignee);
      bar.style.stroke = 'none';
      node.appendChild(bar);
      const idText = svgText(12, 18, `${t.id} · ${t.assignee}${t.write ? ' ✎' : ''}`); idText.setAttribute('class', 'g-id');
      node.appendChild(idText);
      node.appendChild(svgText(12, 37, short(t.title, 24)));
      const title = svgEl('title'); title.textContent = `${t.title} — ${t.status}`; node.appendChild(title);
      node.addEventListener('click', () => openTaskModal(t.key));
      node.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openTaskModal(t.key); } });
      g.appendChild(node);
    }
  }

  function svgText(x, y, text) { const t = svgEl('text', { x, y }); t.textContent = text; return t; }

  // ---------------------------------------------------------------- dosyalar

  function renderFiles() {
    const files = Object.values(state.files).sort((a, b) => a.path.localeCompare(b.path));
    const key = JSON.stringify(files) + '|' + ui.selectedFile;
    if (key === ui.filesKey) return;
    ui.filesKey = key;
    const list = $('file-list');
    clear(list);
    if (!files.length) { list.appendChild(el('li', { class: 'muted', text: 'Henüz değişen dosya yok.' })); return; }
    for (const f of files) {
      const btn = el('button', { type: 'button', class: f.unreported ? 'unreported' : '', 'aria-current': f.path === ui.selectedFile ? 'true' : null },
        el('span', { class: 'file-path', text: f.path }),
        el('span', { class: 'file-meta', text: `${f.kind} · ${f.task} · ${f.agent}${f.unreported ? ' · ⚠ bildirilmemiş' : ''}` }));
      btn.addEventListener('click', () => viewFile(f));
      list.appendChild(el('li', null, btn));
    }
  }

  async function viewFile(f) {
    ui.selectedFile = f.path;
    ui.filesKey = '';
    scheduleRender();
    const head = $('viewer-head'), body = $('viewer-body');
    head.textContent = f.path;
    clear(body);
    if (f.kind === 'silindi') { body.appendChild(el('span', { class: 'ln', text: '(dosya silindi)' })); return; }
    try {
      const data = await api('GET', `/api/dosya?yol=${encodeURIComponent(f.path)}`);
      head.textContent = `${data.yol} · ${data.boyut} bayt · salt-okunur`;
      const lines = data.icerik.split('\n');
      const frag = document.createDocumentFragment();
      lines.forEach((line) => frag.appendChild(el('span', { class: 'ln', text: line })));
      body.appendChild(frag);
    } catch (e) {
      body.appendChild(el('span', { class: 'ln', text: e.message }));
    }
  }

  // ---------------------------------------------------------------- doğrulama

  function renderVerify() {
    if (ui.verifsRendered === state.verifs.length) return;
    ui.verifsRendered = state.verifs.length;
    const list = $('verify-list');
    clear(list);
    if (!state.verifs.length) { list.appendChild(el('li', { class: 'muted', text: 'Henüz doğrulama çalışmadı.' })); return; }
    for (const v of state.verifs.slice().reverse()) {
      list.appendChild(el('li', null,
        el('div', null, el('strong', { text: `${v.ok ? '✓' : '✗'} ${v.cmd}` }),
          el('span', { class: 'muted', text: ` · ${v.task} · ${v.dur} sn · çıkış kodu ${v.code === null ? 'yok (zaman aşımı)' : v.code}` })),
        el('pre', { text: v.out || '(çıktı yok)' })));
    }
  }

  // ---------------------------------------------------------------- rapor

  function renderReport() {
    if (ui.reportRendered === state.report) return;
    ui.reportRendered = state.report;
    const box = $('report');
    clear(box);
    if (!state.report) { box.appendChild(el('p', { class: 'muted', text: 'Sentez raporu henüz hazır değil.' })); $('download-btn').disabled = true; return; }
    box.appendChild(renderMarkdown(state.report));
    $('download-btn').disabled = false;
  }

  function downloadReport() {
    if (!state.report) return;
    const blob = new Blob([state.report], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = el('a');
    a.href = url;
    a.download = `orkestra-rapor-${(state.info && state.info.run) || 'sonuc'}.md`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ---------------------------------------------------------------- not hedefleri

  function renderNoteTargets() {
    const sel = $('note-target');
    if (sel.options.length === state.agents.length + 1) return;
    clear(sel);
    sel.appendChild(el('option', { value: '', text: 'Tüm ekip' }));
    state.agents.forEach((a) => sel.appendChild(el('option', { value: a.ad, text: a.ad })));
  }

  // ---------------------------------------------------------------- modal (odak tuzağı, Esc yalnızca kapatır)

  function openModal({ title, body, actions, danger, key }) {
    const back = $('modal-backdrop'), modal = $('modal');
    if (!ui.modal) ui.returnFocus = document.activeElement;
    ui.modal = key || 'genel';
    modal.classList.toggle('danger', !!danger);
    $('modal-title').textContent = title;
    const b = $('modal-body'); clear(b); add(b, body);
    const act = $('modal-actions'); clear(act);
    for (const a of actions) {
      const btn = el('button', { type: 'button', class: `btn ${a.cls || ''}`, text: a.label });
      btn.addEventListener('click', a.onClick);
      act.appendChild(btn);
    }
    back.hidden = false;
    const first = act.querySelector('.btn-primary, .btn-danger') || act.querySelector('button');
    window.setTimeout(() => (first || modal).focus(), 0);
  }

  function closeModal() {
    $('modal-backdrop').hidden = true;
    ui.modal = null;
    if (ui.returnFocus && document.body.contains(ui.returnFocus)) ui.returnFocus.focus();
    scheduleRender();
  }

  function trapFocus(e) {
    if ($('modal-backdrop').hidden) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      if (state.pending && ui.modal === `onay:${state.pending.istek_id}`) ui.dismissedApproval = state.pending.istek_id;
      closeModal();
      return;
    }
    if (e.key !== 'Tab') return;
    const items = [...$('modal').querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')].filter((x) => !x.disabled);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  function renderModal() {
    const p = state.pending;
    if (ui.modal && ui.modal.startsWith('onay:') && (!p || ui.modal !== `onay:${p.istek_id}`)) closeModal();
    if (p && ui.modal === null && ui.dismissedApproval !== p.istek_id) openApproval(p);
  }

  async function decide(istekId, karar) {
    try {
      await api('POST', '/api/onay', { istek_id: istekId, karar });
      ui.dismissedApproval = istekId;     // onay_cevabi olayı gelene kadar modal yeniden açılmaz
      closeModal();
    } catch (e) {
      dispatch({ tur: '_yerel', metin: `Karar gönderilemedi: ${e.message}` }, false);
    }
  }

  function openApproval(p) {
    const v = p.veri || {};
    const key = `onay:${p.istek_id}`;
    if (p.tur === 'plan') {
      const plan = v.plan || { tasks: [] };
      const rows = (plan.tasks || []).map((t) => el('tr', null,
        el('td', { class: 'mono', text: t.id }), el('td', { text: t.title }), el('td', { text: t.assignee }),
        el('td', { text: (t.depends_on || []).join(', ') || '-' }), el('td', { text: t.needs_write ? 'EVET ✎' : 'hayır' }),
        el('td', { class: 'mono', text: (t.files || []).join(', ') || '-' }), el('td', { class: 'mono', text: (t.verify || []).join(', ') || '-' })));
      const cmds = v.verify_commands || [];
      openModal({
        key, title: v.asama && state.mode === 'asamali' ? `Plan onayı — Aşama ${v.asama}${plan.phase_title ? ': ' + plan.phase_title : ''}` : 'Plan onayı',
        body: [
          el('p', { text: plan.summary || '' }),
          v.has_write ? el('div', { class: 'warn-box', text: '✎ Bu plan çalışma klasöründe dosya YAZAN görevler içeriyor.' }) : null,
          el('div', { class: 'table-wrap' }, el('table', null,
            el('thead', null, el('tr', null, ...['Id', 'Başlık', 'Ajan', 'Bağımlılık', 'Yazma', 'Dosyalar', 'Doğrulama'].map((h) => el('th', { text: h })))),
            el('tbody', null, rows))),
          cmds.length ? [
            el('h3', { text: 'Çalıştırılacak doğrulama komutları' }),
            el('ul', null, cmds.map((c) => el('li', { class: 'mono', text: `${c.name}: ${(c.argv || []).join(' ')}` }))),
            el('div', { class: 'warn-box', text: '⚠ Bu komutlar ajanların yazdığı kodu bilgisayarında çalıştırır. Güvenmediğin işlerde Orkestra\'yı sanal makine, WSL veya Windows Sandbox içinde çalıştır.' }),
          ] : null,
        ],
        actions: [
          { label: 'Reddet', onClick: () => decide(p.istek_id, 'ret') },
          { label: 'Onayla', cls: 'btn-primary', onClick: () => decide(p.istek_id, 'onay') },
        ],
      });
    } else if (p.tur === 'baslangic_uyarisi') {
      openModal({
        key, title: 'Başlangıç uyarısı',
        body: [
          el('p', { text: 'Ajanları yönlendirebilecek talimat veya konfig dosyaları bulundu. CLI araçları bu dosyaları otomatik okur. İçeriklerini kontrol etmeden devam etme.' }),
          el('ul', null, (v.dosyalar || []).map((f) => el('li', { class: 'mono', text: f }))),
        ],
        actions: [
          { label: 'Reddet', onClick: () => decide(p.istek_id, 'ret') },
          { label: 'Devam et', cls: 'btn-primary', onClick: () => decide(p.istek_id, 'onay') },
        ],
      });
    } else if (p.tur === 'ihlal') {
      openModal({
        key, danger: true, title: '⛔ Korumalı yol ihlali',
        body: [
          el('div', { class: 'danger-box', text: `${v.ajan || '?'} (${v.gorev || '?'}): ${v.neden || ''}` }),
          el('ul', null, (v.yollar || []).map((f) => el('li', { class: 'mono', text: f }))),
          el('p', { text: 'Yeni çağrılar duraklatıldı. Orkestra değişiklikleri otomatik geri almaz; gerekirse git ile geri al.' }),
        ],
        actions: [
          { label: 'Devam et', onClick: () => decide(p.istek_id, 'devam') },
          { label: 'Durdur', cls: 'btn-danger', onClick: () => decide(p.istek_id, 'durdur') },
        ],
      });
    }
  }

  function openTaskModal(key) {
    const t = state.tasks[key];
    if (!t) return;
    const section = (title, content) => (content ? [el('h3', { text: title }), content] : null);
    openModal({
      key: 'gorev', title: `[${t.id}] ${t.title}`,
      body: [
        el('p', { class: 'muted', text: `Ajan: ${t.assignee} · Durum: ${t.status}${t.write ? ' · ✎ yazma' : ''}` }),
        section('Talimat', el('p', { text: t.instructions || '' })),
        section('Beklenen çıktı', el('p', { text: t.expected || '' })),
        t.files.length ? section('Dosyalar', el('p', { class: 'mono', text: t.files.join(', ') })) : null,
        t.error ? el('div', { class: 'danger-box', text: t.error }) : null,
        t.warnings.length ? el('div', { class: 'warn-box', text: t.warnings.join('\n') }) : null,
        t.reviews.length ? section('Denetim geçmişi', el('ul', null, t.reviews.map((r) =>
          el('li', { text: `${r.auto ? 'otomatik ' : ''}${r.verdict === 'approve' ? 'ONAY ✓' : 'REVİZYON ↺'}${r.feedback ? ' — ' + r.feedback : ''}` })))) : null,
        t.output ? section('Son çıktı', el('div', { class: 'markdown' }, renderMarkdown(t.output))) : null,
      ],
      actions: [{ label: 'Kapat', cls: 'btn-primary', onClick: closeModal }],
    });
  }

  function confirmStop() {
    openModal({
      key: 'durdur', danger: true, title: 'Çalıştırmayı durdur',
      body: [el('p', { text: 'Çalışan görevler iptal edilir ve durum kaydedilir. Daha sonra başlangıç ekranından kaldığı yerden devam edebilirsin.' })],
      actions: [
        { label: 'Vazgeç', onClick: closeModal },
        { label: 'Durdur', cls: 'btn-danger', onClick: async () => {
          closeModal();
          try { await api('POST', '/api/durdur', {}); } catch (e) { dispatch({ tur: '_yerel', metin: `Durdurulamadı: ${e.message}` }, false); }
        } },
      ],
    });
  }

  // ===================================================================== (5) Güvenli Markdown dönüştürücü

  function safeHref(url) {
    try {
      const u = new URL(url, window.location.href);
      return (u.protocol === 'http:' || u.protocol === 'https:') && /^https?:\/\//i.test(url) ? u.href : null;
    } catch (e) { return null; }
  }

  function inline(text) {
    // Sırayla: `kod`, [metin](url), **kalın**, *italik*. Her parça düğüm olarak üretilir.
    const out = [];
    const re = /(`[^`\n]+`)|(\[[^\]\n]+\]\([^)\s]+\))|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)/g;
    let last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) out.push(document.createTextNode(text.slice(last, m.index)));
      const tok = m[0];
      if (m[1]) out.push(el('code', { text: tok.slice(1, -1) }));
      else if (m[2]) {
        const mm = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(tok);
        const href = safeHref(mm[2]);
        if (href) {
          const a = el('a', { text: mm[1] });
          a.href = href;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          out.push(a);
        } else out.push(document.createTextNode(tok));      // diğer şemalar düz metin
      } else if (m[3]) out.push(el('strong', { text: tok.slice(2, -2) }));
      else if (m[4]) out.push(el('em', { text: tok.slice(1, -1) }));
      last = m.index + tok.length;
    }
    if (last < text.length) out.push(document.createTextNode(text.slice(last)));
    return out;
  }

  function renderMarkdown(src) {
    const root = document.createDocumentFragment();
    const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
    let i = 0, para = [], list = null;
    const flushPara = () => { if (para.length) { root.appendChild(el('p', null, inline(para.join(' ')))); para = []; } };
    const flushList = () => { if (list) { root.appendChild(list); list = null; } };
    while (i < lines.length) {
      const line = lines[i];
      const fence = /^\s*```/.test(line);
      if (fence) {
        flushPara(); flushList();
        const code = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
        i++;
        root.appendChild(el('pre', null, el('code', { text: code.join('\n') })));
        continue;
      }
      let m;
      if ((m = /^(#{1,6})\s+(.*)$/.exec(line))) {
        flushPara(); flushList();
        root.appendChild(el(`h${Math.min(6, m[1].length + 1)}`, null, inline(m[2])));
      } else if ((m = /^\s*[-*+]\s+(.*)$/.exec(line)) || (m = /^\s*\d+[.)]\s+(.*)$/.exec(line))) {
        flushPara();
        const ordered = /^\s*\d/.test(line);
        if (!list || (list.tagName === 'OL') !== ordered) { flushList(); list = el(ordered ? 'ol' : 'ul'); }
        list.appendChild(el('li', null, inline(m[1])));
      } else if ((m = /^>\s?(.*)$/.exec(line))) {
        flushPara(); flushList();
        root.appendChild(el('blockquote', null, inline(m[1])));
      } else if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
        flushPara(); flushList();
        root.appendChild(el('hr'));
      } else if (!line.trim()) {
        flushPara(); flushList();
      } else {
        flushList();
        para.push(line.trim());
      }
      i++;
    }
    flushPara(); flushList();
    return root;
  }

  // ===================================================================== (6) Başlatma ve etkileşimler

  function showFatal(msg) {
    $('start-screen').hidden = false;
    $('run-screen').hidden = true;
    $('start-error').hidden = false;
    $('start-error').textContent = msg;
  }

  function renderStartDefaults() {
    const d = state.defaults || {};
    const ws = $('ws-input');
    if (!ws.value && d.workspace) ws.value = d.workspace;
    $('mock-note').hidden = !d.mock;
    const mem = $('memory-note');
    mem.hidden = !d.memory;
    if (d.memory) mem.textContent = `Bu çalışma klasöründe proje hafızası var: ${d.memory}.`;
    if (d.maxGoal) $('goal-input').maxLength = d.maxGoal;
    loadHistory();
  }

  function fmtRunName(name) {
    const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(name);
    return m ? `${m[3]}.${m[2]}.${m[1]} ${m[4]}:${m[5]}` : name;
  }

  async function loadHistory() {
    const list = $('history-list');
    let rows = [];
    try { rows = (await api('GET', '/api/gecmis')).calistirmalar || []; } catch (e) { rows = []; }
    clear(list);
    $('history-empty').hidden = rows.length > 0;
    const labels = { bitti: '✔ bitti', durduruldu: '⏹ durduruldu', hata: '✖ hata', iptal: 'iptal', calisiyor: '… yarım kaldı' };
    for (const r of rows) {
      const actions = el('div', { class: 'history-actions' });
      if (r.rapor) {
        const b = el('button', { type: 'button', class: 'btn', text: 'Raporu gör' });
        b.addEventListener('click', () => showHistory(r.run));
        actions.appendChild(b);
      }
      if (r.devam_edilebilir) {
        const b = el('button', { type: 'button', class: 'btn', text: 'Kaldığı yerden devam' });
        b.addEventListener('click', () => resumeRun(r.run));
        actions.appendChild(b);
      }
      list.appendChild(el('li', null,
        el('div', { class: 'history-meta', text: `${fmtRunName(r.run)} · ${r.mod === 'asamali' ? 'aşamalı' : 'tek plan'} · ${labels[r.durum] || r.durum}` }),
        el('div', { text: r.hedef || '(hedef yok)' }), actions));
    }
  }

  async function showHistory(run) {
    try {
      const d = await api('GET', `/api/gecmis/${encodeURIComponent(run)}`);
      const summaries = (d.planlar || []).map((p) => el('li', { text: `${p.phase_title ? p.phase_title + ': ' : ''}${p.summary || ''} (${(p.tasks || []).length} görev)` }));
      openModal({
        key: 'gecmis', title: `Çalıştırma ${fmtRunName(run)}`,
        body: [summaries.length ? [el('h3', { text: 'Plan' }), el('ul', null, summaries)] : null,
          el('h3', { text: 'Rapor' }), el('div', { class: 'markdown' }, d.rapor ? renderMarkdown(d.rapor) : el('p', { class: 'muted', text: 'Rapor yok.' }))],
        actions: [{ label: 'Kapat', cls: 'btn-primary', onClick: closeModal }],
      });
    } catch (e) {
      $('start-error').hidden = false;
      $('start-error').textContent = e.message;
    }
  }

  async function resumeRun(run) {
    $('start-error').hidden = true;
    try {
      const res = await api('POST', '/api/devam-et-run', { run });
      dispatch({ tur: '_bilgi', bilgi: res.calistirma }, false);
      ui.forceStart = false;
      scheduleRender();
    } catch (e) {
      $('start-error').hidden = false;
      $('start-error').textContent = e.message;
    }
  }

  async function startRun(e) {
    e.preventDefault();
    const goal = $('goal-input').value.trim();
    const wsErr = $('ws-error'), err = $('start-error');
    wsErr.hidden = true; err.hidden = true;
    if (!goal) { err.hidden = false; err.textContent = 'Hedef boş olamaz.'; $('goal-input').focus(); return; }
    const mode = document.querySelector('input[name="mode"]:checked').value;
    const btn = $('start-btn');
    btn.disabled = true;
    try {
      const res = await api('POST', '/api/calistir', { hedef: goal, workspace: $('ws-input').value.trim() || null, mod: mode });
      dispatch({ tur: '_bilgi', bilgi: res.calistirma }, false);
      ui.forceStart = false;
      scheduleRender();
      $('main').focus();
    } catch (ex) {
      const target = ex.status === 400 && /klasör|dizin|kök/i.test(ex.message) ? wsErr : err;
      target.hidden = false;
      target.textContent = ex.message;
    } finally {
      btn.disabled = false;
    }
  }

  async function sendNote(e) {
    e.preventDefault();
    const input = $('note-text');
    const metin = input.value.trim();
    if (!metin) return;
    try {
      await api('POST', '/api/not', { metin, hedef: $('note-target').value || null });
      input.value = '';
    } catch (ex) {
      dispatch({ tur: '_yerel', metin: `Not gönderilemedi: ${ex.message}` }, false);
    }
  }

  // Tema: prefers-color-scheme ile otomatik; elle seçim localStorage'da (try/catch ile).
  const THEME_KEY = 'orkestra-tema';
  function currentTheme() {
    const forced = document.documentElement.getAttribute('data-theme');
    if (forced) return forced;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  function applyTheme(t) {
    if (t) document.documentElement.setAttribute('data-theme', t);
    $('theme-btn').textContent = currentTheme() === 'dark' ? '☀' : '☾';
  }

  function initTabs() {
    const tabs = [...document.querySelectorAll('[role="tab"]')];
    const select = (tab) => {
      tabs.forEach((t) => {
        const on = t === tab;
        t.setAttribute('aria-selected', String(on));
        t.tabIndex = on ? 0 : -1;
        $(t.getAttribute('aria-controls')).hidden = !on;
      });
      tab.focus();
      scheduleRender();
    };
    tabs.forEach((t) => {
      t.addEventListener('click', () => select(t));
      t.addEventListener('keydown', (e) => {
        const visible = tabs.filter((x) => x.offsetParent !== null);
        const i = visible.indexOf(t);
        if (e.key === 'ArrowRight') { e.preventDefault(); select(visible[(i + 1) % visible.length]); }
        else if (e.key === 'ArrowLeft') { e.preventDefault(); select(visible[(i - 1 + visible.length) % visible.length]); }
        else if (e.key === 'Home') { e.preventDefault(); select(visible[0]); }
        else if (e.key === 'End') { e.preventDefault(); select(visible[visible.length - 1]); }
      });
    });
  }

  // 768–1279 px: sohbet alt sekmelere taşınır.
  function initResponsiveChat() {
    const mq = window.matchMedia('(max-width: 1279px)');
    const apply = () => {
      const chat = $('chat-panel');
      if (mq.matches) {
        if (chat.parentElement.id !== 'panel-chat') $('panel-chat').appendChild(chat);
      } else {
        if (chat.parentElement.id === 'panel-chat') $('run-screen').insertBefore(chat, document.querySelector('.tabs'));
        const chatTab = $('tab-chat');
        if (chatTab.getAttribute('aria-selected') === 'true') $('tab-board').click();
      }
      scheduleRender();
    };
    if (mq.addEventListener) mq.addEventListener('change', apply); else mq.addListener(apply);
    apply();
  }

  function init() {
    let saved = null;
    try { saved = localStorage.getItem(THEME_KEY); } catch (e) { saved = null; }
    applyTheme(saved === 'light' || saved === 'dark' ? saved : null);
    $('theme-btn').addEventListener('click', () => {
      const next = currentTheme() === 'dark' ? 'light' : 'dark';
      applyTheme(next);
      try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* depolama kapalı olabilir */ }
    });
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => applyTheme(null));

    $('goal-input').addEventListener('input', () => {
      $('goal-count').textContent = `${$('goal-input').value.length} / ${$('goal-input').maxLength}`;
    });
    $('start-form').addEventListener('submit', startRun);
    $('note-form').addEventListener('submit', sendNote);
    $('download-btn').addEventListener('click', downloadReport);
    $('phase-select').addEventListener('change', (e) => { ui.selectedPhase = Number(e.target.value); renderGraph(); });
    $('goal-btn').addEventListener('click', () => {
      const full = $('goal-full');
      full.hidden = !full.hidden;
      full.textContent = (state.info && state.info.hedef) || '';
      $('goal-btn').setAttribute('aria-expanded', String(!full.hidden));
    });
    $('pause-btn').addEventListener('click', async () => {
      try { await api('POST', state.paused ? '/api/devam' : '/api/duraklat', {}); } catch (e) { dispatch({ tur: '_yerel', metin: e.message }, false); }
    });
    $('stop-btn').addEventListener('click', confirmStop);
    $('new-btn').addEventListener('click', () => { ui.forceStart = true; loadHistory(); scheduleRender(); $('goal-input').focus(); });
    $('approval-btn').addEventListener('click', () => { ui.dismissedApproval = null; scheduleRender(); });
    document.addEventListener('keydown', trapFocus);
    window.addEventListener('resize', scheduleRender);
    initTabs();
    initResponsiveChat();

    // Geçen süre sayaçları gerçek başlangıç zamanlarından hesaplanır; masalar (hata → boşta geçişi) da yenilenir.
    window.setInterval(() => { updateTimers(); if (state.running) scheduleRender(); }, 1000);

    if (!TOKEN) {
      showFatal('Erişim belirteci yok. Sunucunun konsola yazdığı http://127.0.0.1:…/?t=… adresini aç.');
      setConn('bad');
      return;
    }
    resync();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
