// ── Block & Drift ──
var blockedState = false;

async function checkBlocked() {
  try {
    var r = await fetch('/api/status/blocked');
    var d = await r.json();
    var isBlocked = !!d.blocked;
    if (isBlocked !== blockedState) { blockedState = isBlocked; applyBlockedUI(isBlocked); }
  } catch(e) {}
}

function applyBlockedUI(blocked) {
  var area = document.querySelector('.inp-area');
  var txt = document.getElementById('txt');
  if (blocked) {
    area.classList.add('blocked'); txt.placeholder = '\u2014\u2014'; txt.disabled = true;
  } else {
    area.classList.remove('blocked'); txt.placeholder = 'say something\u2026'; txt.disabled = false;
  }
}

function openBottle() {
  document.getElementById('drift-modal').classList.add('show');
  document.getElementById('drift-txt').value = '';
  document.getElementById('drift-send-btn').style.display = '';
  document.getElementById('drift-send-btn').disabled = false;
  document.getElementById('drift-sent-msg').style.display = 'none';
  loadFloating();
  setTimeout(function(){ document.getElementById('drift-txt').focus(); }, 100);
}
function closeDriftModal() { document.getElementById('drift-modal').classList.remove('show'); }
function driftBgClick(e) { if(e.target===document.getElementById('drift-modal')) closeDriftModal(); }

async function sendBottle() {
  var content = document.getElementById('drift-txt').value.trim();
  if (!content) return;
  var btn = document.getElementById('drift-send-btn');
  btn.disabled = true;
  try {
    await fetch('/api/drift/send', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:content})});
    btn.style.display = 'none';
    document.getElementById('drift-sent-msg').style.display = 'block';
    loadMsgs(false).then(function(){var c=document.getElementById('msgs');c.scrollTop=c.scrollHeight;});
    setTimeout(closeDriftModal, 2500);
  } catch(e) { btn.disabled = false; }
}

checkBlocked();
setInterval(checkBlocked, 15000);

// ── Notifications ──
var swReg=null;
async function notify(title,body){
  if(!('Notification' in window))return;
  if(Notification.permission==='default') await Notification.requestPermission();
  if(Notification.permission!=='granted')return;
  if(swReg&&swReg.active) swReg.active.postMessage({type:'NOTIFY',title,body});
  else new Notification(title,{body,icon:'/icon-192.png'});
}
if('serviceWorker' in navigator){
  navigator.serviceWorker.register('/sw.js').then(function(r){swReg=r;}).catch(console.error);
}
document.addEventListener('click',function askOnce(){
  if('Notification' in window&&Notification.permission==='default') Notification.requestPermission();
  document.removeEventListener('click',askOnce);
},{once:true});

// ── Drift reply notification ──
function notifyNewDriftReplies(bottles){
  var found=bottles.filter(function(b){return b.status==='found'&&b.reply;}).map(function(b){return b.id;});
  if(localStorage.getItem('driftSeen')===null){ localStorage.setItem('driftSeen',JSON.stringify(found)); return; }
  var seen=[]; try{ seen=JSON.parse(localStorage.getItem('driftSeen')||'[]'); }catch(e){}
  var fresh=found.filter(function(id){return seen.indexOf(id)===-1;});
  if(fresh.length){
    var b=bottles.find(function(x){return x.id===fresh[0];});
    notify('漂流瓶被捡到了','费奥多尔回信：\u201C'+(b?b.reply.slice(0,50):'')+'\u2026\u201D');
    localStorage.setItem('driftSeen',JSON.stringify(found));
  }
}

// ── Floating bottles list ──
function timeAgo(dt){
  try{
    var d=new Date(String(dt).replace(' ','T'));
    var mins=Math.max(1,Math.round((Date.now()-d.getTime())/60000));
    if(mins<60) return mins+' 分钟前';
    var h=Math.round(mins/60); if(h<24) return h+' 小时前';
    return Math.round(h/24)+' 天前';
  }catch(e){ return ''; }
}
async function loadFloating(){
  var el=document.getElementById('drift-floating'); el.innerHTML='';
  try{
    var r=await fetch('/api/drift/bottles'); var d=await r.json();
    var fl=(d.bottles||[]).filter(function(b){return b.status==='floating';});
    el.innerHTML=fl.map(function(b){ return '<div class="drift-float-item"><i class="ti ti-ripple"></i>投出于 '+timeAgo(b.created_at)+' \u00B7 还在漂\u2026</div>'; }).join('');
  }catch(e){}
}

// ── Long-press menu ──
var sheetMsg=null, lpTimer=null;
var msgsEl=document.getElementById('msgs');
msgsEl.addEventListener('touchstart',function(e){
  var row=e.target.closest('.msg-row');
  if(!row||row.dataset.idx===undefined) return;
  lpTimer=setTimeout(function(){ openSheet(parseInt(row.dataset.idx)); },550);
},{passive:true});
msgsEl.addEventListener('touchmove',function(){ clearTimeout(lpTimer); },{passive:true});
msgsEl.addEventListener('touchend',function(){ clearTimeout(lpTimer); });
msgsEl.addEventListener('contextmenu',function(e){
  var row=e.target.closest('.msg-row');
  if(!row||row.dataset.idx===undefined) return;
  e.preventDefault(); openSheet(parseInt(row.dataset.idx));
});
function openSheet(idx){
  var list=window.curMsgList||[];
  if(!list[idx]||!(list[idx].content||'').trim()) return;
  sheetMsg=list[idx]; document.getElementById('sheet-ov').classList.add('show');
}
function closeSheet(){ document.getElementById('sheet-ov').classList.remove('show'); }
function showToast(t){
  var el=document.getElementById('toast'); el.textContent=t; el.classList.add('show');
  setTimeout(function(){ el.classList.remove('show'); },1800);
}
async function sheetCopy(){
  if(!sheetMsg) return;
  try{ await navigator.clipboard.writeText(sheetMsg.content); }
  catch(e){ var ta=document.createElement('textarea'); ta.value=sheetMsg.content; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
  showToast('已复制'); closeSheet();
}
async function sheetMemory(){
  if(!sheetMsg) return;
  try{
    await fetch('/api/posts',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({type:'MEMORY',content:sheetMsg.content,author:isFy(sheetMsg.author)?'fyodor':'user'})});
    showToast('已收藏到回忆 \u2661');
  }catch(e){ showToast('收藏失败'); }
  closeSheet();
}

// ── Init ──
loadMsgs(true);
setInterval(function(){ loadMsgs(false); }, 8000);
