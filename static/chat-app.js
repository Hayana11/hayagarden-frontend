// ── Theme ──
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  var ic=document.querySelector('#theme-btn i');
  if(ic) ic.className = t==='dark' ? 'ti ti-sun' : 'ti ti-moon';
}
var curTheme = localStorage.getItem('theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
applyTheme(curTheme);
function toggleTheme(){
  curTheme = curTheme==='dark'?'light':'dark';
  localStorage.setItem('theme', curTheme);
  applyTheme(curTheme);
}

var curAuthor = 'hayana';
var pendingFile = null;
var sending = false;
var allHist = [];

// Font size
var msgFont = parseInt(localStorage.getItem('msgFont')) || 15;
document.documentElement.style.setProperty('--msg-font', msgFont + 'px');
document.getElementById('font-slider').value = msgFont;
document.getElementById('aa-size').textContent = msgFont + 'px';
function setFontSize(v) {
  document.documentElement.style.setProperty('--msg-font', v + 'px');
  localStorage.setItem('msgFont', v);
  document.getElementById('aa-size').textContent = v + 'px';
}

// Panel toggle
function togglePanel(id, btnId) {
  ['aa-panel','status-panel'].forEach(function(p){ if(p !== id) document.getElementById(p).classList.remove('show'); });
  ['aa-btn','sp-btn'].forEach(function(b){ if(b !== btnId) document.getElementById(b).classList.remove('on'); });
  var el = document.getElementById(id);
  var btn = document.getElementById(btnId);
  el.classList.toggle('show');
  btn.classList.toggle('on', el.classList.contains('show'));
}
document.addEventListener('click', function(e) {
  if (!e.target.closest('.hd')) {
    document.getElementById('aa-panel').classList.remove('show');
    document.getElementById('status-panel').classList.remove('show');
    document.getElementById('aa-btn').classList.remove('on');
    document.getElementById('sp-btn').classList.remove('on');
  }
});

// History
async function openHistory() {
  document.getElementById('hist-ov').classList.add('show');
  document.getElementById('hist-search').value = '';
  var r = await fetch('/api/chat/messages?limit=500');
  var d = await r.json();
  allHist = (d.messages || []).reverse();
  renderHist(allHist);
}
function closeHistory() { document.getElementById('hist-ov').classList.remove('show'); }
function filterHist(q) {
  if (!q.trim()) { renderHist(allHist); return; }
  var lq = q.toLowerCase();
  renderHist(allHist.filter(m => (m.content||'').toLowerCase().includes(lq)));
}
function renderHist(list) {
  var body = document.getElementById('hist-body');
  if (!list.length) { body.innerHTML = '<div class="empty-tip">nothing found</div>'; return; }
  var html = '', lastDate = '';
  list.forEach(function(m) {
    var dk = dayKey(m.created_at);
    if (dk !== lastDate) { html += '<div class="hist-date">' + dayLabel(m.created_at) + '</div>'; lastDate = dk; }
    var fy = isFy(m.author);
    html += '<div class="hist-msg"><div class="hm-who ' + (fy?'fy':'ha') + '">' + (fy?'Fyodor':'Haya') + ' · ' + fmtTime(m.created_at) + '</div><div class="hm-text">' + escHtml((m.content||'').slice(0,80)) + '</div></div>';
  });
  body.innerHTML = html;
}

// Utilities
function isFy(a){ return ['fyodor','claude','assistant'].includes(String(a).toLowerCase()); }
function escHtml(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>'); }
function fmtTime(dt){
  if(!dt)return'';
  try{ var d=new Date(dt.replace(' ','T')); return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0'); }catch(e){return'';}
}
function dayKey(dt){ if(!dt)return''; try{ var d=new Date(dt.replace(' ','T')); return d.getFullYear()+'-'+(d.getMonth()+1)+'-'+d.getDate(); }catch(e){return'';} }
function dayLabel(dt){
  if(!dt)return'';
  try{
    var d=new Date(dt.replace(' ','T')); var t=new Date(); var y=new Date(); y.setDate(t.getDate()-1);
    if(d.toDateString()===t.toDateString())return'Today';
    if(d.toDateString()===y.toDateString())return'Yesterday';
    return ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()]+' '+d.getDate();
  }catch(e){return'';}
}

// Tool call cards
var TOOL_LABELS={light_on:'开灯',light_off:'关灯',set_brightness:'调亮度',set_color_temp:'调色温',get_light_status:'查灯状态',save_memory:'存入记忆',search_memories:'搜索记忆',light_main_on:'开主灯',light_main_off:'关主灯',light_bedside_on:'开床头灯',light_bedside_off:'关床头灯',light_all_on:'全部开灯',light_all_off:'全部关灯',light_bedside_warm:'暖灯模式'};
function toggleToolCard(btn){ btn.nextElementSibling.classList.toggle('open'); btn.classList.toggle('open'); }
function buildToolCardHtml(tc) {
  var label = TOOL_LABELS[tc.name] || tc.name;
  var status = tc.success ? '· 成功' : '· 失败';
  var hasArgs = tc.args && Object.keys(tc.args).length > 0;
  var argsHtml = hasArgs ? '<div style="opacity:.65;margin-bottom:4px">参数: ' + escHtml(JSON.stringify(tc.args)) + '</div>' : '';
  var resultHtml = '<div>结果: ' + escHtml(String(tc.result || '').slice(0, 400)) + '</div>';
  return '<div class="tool-card"><button class="tool-card-toggle" onclick="toggleToolCard(this)"><i class="ti ti-chevron-right"></i><span>\u26a1 ' + escHtml(label) + ' <span style="opacity:.6">' + escHtml(status) + '</span></span></button><div class="tool-card-body">' + argsHtml + resultHtml + '</div></div>';
}

// Thinking toggle
var openThinks = new Set();
function toggleThink(btn) {
  var body = btn.nextElementSibling;
  var isOpen = body.classList.contains('open');
  body.classList.toggle('open'); btn.classList.toggle('open');
  var lbl = btn.querySelector('.tl');
  if(lbl) lbl.textContent = isOpen ? '\u25b6 thinking' : '\u25bc thinking';
  var wrap = btn.closest('.think-wrap');
  if (wrap && wrap.dataset.mid !== undefined) { if (isOpen) openThinks.delete(String(wrap.dataset.mid)); else openThinks.add(String(wrap.dataset.mid)); }
}
