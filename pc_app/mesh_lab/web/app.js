'use strict';
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const tokenKey = `mesh-token:${location.origin}`;
let token = new URLSearchParams(location.hash.slice(1)).get('token') || sessionStorage.getItem(tokenKey) || '';
if (token) { sessionStorage.setItem(tokenKey, token); history.replaceState(null, '', location.pathname); }
let state = null, selected = null, page = 'topology', zoom = 1, panX = 0, panY = 0, drag = null;
let toastTimer, polling = false, lastDestinations = '', lastLogs = '', lastMessages = '';
const titles = {
  topology: ['ネットワーク・トポロジ','ノード、経路、リンクの状態をひとつの画面で。','トポロジ'],
  tests: ['通信テスト','到達率と応答時間を、同じ条件で繰り返し測定。','通信テスト'],
  messages: ['メッセージ','宛先を指定して、配達を確認しながら送信。','メッセージ'],
  firmware: ['ファームウェア','C3 / S3、それぞれに正しいビルドを。','ファームウェア'],
  logs: ['イベントログ','無線通信とシリアルの出来事を追跡。','イベントログ']
};
function notify(message, error = false) {
  clearTimeout(toastTimer); $('toast').textContent = message; $('toast').className = `toast${error ? ' error' : ''}`;
  $('toast').hidden = false; toastTimer = setTimeout(() => {$('toast').hidden = true;}, error ? 7000 : 3500);
}
async function api(path, data) {
  const response = await fetch(path, {method: data ? 'POST' : 'GET', cache:'no-store',
    headers: {'X-Mesh-Token':token, ...(data ? {'Content-Type':'application/json'} : {})},
    body: data ? JSON.stringify(data) : undefined});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}
async function command(data) {
  try { await api('/api/command', data); await refresh(); return true; }
  catch (e) { notify(e.message, true); return false; }
}
function showPage(name) {
  page = name;
  for (const el of document.querySelectorAll('.page')) el.hidden = el.id !== `page-${name}`;
  for (const el of document.querySelectorAll('.nav')) el.classList.toggle('active', el.dataset.page === name);
  [$('page-title').textContent, $('page-description').textContent, $('breadcrumb').textContent] = titles[name];
  if (state) render(state);
}
for (const button of document.querySelectorAll('.nav')) button.addEventListener('click', () => showPage(button.dataset.page));
async function ports() {
  try {
    const old = $('port').value;
    const data = await api('/api/ports');
    $('port').innerHTML = '<option value="">ポートを選択</option>' + data.ports.map(p =>
      `<option value="${escapeHTML(p.device)}">${escapeHTML(p.device)} · ${escapeHTML(p.description)}</option>`).join('');
    if (data.ports.some(p => p.device === old)) $('port').value = old;
  } catch (e) { notify(e.message, true); }
}
$('refresh-ports').onclick = ports;
$('connect').onclick = () => command(state && state.connection === 'connected' ? {action:'disconnect'} : {action:'connect',port:$('port').value});
$('refresh-network').onclick = () => command({action:'refresh'});
$('stop-test').onclick = $('stop-message').onclick = () => command({action:'stop'});
$('export').onclick = async () => {
  try {
    const data = await api('/api/export');
    const blob = new Blob([JSON.stringify(data, null, 2)], {type:'application/json'});
    const url = URL.createObjectURL(blob); const a = document.createElement('a');
    a.href = url; a.download = `mesh-session-${new Date().toISOString().replace(/[:.]/g,'-')}.json`;
    a.click(); setTimeout(() => URL.revokeObjectURL(url), 2000); notify('セッションを保存しました。');
  } catch (e) { notify(e.message, true); }
};
$('test-form').onsubmit = async e => {
  e.preventDefault(); const data = Object.fromEntries(new FormData(e.target));
  for (const k of ['count','size','interval_ms','timeout_ms','ttl']) data[k] = Number(data[k]);
  await command({action:'test', ...data});
};
$('message-form').onsubmit = async e => {e.preventDefault(); await command({action:'message', ...Object.fromEntries(new FormData(e.target)),ttl:6});};
$('metadata-form').onsubmit = async e => {
  e.preventDefault(); const data = Object.fromEntries(new FormData(e.target));
  for (const k of ['distance_m','height_m']) data[k] = data[k] === '' ? null : Number(data[k]);
  if (await command({action:'metadata', ...data})) notify('試験条件を保存しました。');
};
$('firmware-form').onsubmit = async e => {
  e.preventDefault(); const data = Object.fromEntries(new FormData(e.target));
  data.lr_authorized = Boolean(data.lr_authorized);
  const operation = e.submitter.value;
  if (operation === 'upload' && !confirm(`対象: ${data.environment}\nポート: ${$('port').value || '未選択'}\n接続を切断し、このボードへ書き込みます。続けますか？`)) return;
  await command({action:'firmware', ...data, operation, port:$('port').value});
};
function value(v, suffix = '') { return v === null || v === undefined ? '—' : `${escapeHTML(v)}${suffix}`; }
function row(label, v) { return `<div class="data-row"><span>${escapeHTML(label)}</span><b>${v}</b></div>`; }
function svg(tag, attrs = {}, text) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, val] of Object.entries(attrs)) el.setAttribute(key, val);
  if (text !== undefined) el.textContent = text;
  return el;
}
function positions(network) {
  const ids = new Set(network.nodes.map(n => n.id));
  for (const edge of network.edges) {ids.add(edge.from); ids.add(edge.to);}
  const all = [...ids].sort(); const root = network.local || all[0];
  const depth = new Map(root ? [[root,0]] : []);
  for (let repeat = 0; repeat < all.length; repeat++) {
    for (const e of network.edges) {
      if (depth.has(e.from) && !depth.has(e.to)) depth.set(e.to, depth.get(e.from) + 1);
      if (depth.has(e.to) && !depth.has(e.from)) depth.set(e.from, depth.get(e.to) + 1);
    }
  }
  const unknown = Math.max(1, ...depth.values()) + 1;
  for (const id of all) if (!depth.has(id)) depth.set(id, unknown);
  const max = Math.max(1, ...depth.values()); const pos = {};
  for (let d = 0; d <= max; d++) {
    const group = all.filter(id => depth.get(id) === d);
    group.forEach((id,i) => {pos[id] = {x:130 + d * (740 / max), y: (i + 1) * (520 / (group.length + 1))};});
  }
  return pos;
}
function drawMap(n) {
  const g = $('map-content'); g.replaceChildren(); const pos = positions(n);
  const known = new Map(n.nodes.map(node => [node.id,node]));
  const edgeMap = new Map();
  for (const edge of n.edges) {
    if (!pos[edge.from] || !pos[edge.to]) continue;
    const key = [edge.from,edge.to].sort().join('/');
    if (!edgeMap.has(key) || edge.kind === 'observed') edgeMap.set(key, edge);
  }
  for (const edge of edgeMap.values()) {
    const a = pos[edge.from], b = pos[edge.to];
    const middle = (a.x + b.x) / 2;
    const path = `M${a.x},${a.y} C${middle},${a.y} ${middle},${b.y} ${b.x},${b.y}`;
    g.append(svg('path',{d:path, class:`link-edge ${edge.kind}`}));
    if (edge.rssi != null) g.append(svg('text',{x:middle, y:(a.y+b.y)/2 - 9,'text-anchor':'middle',class:'edge-label'}, `${edge.rssi} dBm`));
  }
  for (const [id,p] of Object.entries(pos)) {
    const node = known.get(id) || {id, online:false}; const local = id === n.local;
    const group = svg('g', {transform:`translate(${p.x-109} ${p.y-44})`,class:`map-node ${local?'gateway':''} ${id===selected?'selected':''} ${node.online?'':'offline'}`,tabindex:0,role:'button','aria-label':`${id} 詳細を表示`});
    group.append(svg('rect',{width:218,height:88,rx:10}));
    group.append(svg('rect',{x:15,y:15,width:30,height:30,rx:5,class:'node-chip'}));
    group.append(svg('path',{d:'M19 21h11v11H19ZM22 18v3m5-3v3m-5 11v3m5-3v3M16 24h3m-3 5h3m11-5h3m-3 5h3',fill:'none',stroke:local?'#388f88':'#8da3af','stroke-width':1}));
    group.append(svg('text',{x:54,y:35,class:'node-id'}, id));
    group.append(svg('text',{x:16,y:70,class:'node-sub'},local ? `USB GATEWAY · ${node.chip || 'ESP32'}` : node.chip || 'ESP32 · REMOTE NODE'));
    group.append(svg('circle',{cx:205,cy:12,r:4}));
    const choose = () => {selected=id; drawMap(n); drawInspector(n);};
    group.onclick = choose; group.onkeydown = e => {if(e.key==='Enter'||e.key===' '){e.preventDefault();choose();}};
    g.append(group);
  }
  g.setAttribute('transform',`translate(${panX} ${panY}) translate(500 280) scale(${zoom}) translate(-500 -280)`);
  $('graph-empty').hidden = Object.keys(pos).length > 0;
}
function drawInspector(n) {
  const node = n.nodes.find(x => x.id === selected);
  if (!node) {$('inspector').innerHTML='<div class="inspector-empty">マップ上のノードを選択して<br>状態と経路を確認できます。</div>'; return;}
  const local = node.id === n.local;
  const routes = n.routes.filter(r => r.dst_node_id === node.id);
  $('inspector').innerHTML = `<div class="inspector-identity"><div class="chip-icon">⌘</div><h3>${escapeHTML(node.id)}</h3><p>${escapeHTML(node.chip || 'ESP32')} · ${local?'USB GATEWAY':'REMOTE NODE'}</p></div><div class="inspector-fields">`+
    row('状態',`<span class="badge ${node.online?'green':''}">${node.online?'ONLINE':'STALE'}</span>`)+
    row('最終観測',value(node.age_s,' s前'))+row('RSSI / 最終受信ホップ',node.rssi == null?'不明':value(node.rssi,' dBm'))+
    row('空きヒープ',node.heap?`${Math.round(node.heap/1024)} KB`:'—')+
    row('利用可能な経路',String(routes.length))+row('次ホップ',routes[0]?escapeHTML(routes[0].next_hop_node_id || '不明'):'—')+
    `</div>${!local?'<button id="test-node" class="button">このノードをテスト →</button>':''}`;
  if (!local) $('test-node').onclick = () => {showPage('tests'); $('test-form').elements.destination.value=node.id;};
}
$('zoom-in').onclick = () => {zoom=Math.min(3,zoom*1.2); if(state)drawMap(state.network);};
$('zoom-out').onclick = () => {zoom=Math.max(.45,zoom/1.2); if(state)drawMap(state.network);};
$('fit').onclick = () => {zoom=1;panX=panY=0;if(state)drawMap(state.network);};
$('graph').onpointerdown = e => {if(e.target.closest('.map-node'))return;drag={x:e.clientX,y:e.clientY,px:panX,py:panY};$('graph').setPointerCapture(e.pointerId);};
$('graph').onpointermove = e => {if(!drag)return;const rect=$('graph').getBoundingClientRect();panX=drag.px+(e.clientX-drag.x)*1000/rect.width;panY=drag.py+(e.clientY-drag.y)*560/rect.height;if(state)drawMap(state.network);};
$('graph').onpointerup = $('graph').onpointercancel = () => {drag=null;};
function chart(test) {
  const el = $('test-chart'); el.replaceChildren(); if(!test)return;
  const samples=test.samples.slice(-100); const max=Math.max(100,...samples.map(x=>x.rtt_ms || 0));
  for(let i=0;i<4;i++){const y=18+i*45;el.append(svg('line',{x1:38,y1:y,x2:590,y2:y,stroke:'#e8eef1','stroke-dasharray':'3 4'}));el.append(svg('text',{x:0,y:y+4,fill:'#9cafba','font-size':9},Math.round(max*(3-i)/3)));}
  if(!samples.length)return;
  const points=samples.map((s,i)=>[42+i*(540/Math.max(1,samples.length-1)),153-((s.rtt_ms||0)/max)*135,s]);
  let path='';for(const [x,y,s]of points){if(s.ok){path+=(path?' L':'M')+x+','+y;}else{if(path){el.append(svg('path',{d:path,fill:'none',stroke:'#499d97','stroke-width':2}));path='';}el.append(svg('circle',{cx:x,cy:164,r:3,fill:'#c79a87'}));}}
  if(path)el.append(svg('path',{d:path,fill:'none',stroke:'#499d97','stroke-width':2}));
}
function render(s) {
  const n=s.network, connected=s.connection==='connected', test=s.test;
  $('demo-banner').hidden=!s.demo;
  const connectionText={demo:'デモ表示',connected:'接続中',connecting:'接続処理中',disconnected:'未接続'}[s.connection] || s.connection;
  $('connection-status').innerHTML=`<i></i>${escapeHTML(connectionText)}`;
  $('connection-status').classList.toggle('connected',connected);
  $('footer-state').textContent=connectionText;
  $('connect').textContent=connected?'切断':'接続';$('connect').disabled=s.demo||s.connection==='connecting';
  $('nav-nodes').textContent=n.nodes.length;$('metric-nodes').textContent=n.nodes.length || '—';
  $('metric-online').textContent=n.nodes.length?`${n.nodes.filter(x=>x.online).length} online · ${n.nodes.length-n.nodes.filter(x=>x.online).length} stale`:'まだデータがありません';
  $('metric-routes').textContent=n.routes.length || '—';
  $('metric-pdr').innerHTML=test&&test.pdr!==null?value(test.pdr,'<em>%</em>'):'—';
  $('metric-rtt').innerHTML=test&&test.p95_ms!==null?value(Math.round(test.p95_ms),'<em>ms</em>'):'—';
  $('updated').textContent=s.demo?'SAMPLE DATA':connected?'更新中 · 1秒ごと':'接続待ち';
  const r=n.radio;
  $('radio-summary').textContent=r.profile?`${r.chip || 'ESP32'} · ${r.profile} · CH ${r.channel ?? '?'} · ${r.espnow_rate_kbps ?? '?'} kbps`:'C3 / S3 をUSBで接続してください';
  const destinations=n.nodes.filter(x=>x.id!==n.local).map(x=>x.id).sort();const key=destinations.join(',');
  if(key!==lastDestinations){lastDestinations=key;for(const el of document.querySelectorAll('.destination')){const old=el.value;el.innerHTML='<option value="">宛先ノードを選択</option>'+destinations.map(id=>`<option value="${id}">${id}</option>`).join('');if(destinations.includes(old))el.value=old;}}
  if(!selected && n.local)selected=n.local;
  if(page==='topology'){
    if(!drag)drawMap(n);drawInspector(n);
    $('route-count').textContent=n.routes.length;
    $('routes-empty').hidden=n.routes.length>0;
    $('route-rows').innerHTML=n.routes.map(r=>`<tr><td class="mono">${escapeHTML(r.dst_node_id)}</td><td class="mono">${escapeHTML(r.next_hop_node_id||r.next_hop_mac||'不明')}</td><td>${value(r.hops)}</td><td><span class="badge ${r.rank===0?'green':'amber'}">${r.rank===0?'PRIMARY':'BACKUP'}</span></td><td>${typeof r.metric_q8==='number'?(r.metric_q8/256).toFixed(2):'—'}</td><td>${r.hops>1?'途中の経路は未観測':'次ホップが宛先'}</td></tr>`).join('');
    if(n.route_truncated)$('map-subtitle').textContent='経路表は一部のみ取得（truncated）';
  }
  if(page==='tests'){
    $('test-status').textContent=test?({running:'測定中',complete:'完了',stopped:'停止'}[test.status]||test.status):'未実施';
    $('test-pdr').textContent=test&&test.pdr!==null?`${test.pdr}%`:'—';
    $('test-detail').innerHTML=row('送信',test?`${test.sent} / ${test.target}`:'—')+row('受信 / 損失',test?`${test.received} / ${test.lost}`:'—')+row('応答待ち',test?String(test.pending):'—')+row('RTT p95',test?value(test.p95_ms,' ms'):'—')+row('重複・遅延応答',test?String(test.duplicates_or_late):'—')+row('中止した試行',test?String(test.cancelled):'—');
    chart(test);
  }
  if(page==='messages'){
    const t=s.transfer;$('transfer-status').textContent=t?`${({sending:'送信中',delivered:'配達ACKを確認',failed:'配達ACKタイムアウト',cancelled:'中止'}[t.status]||t.status)} · ${t.index}/${t.total} packets · retry ${t.retry}`:'待機中';
    const mk=JSON.stringify(s.messages);if(mk!==lastMessages){lastMessages=mk;$('messages').innerHTML=s.messages.length?s.messages.slice().reverse().map(m=>`<article class="message-item"><header><span>${escapeHTML(m.peer)}</span><time>${new Date(m.time*1000).toLocaleTimeString('ja-JP')}</time></header><p>${escapeHTML(m.text)}</p><small>${escapeHTML(m.status)}</small></article>`).join(''):'<div class="table-empty">受信したテキストをここに表示します。</div>';}
  }
  if(page==='firmware'){
    $('job-status').textContent=s.job.status;$('build-log').textContent=(s.job.log||[]).join('\n')||'まだビルドしていません。';
    if(s.job.sha256)$('build-log').textContent+=`\n\nfirmware.bin SHA-256\n${s.job.sha256}`;
    $('radio-detail').innerHTML=row('SoC',escapeHTML(r.chip||'—'))+row('プロファイル',escapeHTML(r.profile||'—'))+row('設定したPHY',value(r.espnow_rate_kbps,' kbps'))+row('チャンネル',value(r.channel))+row('帯域幅',value(r.bandwidth_mhz,' MHz'))+row('出力設定の読み戻し',r.tx_power_readback_qdbm!=null?`${r.tx_power_readback_qdbm/4} dBm`:'—')+row('省電力',r.power_save===undefined?'—':r.power_save?'ON':'OFF')+row('読み戻し確認',r.readback_ok===undefined?'—':r.readback_ok?'OK':'FAILED');
  }
  if(page==='logs')renderLogs(s);
  for(const form of ['test-form','message-form','firmware-form'])for(const button of $(form).querySelectorAll('button[type="submit"]'))button.disabled=s.demo||(form!=='firmware-form'&&!connected)||(form==='firmware-form'&&s.job.status==='running');
}
function renderLogs(s) {
  const filter=$('log-filter').value.toLowerCase();const key=s.log_count+'|'+filter;if(key===lastLogs)return;lastLogs=key;
  const logs=s.logs.filter(l=>JSON.stringify(l).toLowerCase().includes(filter));
  $('log-count').textContent=`${s.log_count} EVENTS`;
  $('log-rows').innerHTML=logs.slice().reverse().map(l=>`<tr><td>${new Date(l.time*1000).toLocaleTimeString('ja-JP')}</td><td><span class="badge">${escapeHTML(l.kind)}</span></td><td>${escapeHTML(typeof l.data==='string'?l.data:JSON.stringify(l.data))}</td></tr>`).join('');
}
$('log-filter').oninput=()=>{if(state)renderLogs(state);};
async function refresh(){if(polling)return;polling=true;try{state=await api('/api/state');render(state);}catch(e){$('updated').textContent='サーバーとの接続が切れました';if(!state)notify(e.message,true);}finally{polling=false;}}
ports();refresh();setInterval(refresh,1000);
