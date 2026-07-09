// ── Streaming reply (Claude order: thinking → tool_calls → text) ──
var streamAbort = null;
var TW_CPS = 18;

async function streamReply(){
  var ctrl = new AbortController(); streamAbort = ctrl;
  var resp = await fetch('/api/gw/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',signal:ctrl.signal});
  if(!resp.ok||!resp.body) throw new Error('stream failed');
  var reader=resp.body.getReader(), decoder=new TextDecoder();
  var buf='';
  var thinkBuf='', textBuf='', toolBufs=[];
  var c=document.getElementById('msgs');
  var liveRow=null, liveBubble=null, liveThinkBody=null, liveTextNode=null;
  var textShown=0, thinkShown=0;
  var phase=''; // 'thinking','tool','text'
  var err=null;

  function ensureLiveRow(){
    if(liveRow) return;
    hideTyping();
    var div=document.createElement('div');
    div.className='msg-row fy'; div.id='live-row';
    div.innerHTML='<div class="bwrap"><div class="bubble fy" id="live-bubble"></div></div>';
    c.appendChild(div);
    liveRow=div; liveBubble=document.getElementById('live-bubble');
    scrollBottom();
  }
  function scrollBottom(){ var atB=c.scrollHeight-c.scrollTop-c.clientHeight<120; if(atB) c.scrollTop=c.scrollHeight; }

  function ensureThinkBlock(){
    if(liveThinkBody) return;
    ensureLiveRow();
    var wrap=document.createElement('div'); wrap.className='think-wrap';
    wrap.innerHTML='<button class="think-toggle open" onclick="toggleThink(this)"><i class="ti ti-chevron-right"></i><span class="tl">\u25BC thinking</span></button><div class="think-body open"></div>';
    liveBubble.appendChild(wrap);
    liveThinkBody=wrap.querySelector('.think-body');
  }

  function appendToolCard(tc){
    ensureLiveRow();
    var label = TOOL_LABELS[tc.name] || tc.name;
    var div=document.createElement('div'); div.className='tool-card';
    div.innerHTML='<button class="tool-card-toggle" onclick="toggleToolCard(this)"><i class="ti ti-chevron-right"></i><span>\u26A1 '+escHtml(label)+' <span style="opacity:.6">\u00B7 \u2026</span></span></button><div class="tool-card-body"><div style="opacity:.65">'+escHtml(JSON.stringify(tc.args||{}))+'</div></div>';
    liveBubble.appendChild(div);
    liveThinkBody = null; thinkBuf = ''; thinkShown = 0;
  }

  function updateToolResult(tc, idx){
    if(!liveBubble) return;
    var cards=liveBubble.querySelectorAll('.tool-card');
    var card=cards[idx]; if(!card) return;
    var toggle=card.querySelector('.tool-card-toggle span');
    var label=TOOL_LABELS[tc.name]||tc.name;
    var status=tc.success?'\u00B7 成功':'\u00B7 失败';
    if(toggle) toggle.innerHTML='\u26A1 '+escHtml(label)+' <span style="opacity:.6">'+escHtml(status)+'</span>';
    var body=card.querySelector('.tool-card-body');
    if(body) body.innerHTML=(tc.args&&Object.keys(tc.args).length?'<div style="opacity:.65;margin-bottom:4px">参数: '+escHtml(JSON.stringify(tc.args))+'</div>':'')+'<div>结果: '+escHtml(String(tc.result||'').slice(0,400))+'</div>';
  }

  function ensureTextNode(){
    if(liveTextNode) return;
    ensureLiveRow();
    var span=document.createElement('span'); span.id='live-text';
    liveBubble.appendChild(span);
    liveTextNode=span;
  }

  // Typewriter timers
  var thinkTimer=setInterval(function(){
    if(!liveThinkBody||thinkShown>=thinkBuf.length) return;
    thinkShown=Math.min(thinkBuf.length, thinkShown+2);
    liveThinkBody.innerHTML=escHtml(thinkBuf.slice(0,thinkShown));
    scrollBottom();
  }, Math.round(1000/36));

  var textTimer=setInterval(function(){
    if(!liveTextNode||textShown>=textBuf.length) return;
    textShown=Math.min(textBuf.length, textShown+1);
    liveTextNode.innerHTML=escHtml(textBuf.slice(0,textShown));
    scrollBottom();
  }, Math.round(1000/TW_CPS));

  try{
    while(true){
      var chunk=await reader.read();
      if(chunk.done) break;
      buf+=decoder.decode(chunk.value,{stream:true});
      var parts=buf.split('\n\n'); buf=parts.pop();
      for(var i=0;i<parts.length;i++){
        var line=parts[i].trim();
        if(line.indexOf('data:')!==0) continue;
        var ev; try{ ev=JSON.parse(line.slice(5)); }catch(e){ continue; }
        if(ev.t==='think'){
          ensureThinkBlock();
          thinkBuf+=ev.d;
        } else if(ev.t==='tool_call'){
          appendToolCard(ev.d);
          toolBufs.push(ev.d);
        } else if(ev.t==='tool_result'){
          var ti=ev.idx!==undefined?ev.idx:toolBufs.length-1;
          if(ev.d) toolBufs[ti]=ev.d;
          updateToolResult(ev.d||toolBufs[ti], ti);
        } else if(ev.t==='text'){
          ensureTextNode();
          textBuf+=ev.d;
        } else if(ev.t==='notice'){ showToast(ev.d); }
        else if(ev.t==='workspace_job'){
          var jd=ev.d||{};
          showToast((jd.preview||jd.job_id||'后台任务')+': '+(jd.status||'finished'));
        } else if(ev.t==='err'){ err=new Error(ev.d); }
      }
      if(err) break;
    }
    // Wait for typewriters to finish
    while(!err && (thinkShown<thinkBuf.length || textShown<textBuf.length)){
      await new Promise(function(r){setTimeout(r,60);});
    }
  } finally {
    clearInterval(thinkTimer); clearInterval(textTimer);
    var wasAborted=ctrl.signal.aborted;
    streamAbort=null;
    if(liveRow) liveRow.remove();
    if(wasAborted && textBuf && textBuf.trim()){
      fetch('/api/chat/send',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({author:'fyodor',content:textBuf.trim()})}).catch(function(){});
    }
  }
  if(err && err.name!=='AbortError') throw err;
  return textBuf;
}

// ── Send button toggle ──
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
  var range = document.createRange(); range.selectNodeContents(bubble); range.collapse(false);
  window.getSelection().removeAllRanges(); window.getSelection().addRange(range);
  function onKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault(); var nc = bubble.innerText.trim();
      bubble.contentEditable = 'false'; bubble.classList.remove('editing');
      bubble.removeEventListener('keydown', onKey);
      if (nc && !sending && !blockedState) { document.getElementById('txt').value = nc; send(); }
    } else if (e.key === 'Escape') {
      bubble.contentEditable = 'false'; bubble.classList.remove('editing');
      bubble.removeEventListener('keydown', onKey);
      bwrap.querySelector('.msg-actions').style.opacity = ''; loadMsgs(false);
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
