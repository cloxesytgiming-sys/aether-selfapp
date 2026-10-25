/* Music page: search (Spotify / YouTube) and play through Lavalink in a voice channel with any linked account.
 */
(() => {
  'use strict';
  const AE = window.AE;
  if (!AE || document.body.dataset.page !== 'music') return;
  const { $, api, post, toast, busy, esc, sleep } = AE;
  const M = window.MUSIC || { accounts: [], current: null };
  const fmt = (ms) => { const s = Math.max(0, Math.floor((ms || 0) / 1000)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
  const art = (u) => `<span class="m-art">${/^https?:\/\//.test(u || '') ? `<img src="${esc(u)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : '♪'}</span>`;
  let results = [];

  // ---- run an action on a specific linked account (the page can drive any account, not only the selected one)
  async function actFor(accountId, action, payload = {}) {
    let r = await post('/api/act', { action, payload, account_id: accountId });
    for (let i = 0; r.pending && i < 100; i++) { await sleep(1000); r = await api('/api/jobs/' + r.job_id); }
    if (r.pending) throw new Error('Timed out waiting for the account');
    if (r.error) throw new Error(r.error);
    return r.result;
  }

  // ================================================================ status banner
  async function loadStatus() {
    const b = $('#mBanner');
    try {
      const s = await api('/api/music/status');
      const problems = [];
      if (!s.spotify) problems.push('Spotify is not set up (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET)');
      if (!s.lavalink.configured) problems.push('Lavalink is not set up (LAVALINK_HOST / LAVALINK_PORT / LAVALINK_PASSWORD)');
      else if (!s.lavalink.online) problems.push(`Lavalink is not connected - ${s.lavalink.error ? ' - ' + s.lavalink.error : ''}`);
      b.hidden = !problems.length;
      if (problems.length) b.innerHTML = `<div><b>Heads up:</b> ${problems.map(esc).join('. ')}. Add them to <span class="mono">lavalink connection</span> and retry.</div>`;
      if (s.lavalink.online && s.lavalink.version && parseFloat(s.lavalink.version.split('.').slice(0, 2).join('.')) < 4.2) {
        b.hidden = false; b.innerHTML = `<div><b>Lavalink ${esc(s.lavalink.version)} is too old.</b> Discord now requires DAVE encryption for voice - update Lavalink to v4.2.0 or newer.</div>`;
      }
    } catch (e) { /* the page still works */ }
  }

  // ================================================================ search + results
  const slim = (it) => ({ title: it.title, artist: it.artist, art: it.art, duration_ms: it.duration_ms, source: it.source,
    uri: it.source === 'spotify' ? '' : it.uri, query: it.source === 'spotify' ? it.query : '' });
  const row = (it, i) => `<div class="m-row" data-i="${i}">${art(it.art)}<div class="grow"><b>${esc(it.title)}</b><small>${esc(it.artist || '')}${it.album ? ' · ' + esc(it.album) : ''} <span class="chip">${esc(it.source)}</span></small></div>
    <span class="dur">${it.live ? 'LIVE' : fmt(it.duration_ms)}</span>
    <div class="btns"><button class="btn btn-sm btn-primary" data-a="vc">▶ Play</button><button class="btn btn-sm" data-a="vcq">+ Queue</button></div></div>`;

  $('#mSearch').addEventListener('submit', async (e) => {
    e.preventDefault();
    const box = $('#mResults'), col = $('#mCollection');
    await busy($('button', e.target), async () => {
      box.innerHTML = '<div class="empty-state">Searching…</div>'; col.hidden = true;
      try {
        const d = await api(`/api/music/search?q=${encodeURIComponent($('#mQ').value)}&source=${$('#mSource').value}`);
        results = d.items || [];
        box.innerHTML = (d.note ? `<div class="muted small" style="padding:4px 2px">${esc(d.note)}</div>` : '') + (results.length ? results.map(row).join('') : '<div class="empty-state">No results.</div>');
        if (d.collection && results.length) {
          col.hidden = false;
          col.innerHTML = `${art(d.collection.art)}<div class="grow"><b>${esc(d.collection.title || 'Collection')}</b><br><small class="muted">${esc(d.collection.kind)} · ${results.length} songs</small></div>
            <button class="btn btn-sm btn-primary" data-all="vc">▶ Play all</button><button class="btn btn-sm" data-all="vcq">+ Queue all</button>`;
        }
      } catch (err) { box.innerHTML = `<div class="empty-state">${esc(err.message)}</div>`; }
    });
  });
  $('#mResults').addEventListener('click', (e) => {
    const b = e.target.closest('[data-a]'); if (!b) return;
    const it = results[+b.closest('.m-row').dataset.i];
    busy(b, () => playVC([it], b.dataset.a === 'vc' ? 'now' : 'queue'));
  });
  $('#mCollection').addEventListener('click', (e) => {
    const b = e.target.closest('[data-all]'); if (b) busy(b, () => playVC(results, b.dataset.all === 'vc' ? 'now' : 'queue'));
  });

  // ================================================================ voice channel (Lavalink)
  const V = { guilds: [], current: null, state: null, t0: 0 };
  const accSel = $('#vcAcc'), gSel = $('#vcGuild'), cSel = $('#vcChan');
  const account = () => M.accounts.find((a) => String(a.id) === accSel.value);
  const online = () => { const a = account(); return a && a.state === 'online'; };
  accSel.innerHTML = M.accounts.map((a) => `<option value="${esc(a.id)}" ${a.state === 'online' ? '' : 'disabled'}>${esc(a.name)} (${esc(a.state)})</option>`).join('');
  const pref = M.accounts.find((a) => a.id === M.current && a.state === 'online') || M.accounts.find((a) => a.state === 'online');
  if (pref) accSel.value = String(pref.id);
  $('#vcPill').textContent = pref ? 'ready' : 'no account online';

  async function loadVoice() {
    gSel.innerHTML = '<option value="">Choose server…</option>'; cSel.innerHTML = '<option value="">Choose channel…</option>';
    if (!online()) return;
    try {
      const d = await actFor(accSel.value, 'music_voice');
      V.guilds = d.guilds || []; V.current = d.current;
      gSel.innerHTML += V.guilds.map((g) => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join('');
      if (V.current) useCurrent();
    } catch (e) { toast(e.message, 'err'); }
  }
  const fillChannels = () => {
    const g = V.guilds.find((x) => x.id === gSel.value);
    cSel.innerHTML = '<option value="">Choose channel…</option>' + (g ? g.channels.map((c) => `<option value="${esc(c.id)}">${c.stage ? '🎙 ' : '🔊 '}${esc(c.name)}${c.members ? ` (${c.members})` : ''}</option>`).join('') : '');
  };
  const useCurrent = () => { if (!V.current) return toast('This account is not in a voice channel right now', 'err'); gSel.value = V.current.guild_id; fillChannels(); cSel.value = V.current.channel_id; };
  gSel.addEventListener('change', fillChannels);
  accSel.addEventListener('change', () => { V.state = null; loadVoice(); poll(); });
  $('#vcHere').addEventListener('click', useCurrent);

  async function playVC(items, mode) {
    if (!online()) return toast('Pick an account that is online', 'err');
    if (!gSel.value || !cSel.value) return toast('Choose a server and a voice channel first', 'err');
    for (let i = 0; i < items.length; i += 10) {
      await actFor(accSel.value, 'music_play', { guild_id: gSel.value, channel_id: cSel.value, items: items.slice(i, i + 10).map(slim),
        mode: i === 0 ? mode : 'queue' });
    }
    toast(mode === 'now' ? 'Playing in voice' : 'Added to the voice queue');
    poll();
  }

  const pickPlayer = (st) => (st.players || []).find((p) => p.guild_id === gSel.value) || (st.players || [])[0];
  function render(st) {
    V.state = st; V.t0 = Date.now();
    const p = pickPlayer(st), now = $('#vcNow'), ctl = $('#vcControls');
    $('#vcPill').textContent = st.online ? 'lavalink online' : (st.configured ? 'lavalink offline' : 'not configured');
    if (!p) { now.innerHTML = '<div class="empty-state">Nothing playing in voice.</div>'; ctl.hidden = true; return; }
    ctl.hidden = false;
    const n = p.now;
    now.innerHTML = n ? `${art(n.art)}<div class="grow"><b>${p.paused ? '⏸ ' : ''}${esc(n.title)}</b><small class="muted">${esc(n.artist || 'Unknown artist')}</small>
      <div class="m-bar" id="vcBar"><i id="vcFill"></i></div><div class="m-times"><span id="vcPos">0:00</span><span>${fmt(n.duration_ms)}</span></div></div>
      <div class="vc-where">🔊 ${esc(p.guild_name)} / ${esc(p.channel_name)}</div>`
      : `<div class="empty-state">Connected to ${esc(p.channel_name)} - queue is empty.</div>`;
    $('#vcPause').textContent = p.paused ? '▶ Resume' : '⏸ Pause';
    $('#vcVol').value = p.volume; $('#vcVolText').textContent = p.volume; $('#vcLoop').value = p.loop;
    $('#vcQueue').innerHTML = (p.queue || []).map((q, i) => `<div class="m-q"><small>${i + 1}</small><span>${esc(q.title)} <small>${esc(q.artist || '')}</small></span><button class="icon-btn sm" data-qdel="${i}" title="Remove">✕</button></div>`).join('')
      + (p.queue_total > (p.queue || []).length ? `<small class="muted">+ ${p.queue_total - p.queue.length} more</small>` : '');
    tick();
  }
  function tick() {
    const p = V.state && pickPlayer(V.state); if (!p || !p.now || !$('#vcFill')) return;
    const pos = Math.min(p.now.duration_ms || 0, p.now.position_ms + (p.paused ? 0 : Date.now() - V.t0));
    $('#vcFill').style.width = (p.now.duration_ms ? (pos / p.now.duration_ms) * 100 : 0) + '%'; $('#vcPos').textContent = fmt(pos);
  }
  setInterval(tick, 500);
  let polling = false;
  async function poll() {
    if (polling || !online() || document.hidden) return;
    polling = true;
    try { render(await actFor(accSel.value, 'music_status')); } catch (e) { /* transient */ } finally { polling = false; }
  }
  setInterval(poll, 4500);

  const control = (action, extra = {}) => async (e) => {
    const p = V.state && pickPlayer(V.state); if (!p) return;
    await busy(e.currentTarget || e.target, async () => { await actFor(accSel.value, action, { guild_id: p.guild_id, ...extra }); await poll(); });
  };
  $('#vcPause').addEventListener('click', (e) => { const p = V.state && pickPlayer(V.state); return control(p && p.paused ? 'music_resume' : 'music_pause')(e); });
  $('#vcSkip').addEventListener('click', control('music_skip'));
  $('#vcStop').addEventListener('click', control('music_stop'));
  let vt; $('#vcVol').addEventListener('input', (e) => { $('#vcVolText').textContent = e.target.value; clearTimeout(vt); vt = setTimeout(() => control('music_volume', { volume: +e.target.value })(e), 300); });
  $('#vcLoop').addEventListener('change', (e) => control('music_loop', { mode: e.target.value })(e));
  $('#vcQueue').addEventListener('click', (e) => { const d = e.target.closest('[data-qdel]'); if (d) control('music_remove', { index: +d.dataset.qdel })({ currentTarget: d }); });
  $('#vcNow').addEventListener('click', (e) => {
    const bar = e.target.closest('#vcBar'), p = V.state && pickPlayer(V.state); if (!bar || !p || !p.now) return;
    const r = bar.getBoundingClientRect();
    control('music_seek', { position_ms: Math.floor(((e.clientX - r.left) / r.width) * p.now.duration_ms) })({ currentTarget: bar });
  });

  loadStatus(); loadVoice().then(poll);
})();
