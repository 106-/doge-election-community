const form = document.querySelector('#config-form');
const manual = document.querySelector('#manual');
const fields = ['l', 'e', 'k'].map(name => form.elements[name]);
let revision = 0;
async function preview() {
  const version = ++revision;
  fields.forEach(field => field.disabled = !manual.checked);
  const params = new URLSearchParams();
  if (manual.checked) fields.forEach(field => params.set(field.name, field.value));
  const panel = document.querySelector('#preview'), button = document.querySelector('#start');
  button.disabled = true;
  try {
    const response = await fetch(`/api/config/${form.dataset.dataset}?${params}`);
    const data = await response.json();
    if (version !== revision) return;
    if (!response.ok) throw new Error(data.error || '人数設定を確認してください');
    const c = data.config, e = data.estimate;
    panel.replaceChildren();
    for (const text of [
      `くじ ${c.l}人 → 選出 ${c.e}人 × ${c.K}回 → ドージェ`,
      `選出 ${c.m}/${c.l}票 · 最終 ${c.final}/${c.e}票`,
      `基準とのずれ ${c.deviation.toFixed(4)}`,
      ...(c.warning ? [c.warning_text] : []),
      `判定の上限目安：${e.pairs}件 · $${e.usd.toFixed(4)}`]) {
      const p = document.createElement('p'); p.textContent = text;
      if (text === c.warning_text) p.className = 'warning';
      panel.append(p);
    }
    button.disabled = false;
  } catch (error) {
    if (version !== revision) return;
    panel.textContent = error.message;
  }
}
manual.addEventListener('change', preview);
fields.forEach(field => field.addEventListener('input', preview));
preview();
