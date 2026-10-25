/* AetherPanel extras: vanity sniper panel, Discord shop preview, developer portal.
   Loaded after app.js, which exposes the shared helpers as window.AE. */
(() => {
  'use strict';
  const AE = window.AE;
  if (!AE) return;
  const { $, $$, api, post, act, toast, fail, busy, esc, ico, fmtTime, saveSetting, savedMsg } = AE;
  const page = document.body.dataset.page;

  const idOk = (s) => /^\d{5,25}$/.test(String(s));
  // only ever put Discord CDN links into src / url(); everything else is dropped
  const cdn = (u) => (/^https:\/\/(cdn\.discordapp\.com|media\.discordapp\.net)\/[A-Za-z0-9_\-./?=&%@:+~]+$/.test(String(u || '')) ? u : '');
  const copyText = async (text, okMsg = 'Copied') => {
    try { await navigator.clipboard.writeText(text); toast(okMsg); }
    catch (e) { window.prompt('Copy this:', text); }
  };
  const letterAvatar = (name) => 'data:image/svg+xml;utf8,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" fill="#5865f2"/><text x="50" y="66" font-size="52" font-family="sans-serif" font-weight="700" text-anchor="middle" fill="#fff">${esc((name || '?').slice(0, 1).toUpperCase())}</text></svg>`);

  // ================================================================ vanity sniper panel
  function initVanity() {
    const form = $('#vnForm');
    if (!form) return;
    let watch = [...((window.VANITY || {}).watch || [])];
    const state = {};                                   // code -> {status, text}
    const WORDS = { free: 'free', taken: 'taken', limited: 'rate limited', unknown: 'unknown' };
    const describe = (d) => (d.status === 'taken' && d.guild_name ? `taken by ${d.guild_name}` : d.status === 'taken' ? 'taken' : d.status === 'free' ? 'free right now' : d.status === 'limited' ? 'rate limited - try again shortly' : `could not check${d.detail ? ' (' + d.detail + ')' : ''}`);
    const draw = () => {
      const box = $('#vnWatchList');
      box.innerHTML = watch.length ? watch.map((c) => {
        const s = state[c];
        return `<div class="vn-row"><div class="grow"><b class="mono">discord.gg/${esc(c)}</b><small>${s ? esc(s.text) : 'not checked yet'}</small></div>
          <div class="btns">${s ? `<span class="pill ${esc(s.status)}">${esc(WORDS[s.status] || s.status)}</span>` : ''}
          <button class="btn btn-sm" type="button" data-chk="${esc(c)}">Check</button>
          <button class="btn btn-sm btn-danger" type="button" data-rm="${esc(c)}" title="Stop watching">${ico('x', 14)}</button></div></div>`;
      }).join('') : '<div class="empty-state">Nothing watched yet. Add a vanity code above.</div>';
      $('#vnStatWatch').textContent = watch.length;
      $$('[data-chk]', box).forEach((b) => b.addEventListener('click', () => busy(b, () => check(b.dataset.chk))));
      $$('[data-rm]', box).forEach((b) => b.addEventListener('click', () => busy(b, async () => { await persistWatch(watch.filter((x) => x !== b.dataset.rm)); })));
    };
    const check = async (code) => {
      const d = await api('/api/vanity/check?code=' + encodeURIComponent(code));
      state[code] = { status: d.status, text: describe(d) };
      if (watch.includes(code)) draw();
      return d;
    };
    const persistWatch = async (next) => {
      const r = await saveSetting('vanity', { watch: next });
      watch = r.value.watch || next; draw(); savedMsg(r);
    };

    $('#vnAddForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const input = e.target.elements.code;
      await busy($('button', e.target), async () => {
        await persistWatch([...new Set([...watch, input.value])]);
        input.value = '';
      });
    });
    $('#vnCheckAll').addEventListener('click', (e) => busy(e.currentTarget, async () => {
      if (!watch.length) return toast('Add a code first', 'err');
      for (const code of [...watch]) { await check(code); }
      toast('Checked ' + watch.length + ' code(s)');
    }));
    $('#vnQuick').addEventListener('submit', async (e) => {
      e.preventDefault();
      const out = $('#vnQuickOut');
      await busy($('button', e.target), async () => {
        out.hidden = false; out.className = 'out'; out.textContent = 'Checking…';
        try {
          const d = await api('/api/vanity/check?code=' + encodeURIComponent(e.target.elements.code.value));
          out.innerHTML = `<div class="res-title">discord.gg/${esc(d.code)}</div><div class="res-text">${esc(describe(d))}</div>`;
          if (d.status === 'free' && !watch.includes(d.code)) out.innerHTML += `<button class="btn btn-sm btn-primary" type="button" id="vnWatchIt">${ico('plus', 14)} Watch it</button>`;
          const w = $('#vnWatchIt'); if (w) w.addEventListener('click', () => busy(w, () => persistWatch([...watch, d.code])));
        } catch (err) { out.className = 'out err'; out.textContent = err.message; }
      });
    });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const f = form.elements;
      const hookChanged = !!f.webhook_url.value.trim() || !!(f.clear_webhook && f.clear_webhook.checked);
      await busy($('button[type=submit]', form), async () => {
        const r = await saveSetting('vanity', {
          enabled: f.enabled.checked, interval: f.interval.value, notify_free: f.notify_free.checked, notify_changes: f.notify_changes.checked,
          webhook_url: f.webhook_url.value, clear_webhook: !!(f.clear_webhook && f.clear_webhook.checked),
          webhook_name: f.webhook_name.value, webhook_avatar: f.webhook_avatar.value });
        savedMsg(r);
        $('#vnStatState').textContent = r.value.enabled ? 'On' : 'Off'; $('#vnStatState').classList.toggle('lime-text', !!r.value.enabled);
        $('#vnStatInterval').textContent = r.value.interval + 's';
        f.webhook_url.value = '';
        if (hookChanged) setTimeout(() => location.reload(), 700);
      });
    });
    $('#vnTest').addEventListener('click', (e) => busy(e.currentTarget, async () => { await post('/api/vanity/test', {}); toast('Test alert sent - check your webhook channel'); }));
    draw();
  }

  // ================================================================ shop
  function initShop() {
    const root = $('#shopRoot');
    if (!root) return;
    const me = { name: root.dataset.name || 'You', username: root.dataset.username || '', avatar: cdn(root.dataset.avatar) || letterAvatar(root.dataset.name) };
    const KIND = { decoration: 'Decoration', nameplate: 'Nameplate', effect: 'Profile effect' };
    const STEP = 60;
    let items = [], kind = 'all', cat = '', q = '', shown = STEP;
    const picked = { decoration: null, nameplate: null, effect: null };
    const keyOf = (i) => i.kind + ':' + i.id;
    const byKey = {};

    // ----- grid
    const filtered = () => items.filter((i) => (kind === 'all' || i.kind === kind) && (!cat || i.category === cat) && (!q || (i.name + ' ' + i.category).toLowerCase().includes(q)));
    const card = (i) => {
      let art = '';
      if (i.kind === 'decoration') art = `<div class="sh-av"><img class="a" src="${esc(me.avatar)}" alt=""><img class="d" loading="lazy" src="${esc(cdn(i.thumb))}" alt="" onerror="this.remove()"></div>`;
      else if (i.kind === 'nameplate') art = `<div class="sh-np" style="background-color:${esc(/^#[0-9a-f]{3,8}$/i.test(i.color) ? i.color : '#2b2d31')};${cdn(i.thumb) ? `background-image:url('${esc(cdn(i.thumb))}')` : ''}"><img src="${esc(me.avatar)}" alt=""><span>${esc(me.name)}</span></div>`;
      else art = `<div class="sh-fx">${cdn(i.thumb) ? `<img loading="lazy" src="${esc(cdn(i.thumb))}" alt="" onerror="this.remove()">` : '<span class="muted small">effect</span>'}</div>`;
      return `<button type="button" class="shop-item ${picked[i.kind] && keyOf(picked[i.kind]) === keyOf(i) ? 'picked' : ''}" data-k="${esc(keyOf(i))}"><span class="kind">${esc(KIND[i.kind])}</span>${art}<b>${esc(i.name)}</b><small>${esc(i.price || i.category)}</small></button>`;
    };
    const drawGrid = () => {
      const list = filtered();
      $('#shopGrid').innerHTML = list.length ? list.slice(0, shown).map(card).join('') : '<div class="empty-state">Nothing matches.</div>';
      $('#shopMore').hidden = list.length <= shown;
      $('#shopMore').textContent = `Show more (${list.length - shown} left)`;
      $$('.shop-item', $('#shopGrid')).forEach((b) => b.addEventListener('click', () => choose(byKey[b.dataset.k])));
    };
    const drawCounts = (counts) => {
      $('#cAll').textContent = items.length; $('#cDecoration').textContent = counts.decoration || 0;
      $('#cNameplate').textContent = counts.nameplate || 0; $('#cEffect').textContent = counts.effect || 0;
    };

    // ----- preview
    const video = $('#pvVideo'), row = $('#pvRow');
    video.addEventListener('error', () => { video.hidden = true; const n = picked.nameplate; if (n && cdn(n.thumb)) row.style.backgroundImage = `url('${cdn(n.thumb)}')`; });
    const drawPreview = () => {
      $('#pvAvatar').src = me.avatar; $('#pvRowAv').src = me.avatar;
      $('#pvName').textContent = me.name; $('#pvUser').textContent = me.username ? '@' + me.username : ''; $('#pvRowName').textContent = me.name;
      const d = picked.decoration, n = picked.nameplate, f = picked.effect;
      const deco = $('#pvDeco'); deco.hidden = !(d && cdn(d.preview)); if (!deco.hidden) deco.src = cdn(d.preview);
      $('#pvFx').innerHTML = f ? ((f.layers || []).filter((l) => cdn(l.src)).sort((a, b) => (a.z || 0) - (b.z || 0)).map((l) => `<img src="${esc(cdn(l.src))}" alt="">`).join('') || (cdn(f.thumb) ? `<img src="${esc(cdn(f.thumb))}" alt="">` : '')) : '';
      row.style.backgroundImage = ''; row.style.backgroundColor = n && /^#[0-9a-f]{3,8}$/i.test(n.color) ? n.color : '';
      if (n && cdn(n.video)) { video.hidden = false; video.src = cdn(n.video); video.play().catch(() => {}); }
      else { video.hidden = true; video.removeAttribute('src'); if (n && cdn(n.thumb)) row.style.backgroundImage = `url('${cdn(n.thumb)}')`; }
      const chosen = [d, n, f].filter(Boolean);
      $('#pvSlots').innerHTML = chosen.map((i) => `<span class="chip lime">${esc(KIND[i.kind])}: ${esc(i.name)}<button type="button" data-drop="${esc(i.kind)}" title="Remove">${ico('x', 12)}</button></span>`).join('');
      $$('[data-drop]', $('#pvSlots')).forEach((b) => b.addEventListener('click', () => { picked[b.dataset.drop] = null; drawPreview(); drawGrid(); }));
      $('#pvInfo').innerHTML = chosen.length ? chosen.map((i) => `<div><b>${esc(i.name)}</b> <span class="muted">· ${esc(KIND[i.kind])} · ${esc(i.category)}</span>${i.price ? ` <span class="chip">${esc(i.price)}</span>` : ''}
        ${i.summary ? `<p class="muted small">${esc(i.summary)}</p>` : ''}
        <div class="btns">${i.url && i.url.startsWith('https://discord.com/shop') ? `<a class="btn btn-sm" href="${esc(i.url)}" target="_blank" rel="noopener">${ico('arrow', 14)} Open in Discord shop</a>` : ''}<button class="btn btn-sm" type="button" data-copysku="${esc(i.sku_id)}">${ico('copy', 14)} Copy SKU</button></div></div>`).join('')
        : '<p class="muted small">Pick any decoration, nameplate or profile effect on the left to try it on. You can combine one of each. Previews are approximate - Discord renders the final look.</p>';
      $$('[data-copysku]', $('#pvInfo')).forEach((b) => b.addEventListener('click', () => copyText(b.dataset.copysku, 'SKU copied')));
    };
    const choose = (i) => {
      if (!i) return;
      picked[i.kind] = picked[i.kind] && keyOf(picked[i.kind]) === keyOf(i) ? null : i;
      drawPreview(); drawGrid();
    };

    // ----- filters
    $$('#shopKinds button').forEach((b) => b.addEventListener('click', () => { kind = b.dataset.kind; shown = STEP; $$('#shopKinds button').forEach((x) => x.classList.toggle('active', x === b)); drawGrid(); }));
    $('#shopCat').addEventListener('change', (e) => { cat = e.target.value; shown = STEP; drawGrid(); });
    let t; $('#shopSearch').addEventListener('input', (e) => { clearTimeout(t); t = setTimeout(() => { q = e.target.value.trim().toLowerCase(); shown = STEP; drawGrid(); }, 200); });
    $('#shopMore').addEventListener('click', () => { shown += STEP; drawGrid(); });
    $('#pvClear').addEventListener('click', () => { picked.decoration = picked.nameplate = picked.effect = null; drawPreview(); drawGrid(); });

    const load = async (refresh) => {
      $('#shopGrid').innerHTML = '<div class="empty-state">Loading the shop…</div>';
      try {
        const d = await act('shop_catalog', refresh ? { refresh: true } : {});
        items = d.items || []; items.forEach((i) => { byKey[keyOf(i)] = i; });
        $('#shopCat').innerHTML = '<option value="">All collections</option>' + (d.categories || []).map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
        drawCounts(d.counts || {}); drawGrid();
        if (!items.length) $('#shopGrid').innerHTML = '<div class="empty-state">The shop returned nothing for this account.</div>';
      } catch (e) { $('#shopGrid').innerHTML = `<div class="empty-state">${esc(e.message)}</div>`; }
    };
    $('#shopRefresh').addEventListener('click', (e) => busy(e.currentTarget, () => load(true)));
    drawPreview(); load(false);
  }

  // ================================================================ developer portal
  function initDevPortal() {
    const appsEl = $('#dpAppList');
    if (!appsEl) return;
    const teamsEl = $('#dpTeamList');
    let apps = [], teams = [];
    const timers = {};                                  // app id -> auto-hide timer for a revealed token
    const P = 'https://discord.com/developers';
    const initial = (n) => esc((n || '?').slice(0, 1).toUpperCase());
    const icon = (url, name) => `<span class="dp-ico">${initial(name)}${cdn(url) ? `<img src="${esc(cdn(url))}" alt="" loading="lazy" onerror="this.remove()">` : ''}</span>`;
    const kv = (label, html) => `<span>${esc(label)}</span><div>${html}</div>`;
    const copyField = (value) => `<div class="copy-field"><code>${esc(value)}</code><button class="icon-btn sm" type="button" data-copy="${esc(value)}" title="Copy">${ico('copy', 14)}</button></div>`;

    const appCard = (a) => {
      if (!idOk(a.id)) return '';
      const chips = [a.team_name ? `<span class="chip sky">${ico('friends', 12)} ${esc(a.team_name)}</span>` : '<span class="chip">Personal</span>',
        `<span class="chip ${a.public ? 'lime' : ''}">${a.public ? 'Public bot' : 'Private bot'}</span>`,
        a.code_grant ? '<span class="chip">Code grant</span>' : '', ...(a.intents || []).map((i) => `<span class="chip sky">${esc(i)}</span>`)].join('');
      const rows = [kv('App ID', copyField(a.id)), a.public_key ? kv('Public key', copyField(a.public_key)) : '',
        a.bot ? kv('Bot user', `${esc(a.bot.name || '')} ${idOk(a.bot.id) ? copyField(a.bot.id) : ''}`) : '',
        kv('Created', esc(fmtTime(a.created))), a.guilds != null ? kv('Servers', esc(Number(a.guilds).toLocaleString())) : '',
        a.interactions_url ? kv('Interactions', `<span class="mono small">${esc(a.interactions_url)}</span>`) : ''].join('');
      return `<article class="dp-card" data-app="${esc(a.id)}">
        <div class="dp-head">${icon(a.icon, a.name)}<div class="grow"><b>${esc(a.name)}</b><small>${esc(a.team_name || 'Personal application')}</small></div></div>
        <div class="chips" style="margin:0">${chips}</div>
        ${a.description ? `<p class="dp-desc">${esc(a.description)}</p>` : ''}
        <div class="dp-kv">${rows}</div>
        <div class="dp-links">
          <a class="btn btn-sm" href="${P}/applications/${a.id}/information" target="_blank" rel="noopener">${ico('arrow', 13)} Portal</a>
          <a class="btn btn-sm" href="${P}/applications/${a.id}/bot" target="_blank" rel="noopener">Bot</a>
          <a class="btn btn-sm" href="${P}/applications/${a.id}/oauth2" target="_blank" rel="noopener">OAuth2</a>
          <a class="btn btn-sm" href="https://discord.com/oauth2/authorize?client_id=${a.id}&scope=bot%20applications.commands&permissions=0" target="_blank" rel="noopener">Invite link</a>
          ${a.team_id && idOk(a.team_id) ? `<a class="btn btn-sm" href="${P}/teams/${a.team_id}/information" target="_blank" rel="noopener">Team</a>` : ''}
        </div>
        <div class="dp-token"><div class="dp-reveal"><button class="btn btn-sm btn-danger" type="button" data-reveal="${esc(a.id)}">${ico('key', 14)} Reveal token</button><input class="input code" inputmode="numeric" maxlength="8" placeholder="2FA code (if asked)" autocomplete="off"></div><div class="tok-out"></div></div>
      </article>`;
    };
    const teamCard = (t) => {
      if (!idOk(t.id)) return '';
      const members = (t.members || []).map((m) => `<div>${ico('user', 13)} ${esc(m.name)}${m.owner ? ' <span class="chip lime">owner</span>' : ''}${m.pending ? ' <span class="chip">invited</span>' : ''}<small>${esc(m.role)}</small></div>`).join('');
      const tApps = (t.apps || []).map((id) => apps.find((a) => a.id === id)).filter(Boolean);
      return `<article class="dp-card">
        <div class="dp-head">${icon(t.icon, t.name)}<div class="grow"><b>${esc(t.name)}</b><small>${tApps.length} application${tApps.length === 1 ? '' : 's'} · ${(t.members || []).length} member${(t.members || []).length === 1 ? '' : 's'}</small></div></div>
        <div class="dp-kv">${kv('Team ID', copyField(t.id))}${t.owner_id && idOk(t.owner_id) ? kv('Owner ID', copyField(t.owner_id)) : ''}</div>
        ${members ? `<div class="dp-members">${members}</div>` : ''}
        ${tApps.length ? `<div class="chips" style="margin:0">${tApps.map((a) => `<span class="chip">${esc(a.name)}</span>`).join('')}</div>` : ''}
        <div class="dp-links"><a class="btn btn-sm" href="${P}/teams/${t.id}/information" target="_blank" rel="noopener">${ico('arrow', 13)} Open team</a>
        <a class="btn btn-sm" href="${P}/teams/${t.id}/members" target="_blank" rel="noopener">Members</a></div>
      </article>`;
    };

    const draw = () => {
      const q = $('#dpSearch').value.trim().toLowerCase(), f = $('#dpTeamFilter').value;
      const list = apps.filter((a) => (!q || (a.name + ' ' + a.id + ' ' + (a.team_name || '')).toLowerCase().includes(q)) && (!f || (f === '__personal' ? !a.team_id : a.team_id === f)));
      Object.keys(timers).forEach((k) => { clearTimeout(timers[k]); delete timers[k]; });  // a redraw drops any revealed token
      appsEl.innerHTML = list.length ? list.map(appCard).join('') : `<div class="empty-state">${apps.length ? 'No application matches.' : 'This account has no applications.'}</div>`;
      teamsEl.innerHTML = teams.length ? teams.map(teamCard).join('') : '<div class="empty-state">This account is not in any team.</div>';
    };
    const load = async () => {
      appsEl.innerHTML = '<div class="empty-state">Loading applications…</div>';
      try {
        const d = await act('dev_overview');
        apps = d.apps || []; teams = d.teams || [];
        $('#dpApps').textContent = apps.length; $('#dpTeams').textContent = teams.length; $('#dpBots').textContent = apps.filter((a) => a.bot).length;
        $('#dpTeamFilter').innerHTML = '<option value="">All applications</option><option value="__personal">Personal only</option>' + teams.filter((t) => idOk(t.id)).map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('');
        draw();
      } catch (e) { appsEl.innerHTML = `<div class="empty-state">${esc(e.message)}</div>`; teamsEl.innerHTML = ''; }
    };

    const hideToken = (card) => { const id = card.dataset.app; clearTimeout(timers[id]); delete timers[id]; $('.tok-out', card).innerHTML = ''; };
    const showToken = (card, token) => {
      const out = $('.tok-out', card), id = card.dataset.app;
      out.innerHTML = `<div class="two"><input class="input tok" type="password" readonly autocomplete="off" value="${esc(token)}"><div class="btns"><button class="icon-btn sm" type="button" data-tokshow title="Show / hide">${ico('eye', 14)}</button><button class="icon-btn sm" type="button" data-tokcopy title="Copy">${ico('copy', 14)}</button><button class="icon-btn sm danger" type="button" data-tokhide title="Hide now">${ico('x', 14)}</button></div></div><small class="muted">New token - shown once, hides itself in 90 s.</small>`;
      clearTimeout(timers[id]); timers[id] = setTimeout(() => hideToken(card), 90000);
    };

    appsEl.addEventListener('click', async (e) => {
      const copy = e.target.closest('[data-copy]');
      if (copy) return copyText(copy.dataset.copy);
      const card = e.target.closest('.dp-card'); if (!card) return;
      if (e.target.closest('[data-tokshow]')) { const i = $('.tok', card); i.type = i.type === 'password' ? 'text' : 'password'; return; }
      if (e.target.closest('[data-tokcopy]')) return copyText($('.tok', card).value, 'Token copied');
      if (e.target.closest('[data-tokhide]')) return hideToken(card);
      const rev = e.target.closest('[data-reveal]');
      if (!rev) return;
      const app = apps.find((a) => a.id === rev.dataset.reveal); if (!app) return;
      if (!confirm(`Reveal the bot token of "${app.name}"?\n\nDiscord only shows a token right after a reset, so this RESETS it. Anything using the old token will be logged out.`)) return;
      await busy(rev, async () => {
        const r = await act('dev_token', { app_id: app.id, confirm: true, code: $('.code', card).value.trim() || undefined });
        $('.code', card).value = '';
        showToken(card, r.token);
        toast('Token reset - copy it now', 'ok', app.name);
      });
    });
    teamsEl.addEventListener('click', (e) => { const c = e.target.closest('[data-copy]'); if (c) copyText(c.dataset.copy); });
    let t; $('#dpSearch').addEventListener('input', () => { clearTimeout(t); t = setTimeout(draw, 200); });
    $('#dpTeamFilter').addEventListener('change', draw);
    $('#dpRefresh').addEventListener('click', (e) => busy(e.currentTarget, load));
    load();
  }

  // ================================================================ overview: HypeSquad badge
  function initOverviewExtras() {
    const form = $('#hsForm');
    if (!form) return;
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const house = $('#hsHouse').value;
      if (house === 'leave' && !confirm('Remove the HypeSquad badge from this account?')) return;
      await busy($('button', form), async () => {
        const r = await act('hypesquad', { house });
        toast(r.text || 'Done', 'ok', r.title);
        try { await act('refresh_snapshot'); } catch (err) { /* badge list refreshes next time */ }
        setTimeout(() => location.reload(), 900);
      });
    });
  }

  // ================================================================ commands -> moderation: channel tools
  function initChanTools() {
    const box = $('#chanTools');
    if (!box) return;
    const g = $('#ctGuild'), chan = $('#ctChan'), cat = $('#ctCat'), out = $('#ctOut');
    const ICON = { text: '#', voice: '🔊', stage: '🎙', forum: '💬', category: '📁' };
    const say = (title, text) => { out.hidden = false; out.className = 'out'; out.innerHTML = `<div class="res-title">${esc(title)}</div><div class="res-text">${esc(text || '')}</div>`; };
    const load = async () => {
      chan.innerHTML = '<option value="">Choose channel…</option>'; cat.innerHTML = '<option value="">No category</option>';
      if (!g.value) return;
      const d = await act('chan_list', { guild_id: g.value });
      chan.innerHTML += d.channels.filter((c) => c.kind !== 'category').map((c) => `<option value="${esc(c.id)}">${c.locked ? '🔒 ' : ''}${ICON[c.kind] || '#'} ${esc(c.name)}${c.category ? ' - ' + esc(c.category) : ''}</option>`).join('');
      cat.innerHTML += d.channels.filter((c) => c.kind === 'category').map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
    };
    g.addEventListener('change', () => load().catch(fail));
    box.addEventListener('click', (e) => {
      const b = e.target.closest('[data-ct]');
      if (!b) return;
      const reason = $('#ctReason').value.trim(), base = { guild_id: g.value, reason: reason || undefined };
      const jobs = {
        lock: ['chan_lock', { ...base, channel_id: chan.value }], unlock: ['chan_unlock', { ...base, channel_id: chan.value }],
        rename: ['chan_rename', { ...base, channel_id: chan.value, name: $('#ctRename').value }],
        create: ['chan_create', { ...base, name: $('#ctNew').value, kind: $('#ctKind').value, category_id: cat.value || undefined }],
      };
      const [action, payload] = jobs[b.dataset.ct];
      if (!g.value) return toast('Choose a server first', 'err');
      busy(b, async () => {
        const r = await act(action, payload);
        say(r.title, (r.fields || []).map((f) => f.join(': ')).join(' · '));
        await load();
      });
    });
  }

  // ================================================================ mobile navigation drawer
  function initDrawer() {
    const btn = $('#menuBtn'), back = $('#navBackdrop'), side = $('#sidebar'), close = $('#menuClose');
    if (!btn || !side) return;
    const mq = window.matchMedia('(max-width: 860px)');
    const isOpen = () => document.body.classList.contains('nav-open');
    const set = (open) => {
      const on = !!open && mq.matches;
      document.body.classList.toggle('nav-open', on);
      btn.setAttribute('aria-expanded', String(on));
      if (on) side.scrollTop = 0;
    };
    btn.addEventListener('click', () => set(!isOpen()));
    back.addEventListener('click', () => set(false));
    close.addEventListener('click', () => set(false));
    side.addEventListener('click', (e) => { if (e.target.closest('a')) set(false); });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') set(false); });
    mq.addEventListener('change', (e) => { if (!e.matches) set(false); });
    // swipe: from the left edge to open, swipe left anywhere on the drawer / backdrop to close
    let sx = 0, sy = 0, tracking = false;
    document.addEventListener('touchstart', (e) => {
      const t = e.touches[0]; sx = t.clientX; sy = t.clientY;
      tracking = mq.matches && (isOpen() || sx <= 28);
    }, { passive: true });
    document.addEventListener('touchend', (e) => {
      if (!tracking) return;
      tracking = false;
      const t = e.changedTouches[0], dx = t.clientX - sx, dy = t.clientY - sy;
      if (Math.abs(dx) < 70 || Math.abs(dy) > Math.abs(dx) * 0.6) return;
      if (!isOpen() && dx > 0) set(true);
      else if (isOpen() && dx < 0) set(false);
    }, { passive: true });
  }

  initDrawer();
  ({ commands: initChanTools, overview: initOverviewExtras, snipers: initVanity, shop: initShop, devportal: initDevPortal }[page] || (() => {}))();
})();
