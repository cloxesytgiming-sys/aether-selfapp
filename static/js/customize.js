/* Customize page: live preview through /api/theme/css (the server is the single source of truth for the CSS). */
(() => {
  'use strict';
  const AE = window.AE;
  if (!AE || document.body.dataset.page !== 'customize') return;
  const { $, $$, api, post, toast, fail, busy } = AE;
  const state = { ...window.THEME };
  const style = $('#themeStyle');
  const inputs = $$('[data-k]');
  let timer;

  const sync = () => {                                   // state -> form
    inputs.forEach((el) => {
      const v = state[el.dataset.k];
      if (el.type === 'checkbox') el.checked = !!v; else el.value = v ?? '';
    });
    $$('[data-hex]').forEach((c) => { c.textContent = state[c.dataset.hex]; });
    $$('[data-out]').forEach((o) => { o.textContent = state[o.dataset.out]; });
    $('#czGradient').hidden = state.bg_mode !== 'gradient';
    $('#czImage').hidden = state.bg_mode !== 'image';
    $$('.cz-preset').forEach((b) => b.classList.toggle('active', b.dataset.preset === state.preset));
  };
  const preview = () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try { style.textContent = (await post('/api/theme/css', state)).css; } catch (e) { toast(e.message, 'err'); }
    }, 150);
  };
  const changed = (custom = true) => { if (custom) state.preset = 'custom'; sync(); preview(); };

  inputs.forEach((el) => {
    const handler = () => { state[el.dataset.k] = el.type === 'checkbox' ? el.checked : (el.type === 'range' ? Number(el.value) : el.value); changed(); };
    el.addEventListener(el.type === 'range' || el.type === 'color' ? 'input' : 'change', handler);
    if (el.tagName === 'TEXTAREA' || el.type === 'text') el.addEventListener('input', () => { state[el.dataset.k] = el.value; clearTimeout(el._t); el._t = setTimeout(() => changed(), 400); });
  });
  $$('.cz-preset').forEach((b) => b.addEventListener('click', () => {
    const keep = { brand_title: state.brand_title, brand_tag: state.brand_tag, custom_css: state.custom_css, bg_image: state.bg_image };
    Object.assign(state, window.THEME_PRESETS[b.dataset.preset], keep, { preset: b.dataset.preset });
    changed(false);
  }));
  $('#czUpload').addEventListener('click', (e) => busy(e.currentTarget, async () => {
    const file = $('#czFile').files[0];
    if (!file) return toast('Choose an image first', 'err');
    const fd = new FormData(); fd.append('file', file);
    const r = await fetch('/api/theme/upload', { method: 'POST', body: fd });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || 'Upload failed');
    state.bg_image = d.url; state.bg_mode = 'image'; changed();
    toast('Wallpaper uploaded - press Save to keep it');
  }));
  $('#czSave').addEventListener('click', (e) => busy(e.currentTarget, async () => {
    await post('/api/theme', state);
    toast('Theme saved'); setTimeout(() => location.reload(), 600);
  }));
  $('#czReset').addEventListener('click', (e) => busy(e.currentTarget, async () => {
    if (!confirm('Reset the dashboard to the default look?')) return;
    await post('/api/theme/reset', {}); location.reload();
  }));
  $('#czExport').addEventListener('click', () => {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(state, null, 2)], { type: 'application/json' }));
    a.download = 'aether-theme.json'; a.click(); URL.revokeObjectURL(a.href);
  });
  $('#czImportBtn').addEventListener('click', () => $('#czImport').click());
  $('#czImport').addEventListener('change', async (e) => {
    try {
      const data = JSON.parse(await e.target.files[0].text());
      Object.keys(window.THEME).forEach((k) => { if (k in data) state[k] = data[k]; });
      changed(); toast('Imported - check the preview, then Save');
    } catch (err) { toast('That is not a valid theme file', 'err'); }
    e.target.value = '';
  });
  sync();
})();
