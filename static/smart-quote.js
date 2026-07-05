// 安卓输入法配对符号修复（v2，兼容 app 内 WebView Chrome 78）：
// 安卓系统输入法打"《(（「【'等配对符号时，会把开符号+闭符号一起插入，光标停在闭符号后面，
// 导致没法直接在中间打字。
// 1) 自动进中间：一次输入事件同时插入了"空"配对符号（中间没内容）且光标停在闭符号后面时，
//    自动把光标挪到两个符号中间，直接打字即可。手动逐个敲 ( 再敲 ) 不会触发。
// 2) 竖线技巧保留：空配对符号后打一个"|"，吃掉竖线并把光标挪到中间。
//    v2 不再依赖 event.data（部分输入法/WebView 组合里 data 为空导致旧版失灵），
//    改为直接检查光标前的文本。全文件只用 ES5 语法（app 的 WebView 是 Chrome 78）。
(function(){
  var PAIRS = {
    '“':'”', '《':'》', '（':'）', '「':'」', '【':'】', '‘':'’', '〈':'〉', '〔':'〕',
    '(':')', '[':']', '{':'}', '"':'"', "'":"'"
  };
  function isEditable(el){
    return !!el && (el.tagName==='TEXTAREA' || el.tagName==='INPUT') && typeof el.selectionStart==='number';
  }
  function fire(el){
    var ev;
    try { ev = new Event('input',{bubbles:true}); }
    catch(err){ ev = document.createEvent('Event'); ev.initEvent('input',true,false); }
    el.dispatchEvent(ev);
  }
  function onInput(e){
    var el = e.target;
    if(!isEditable(el)) return;
    var v = el.value, pos = el.selectionStart;
    var prev = el.__sqPrev == null ? null : el.__sqPrev;
    el.__sqPrev = v;
    // ── 竖线技巧：光标前是 开+闭+| ──
    if(pos >= 3){
      var open = v.charAt(pos-3), close = v.charAt(pos-2), bar = v.charAt(pos-1);
      if(bar === '|' && PAIRS[open] === close){
        el.value = v.slice(0, pos-1) + v.slice(pos);
        el.__sqPrev = el.value;
        el.setSelectionRange(pos-2, pos-2);
        // 触发一下 input 事件，让用到 oninput 的地方（自动调整高度等）能感知到变化
        fire(el);
        return;
      }
    }
    // ── 自动进中间：本次事件恰好一次性插入了 开+闭，且光标停在闭符号后面 ──
    if(prev !== null && pos >= 2 && v.length === prev.length + 2){
      var o = v.charAt(pos-2), c = v.charAt(pos-1);
      if(PAIRS[o] === c && prev === v.slice(0, pos-2) + v.slice(pos)){
        el.setSelectionRange(pos-1, pos-1);
      }
    }
  }
  function onFocus(e){
    var el = e.target;
    if(isEditable(el)) el.__sqPrev = el.value;
  }
  document.addEventListener('focusin', onFocus, true);
  document.addEventListener('input', onInput, true);
})();
