/* Server cloner page: pick source + target on the selected account, choose what to copy, watch the progress. */
(() => {
  'use strict';
  const AE = window.AE;
  if (!AE || document.body.dataset.page !== 'cloner') return;
  const { $, $$, act, toast, busy, esc } = AE;
  let guilds = [], timer = null;
  const byId = (id) => guilds.find((g) => g.id === id);
  const STATE = { pending: 'waiting', running: 'working…', done: 'done', failed: 'problem', skipped: 'skipped' };
  const STATS = [['roles', 'Roles'], ['categories', 'Categories'], ['channels', 'Channels'], ['permissions', 'Permissions'], ['emojis', 'Emojis'], ['stickers', 'Stickers'],
    ['messages', 'Messages'], ['files', 'Files'], ['fallback', 'Stand-ins'], ['deleted', 'Deleted'], ['failed', 'Failed']];

  // ---------------------------------------------------------------- servers
  const chips = (g) => g ? [`${g.members || '?'} members`, `${g.roles} roles`, `${g.channels} channels`, `${g.emojis} emojis`, g.community ? 'Community' : ''].filter(Boolean).map((t) => `<span class="chip">${esc(t)}</span>`).join('') : '';
  const fill = () => {
    const opt = (g, forTarget) => `<option value="${esc(g.id)}" ${forTarget && !g.can_target ? 'disabled' : ''}>${esc(g.name)}${forTarget && !g.can_target ? ' (no permission)' : ''}${g.owner ? ' ★' : ''}</option>`;
    $('#clSource').innerHTML = '<option value="">Choose the server to copy…</option>' + guilds.map((g) => opt(g, false)).join('');
    $('#clTarget').innerHTML = '<option value="">Choose the server to paste into…</option>' + guilds.map((g) => opt(g, true)).join('');
  };
  const info = () => {
    $('#clSrcInfo').innerHTML = chips(byId($('#clSource').value));
    const t = byId($('#clTarget').value);
    $('#clTgtInfo').textContent = t ? `${t.name}: ${t.members || '?'} members, ${t.roles} roles, ${t.channels} channels${t.community ? ', Community enabled' : ''}.${t.community ? '' : ' Announcement / stage / forum channels need Community mode - they are created once it can be switched on, otherwise as text / voice.'}`
      : 'Only servers where this account has Manage Channels and Manage Roles can be a target.';
  };
  async function load() {
    try { guilds = (await act('clone_guilds')).guilds || []; fill(); info(); } catch (e) { $('#clSource').innerHTML = `<option value="">${esc(e.message)}</option>`; $('#clTarget').innerHTML = $('#clSource').innerHTML; }
  }
  $('#clSource').addEventListener('change', info);
  $('#clTarget').addEventListener('change', info);
  $('#clReload').addEventListener('click', (e) => busy(e.currentTarget, load));

  // ---------------------------------------------------------------- progress
  const dur = (s, f) => { const t = Math.max(0, Math.round((f || Date.now() / 1000) - s)); return t >= 60 ? `${Math.floor(t / 60)}m ${t % 60}s` : `${t}s`; };
  function render(st) {
    $('#clCancel').hidden = !st.running;
    $('#clStart').disabled = st.running;
    $('#clTitle').textContent = st.idle ? 'Idle' : st.running ? `Cloning ${st.source} → ${st.target}` : st.error ? `Stopped: ${st.error}` : `Finished in ${dur(st.started, st.finished)}`;
    if (st.idle) return;
    const steps = st.steps.filter((s) => s.state !== 'skipped');
    $('#clSteps').innerHTML = steps.map((s) => `<div class="cl-phase ${esc(s.state)}"><i></i><span>${esc(s.label)}</span><em>${esc(STATE[s.state] || s.state)}</em></div>`).join('');
    $('#clStats').innerHTML = STATS.filter(([k]) => ['roles', 'categories', 'channels', 'permissions', 'failed'].includes(k) || st.stats[k]).map(([k, label]) => `<div><b>${st.stats[k]}</b><small>${label}</small></div>`).join('');
    $('#clNotes').innerHTML = (st.notes || []).map((n) => `<div class="cl-note">• ${esc(n)}</div>`).join('');
  }
  async function poll() {
    clearTimeout(timer);
    try { const st = await act('clone_status'); render(st); if (st.running) timer = setTimeout(poll, 2500); }
    catch (e) { timer = setTimeout(poll, 6000); }
  }

  // ---------------------------------------------------------------- start / stop
  $('#clStart').addEventListener('click', (e) => busy(e.currentTarget, async () => {
    const src = byId($('#clSource').value), tgt = byId($('#clTarget').value);
    if (!src || !tgt) return toast('Choose a source and a target server', 'err');
    if (src.id === tgt.id) return toast('Source and target must be different', 'err');
    const options = { messages: $('#clMessages').value, message_limit: +$('#clLimit').value || 100 };
    $$('[data-o]').forEach((el) => { options[el.dataset.o] = el.checked; });
    const wipes = [options.delete_channels && 'all channels', options.delete_roles && 'all roles'].filter(Boolean);
    if (wipes.length) {
      const typed = prompt(`This will DELETE ${wipes.join(' and ')} in "${tgt.name}" before copying "${src.name}" into it.\n\nType the target server's name to confirm:`);
      if (typed === null) return;
      if (typed.trim() !== tgt.name) return toast('The name did not match - nothing was changed', 'err');
    } else if (!confirm(`Clone "${src.name}" into "${tgt.name}"?`)) return;
    await act('clone_start', { source_id: src.id, target_id: tgt.id, options, confirm: true });
    toast('Cloning started');
    poll();
  }));
  $('#clCancel').addEventListener('click', (e) => busy(e.currentTarget, async () => { await act('clone_cancel'); toast('Stopping after the current step'); poll(); }));

  load(); poll();
})();
