const panel = document.querySelector('#progress-panel');
if (panel) {
  async function poll() {
    try {
      const response = await fetch(`/api/runs/${panel.dataset.run}`);
      if (!response.ok) throw new Error('進捗の取得に失敗しました。再試行します。');
      const run = await response.json(), m = run.metrics;
      if (['completed','failed'].includes(run.status)) { location.reload(); return; }
      document.querySelector('#status').textContent = run.status === 'running' ? '実行中' : '待機中';
      document.querySelector('#current').textContent = run.current;
      document.querySelector('#completed').textContent = m.completed;
      document.querySelector('#cached').textContent = m.cached;
      document.querySelector('#cost').textContent = '$' + m.cost_usd.toFixed(6);
      document.querySelector('#refusals').textContent = m.refusals;
      document.querySelector('#stage-count').textContent = `${run.stage_done}/${run.stage_total}`;
      document.querySelector('#progress').max = run.stage_total || 1;
      document.querySelector('#progress').value = run.stage_done;
      document.querySelector('#poll-error').textContent = '';
    } catch (error) { document.querySelector('#poll-error').textContent = error.message; }
    setTimeout(poll, 1500);
  }
  setTimeout(poll, 1000);
}
const data = JSON.parse(document.querySelector('#map-data').textContent);
const svg = document.querySelector('#truth-map'), slider = document.querySelector('#stage-slider');
const people = data.members.filter(p => p.truth);
const groups = [...new Set(people.map(p => p.truth.group))].sort();
const colors = ['#43856f','#bd6d58','#7b78a8','#c59e3e','#5b8fa9','#a76185'];
function node(tag, attrs, text) {
  const n = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key,value] of Object.entries(attrs)) n.setAttribute(key, value);
  if (text !== undefined) n.textContent = text;
  return n;
}
function draw() {
  svg.replaceChildren();
  const index = Number(slider.value), stage = data.stages[index-1];
  document.querySelector('#map-label').textContent = stage ? stage.label : '全体';
  if (!people.length) { svg.style.display = 'none'; document.querySelector('#map-empty').hidden = false; return; }
  const selected = new Set(stage ? stage.selected : []);
  const bound = Math.max(1, ...people.flatMap(p => [Math.abs(p.truth.x),Math.abs(p.truth.y)]))*1.15;
  const x = v => 350+v/bound*265, y = v => 215-v/bound*155;
  svg.append(node('rect',{x:75,y:55,width:550,height:320,fill:'#f7f9f3',rx:12}));
  svg.append(node('line',{x1:75,y1:215,x2:625,y2:215,stroke:'#ccd6c8'}));
  svg.append(node('line',{x1:350,y1:55,x2:350,y2:375,stroke:'#ccd6c8'}));
  for (const [px,py,text] of [[350,30,'気軽さ重視'],[350,410,'講評重視'],[135,445,'AI作品を禁止'],[565,445,'AI作品を容認']])
    svg.append(node('text',{x:px,y:py,'text-anchor':'middle',fill:'#6b796e','font-size':13},text));
  for (const p of people) {
    const px=x(p.truth.x), py=y(p.truth.y), g=node('g',{});
    if (data.centers.includes(p.user_id)) g.append(node('rect',{x:px-12,y:py-12,width:24,height:24,fill:'none',stroke:'#364e3d','stroke-dasharray':'3 2',rx:2}));
    if (p.user_id === data.winner) g.append(node('circle',{cx:px,cy:py,r:16,fill:'none',stroke:'#d6a531','stroke-width':3}));
    g.append(node('circle',{cx:px,cy:py,r:7,fill:colors[groups.indexOf(p.truth.group)%colors.length],stroke:selected.has(p.user_id)?'#183d33':'white','stroke-width':selected.has(p.user_id)?3:1,opacity:(stage && !selected.has(p.user_id) && p.user_id !== data.winner) ? 0.45 : 1}));
    g.append(node('title',{},`${p.user_id} · ${p.truth.group} (${p.truth.x}, ${p.truth.y})`));
    if (selected.has(p.user_id) || p.user_id === data.winner) g.append(node('text',{x:px+10,y:py-9,'font-size':11,fill:'#243e31'},p.user_id));
    svg.append(g);
  }
}
groups.forEach((group,i) => {const item=document.createElement('span');item.textContent='● '+group;item.style.color=colors[i%colors.length];document.querySelector('#map-legend').append(item);});
slider.addEventListener('input', draw); draw();
