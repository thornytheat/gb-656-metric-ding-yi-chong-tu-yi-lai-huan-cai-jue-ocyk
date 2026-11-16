/* 表单与请求：动作集中在 ACTIONS 里，失败必须写到页面上 */
const ACTIONS = {
  async import() {
    return request('/api/schemas/import', {
      files: ['schema_v2026_06.sql', 'schema_v2026_09.sql'],
    });
  },

  async run(button) {
    const body = {};
    if (button.dataset.forceStale) {
      body.base_version_seq = 0;
    }
    const run = await request('/api/runs', body);
    return run.reused
      ? `命中已有批次 ${run.run_id}`
      : `新批次 ${run.run_id} 序号 ${run.version_seq}`;
  },

  async 'compare-types'() {
    const oldValue = document.getElementById('old-type');
    const newValue = document.getElementById('new-type');
    const result = await request('/api/types/compare', {
      old: oldValue ? oldValue.value : '',
      new: newValue ? newValue.value : '',
    });
    const target = document.querySelector('[data-compare-result]');
    if (target) {
      const detail = result.detail ? `/${result.detail}` : '';
      target.textContent = `${result.old.norm} → ${result.new.norm}　${result.change}${detail}　${result.breaking ? '破坏' : '兼容'}`;
    }
    return `比较完成 ${result.change}`;
  },

  async 'dry-run'(button) {
    const result = await request('/api/rulings/dry-run', {
      sample: true,
      revision_id: button.dataset.revision || '样例修订',
    });
    const target = document.querySelector('[data-dry-result]');
    if (target) {
      target.innerHTML = renderDryRun(result);
    }
    window.__lastDryRun = result.dry_run_id;
    return `演练 ${result.dry_run_id}：受影响 ${result.affected_count}，冲突 ${result.conflicts.after.length}，环 ${result.cycles.after.cycles.length + result.cycles.after.self_loops.length}`;
  },

  async publish() {
    if (!window.__lastDryRun) {
      throw new Error('先演练再发布');
    }
    const result = await request('/api/rulings/publish', { dry_run_id: window.__lastDryRun });
    setTimeout(() => window.location.reload(), 600);
    return result.reused
      ? `命中已有裁决 v${result.ruling_seq}`
      : `已发布裁决 v${result.ruling_seq}`;
  },
};

function escapeHTML(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function renderDryRun(result) {
  const affected = (result.affected || []).map((item) =>
    `<tr><td>${escapeHTML(item.name)}</td><td>${escapeHTML(item.version)}</td>`
    + `<td>${escapeHTML(item.changes.join('、'))}</td>`
    + `<td>${escapeHTML(item.before.canonical_name || '—')} → ${escapeHTML(item.after.canonical_name || '—')}</td>`
    + `<td>${escapeHTML(item.basis.join('；') || '—')}</td></tr>`).join('');
  const conflicts = (result.conflicts.after || []).map((item) =>
    `<tr><td>${escapeHTML(item.name)}</td><td>${escapeHTML(item.version)}</td>`
    + `<td>${escapeHTML(item.reason)}</td><td>${escapeHTML(item.basis)}</td></tr>`).join('');
  const cycles = [...(result.cycles.after.cycles || []), ...(result.cycles.after.self_loops || [])]
    .map((item) => `<tr><td>${escapeHTML(item.basis)}</td></tr>`).join('');
  const pending = (result.pending || []).map((item) =>
    `<tr><td>${escapeHTML(item.alias)}</td><td>${escapeHTML(item.detail)}</td></tr>`).join('');
  return `
    <p class="lede">演练 <span class="mono">${escapeHTML(result.dry_run_id)}</span>　受影响 <span class="mono">${result.affected_count}</span></p>
    <h3>受影响指标</h3>
    <table><thead><tr><th>指标</th><th>版本</th><th>变化</th><th>正名</th><th>依据</th></tr></thead>
    <tbody>${affected || '<tr><td colspan="5">没有受影响指标。</td></tr>'}</tbody></table>
    <h3>同名冲突</h3>
    <table><thead><tr><th>指标</th><th>版本</th><th>原因</th><th>依据</th></tr></thead>
    <tbody>${conflicts || '<tr><td colspan="4">没有冲突。</td></tr>'}</tbody></table>
    <h3>依赖环</h3>
    <table><thead><tr><th>路径</th></tr></thead>
    <tbody>${cycles || '<tr><td>没有环。</td></tr>'}</tbody></table>
    <h3>歧义待处理</h3>
    <table><thead><tr><th>别名</th><th>说明</th></tr></thead>
    <tbody>${pending || '<tr><td colspan="2">没有待处理映射。</td></tr>'}</tbody></table>
    <div class="actions"><button type="button" data-action="publish">发布新裁决版本</button></div>`;
}

async function request(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch (error) {
    payload = null;
  }
  if (!response.ok) {
    const message = (payload && (payload.message || payload.error)) || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

function flash(target, message, bad) {
  if (!target) return;
  target.textContent = message;
  target.classList.toggle('is-bad', Boolean(bad));
  target.classList.toggle('is-ok', !bad);
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const name = button.dataset.action;
  const handler = ACTIONS[name];
  if (!handler) return;
  const target = document.querySelector('[data-flash]');
  button.disabled = true;
  flash(target, '…', false);
  try {
    const message = await handler(button);
    flash(target, message || '完成', false);
    if (name === 'run') {
      setTimeout(() => window.location.reload(), 400);
    }
  } catch (error) {
    flash(target, error.message, true);
  } finally {
    button.disabled = false;
  }
});
