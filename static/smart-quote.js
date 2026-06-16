// 安卓输入法配对符号修复：
// 安卓系统输入法打"《(（「【‘等配对符号时，会把开符号+闭符号一起插入，光标停在闭符号后面，
// 导致没法直接在中间打字（必须先打开符号，写字，再手动打闭符号）。
// 这里给所有 input/textarea 加一个全局监听：刚打完"空"配对符号（中间没内容）后，
// 紧接着输入一个竖线 "|"，就把竖线吃掉，把光标挪到两个符号中间。
(function(){
  var PAIRS = {
    '“':'”', '《':'》', '（':'）', '「':'」', '【':'】', '‘':'’', '〈':'〉', '〔':'〕',
    '(':')', '[':']', '{':'}', '"':'"', "'":"'"
  };
  function onInput(e){
    var el = e.target;
    if(!el || (el.tagName!=='TEXTAREA' && el.tagName!=='INPUT')) return;
    if(e.data !== '|') return;
    if(typeof el.selectionStart !== 'number') return;
    var pos = el.selectionStart;
    if(pos < 3) return;
    var v = el.value;
    var open = v.charAt(pos-3), close = v.charAt(pos-2), bar = v.charAt(pos-1);
    if(bar === '|' && PAIRS[open] === close){
      el.value = v.slice(0, pos-1) + v.slice(pos);
      var np = pos-2;
      el.setSelectionRange(np, np);
      // 触发一下 input 事件，让用到 oninput 的地方（自动调整高度等）能感知到变化
      el.dispatchEvent(new Event('input', {bubbles:true}));
    }
  }
  document.addEventListener('input', onInput, true);
})();
