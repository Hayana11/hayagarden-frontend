// ── Build message HTML ──
function buildHtml(list) {
  window.curMsgList = list;
  if(!list.length) return '<div class="empty-tip">no messages yet<br>say the first thing</div>';
  var html='', lastDay='';
  list.forEach(function(m,i){
    var dk = dayKey(m.created_at);
    if(dk !== lastDay){ html += '<div class="date-sep">'+dayLabel(m.created_at)+'</div>'; lastDay=dk; }
    if(m.drift){
      var dside = m.drift==='sent' ? 'ha' : 'fy';
      html += '<div class="msg-row '+dside+'" data-idx="'+i+'"><div class="bwrap"><div class="drift-badge"'+(dside==='ha'?' style="justify-content:flex-end"':'')+'><i class="ti ti-mail-forward"></i> '+(m.drift==='sent'?'漂流瓶 · 已投出':'漂流瓶回信')+'</div><div class="bubble drift'+(dside==='ha'?' sent':'')+'">'+escHtml(m.content)+'</div><div class="msg-time">'+fmtTime(m.created_at)+'</div></div></div>';
      return;
    }
    var fy = isFy(m.author);
    var side = fy?'fy':'ha';
    var hasImg = !!m.image_url;
    var hasTxt = !!(m.content && m.content.trim());
    var imgOnly = hasImg && !hasTxt;
    var thinking = (m.thinking||'').trim();
    var innerHtml = '';
    if(fy && thinking) {
      var tOpen = openThinks.has(String(m.id));
      innerHtml += '<div class="think-wrap" data-mid="'+m.id+'"><button class="think-toggle'+(tOpen?' open':'')+'" onclick="toggleThink(this)"><i class="ti ti-chevron-right"></i><span class="tl">'+(tOpen?'\u25BC thinking':'\u25B6 thinking')+'</span></button><div class="think-body'+(tOpen?' open':'')+'">'+escHtml(thinking)+'</div></div>';
    }
    if(fy && m.tool_calls) {
      var tcs = []; try { tcs = JSON.parse(m.tool_calls); } catch(e) {}
      tcs.forEach(function(tc) { innerHtml += buildToolCardHtml(tc); });
    }
    if(hasImg) innerHtml += '<img src="'+escHtml(m.image_url)+'" loading="lazy" onclick="openLb(\''+escHtml(m.image_url)+'\')">';
    if(hasTxt) innerHtml += (hasImg?'<div class="img-then-text">':'')+escHtml(m.content)+(hasImg?'</div>':'');
    var actHtml = fy
      ? '<div class="msg-actions"><button class="mac-btn" title="复制" onclick="copyBubble(this)"><i class="ti ti-copy"></i></button></div>'
      : '<div class="msg-actions"><button class="mac-btn" title="重发" onclick="replayMsg(this)"><i class="ti ti-refresh"></i></button><button class="mac-btn" title="编辑" onclick="editMsg(this)"><i class="ti ti-edit"></i></button></div>';
    html += '<div class="msg-row '+side+'" data-idx="'+i+'"><div class="bwrap"><div class="bubble '+side+(imgOnly?' img-only':'')+'">'+innerHtml+'</div><div class="msg-time-row"><span class="msg-time">'+fmtTime(m.created_at)+'</span>'+actHtml+'</div></div></div>';
  });
  return html;
}

async function loadMsgs(isInit){
  try{
    var r=await fetch('/api/chat/messages');
    var d=await r.json();
    var list=d.messages||[];
    try{
      var rb=await fetch('/api/drift/bottles');
      var db=await rb.json();
      (db.bottles||[]).forEach(function(b){
        list.push({author:'hayana',content:b.content,created_at:b.created_at,drift:'sent'});
        if(b.status==='found'&&b.reply) list.push({author:'fyodor',content:b.reply,created_at:b.found_at,drift:'reply'});
      });
      list.sort(function(a,b){return String(a.created_at).localeCompare(String(b.created_at));});
      notifyNewDriftReplies(db.bottles||[]);
    }catch(e){}
    var c=document.getElementById('msgs');
    var wasAtBottom=c.scrollHeight-c.scrollTop-c.clientHeight<80;
    c.innerHTML=buildHtml(list);
    if(isInit||wasAtBottom) c.scrollTop=c.scrollHeight;
  }catch(e){
    if(isInit) document.getElementById('msgs').innerHTML='<div class="empty-tip">load failed</div>';
  }
}

// Author toggle
function toggleAuthor(){
  curAuthor = curAuthor==='hayana'?'fyodor':'hayana';
  var btn=document.getElementById('author-btn');
  btn.textContent=curAuthor==='hayana'?'Haya':'Fyodor';
  btn.className='inp-author-btn '+(curAuthor==='hayana'?'ha':'fy');
}

// File input
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
          var gd=await gr.json(); fullText=gd.content||'';
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

function openLb(src){ document.getElementById('lb-img').src=src; document.getElementById('lb').classList.add('show'); }
function closeLb(){ document.getElementById('lb').classList.remove('show'); }
