(() => {
  'use strict';
  const BOOT = window.BOOT || {};
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const ico = (n, s = 16) => `<svg class="icon" width="${s}" height="${s}" aria-hidden="true"><use href="#i-${n}"/></svg>`;
  const fmtTime = (iso) => iso ? new Date(iso).toLocaleString([], { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—';
  const letter = (s) => esc((s || '?').trim().slice(0, 1).toUpperCase());
  const avatar = (url, name, cls = 'sm') => `<span class="avatar ${cls}">${url ? `<img src="${esc(url)}" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()">` : ''}<b>${letter(name)}</b></span>`;

  // ---------------------------------------------------------------- api
  async function api(url, opts = {}) {
    const init = { method: opts.method || 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (opts.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    const res = await fetch(url, init);
    if (res.status === 401) { location.href = '/login'; throw new Error('Signed out'); }
    let data = {};
    try { data = await res.json(); } catch (e) { /* empty body */ }
    if (!res.ok) throw new Error(data.error || res.statusText || 'Request failed');
    return data;
  }
  const post = (url, body = {}) => api(url, { method: 'POST', body });

  async function act(action, payload = {}) {
    let r = await post('/api/act', { action, payload });
    for (let i = 0; r.pending && i < 150; i++) { await sleep(1000); r = await api('/api/jobs/' + r.job_id); }
    if (r.pending) throw new Error('The bot took too long to answer');
    if (r.status === 'error') throw new Error(r.error || 'Failed');
    return r.result;
  }

  function toast(msg, kind = 'ok', title) {
    const el = document.createElement('div');
    el.className = 'toast ' + kind;
    el.innerHTML = (title ? `<b>${esc(title)}</b>` : '') + `<span>${esc(msg)}</span>`;
    $('#toasts').appendChild(el);
    setTimeout(() => el.remove(), 5500);
  }
  const fail = (e) => toast(e.message || String(e), 'err');

  async function busy(btn, fn) {
    if (btn) btn.disabled = true;
    try { return await fn(); } catch (e) { fail(e); } finally { if (btn) btn.disabled = false; }
  }

  function renderResult(el, r) {
    let h = '';
    if (r.title) h += `<div class="res-title">${esc(r.title)}</div>`;
    if (r.banner) h += `<img class="res-banner" src="${esc(r.banner)}" alt="" referrerpolicy="no-referrer">`;
    if (r.image) h += `<img class="res-img" src="${esc(r.image)}" alt="" referrerpolicy="no-referrer">`;
    if (r.fields && r.fields.length) h += '<dl class="res-fields">' + r.fields.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('') + '</dl>';
    if (r.text) h += `<div class="res-text">${esc(r.text)}</div>`;
    el.className = 'out';
    el.innerHTML = h || '<div class="res-text">Done.</div>';
    el.hidden = false;
  }

  // ---------------------------------------------------------------- tabs
  function initTabs() {
    $$('[data-tabs]').forEach((bar) => {
      const show = (name) => {
        $$('.tab', bar).forEach((t) => t.classList.toggle('active', t.dataset.tab === name));
        $$('.tab-panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === name));
        document.dispatchEvent(new CustomEvent('tabshown', { detail: name }));
      };
      $$('.tab', bar).forEach((t) => t.addEventListener('click', () => { show(t.dataset.tab); history.replaceState(null, '', '#' + t.dataset.tab); }));
      const hash = location.hash.slice(1);
      if (hash && $(`.tab[data-tab="${hash}"]`, bar)) show(hash);
    });
  }

  // ---------------------------------------------------------------- account switching + power
  function initAccounts() {
    const btn = $('#acctBtn'), menu = $('#acctMenu');
    if (btn) {
      btn.addEventListener('click', (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; });
      document.addEventListener('click', () => { menu.hidden = true; });
    }
    document.addEventListener('click', async (e) => {
      const sw = e.target.closest('[data-switch]');
      if (sw) { await busy(sw, async () => { await post('/api/switch', { account_id: +sw.dataset.switch }); location.href = location.pathname === '/accounts' ? '/accounts' : location.pathname; }); return; }
      const pw = e.target.closest('[data-power]');
      if (pw) powerAction(pw, pw.dataset.id, pw.dataset.power);
    });
    const quick = $('#quickPower');
    if (quick) quick.addEventListener('click', () => powerAction(quick, quick.dataset.id, BOOT.state === 'offline' ? 'start' : 'stop'));
  }
  async function powerAction(btn, id, verb) {
    await busy(btn, async () => {
      await post(`/api/accounts/${id}/${verb}`);
      toast(verb === 'stop' ? 'Stopping…' : 'Starting the bot…');
      await sleep(1500);
      location.reload();
    });
  }

  // ---------------------------------------------------------------- global polling (status, badges, notifications)
  const STATE_LABEL = { online: ['Bot online', 'LIVE'], starting: ['Starting…', 'STARTING'], offline: ['Bot offline', 'OFFLINE'] };
  function setBadges(n) {
    ['#unreadBadge', '#bellBadge', '#tabBadge'].forEach((s) => { const el = $(s); if (el) { el.textContent = n; el.hidden = !n; } });
  }
  function bindState(st) {
    (st.accounts || []).forEach((a) => {
      $$(`[data-state-dot="${a.id}"]`).forEach((el) => { el.className = 'dot ' + a.state; });
      $$(`[data-state-text="${a.id}"]`).forEach((el) => { el.textContent = a.state; });
      $$(`[data-state-pill="${a.id}"]`).forEach((el) => { el.className = 'pill ' + a.state; el.textContent = a.state; });
      $$(`[data-state-pulse="${a.id}"]`).forEach((el) => el.classList.toggle('offline', a.state !== 'online'));
      $$(`[data-state-label="${a.id}"]`).forEach((el) => { el.textContent = STATE_LABEL[a.state][0]; });
      $$(`[data-state-label-caps="${a.id}"]`).forEach((el) => { el.textContent = STATE_LABEL[a.state][1]; });
    });
    $$('[data-bind]').forEach((el) => {
      let v = el.dataset.bind.split('.').reduce((o, k) => (o == null ? o : o[k]), st);
      if (v == null) v = 0;
      el.textContent = el.dataset.fmt === 'num' ? Number(v).toLocaleString() : v;
    });
    setBadges(st.unread || 0);
    const cur = (st.accounts || []).find((a) => a.id === BOOT.accountId);
    if (cur && BOOT.state && cur.state !== BOOT.state) {
      const typing = document.activeElement && /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
      if (!typing) location.reload();
    }
  }
  let lastNotif = BOOT.lastNotifId || 0;
  async function pollNotifications() {
    try {
      const d = await api('/api/notifications?since_id=' + lastNotif + '&limit=20');
      setBadges(d.unread || 0);
      d.items.slice().reverse().forEach((n) => {
        lastNotif = Math.max(lastNotif, n.id);
        const title = ({ dm: 'DM', ping: 'Ping', friend: 'Friend', sniper: 'Snipe', win: 'Giveaway win 🎉', quest: 'Quest' }[n.kind] || 'Alert') + ' · ' + (n.author || '');
        toast(n.preview || '', 'ok', title);
        if (window.Notification && Notification.permission === 'granted' && document.hidden) new Notification(title, { body: n.preview || '' });
      });
      if (d.items.length) document.dispatchEvent(new CustomEvent('newnotifs'));
    } catch (e) { /* ignore */ }
  }
  function startPolling() {
    if (!BOOT.accountId) return;
    setInterval(async () => { try { bindState(await api('/api/state')); } catch (e) { /* ignore */ } }, 8000);
    setInterval(pollNotifications, 5000);
  }

  // ---------------------------------------------------------------- shared renderers
  const logRow = (l) => {
    const ts = l.timestamp || '';
    const role = l.role_name ? `<div class="log-sub">ROLE · ${esc(l.role_name)} · ${esc(l.role_id)}</div>` : '';
    return `<tr class="lvl-${esc(l.level)}"><td><span class="event-tag ${esc(String(l.category).toLowerCase())}">${esc(l.category)}</span></td>` +
      `<td><div class="log-message">${esc(l.message)}</div>${role}</td>` +
      `<td><div class="server-cell">${esc(l.guild_name || '—')}</div><div class="log-sub">${esc(l.actor || '')}</div></td>` +
      `<td class="time-cell">${esc(ts.slice(11, 19))}<small>${esc(ts.slice(0, 10))}</small></td></tr>`;
  };
  const notifHtml = (n) => `<div class="notif ${n.is_read ? '' : 'unread'}" data-id="${n.id}">${avatar(n.avatar_url, n.author, 'md')}
    <div class="body"><div class="top"><b>${esc(n.author || 'Discord')} <span class="event-tag ${esc(n.kind)}">${esc(n.kind)}</span></b><span>${esc(fmtTime(n.created_at))}</span></div>
    <p>${esc(n.preview || '')}</p>
    ${n.guild_name || n.channel_name ? `<p class="log-sub">${esc([n.guild_name, n.channel_name].filter(Boolean).join(' · '))}</p>` : ''}
    ${n.jump_url ? `<a class="jump" href="${esc(n.jump_url)}" target="_blank" rel="noopener">Open in Discord ↗</a>` : ''}</div></div>`;

  // ---------------------------------------------------------------- overview
  function initOverview() {
    const logs = $('#recentLogs'), notifs = $('#recentNotifs');
    const load = async () => {
      try {
        const l = await api('/api/logs?limit=12');
        logs.innerHTML = l.length ? l.map(logRow).join('') : '<tr><td colspan="4" class="empty-state">No events yet for this account.</td></tr>';
        const n = await api('/api/notifications?limit=5');
        notifs.innerHTML = n.items.length ? n.items.map(notifHtml).join('') : '<div class="empty-state">No notifications yet.</div>';
      } catch (e) { /* ignore */ }
    };
    load(); setInterval(load, 6000);
    const r = $('#refreshSnap');
    if (r) r.addEventListener('click', () => busy(r, async () => { await act('refresh_snapshot'); toast('Account data refreshed'); setTimeout(() => location.reload(), 600); }));
  }

  // ---------------------------------------------------------------- accounts page
  function initAccountsPage() {
    $('#addAcct').addEventListener('submit', async (e) => {
      e.preventDefault();
      await busy($('button', e.target), async () => {
        await post('/api/accounts', { label: $('#newLabel').value, token: $('#newToken').value });
        toast('Account added - press Start to log it in');
        setTimeout(() => location.reload(), 700);
      });
    });
    $$('[data-edit]').forEach((b) => b.addEventListener('click', async () => {
      const label = prompt('Account label:', b.dataset.label);
      if (label === null) return;
      const token = prompt('New token (leave empty to keep the current one):', '');
      const body = { label };
      if (token) body.token = token;
      try { const r = await api('/api/accounts/' + b.dataset.edit, { method: 'PATCH', body }); toast(r.restart_needed ? 'Saved - restart the bot to use the new token' : 'Saved'); setTimeout(() => location.reload(), 800); } catch (e) { fail(e); }
    }));
    $$('[data-delete]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm(`Delete "${b.dataset.label}" and all of its logs, settings and notifications?`)) return;
      try { await api('/api/accounts/' + b.dataset.delete, { method: 'DELETE' }); location.reload(); } catch (e) { fail(e); }
    }));
    $$('[data-autostart]').forEach((c) => c.addEventListener('change', async () => {
      try { await api('/api/accounts/' + c.dataset.autostart, { method: 'PATCH', body: { auto_start: c.checked } }); toast(c.checked ? 'Will start automatically' : 'Auto-start off'); } catch (e) { fail(e); c.checked = !c.checked; }
    }));
    let consoleTimer = null;
    const panel = $('#consolePanel'), box = $('#consoleBox');
    const pull = async (id) => {
      try { const d = await api(`/api/accounts/${id}/console`); const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 30; box.textContent = d.text || '(no output yet)'; if (atEnd) box.scrollTop = box.scrollHeight; } catch (e) { /* ignore */ }
    };
    $$('[data-console]').forEach((b) => b.addEventListener('click', () => {
      clearInterval(consoleTimer);
      $('#consoleName').textContent = '· ' + (b.closest('.acct-card').querySelector('b').textContent);
      panel.hidden = false; pull(b.dataset.console); box.scrollTop = box.scrollHeight;
      consoleTimer = setInterval(() => pull(b.dataset.console), 3000);
      panel.scrollIntoView({ behavior: 'smooth' });
    }));
    $('#consoleClose').addEventListener('click', () => { clearInterval(consoleTimer); panel.hidden = true; });
  }

  // ---------------------------------------------------------------- commands page
  async function saveSetting(key, value) {
    const r = await post('/api/settings/' + key, { value });
    (window.SETTINGS || (window.SETTINGS = {}))[key] = r.value;
    return r;
  }
  const savedMsg = (r) => toast(r.live ? 'Saved and applied' : 'Saved - applies when the bot starts');

  // ---------------------------------------------------------------- rich presence editor
  function initRpc() {
    const form = $('#rpcForm');
    if (!form) return;
    const v = (n) => form.elements[n].value.trim();
    const FIELDS = ['type', 'name', 'details', 'state', 'large_image', 'large_text', 'small_image', 'small_text', 'btn1_label', 'btn1_url',
      'btn2_label', 'btn2_url', 'timer_mode', 'timer_span', 'party_cur', 'party_max', 'app_id', 'url'];
    const HEAD = { playing: 'Playing', streaming: 'Live on', listening: 'Listening to', watching: 'Watching', competing: 'Competing in' };
    const setImg = (el, url) => { const ok = /^https?:\/\//i.test(url); el.hidden = !ok; if (ok) el.src = url; else el.removeAttribute('src'); };
    const preview = () => {
      $('#rpHead').textContent = (HEAD[v('type')] || 'Playing') + ' ' + (v('name') || 'Aether');
      $('#rpName').textContent = v('name') || 'Aether';
      $('#rpDetails').textContent = v('details'); $('#rpDetails').hidden = !v('details');
      const party = v('party_cur') && v('party_max') ? ` (${v('party_cur')} of ${v('party_max')})` : '';
      $('#rpStateLine').textContent = v('state') + party; $('#rpStateLine').hidden = !(v('state') || party);
      const mode = v('timer_mode'), span = v('timer_span');
      $('#rpTime').textContent = mode === 'elapsed' ? (span ? span + ' elapsed' : '00:00 elapsed') : mode === 'countdown' ? (span || '?') + ' left' : '';
      $('#rpTime').hidden = mode === 'none';
      setImg($('#rpBig'), v('large_image')); setImg($('#rpSmallImg'), v('small_image'));
      $('#rpBtns').innerHTML = [1, 2].filter((i) => v('btn' + i + '_label')).map((i) => `<a>${esc(v('btn' + i + '_label'))}</a>`).join('');
    };
    const fill = (r) => {
      const b = r.buttons || [], t = r.timer || {};
      const map = { type: r.type, name: r.name, details: r.details, state: r.state, large_image: r.large_image, large_text: r.large_text,
        small_image: r.small_image, small_text: r.small_text, btn1_label: (b[0] || {}).label, btn1_url: (b[0] || {}).url,
        btn2_label: (b[1] || {}).label, btn2_url: (b[1] || {}).url, timer_mode: t.mode || 'none', timer_span: t.span ? t.span + 's' : '',
        party_cur: (r.party || [])[0], party_max: (r.party || [])[1], app_id: r.app_id, url: r.url };
      FIELDS.forEach((n) => { form.elements[n].value = map[n] == null ? '' : map[n]; });
      form.elements.enabled.checked = !!r.enabled;
      $('#rpcState').textContent = r.enabled ? 'ON' : 'off';
      preview();
    };
    form.addEventListener('input', preview);
    form.addEventListener('change', preview);
    preview();
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const body = { enabled: form.elements.enabled.checked };
      FIELDS.forEach((n) => { body[n] = form.elements[n].value; });
      await busy($('button[type=submit]', form), async () => {
        const r = await saveSetting('rpc', body);
        fill(r.value); savedMsg(r);
      });
    });
    $('#rpcClear').addEventListener('click', async (e) => {
      if (!confirm('Clear the whole activity?')) return;
      await busy(e.currentTarget, async () => {
        await saveSetting('presence', { ...SETTINGS.presence, kind: 'none', text: '' });
        const r = await saveSetting('rpc', { clear: true });
        fill(r.value); savedMsg(r);
      });
    });
  }

  function initCommands() {
    const cache = {};
    $$('[data-target]').forEach((box) => {
      const srv = $('.t-server', box), chan = $('.t-channel', box);
      srv.addEventListener('change', async () => {
        chan.innerHTML = '<option value="">Loading…</option>';
        if (!srv.value) { chan.innerHTML = '<option value="">Choose channel…</option>'; return; }
        try {
          if (!cache[srv.value]) cache[srv.value] = (await act('channels', { guild_id: srv.value })).channels;
          chan.innerHTML = '<option value="">Choose channel…</option>' + cache[srv.value].map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
        } catch (e) { chan.innerHTML = '<option value="">Choose channel…</option>'; fail(e); }
      });
    });
    $$('form.cmd').forEach((form) => form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const out = $('.out', form), btn = $('button.btn', form);
      const payload = {};
      new FormData(form).forEach((v, k) => { if (String(v).trim() !== '') payload[k] = v; });
      if (form.dataset.action === 'purge' && !confirm('Delete your own recent messages in that channel?')) return;
      out.hidden = false; out.className = 'out'; out.textContent = 'Running…';
      await busy(btn, async () => {
        try { renderResult(out, await act(form.dataset.action, payload)); if (form.dataset.action === 'send') form.querySelector('textarea').value = ''; }
        catch (err) { out.className = 'out err'; out.textContent = err.message; }
      });
    }));

    initModeration();
    $('#prefixForm').addEventListener('submit', async (e) => { e.preventDefault(); await busy($('button', e.target), async () => { savedMsg(await saveSetting('prefix', $('#prefixInput').value)); }); });
    $('#statusForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      await busy($('button', e.target), async () => { savedMsg(await saveSetting('presence', { ...SETTINGS.presence, status: $('#pStatus').value })); });
    });
    initRpc();
    const devForm = $('#deviceForm');
    if (devForm) {
      devForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const picked = devForm.querySelector('input[name=device]:checked');
        await busy($('button', devForm), async () => {
          savedMsg(await saveSetting('device', { type: picked ? picked.value : 'desktop' }));
        });
      });
    }

    const renderMap = (el, map, keyFmt, removeFn) => {
      const entries = Object.entries(map || {});
      el.innerHTML = entries.length ? entries.map(([k, v]) => `<div class="list-row"><div class="grow"><b>${esc(keyFmt(k))}</b><small>${esc(v)}</small></div><button class="btn btn-sm btn-danger" data-rm="${esc(k)}">${ico('x', 14)}</button></div>`).join('') : '<div class="muted small">Nothing added yet.</div>';
      $$('[data-rm]', el).forEach((b) => b.addEventListener('click', () => removeFn(b.dataset.rm)));
    };
    const drawReact = () => renderMap($('#reactList'), SETTINGS.autoreact, (k) => 'User ' + k, async (k) => {
      const m = { ...SETTINGS.autoreact }; delete m[k]; try { savedMsg(await saveSetting('autoreact', m)); drawReact(); } catch (e) { fail(e); }
    });
    const drawReply = () => renderMap($('#replyList'), SETTINGS.autoreply, (k) => '“' + k + '”', async (k) => {
      const m = { ...SETTINGS.autoreply }; delete m[k]; try { savedMsg(await saveSetting('autoreply', m)); drawReply(); } catch (e) { fail(e); }
    });
    drawReact(); drawReply();
    $('#reactForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      await busy($('button', e.target), async () => {
        savedMsg(await saveSetting('autoreact', { ...SETTINGS.autoreact, [$('#reactUser').value.trim()]: $('#reactEmoji').value.trim() }));
        e.target.reset(); drawReact();
      });
    });
    $('#replyForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      await busy($('button', e.target), async () => {
        savedMsg(await saveSetting('autoreply', { ...SETTINGS.autoreply, [$('#replyTrigger').value.trim().toLowerCase()]: $('#replyText').value.trim() }));
        e.target.reset(); drawReply();
      });
    });
    $('#afkForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      await busy($('button', e.target), async () => {
        const on = $('#afkOn').checked;
        savedMsg(await saveSetting('afk', { on, reason: $('#afkReason').value || 'AFK', since: on && SETTINGS.afk.on ? SETTINGS.afk.since : 0 }));
        $('#afkState').textContent = on ? 'ON' : 'off';
      });
    });
  }

  // ---------------------------------------------------------------- snipers page
  function initSnipers() {
    $$('.sn-form').forEach((form) => form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const kind = form.dataset.kind, f = form.elements;
      const value = { enabled: f.enabled.checked, blacklist: f.blacklist.value };
      if (kind === 'nitro') value.delay_ms = f.delay_ms.value;
      else Object.assign(value, { triggers: f.triggers.value, ignore_words: f.ignore_words.value, min_delay: f.min_delay.value, max_delay: f.max_delay.value,
        use_buttons: f.use_buttons.checked, use_reactions: f.use_reactions.checked, win_alerts: f.win_alerts.checked, win_keywords: f.win_keywords.value,
        webhook_url: f.webhook_url.value, clear_webhook: !!(f.clear_webhook && f.clear_webhook.checked) });
      await busy($('button[type=submit]', form), async () => { savedMsg(await saveSetting(kind, value)); });
    }));
    const load = async () => {
      for (const kind of ['nitro', 'giveaway']) {
        try {
          const d = await api('/api/sniper?kind=' + kind);
          $('#sn-' + kind).innerHTML = d.events.length ? d.events.map((x) => `<tr><td><span class="event-tag ${esc(x.result)}">${esc(x.result)}</span></td>
            <td><div class="log-message mono">${esc(x.ref)}</div><div class="log-sub">${esc(x.detail || '')}</div></td>
            <td><div class="server-cell">${esc(x.guild_name || '—')}</div><div class="log-sub">${esc(x.channel_name || '')} · ${esc(x.author || '')}</div></td>
            <td class="time-cell">${esc(x.latency_ms)} ms</td><td class="time-cell">${esc(fmtTime(x.created_at))}</td></tr>`).join('')
            : '<tr><td colspan="5" class="empty-state">Nothing sniped yet.</td></tr>';
          ['seen', 'success', 'failed', 'wins'].forEach((k) => { const el = $(`[data-sn="${kind}.${k}"]`); if (el) el.textContent = d.summary[kind][k]; });
        } catch (e) { /* ignore */ }
      }
    };
    load(); setInterval(load, 6000);
  }

  // ---------------------------------------------------------------- inbox page
  function initInbox() {
    let channels = [], current = null, chatTimer = null, replyTo = null, members = [], msgs = [];
    const names = {};                                   // user id -> display name (for rendering <@id>)
    const list = $('#dmList'), chat = $('#chat'), input = $('#chatInput'), pop = $('#mentionPop'), bar = $('#replyBar');
    const drawList = () => {
      const q = $('#dmSearch').value.toLowerCase();
      const items = channels.filter((c) => c.name.toLowerCase().includes(q));
      list.innerHTML = items.length ? items.map((c) => `<button class="dm-item ${current === c.id ? 'active' : ''}" data-id="${esc(c.id)}">${avatar(c.avatar, c.name, 'md')}<span class="acct-meta"><b>${esc(c.name)}</b><small>${c.group ? 'Group · ' : ''}${esc(c.last_at ? fmtTime(c.last_at) : 'No messages')}</small></span></button>`).join('') : '<div class="empty-state">No conversations.</div>';
      $$('.dm-item', list).forEach((b) => b.addEventListener('click', () => openChat(b.dataset.id)));
    };
    const loadList = async () => {
      try { channels = (await act('dm_list')).channels; drawList(); } catch (e) { list.innerHTML = `<div class="empty-state">${esc(e.message)}</div>`; }
    };
    // message text -> safe html: links, and <@id> shown as a highlighted @name
    const fmt = (t) => esc(t)
      .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
      .replace(/&lt;@!?(\d+)&gt;/g, (_, id) => `<span class="mention">@${esc(names[id] || 'user')}</span>`);

    // ----- replies
    const showReply = () => {
      bar.hidden = !replyTo;
      if (replyTo) {
        bar.innerHTML = `${ico('reply', 14)}<span>Replying to <b>${esc(replyTo.author)}</b> · ${esc(replyTo.preview)}</span><button class="icon-btn sm" type="button" id="replyCancel" title="Cancel">${ico('x', 14)}</button>`;
        $('#replyCancel').addEventListener('click', () => { replyTo = null; showReply(); });
      }
    };
    const setReply = (id) => {
      const m = msgs.find((x) => x.id === id); if (!m) return;
      replyTo = { id, author: m.mine ? 'yourself' : m.author, preview: (m.content || (m.attachments.length ? 'attachment' : 'message')).slice(0, 80) };
      showReply(); input.focus();
    };

    // ----- chat view
    const loadChat = async (quiet) => {
      if (!current) return;
      const channel = current;
      try {
        const d = await act('dm_history2', { channel_id: channel, limit: 50 });
        if (channel !== current) return;
        msgs = d.messages;
        msgs.forEach((m) => { names[m.author_id] = m.author; (m.mentions || []).forEach((u) => { names[u.id] = u.name; }); });
        const atEnd = chat.scrollTop + chat.clientHeight >= chat.scrollHeight - 60;
        chat.innerHTML = msgs.length ? msgs.map((m) => `<div class="bubble ${m.mine ? 'mine' : ''}" data-mid="${esc(m.id)}">${m.ref ? `<div class="b-ref">${ico('reply', 11)} <b>${esc(m.ref.author || '')}</b> ${esc(m.ref.preview || '')}</div>` : ''}${m.mine ? '' : `<b>${esc(m.author)}</b><br>`}${fmt(m.content)}${m.attachments.map((u) => `<br><a href="${esc(u)}" target="_blank" rel="noopener">attachment</a>`).join('')}${m.embeds ? '<br><small>[embed]</small>' : ''}<small>${esc(fmtTime(m.ts))}</small><button class="b-reply" type="button" data-reply="${esc(m.id)}" title="Reply">${ico('reply', 13)}</button></div>`).join('') : '<div class="empty-state">No messages yet.</div>';
        $$('[data-reply]', chat).forEach((b) => b.addEventListener('click', () => setReply(b.dataset.reply)));
        if (!quiet || atEnd) chat.scrollTop = chat.scrollHeight;
      } catch (e) { if (!quiet) chat.innerHTML = `<div class="empty-state">${esc(e.message)}</div>`; }
    };
    const loadMembers = async () => {
      members = [];
      try { members = (await act('dm_members', { channel_id: current })).members; members.forEach((u) => { names[u.id] = u.display_name; }); } catch (e) { /* mentions just stay empty */ }
    };
    const openChat = (id) => {
      current = id; replyTo = null; showReply(); closePop();
      const c = channels.find((x) => x.id === id);
      $('#dmTitle').textContent = c ? c.name : 'Conversation';
      input.disabled = false; $('#chatForm button').disabled = false; drawList();
      chat.innerHTML = '<div class="empty-state">Loading…</div>';
      loadChat(false); loadMembers(); clearInterval(chatTimer); chatTimer = setInterval(() => loadChat(true), 7000);
    };

    // ----- @ mentions (works like Discord: type @, pick a name)
    let popItems = [], popIdx = 0;
    const mentionMap = {};                              // "@name" text typed/picked -> user id
    const closePop = () => { pop.hidden = true; popItems = []; };
    const mentionQuery = () => {
      const upto = input.value.slice(0, input.selectionStart ?? input.value.length);
      const m = upto.match(/(^|\s)@([^\s@]{0,32})$/);
      return m ? { q: m[2].toLowerCase(), start: upto.length - m[2].length - 1 } : null;
    };
    const drawPop = () => {
      pop.innerHTML = `<div class="mp-head">MEMBERS</div>` + popItems.map((u, i) => `<button type="button" class="${i === popIdx ? 'active' : ''}" data-i="${i}">${avatar(u.avatar, u.display_name, 'sm')}<b>${esc(u.display_name)}</b><small>@${esc(u.username)}</small></button>`).join('');
      pop.hidden = false;
      $$('button', pop).forEach((b) => b.addEventListener('mousedown', (e) => { e.preventDefault(); pick(+b.dataset.i); }));
    };
    const updatePop = () => {
      const mq = mentionQuery();
      if (!mq || !current) return closePop();
      popItems = members.filter((u) => !mq.q || u.display_name.toLowerCase().includes(mq.q) || u.username.toLowerCase().includes(mq.q))
        .sort((a, b) => (b.display_name.toLowerCase().startsWith(mq.q) - a.display_name.toLowerCase().startsWith(mq.q)) || a.display_name.localeCompare(b.display_name)).slice(0, 8);
      popIdx = 0;
      popItems.length ? drawPop() : closePop();
    };
    const pick = (i) => {
      const u = popItems[i], mq = mentionQuery(); if (!u || !mq) return;
      const caret = input.selectionStart, tag = '@' + u.display_name;
      input.value = input.value.slice(0, mq.start) + tag + ' ' + input.value.slice(caret);
      const pos = mq.start + tag.length + 1; input.setSelectionRange(pos, pos);
      mentionMap[tag.toLowerCase()] = u.id; closePop(); input.focus();
    };
    // "@Name" -> <@id> right before sending (picked names first, then exact username / display-name matches)
    const withMentions = (text) => {
      const table = { ...mentionMap };
      members.forEach((u) => { table['@' + u.display_name.toLowerCase()] = table['@' + u.display_name.toLowerCase()] || u.id; table['@' + u.username.toLowerCase()] = table['@' + u.username.toLowerCase()] || u.id; });
      Object.keys(table).sort((a, b) => b.length - a.length).forEach((tag) => {
        const re = new RegExp('(^|\\s)' + tag.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '(?=$|[\\s.,!?;:])', 'gi');
        text = text.replace(re, (_, pre) => `${pre}<@${table[tag]}>`);
      });
      return text;
    };
    input.addEventListener('input', updatePop);
    input.addEventListener('click', updatePop);
    input.addEventListener('blur', () => setTimeout(closePop, 120));
    input.addEventListener('keydown', (e) => {
      if (pop.hidden || !popItems.length) return;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); popIdx = (popIdx + (e.key === 'ArrowDown' ? 1 : popItems.length - 1)) % popItems.length; drawPop(); }
      else if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); pick(popIdx); }
      else if (e.key === 'Escape') { e.preventDefault(); closePop(); }
    });

    $('#dmSearch').addEventListener('input', drawList);
    $('#dmRefresh').addEventListener('click', () => { loadList(); loadChat(false); loadMembers(); });
    $('#chatForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const text = input.value.trim(); if (!text || !current) return;
      await busy($('#chatForm button'), async () => {
        await act('send2', { channel_id: current, content: withMentions(text), reply_to: replyTo ? replyTo.id : undefined });
        input.value = ''; replyTo = null; showReply(); closePop();
        await loadChat(false);
      });
    });
    loadList();

    // notifications tab
    const nl = $('#notifList'), kindSel = $('#notifKind');
    const loadNotifs = async () => {
      try {
        const d = await api('/api/notifications?kind=' + kindSel.value + '&limit=150');
        nl.innerHTML = d.items.length ? d.items.map(notifHtml).join('') : '<div class="empty-state">Nothing here yet. DMs, pings and friend requests will show up as they happen.</div>';
        setBadges(d.unread);
        return d.items.filter((n) => !n.is_read).map((n) => n.id);
      } catch (e) { return []; }
    };
    kindSel.addEventListener('change', loadNotifs);
    document.addEventListener('newnotifs', loadNotifs);
    document.addEventListener('tabshown', async (ev) => {
      if (ev.detail !== 'notifications') return;
      const unread = await loadNotifs();
      if (unread.length) setTimeout(async () => { await post('/api/notifications/read', { ids: unread }); loadNotifs(); }, 3000);
    });
    if (location.hash === '#notifications') loadNotifs();
    $('#markAll').addEventListener('click', async () => { await post('/api/notifications/read', {}); loadNotifs(); });
    $('#clearNotifs').addEventListener('click', async () => { if (confirm('Delete all notifications for this account?')) { await post('/api/notifications/clear', {}); loadNotifs(); } });
    const saveNotify = async () => { try { savedMsg(await saveSetting('notify', { dms: $('#nDms').checked, pings: $('#nPings').checked })); } catch (e) { fail(e); } };
    $('#nDms').addEventListener('change', saveNotify); $('#nPings').addEventListener('change', saveNotify);
    $('#browserNotif').addEventListener('click', async () => {
      if (!window.Notification) return toast('This browser does not support notifications', 'err');
      toast((await Notification.requestPermission()) === 'granted' ? 'Browser alerts enabled' : 'Permission was not granted');
    });
  }

  // ---------------------------------------------------------------- friends page
  function initFriends() {
    let data = { friends: [], incoming: [], outgoing: [] };
    const row = (u, buttons) => `<div class="list-row">${avatar(u.avatar, u.display_name, 'md')}<div class="grow"><b>${esc(u.display_name)}</b><small>@${esc(u.username)} · ${esc(u.id)}</small></div><div class="btns">${buttons}</div></div>`;
    const rm = (u, label, cls = 'btn-danger') => `<button class="btn btn-sm ${cls}" data-friend-remove="${esc(u.id)}">${label}</button>`;
    const draw = () => {
      const q = $('#friendSearch').value.toLowerCase();
      const fr = data.friends.filter((u) => (u.display_name + u.username).toLowerCase().includes(q));
      $('#friendList').innerHTML = fr.length ? fr.map((u) => row(u, rm(u, 'Remove'))).join('') : '<div class="empty-state">No friends found.</div>';
      $('#incomingList').innerHTML = data.incoming.length ? data.incoming.map((u) => row(u, `<button class="btn btn-sm btn-primary" data-friend-accept="${esc(u.id)}">${ico('check', 14)} Accept</button>` + rm(u, ico('x', 14)))).join('') : '<div class="empty-state">No incoming requests.</div>';
      $('#outgoingList').innerHTML = data.outgoing.length ? data.outgoing.map((u) => row(u, rm(u, 'Cancel'))).join('') : '<div class="empty-state">No outgoing requests.</div>';
      $('#cntFriends').textContent = data.friends.length;
      const c = $('#cntReq'); c.textContent = data.incoming.length; c.hidden = !data.incoming.length;
      $$('[data-friend-accept]').forEach((b) => b.addEventListener('click', () => doFriend(b, 'friend_accept', b.dataset.friendAccept)));
      $$('[data-friend-remove]').forEach((b) => b.addEventListener('click', () => { if (b.textContent.trim() !== 'Remove' || confirm('Remove this friend?')) doFriend(b, 'friend_remove', b.dataset.friendRemove); }));
    };
    const load = async () => { try { data = await act('friends'); draw(); } catch (e) { $('#friendList').innerHTML = `<div class="empty-state">${esc(e.message)}</div>`; } };
    const doFriend = (btn, action, id) => busy(btn, async () => { await act(action, { user_id: id }); toast('Done'); await sleep(600); await load(); });
    $('#friendSearch').addEventListener('input', draw);
    $('#friendsRefresh').addEventListener('click', load);
    $('#addFriend').addEventListener('submit', async (e) => {
      e.preventDefault();
      await busy($('button', e.target), async () => { await act('friend_add', { user: $('#friendUser').value }); toast('Friend request sent'); e.target.reset(); await sleep(600); load(); });
    });
    document.addEventListener('newnotifs', load);
    load();
  }

  // ---------------------------------------------------------------- logs page
  function initLogs() {
    const rows = $('#logRows'), filter = $('#categoryFilter'), search = $('#searchInput'), last = $('#lastRefresh');
    const load = async () => {
      try {
        const d = await api('/api/logs?' + new URLSearchParams({ category: filter.value, q: search.value.trim(), limit: '200' }));
        rows.innerHTML = d.length ? d.map(logRow).join('') : '<tr><td colspan="4" class="empty-state">No events match.</td></tr>';
        last.textContent = 'Updated ' + new Date().toLocaleTimeString();
      } catch (e) { last.textContent = 'Could not refresh'; }
    };
    let t; search.addEventListener('input', () => { clearTimeout(t); t = setTimeout(load, 250); });
    filter.addEventListener('change', load);
    $('#refreshBtn').addEventListener('click', load);
    $('#clearLogs').addEventListener('click', async () => { if (confirm('Clear all logs for this account?')) { await post('/api/logs/clear', {}); load(); } });
    load(); setInterval(() => { if (!document.hidden) load(); }, 5000);
  }

  // ---------------------------------------------------------------- servers page
  function initServers() {
    $('#serverSearch').addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase();
      $$('.srv-card').forEach((c) => { c.hidden = !c.dataset.name.includes(q); });
    });
  }

  // ---------------------------------------------------------------- quests page
  function initQuests() {
    const grid = $('#questGrid');
    let quests = [], state = 'offline';
    let filter = ['new', 'available', 'running', 'paused', 'completed'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'all';
    const TASKS = { PLAY_ON_DESKTOP: 'Play a game', WATCH_VIDEO: 'Watch a video', WATCH_VIDEO_ON_MOBILE: 'Watch a video (mobile)', STREAM_ON_DESKTOP: 'Stream a game', PLAY_ACTIVITY: 'Play an activity' };
    const mins = (s) => (s >= 3600 ? (s / 3600).toFixed(1) + ' h' : s >= 60 ? Math.round(s / 60) + ' min' : Math.round(s) + ' s');
    const left = (iso) => {
      if (!iso) return '';
      const s = (new Date(iso) - Date.now()) / 1000;
      if (s <= 0) return 'expired';
      return s > 86400 ? `ends in ${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h` : `ends in ${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
    };
    const buttons = (q) => {
      if (state !== 'online') return '';
      if (!q.supported && q.status !== 'completed') return '<small class="muted">Unsupported type</small>';
      if (q.status === 'running') return `<button class="btn btn-sm btn-danger" data-q="quest_pause" data-id="${esc(q.id)}">${ico('stop', 14)} Pause</button>`;
      if (q.status === 'paused') return `<button class="btn btn-sm btn-primary" data-q="quest_start" data-id="${esc(q.id)}">${ico('play', 14)} Resume</button>`;
      if (q.status === 'new' || q.status === 'available') {
        return (q.enrolled_at ? '' : `<button class="btn btn-sm" data-q="quest_enroll" data-id="${esc(q.id)}">Accept</button>`) +
          `<button class="btn btn-sm btn-primary" data-q="quest_start" data-id="${esc(q.id)}">${ico('play', 14)} Complete</button>`;
      }
      return q.status === 'completed' ? '<small class="muted">Claim in Discord</small>' : '';
    };
    const draw = () => {
      const list = quests.filter((q) => filter === 'all' || q.status === filter);
      grid.innerHTML = list.length ? list.map((q) => {
        const pct = q.target ? Math.min(100, Math.round((q.progress / q.target) * 100)) : (q.status === 'completed' ? 100 : 0);
        return `<article class="quest-card ${esc(q.status)}"><div class="quest-top"><span class="stat-icon ${q.task === 'PLAY_ON_DESKTOP' ? 'lime' : 'blue'}">${ico(q.task === 'PLAY_ON_DESKTOP' ? 'play' : 'flag', 20)}</span>
          <div class="grow"><b>${esc(q.name)}</b><small>${esc([q.game, q.publisher].filter(Boolean).join(' · '))}</small></div><span class="pill ${esc(q.status)}">${esc(q.status)}</span></div>
          <div class="quest-meta"><span>${esc(TASKS[q.task] || q.task || 'Quest')}${q.target ? ' · ' + mins(q.target) : ''}</span>${q.reward ? `<span class="chip lime">${ico('gift', 12)} ${esc(q.reward)}</span>` : ''}</div>
          <div class="bar"><i style="width:${pct}%"></i></div>
          <div class="bar-label"><span>${q.target ? `${mins(q.progress)} / ${mins(q.target)} · ${pct}%` : ''}</span><span>${esc(q.run_message || '')}</span></div>
          <div class="quest-foot"><small class="muted">${esc(left(q.expires_at))}</small><div class="btns">${buttons(q)}</div></div></article>`;
      }).join('') : `<div class="empty-state">No ${filter === 'all' ? '' : filter + ' '}quests. ${quests.length ? '' : 'Start the bot and press Refresh.'}</div>`;
      $$('[data-q]', grid).forEach((b) => b.addEventListener('click', () => busy(b, async () => {
        const r = await act(b.dataset.q, { quest_id: b.dataset.id });
        toast(r.title || 'Done'); await sleep(400); await load();
      })));
    };
    const load = async () => {
      try {
        const d = await api('/api/quests');
        quests = d.quests; state = d.state;
        $$('[data-qc]').forEach((el) => { el.textContent = (d.counts || {})[el.dataset.qc] || 0; });
        $('#questUpdated').textContent = d.updated ? 'List updated ' + fmtTime(d.updated) : '';
        draw();
      } catch (e) { grid.innerHTML = `<div class="empty-state">${esc(e.message)}</div>`; }
    };
    document.addEventListener('tabshown', (ev) => { filter = ev.detail; draw(); });
    $('#qRefresh').addEventListener('click', (e) => busy(e.currentTarget, async () => { await act('quest_list'); toast('Quests refreshed'); await load(); }));
    $('#qStartAll').addEventListener('click', (e) => { if (confirm('Start every supported quest? They run two at a time and take real time to finish.')) busy(e.currentTarget, async () => { const r = await act('quest_start_all'); toast(`Started ${r.fields[0][1]} quest(s)`); await sleep(500); await load(); }); });
    $('#qPauseAll').addEventListener('click', (e) => busy(e.currentTarget, async () => { await act('quest_pause_all'); toast('Paused'); await load(); }));
    load(); setInterval(load, 5000);
  }

  // ---------------------------------------------------------------- crypto page
  const CUR = { usd: 'USD', eur: 'EUR', gbp: 'GBP', inr: 'INR', jpy: 'JPY' };
  const money = (n, vs) => {
    if (n == null || isNaN(n)) return '—';
    if (vs === 'btc') return Number(n).toFixed(n < 1 ? 8 : 4) + ' ₿';
    const big = Math.abs(n) >= 1;
    return new Intl.NumberFormat(undefined, { style: 'currency', currency: CUR[vs] || 'USD', minimumFractionDigits: big ? 2 : 2, maximumSignificantDigits: big ? undefined : 4 }).format(n);
  };
  const compact = (n, vs) => (n == null ? '—' : (vs === 'btc' ? '' : (CUR[vs] === 'USD' ? '$' : '')) + new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 2 }).format(n));
  const pct = (n) => (n == null || isNaN(n) ? '<span class="muted">—</span>' : `<span class="${n >= 0 ? 'up' : 'down'}">${n >= 0 ? '▲' : '▼'} ${Math.abs(n).toFixed(2)}%</span>`);
  const sparkline = (vals) => {
    if (!vals || vals.length < 2) return '';
    const min = Math.min(...vals), max = Math.max(...vals), span = max - min || 1, w = 110, h = 32;
    const pts = vals.map((v, i) => `${((i / (vals.length - 1)) * w).toFixed(1)},${(h - 3 - ((v - min) / span) * (h - 6)).toFixed(1)}`).join(' ');
    return `<svg class="spark-svg ${vals[vals.length - 1] >= vals[0] ? 'up' : 'down'}" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
  };
  let chartSeq = 0;
  function drawChart(el, prices, vs) {
    if (!prices || prices.length < 2) { el.innerHTML = '<div class="empty-state">No chart data.</div>'; return; }
    const W = 800, H = 300, PL = 6, PR = 64, PT = 12, PB = 26, id = 'cg' + (++chartSeq);
    const xs = prices.map((p) => p[0]), ys = prices.map((p) => p[1]);
    const minY = Math.min(...ys), maxY = Math.max(...ys), spanY = maxY - minY || 1, minX = xs[0], spanX = xs[xs.length - 1] - minX || 1;
    const X = (t) => PL + ((t - minX) / spanX) * (W - PL - PR), Y = (v) => PT + (1 - (v - minY) / spanY) * (H - PT - PB);
    const up = ys[ys.length - 1] >= ys[0], color = up ? '#b8f43c' : '#ff6b81';
    const line = prices.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join('');
    const area = `${line}L${X(xs[xs.length - 1]).toFixed(1)},${H - PB}L${X(xs[0]).toFixed(1)},${H - PB}Z`;
    const span = spanX / 86400000, fmtX = (t) => new Date(t).toLocaleString([], span <= 1.2 ? { hour: '2-digit', minute: '2-digit' } : { month: 'short', day: 'numeric' });
    const grid = [0, 0.5, 1].map((f) => { const v = minY + spanY * (1 - f), y = PT + f * (H - PT - PB); return `<line x1="${PL}" x2="${W - PR}" y1="${y}" y2="${y}" stroke="rgba(148,177,255,.12)"/><text x="${W - PR + 6}" y="${y + 4}" fill="#8693b0" font-size="11">${esc(compact(v, vs))}</text>`; }).join('');
    const xl = [0, 0.5, 1].map((f) => `<text x="${PL + f * (W - PL - PR)}" y="${H - 6}" fill="#5d6a88" font-size="11" text-anchor="${f === 0 ? 'start' : f === 1 ? 'end' : 'middle'}">${esc(fmtX(minX + spanX * f))}</text>`).join('');
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="chart-svg"><defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".35"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>${grid}${xl}
      <path d="${area}" fill="url(#${id})"/><path d="${line}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
      <line class="cross" y1="${PT}" y2="${H - PB}" stroke="rgba(255,255,255,.35)" hidden/><circle class="dotc" r="4" fill="${color}" hidden/></svg><div class="chart-tip" hidden></div>`;
    const svg = $('svg', el), tip = $('.chart-tip', el), cross = $('.cross', el), dot = $('.dotc', el);
    const move = (ev) => {
      const r = svg.getBoundingClientRect(), cx = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
      const t = minX + ((cx / r.width) * W - PL) / (W - PL - PR) * spanX;
      let lo = 0, hi = xs.length - 1; while (lo < hi) { const m = (lo + hi) >> 1; xs[m] < t ? (lo = m + 1) : (hi = m); }
      const i = Math.max(0, Math.min(xs.length - 1, lo)), px = X(xs[i]), py = Y(ys[i]);
      cross.setAttribute('x1', px); cross.setAttribute('x2', px); dot.setAttribute('cx', px); dot.setAttribute('cy', py);
      [cross, dot, tip].forEach((n) => { n.hidden = false; });
      tip.innerHTML = `<b>${esc(money(ys[i], vs))}</b><small>${esc(new Date(xs[i]).toLocaleString())}</small>`;
      tip.style.left = Math.min(Math.max((px / W) * r.width - 60, 0), r.width - 130) + 'px';
    };
    const leave = () => [cross, dot, tip].forEach((n) => { n.hidden = true; });
    svg.addEventListener('mousemove', move); svg.addEventListener('touchmove', move, { passive: true }); svg.addEventListener('mouseleave', leave); svg.addEventListener('touchend', leave);
  }

  function initCrypto() {
    let vs = 'usd', page = 1, query = '', coins = [], selected = null, days = '7', ws = null, wsTimer = null;
    const rows = $('#coinRows'), live = { dot: $('#liveDot'), text: $('#liveText') };
    const setLive = (on, text) => { live.dot.classList.toggle('offline', !on); live.text.textContent = text; };

    // ----- global stats
    const loadGlobal = async () => {
      try {
        const g = await api('/api/crypto/global?vs=' + vs);
        $('#gMcap').textContent = compact(g.mcap, vs); $('#gVol').textContent = compact(g.volume, vs);
        $('#gDom').textContent = `${(g.btc_dom || 0).toFixed(1)}% · ${(g.eth_dom || 0).toFixed(1)}%`;
        $('#gChange').innerHTML = pct(g.change24h) + ' 24H'; $('#gCoins').textContent = g.coins ? g.coins.toLocaleString() + ' coins' : '';
      } catch (e) { /* ignore */ }
    };

    // ----- market table
    const rowHtml = (c) => `<tr data-id="${esc(c.id)}" data-sym="${esc((c.symbol || '').toUpperCase())}"><td class="muted">${c.rank ?? '—'}</td>
      <td><div class="coin-name">${c.image ? `<img src="${esc(c.image)}" alt="" loading="lazy" width="24" height="24" referrerpolicy="no-referrer">` : ''}<div><b>${esc(c.name)}</b><small>${esc((c.symbol || '').toUpperCase())}</small></div></div></td>
      <td class="px mono">${esc(money(c.price, vs))}</td><td class="c1h">${pct(c.ch1h)}</td><td class="c24">${pct(c.ch24h)}</td><td>${pct(c.ch7d)}</td>
      <td class="mono">${esc(compact(c.mcap, vs))}</td><td>${sparkline(c.spark)}</td></tr>`;
    const loadMarket = async () => {
      try {
        const d = await api('/api/crypto/markets?' + new URLSearchParams({ vs, page, q: query, per_page: 50 }));
        coins = d.coins;
        rows.innerHTML = coins.length ? coins.map(rowHtml).join('') : '<tr><td colspan="8" class="empty-state">No coins found.</td></tr>';
        $$('tr[data-id]', rows).forEach((tr) => tr.addEventListener('click', () => openDetail(coins.find((c) => c.id === tr.dataset.id))));
        $('#pageInfo').textContent = query ? `Search results` : `Page ${page}`;
        $('#prevPage').disabled = page <= 1 || !!query; $('#nextPage').disabled = !!query || coins.length < 50;
        connectLive();
        if (selected) { const c = coins.find((x) => x.id === selected.id); if (c) { selected = c; paintDetail(); } }
      } catch (e) { rows.innerHTML = `<tr><td colspan="8" class="empty-state">${esc(e.message)}</td></tr>`; }
    };

    // ----- real-time prices (Binance public stream) for the coins on screen; falls back to polling
    const closeLive = () => { clearTimeout(wsTimer); if (ws) { ws.onclose = null; ws.close(); ws = null; } };
    function connectLive() {
      closeLive();
      if (vs !== 'usd' || !window.WebSocket) { setLive(false, 'polling every 30s'); return; }
      const syms = coins.map((c) => (c.symbol || '').toLowerCase()).filter((s) => /^[a-z0-9]{2,12}$/.test(s)).slice(0, 80);
      if (!syms.length) return;
      try { ws = new WebSocket('wss://stream.binance.com:9443/ws'); } catch (e) { setLive(false, 'polling every 30s'); return; }
      const failTimer = setTimeout(() => { if (ws && ws.readyState !== 1) setLive(false, 'polling every 30s'); }, 6000);
      ws.onopen = () => ws.send(JSON.stringify({ method: 'SUBSCRIBE', params: syms.map((s) => `${s}usdt@miniTicker`), id: 1 }));
      ws.onmessage = (ev) => {
        let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (m.result === null) { clearTimeout(failTimer); setLive(true, 'live'); return; }
        if (!m.s || !m.c) return;
        const sym = m.s.replace(/USDT$/, ''), price = parseFloat(m.c), open = parseFloat(m.o);
        const tr = rows.querySelector(`tr[data-sym="${sym}"]`);
        if (tr) {
          const cell = $('.px', tr), prev = parseFloat(cell.dataset.p || 0);
          cell.textContent = money(price, 'usd'); cell.dataset.p = price;
          if (prev && prev !== price) { cell.classList.remove('flash-up', 'flash-down'); void cell.offsetWidth; cell.classList.add(price > prev ? 'flash-up' : 'flash-down'); }
          if (open) $('.c24', tr).innerHTML = pct(((price - open) / open) * 100);
        }
        if (selected && (selected.symbol || '').toUpperCase() === sym) { selected.price = price; $('#dPrice').textContent = money(price, 'usd'); }
      };
      ws.onclose = () => { setLive(false, 'reconnecting…'); wsTimer = setTimeout(connectLive, 8000); };
      ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
    }

    // ----- detail chart
    const paintDetail = () => {
      $('#dSym').textContent = (selected.symbol || '').toUpperCase() + (selected.rank ? ' · RANK #' + selected.rank : '');
      $('#dName').textContent = selected.name; $('#dPrice').textContent = money(selected.price, vs);
      $('#dChange').innerHTML = pct(selected.ch24h) + ' <span class="muted">24h</span>';
    };
    const loadChart = async () => {
      const box = $('#bigChart'); box.innerHTML = '<div class="empty-state">Loading chart…</div>';
      try { const d = await api(`/api/crypto/chart/${encodeURIComponent(selected.id)}?days=${days}&vs=${vs}`); drawChart(box, d.prices, vs); }
      catch (e) { box.innerHTML = `<div class="empty-state">${esc(e.message)}</div>`; }
    };
    function openDetail(c) { if (!c) return; selected = c; $('#detailPanel').hidden = false; paintDetail(); loadChart(); $('#detailPanel').scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    $('#detailClose').addEventListener('click', () => { $('#detailPanel').hidden = true; selected = null; });
    $$('#rangeSeg .seg-btn').forEach((b) => b.addEventListener('click', () => { $$('#rangeSeg .seg-btn').forEach((x) => x.classList.toggle('active', x === b)); days = b.dataset.days; if (selected) loadChart(); }));
    let t; $('#coinSearch').addEventListener('input', (e) => { clearTimeout(t); t = setTimeout(() => { query = e.target.value.trim(); page = 1; loadMarket(); }, 350); });
    $('#vsSel').addEventListener('change', (e) => { vs = e.target.value; page = 1; loadMarket(); loadGlobal(); if (selected) loadChart(); });
    $('#prevPage').addEventListener('click', () => { page = Math.max(1, page - 1); loadMarket(); });
    $('#nextPage').addEventListener('click', () => { page += 1; loadMarket(); });

    // ----- trending + movers
    const miniRow = (c) => `<div class="list-row" data-open="${esc(c.id)}">${c.image ? `<img class="mini-img" src="${esc(c.image)}" alt="" width="28" height="28" loading="lazy" referrerpolicy="no-referrer">` : ''}<div class="grow"><b>${esc(c.name)}</b><small>${esc((c.symbol || '').toUpperCase())}${c.rank ? ' · #' + c.rank : ''}</small></div><div class="mini-right"><span class="mono">${esc(money(c.price, c.price != null && vs ? vs : 'usd'))}</span>${pct(c.ch24h)}</div></div>`;
    const loadMovers = async () => {
      try {
        const tr = await api('/api/crypto/trending'); $('#trendList').innerHTML = tr.coins.map(miniRow).join('') || '<div class="empty-state">Nothing trending.</div>';
        const m = await api('/api/crypto/markets?' + new URLSearchParams({ vs, page: 1, per_page: 100 }));
        const sorted = m.coins.filter((c) => c.ch24h != null).sort((a, b) => b.ch24h - a.ch24h);
        $('#gainList').innerHTML = sorted.slice(0, 6).map(miniRow).join(''); $('#loseList').innerHTML = sorted.slice(-6).reverse().map(miniRow).join('');
        $$('[data-open]').forEach((r) => r.addEventListener('click', () => { const id = r.dataset.open; const c = coins.find((x) => x.id === id) || m.coins.find((x) => x.id === id) || { id, name: id, price: null }; $('.tab[data-tab="market"]').click(); openDetail(c); }));
      } catch (e) { $('#trendList').innerHTML = `<div class="empty-state">${esc(e.message)}</div>`; }
    };

    // ----- wallets + portfolio
    const wallets = () => SETTINGS.wallets || {};
    const COLORS = ['#47c2ff', '#b8f43c', '#a78bfa', '#ffc857', '#ff6b81', '#5eead4', '#f472b6', '#94a3b8'];
    let prices = {};
    const short = (a) => (a.length > 22 ? a.slice(0, 10) + '…' + a.slice(-8) : a);
    const drawWallets = () => {
      const list = $('#walletList'), entries = Object.entries(wallets());
      let total = 0; const parts = [];
      entries.forEach(([k, w], i) => {
        const p = prices[w.coin_id] && prices[w.coin_id].usd; const val = p && w.amount ? p * w.amount : 0; total += val; parts.push({ k, w, val, color: COLORS[i % COLORS.length] });
      });
      list.innerHTML = entries.length ? parts.map(({ k, w, val, color }) => `<div class="list-row"><span class="swatch" style="background:${color}"></span>
        <div class="grow"><b>${esc(w.symbol)} <small class="muted">${esc(w.coin !== w.symbol ? w.coin : '')}</small>${w.network ? ` <span class="chip">${esc(w.network)}</span>` : ''}</b><small class="mono">${esc(short(w.address))}</small>${w.note ? `<small>${esc(w.note)}</small>` : ''}</div>
        <div class="mini-right">${w.amount ? `<span class="mono">${w.amount} ${esc(w.symbol)}</span>` : ''}${val ? `<small class="muted">≈ ${esc(money(val, 'usd'))}</small>` : ''}</div>
        <div class="btns"><button class="icon-btn sm" data-copy="${esc(w.address)}" title="Copy">${ico('copy', 14)}</button><button class="icon-btn sm" data-edit="${esc(k)}" title="Edit">${ico('refresh', 14)}</button><button class="icon-btn sm danger" data-del="${esc(k)}" title="Delete">${ico('trash', 14)}</button></div></div>`).join('')
        : '<div class="empty-state">No wallets yet. Add your first public address on the right.</div>';
      $('#portTotal').textContent = total ? 'Portfolio ' + money(total, 'usd') : 'Portfolio —';
      $('#allocBar').innerHTML = total ? parts.filter((x) => x.val).map((x) => `<i style="flex:${x.val};background:${x.color}" title="${esc(x.w.symbol)} ${(x.val / total * 100).toFixed(1)}%"></i>`).join('') : '';
      $$('[data-copy]', list).forEach((b) => b.addEventListener('click', async () => { try { await navigator.clipboard.writeText(b.dataset.copy); toast('Address copied'); } catch (e) { toast('Copy failed - select the address manually', 'err'); } }));
      $$('[data-edit]', list).forEach((b) => b.addEventListener('click', () => { const w = wallets()[b.dataset.edit]; $('#wCoin').value = w.symbol; $('#wNet').value = w.network || ''; $('#wAmt').value = w.amount || ''; $('#wAddr').value = w.address; $('#wNote').value = w.note || ''; $('#wCoin').scrollIntoView({ behavior: 'smooth', block: 'center' }); }));
      $$('[data-del]', list).forEach((b) => b.addEventListener('click', async () => {
        if (!confirm('Remove this wallet?')) return;
        const next = { ...wallets() }; delete next[b.dataset.del];
        try { await saveSetting('wallets', next); drawWallets(); toast('Removed'); } catch (e) { fail(e); }
      }));
    };
    const loadPrices = async () => {
      const ids = [...new Set(Object.values(wallets()).filter((w) => w.coin_id && w.amount > 0).map((w) => w.coin_id))];
      if (!ids.length) return drawWallets();
      try { prices = (await api('/api/crypto/prices?vs=usd&ids=' + ids.join(','))).prices; } catch (e) { /* keep old */ }
      drawWallets();
    };
    const hints = {};
    let ht; $('#wCoin').addEventListener('input', (e) => {
      clearTimeout(ht); const q = e.target.value.trim(); if (q.length < 2) return;
      ht = setTimeout(async () => {
        try { const d = await api('/api/crypto/markets?' + new URLSearchParams({ q, per_page: 10 })); $('#coinHints').innerHTML = d.coins.map((c) => { hints[c.symbol.toUpperCase()] = c; return `<option value="${esc(c.symbol.toUpperCase())}">${esc(c.name)}</option>`; }).join(''); } catch (err) { /* ignore */ }
      }, 400);
    });
    $('#walletForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const sym = $('#wCoin').value.trim().toUpperCase(), net = $('#wNet').value.trim(), hint = hints[sym];
      const entry = { symbol: sym, coin: hint ? hint.name : sym, coin_id: hint ? hint.id : '', network: net, address: $('#wAddr').value.trim(), amount: $('#wAmt').value || 0, note: $('#wNote').value.trim() };
      const key = sym.toLowerCase().replace(/[^a-z0-9]/g, '') + (net ? '-' + net.toLowerCase().replace(/[^a-z0-9]/g, '') : '');
      const old = wallets()[key]; if (old && !hint) { entry.coin = old.coin || sym; entry.coin_id = old.coin_id || ''; }
      await busy($('button', e.target), async () => { await saveSetting('wallets', { ...wallets(), [key]: entry }); toast('Wallet saved'); e.target.reset(); loadPrices(); });
    });
    $('#importBtn').addEventListener('click', () => busy($('#importBtn'), async () => {
      const from = $('#importFrom').value; if (!from) return toast('Choose an account first', 'err');
      const d = await api('/api/settings/wallets?account_id=' + from);
      const merged = { ...d.value, ...wallets() };
      await saveSetting('wallets', merged); toast('Wallets imported'); loadPrices();
    }));

    // ----- boot
    loadGlobal(); loadMarket(); loadMovers(); loadPrices(); setLive(false, 'connecting…');
    setInterval(() => { if (!document.hidden) { loadMarket(); loadGlobal(); } }, 30000);
    setInterval(() => { if (!document.hidden) loadPrices(); }, 45000);
  }

  // ---------------------------------------------------------------- moderation (commands page)
  function initModeration() {
    const form = $('#modForm'); if (!form) return;
    $$('[data-mod]', form).forEach((b) => b.addEventListener('click', async () => {
      const action = b.dataset.mod, f = form.elements, out = $('.out', form);
      if (!f.guild_id.value || !f.user_id.value) return toast('Choose a server and enter a user ID', 'err');
      if (!confirm(`${action.toUpperCase()} user ${f.user_id.value}?`)) return;
      out.hidden = false; out.className = 'out'; out.textContent = 'Running…';
      await busy(b, async () => {
        try { renderResult(out, await act('mod', { action, guild_id: f.guild_id.value, user_id: f.user_id.value.trim(), reason: f.reason.value, duration: f.duration.value.trim() || undefined })); }
        catch (err) { out.className = 'out err'; out.textContent = err.message; }
      });
    }));
  }

  // helpers shared with extra.js (shop, developer portal, vanity panel)
  window.AE = { $, $$, api, post, act, toast, fail, busy, esc, ico, avatar, fmtTime, sleep, saveSetting, savedMsg, BOOT };

  // ---------------------------------------------------------------- boot
  initTabs();
  const page = document.body.dataset.page;
  if (!page) return; // login page
  initAccounts();
  startPolling();
  ({ overview: initOverview, accounts: initAccountsPage, commands: initCommands, snipers: initSnipers, quests: initQuests, crypto: initCrypto,
     inbox: initInbox, friends: initFriends, logs: initLogs, servers: initServers }[page] || (() => {}))();
})();
