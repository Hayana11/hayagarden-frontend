// ── Author toggle ──
function toggleAuthor(){
  curAuthor = curAuthor==='hayana'?'fyodor':'hayana';
  var btn=document.getElementById('author-btn');
  btn.textContent=curAuthor==='hayana'?'Haya':'Fyodor';
  btn.className='inp-author-btn '+(curAuthor==='hayana'?'ha':'fy');
}

// ── File input ──
function onFile(e){
  var f=e.target.files[0]; if(!f)return;
  pendingFile=f;
  var reader=new FileReader();
  reader.onload=function(ev){
    document.getElementById('thumb').src=ev.target.result;
    document.getElementById('img-strip').style.display='block';
  };
  reader.readAsDataURL(f); e.target.value='';
}
function removeImg(){
  pendingFile=null;
  document.getElementById('img-strip').style.display='none';
  document.getElementById('thumb').src='';
}

function resize(el){ el.style.height='auto'; el.style.height=Math.min(el.scrollHeight,96)+'px'; }
function onEnter(e){ if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();} }
function showTyping(){ document.getElementById('typing').style.display='block'; var c=document.getElementById('msgs'); c.scrollTop=c.scrollHeight; }
function hideTyping(){ document.getElementById('typing').style.display='none'; }

// ── Send ──
async function send(){
  if(sending||blockedState)return;
  var txt=document.getElementById('txt').value.trim();
  if(!txt&&!pendingFile)return;
  sending=true; setSendBtn('stop');
  var fromHayana=(curAuthor==='hayana');
  var fd=new FormData();
  fd.append('author',curAuthor);
  if(txt) fd.append('content',txt);
  if(pendingFile) fd.append('image',pendingFile);
  document.getElementById('txt').value='';
  document.getElementById('txt').style.height='auto';
  removeImg();
  try{
    await fetch('/api/chat/send',{method:'POST',body:fd});
    await loadMsgs(false);
    if(fromHayana){
      showTyping();
      try{
        var fullText='';
        try{ fullText=await streamReply(); }
        catch(se){
          console.warn('stream failed, fallback', se);
          var gr=await fetch('/api/gw/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
          var gd=await gr.json();
          fullText=gd.content||'';
        }
        await loadMsgs(false);
        if(fullText&&document.hidden) notify('Fyodor',fullText.slice(0,50));
      }catch(e){ console.error('gateway',e); }
      hideTyping();
    }
  }catch(e){ console.error('send',e); }
  sending=false; setSendBtn('send');
  document.getElementById('txt').focus();
}

// ── Send btn mode ──
function setSendBtn(mode) {
  var btn = document.getElementById('send-btn');
  if (mode === 'stop') {
    btn.innerHTML = '<i class="ti ti-player-stop"></i>';
    btn.style.background = '#e05555'; btn.style.color = '#fff';
    btn.onclick = function(){ if(streamAbort) streamAbort.abort(); };
    btn.disabled = false;
  } else {
    btn.innerHTML = '<i class="ti ti-send"></i>';
    btn.style.background = ''; btn.style.color = '';
    btn.onclick = send; btn.disabled = false;
  }
}

// ── Msg actions ──
function replayMsg(btn) {
  var bubble = btn.closest('.bwrap').querySelector('.bubble');
  var content = bubble ? bubble.innerText.trim() : '';
  if (!content || sending || blockedState) return;
  document.getElementById('txt').value = content; send();
}
function editMsg(btn) {
  var bwrap = btn.closest('.bwrap');
  var bubble = bwrap ? bwrap.querySelector('.bubble') : null;
  if (!bubble || bubble.contentEditable === 'true') return;
  bwrap.querySelector('.msg-actions').style.opacity = '0';
  bubble.contentEditable = 'true'; bubble.classList.add('editing'); bubble.focus();
  var range = document.createRange();
  range.selectNodeContents(bubble); range.collapse(false);
  window.getSelection().removeAllRanges(); window.getSelection().addRange(range);
  function onKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      var newContent = bubble.innerText.trim();
      bubble.contentEditable = 'false'; bubble.classList.remove('editing');
      bubble.removeEventListener('keydown', onKey);
      if (newContent && !sending && !blockedState) { document.getElementById('txt').value = newContent; send(); }
    } else if (e.key === 'Escape') {
      bubble.contentEditable = 'false'; bubble.classList.remove('editing');
      bubble.removeEventListener('keydown', onKey);
      bwrap.querySelector('.msg-actions').style.opacity = '';
      loadMsgs(false);
    }
  }
  bubble.addEventListener('keydown', onKey);
}
async function copyBubble(btn) {
  var bubble = btn.closest('.bwrap').querySelector('.bubble');
  if (!bubble) return;
  var text = bubble.innerText.trim();
  try { await navigator.clipboard.writeText(text); }
  catch(e) { var ta=document.createElement('textarea'); ta.value=text; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
  showToast('已复制');
}

// ── Lightbox ──
function openLb(src){ document.getElementById('lb-img').src=src; document.getElementById('lb').classList.add('show'); }
function closeLb(){ document.getElementById('lb').classList.remove('show'); }

// ── Block & Drift ──
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
  if (blocked) { area.classList.add('blocked'); txt.placeholder = '——'; txt.disabled = true; }
  else { area.classList.remove('blocked'); txt.placeholder = 'say something…'; txt.disabled = false; }
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

// ── Notifications ──
async function notify(title,body){
  if(!('Notification' in window))return;
  if(Notification.permission==='default') await Notification.requestPermission();
  if(Notification.permission!=='granted')return;
  if(swReg&&swReg.active) swReg.active.postMessage({type:'NOTIFY',title,body});
  else new Notification(title,{body,icon:'/icon-192.png'});
}
function notifyNewDriftReplies(bottles){
  var found=bottles.filter(function(b){return b.status==='found'&&b.reply;}).map(function(b){return b.id;});
  if(localStorage.getItem('driftSeen')===null){ localStorage.setItem('driftSeen',JSON.stringify(found)); return; }
  var seen=[]; try{ seen=JSON.parse(localStorage.getItem('driftSeen')||'[]'); }catch(e){}
  var fresh=found.filter(function(id){return seen.indexOf(id)===-1;});
  if(fresh.length){
    var b=bottles.find(function(x){return x.id===fresh[0];});
    notify('漂流瓶被捡到了','费奥多尔回信："'+(b?b.reply.slice(0,50):'')+'…"');
    localStorage.setItem('driftSeen',JSON.stringify(found));
  }
}
function timeAgo(dt){
  try{ var d=new Date(String(dt).replace(' ','T')); var mins=Math.max(1,Math.round((Date.now()-d.getTime())/60000));
    if(mins<60) return mins+' 分钟前'; var h=Math.round(mins/60); if(h<24) return h+' 小时前'; return Math.round(h/24)+' 天前';
  }catch(e){ return ''; }
}
async function loadFloating(){
  var el=document.getElementById('drift-floating'); el.innerHTML='';
  try{
    var r=await fetch('/api/drift/bottles'); var d=await r.json();
    var fl=(d.bottles||[]).filter(function(b){return b.status==='floating';});
    el.innerHTML=fl.map(function(b){ return '<div class="drift-float-item"><i class="ti ti-ripple"></i>投出于 '+timeAgo(b.created_at)+' · 还在漂…</div>'; }).join('');
  }catch(e){}
}

// ── Long-press & sheet ──
function openSheet(idx){
  var list=window.curMsgList||[];
  if(!list[idx]||!(list[idx].content||'').trim()) return;
  sheetMsg=list[idx];
  document.getElementById('sheet-ov').classList.add('show');
}
function closeSheet(){ document.getElementById('sheet-ov').classList.remove('show'); }
function showToast(t){ var el=document.getElementById('toast'); el.textContent=t; el.classList.add('show'); setTimeout(function(){ el.classList.remove('show'); },1800); }
async function sheetCopy(){
  if(!sheetMsg) return;
  try{ await navigator.clipboard.writeText(sheetMsg.content); }
  catch(e){ var ta=document.createElement('textarea'); ta.value=sheetMsg.content; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
  showToast('已复制'); closeSheet();
}
async function sheetMemory(){
  if(!sheetMsg) return;
  try{ await fetch('/api/posts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:'MEMORY',content:sheetMsg.content,author:isFy(sheetMsg.author)?'fyodor':'user'})}); showToast('已收藏到回忆 ♡'); }
  catch(e){ showToast('收藏失败'); }
  closeSheet();
}

// ── Init ──
(function init(){
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

  if('serviceWorker' in navigator){ navigator.serviceWorker.register('/sw.js').then(function(r){swReg=r;}).catch(console.error); }
  document.addEventListener('click',function askOnce(){
    if('Notification' in window&&Notification.permission==='default') Notification.requestPermission();
    document.removeEventListener('click',askOnce);
  },{once:true});

  loadMsgs(true);
  setInterval(function(){ loadMsgs(false); }, 8000);
  checkBlocked();
  setInterval(checkBlocked, 15000);
})();
