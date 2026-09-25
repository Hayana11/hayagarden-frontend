import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const screenPath = path.join(root, 'src/screens/ChatScreen.tsx');
const cssPath = path.join(root, 'src/index.css');
const screen = fs.readFileSync(screenPath, 'utf8');
const css = fs.readFileSync(cssPath, 'utf8');

const {
  CHAT_PINNED_MODELS,
  CHAT_PINNED_MODEL_IDS,
  pinnedModelsFromCatalog,
  moreModelsFromCatalog,
  isPickerRuntimeIncompatible,
  chatModelCapsuleLabel,
  effortDisplayLabel,
  isExplicitCurrentModel,
  moreModelsEntrySubtitle,
  ChatModelPickerSheet,
  thinkingControlForPicker,
  thinkingRowCopy,
  authoredPromptRowCopy,
} = await import('../src/screens/ChatScreen.tsx');

const catalog = [
  { id: 'claude-opus-5-5', label: 'Opus 5.5', runtime_compatible: true, dot: '#c4a07a' },
  { id: 'claude-opus-4-6', label: 'Opus 4.6', runtimeCompatible: true, dot: '#b8897a' },
  { id: 'claude-sonnet-5', label: 'Sonnet 5', dot: '#9a8a7a' },
  { id: 'claude-haiku-4-5-20251001', label: 'Haiku 4.5', dot: '#8a9a8a' },
  { id: 'claude-sonnet-4-6', label: 'Sonnet 4.6', dot: '#7a8a9a' },
  { id: 'claude-opus-5', label: 'Opus 5', runtime_compatible: false, runtime_requirement: '2.1.280', dot: '#b89a7a' },
];

function renderSheet(overrides = {}) {
  return renderToStaticMarkup(createElement(ChatModelPickerSheet, {
    panel: 'main',
    models: catalog,
    currentModel: 'claude-opus-4-6',
    modelMode: 'explicit',
    chatProvider: 'claude_code',
    currentEffort: 'high',
    effortMode: 'explicit',
    allowedEfforts: ['low', 'medium', 'high', 'xhigh', 'max'],
    authoredPromptEffective: false,
    busy: false,
    onClose() {},
    onBack() {},
    onOpenEffort() {},
    onOpenMore() {},
    onSelectModel() {},
    onSelectEffort() {},
    onToggleAuthoredPrompt() {},
    ...overrides,
  }));
}

// A. Main sheet only surfaces the four pinned model ids, by id lookup not catalog order.
{
  assert.deepEqual(CHAT_PINNED_MODEL_IDS, [
    'claude-opus-5-5',
    'claude-opus-4-6',
    'claude-sonnet-5',
    'claude-haiku-4-5-20251001',
  ]);
  assert.equal(CHAT_PINNED_MODELS[0].title, 'Opus 5.5');
  assert.equal(CHAT_PINNED_MODELS[1].title, 'Opus 4.6');
  assert.equal(CHAT_PINNED_MODELS[2].title, 'Sonnet 5');
  assert.equal(CHAT_PINNED_MODELS[3].title, 'Haiku 4.5');
  const pinned = pinnedModelsFromCatalog(catalog);
  assert.deepEqual(pinned.map((row) => row.id), CHAT_PINNED_MODEL_IDS);
  const missingHaiku = catalog.filter((model) => model.id !== 'claude-haiku-4-5-20251001');
  assert.deepEqual(
    pinnedModelsFromCatalog(missingHaiku).map((row) => row.id),
    ['claude-opus-5-5', 'claude-opus-4-6', 'claude-sonnet-5'],
  );
  const html = renderSheet();
  for (const id of CHAT_PINNED_MODEL_IDS) {
    assert.match(html, new RegExp(`data-model-id="${id}"`));
  }
  assert.doesNotMatch(html, /data-model-id="claude-sonnet-4-6"/);
  assert.doesNotMatch(html, /data-model-id="claude-opus-5"/);
}

// B. No “默认（跟随 Claude Code）” option.
{
  const html = renderSheet({ modelMode: 'default', currentModel: '' });
  assert.doesNotMatch(html, /默认（跟随 Claude Code）/);
  assert.doesNotMatch(screen, /默认（跟随 Claude Code）/);
  assert.doesNotMatch(screen, /setChatModel\(null\)/);
}

// C. Pinned model click still calls setChatModel(model.id)
{
  assert.match(screen, /const result = await setChatModel\(modelId\)/);
  assert.match(screen, /props\.onSelectModel\(row\.id\)/);
  let selected = '';
  renderSheet({
    onSelectModel(modelId) { selected = modelId; },
  });
  const pinned = pinnedModelsFromCatalog(catalog);
  assert.equal(pinned[0].id, 'claude-opus-5-5');
}

// D. Effort subsheet uses allowedEfforts dynamically.
{
  const html = renderSheet({
    panel: 'effort',
    allowedEfforts: ['low', 'xhigh', 'max'],
  });
  assert.match(html, />低</);
  assert.match(html, />极高</);
  assert.match(html, />最大</);
  assert.doesNotMatch(html, />中</);
  assert.doesNotMatch(html, />高</);
  assert.match(screen, /allowedEfforts\.map/);
}

// E. Clicking effort calls setChatEffort
{
  assert.match(screen, /const result = await setChatEffort\(effort\)/);
  assert.match(screen, /props\.onSelectEffort\(effort\)/);
  assert.doesNotMatch(screen, /setChatEffort\(null\)/);
}

// F. More models excludes the four pinned ids.
{
  assert.deepEqual(
    moreModelsFromCatalog(catalog).map((model) => model.id),
    ['claude-sonnet-4-6', 'claude-opus-5'],
  );
  const html = renderSheet({ panel: 'more' });
  assert.match(html, /data-model-id="claude-sonnet-4-6"/);
  assert.match(html, /data-model-id="claude-opus-5"/);
  for (const id of CHAT_PINNED_MODEL_IDS) {
    assert.doesNotMatch(html, new RegExp(`data-model-id="${id}"`));
  }
}

// G. More models can switch a non-pinned model.
{
  assert.match(screen, /props\.onSelectModel\(model\.id\)/);
  const html = renderSheet({
    panel: 'more',
    currentModel: 'claude-sonnet-4-6',
    modelMode: 'explicit',
  });
  assert.match(html, /Sonnet 4\.6/);
  assert.match(html, /aria-pressed="true"/);
}

// H. Runtime incompatible models cannot be switched.
{
  const incompatible = catalog.find((model) => model.id === 'claude-opus-5');
  assert.equal(isPickerRuntimeIncompatible(incompatible), true);
  const html = renderSheet({ panel: 'more' });
  assert.match(html, /需要 Claude Code ≥ 2\.1\.280/);
  assert.match(html, /disabled/);
  const pinnedIncompatible = pinnedModelsFromCatalog([
    { id: 'claude-opus-5-5', label: 'Opus 5.5', runtimeCompatible: false, runtimeRequirement: '9.9.9' },
  ]);
  const pinHtml = renderSheet({ models: pinnedIncompatible.map((row) => row.model) });
  assert.match(pinHtml, /需要 Claude Code ≥ 9\.9\.9/);
}

// I. modelMode=default does not guess a concrete default model.
{
  assert.equal(isExplicitCurrentModel('default', '', 'claude-opus-5-5'), false);
  assert.equal(chatModelCapsuleLabel({
    chatProvider: 'claude_code',
    modelMode: 'default',
    currentModel: '',
    models: catalog,
  }), '默认');
  const html = renderSheet({ modelMode: 'default', currentModel: '' });
  assert.match(html, /当前跟随 Claude Code 默认模型/);
  assert.doesNotMatch(html, /is-current/);
  assert.equal(moreModelsEntrySubtitle({
    modelMode: 'default',
    currentModel: '',
    models: catalog,
  }), '查看全部可用模型');
}

// J. Next-turn toast is preserved.
{
  assert.match(screen, /showToast\('下一条消息起生效'\)/);
  assert.match(screen, /showToast\('模型切换失败'\)/);
}

{
  assert.equal(effortDisplayLabel('default', ''), '默认');
  assert.equal(effortDisplayLabel('explicit', 'xhigh'), '极高');
  assert.equal(chatModelCapsuleLabel({
    chatProvider: 'claude_code',
    modelMode: 'explicit',
    currentModel: 'claude-sonnet-4-6',
    models: catalog,
  }), 'Sonnet 4.6');
  assert.equal(moreModelsEntrySubtitle({
    modelMode: 'explicit',
    currentModel: 'claude-sonnet-4-6',
    models: catalog,
  }), '当前：Sonnet 4.6');
}

{
  assert.match(screen, /ensureModelCatalog/);
  assert.match(screen, /setChatModel/);
  assert.match(screen, /getChatEffort/);
  assert.match(screen, /setChatEffort/);
  assert.match(screen, /chat-model-pop/);
  assert.doesNotMatch(screen, /chat-model-sheet/);
  assert.doesNotMatch(css, /chat-model-sheet/);
  assert.match(css, /\.chat-model-pop \{/);
  assert.doesNotMatch(css, /:has\(/);
  assert.doesNotMatch(css, /@container/);
  const popCss = css.slice(css.indexOf('.chat-model-pop {'), css.indexOf('.contacts-monopoly-card'));
  assert.match(popCss, /position:\s*absolute/);
  assert.match(popCss, /bottom:\s*calc\(100% \+ 10px\)/);
  assert.match(popCss, /max-width:\s*calc\(100vw - 24px\)/);
  assert.match(popCss, /max-height:\s*56vh/);
  assert.doesNotMatch(popCss, /78vh/);
  assert.doesNotMatch(popCss, /max-width:\s*430px/);
  assert.doesNotMatch(popCss, /\bmin\(/);
  assert.doesNotMatch(popCss, /\bmax\(/);
  assert.doesNotMatch(popCss, /\bclamp\(/);
  assert.doesNotMatch(popCss, /\binset\s*:/);
  assert.doesNotMatch(popCss, /\bgap\s*:/);
  assert.doesNotMatch(popCss, /100dvh/);
}

for (const forbidden of ['Array.prototype.at', 'Object.hasOwn', 'replaceAll', 'structuredClone']) {
  assert.equal(screen.includes(forbidden), false, `Chrome 78 builtin: ${forbidden}`);
}

// Visual correction A–L
{
  const html = renderSheet();
  const effortHtml = renderSheet({ panel: 'effort' });
  const moreHtml = renderSheet({ panel: 'more' });
  const popCss = css.slice(css.indexOf('.chat-model-pop {'), css.indexOf('.contacts-monopoly-card'));

  // A. no bottom sheet layout
  assert.match(html, /chat-model-pop /);
  assert.doesNotMatch(html, /chat-model-sheet/);
  assert.doesNotMatch(popCss, /border-radius:\s*24px 24px 0 0/);

  // B. no drag handle
  assert.doesNotMatch(html, /chat-model-sheet-handle|chat-model-pop-handle/);
  assert.doesNotMatch(css, /sheet-handle/);

  // C. no full-screen gray modal backdrop
  assert.match(popCss, /background:\s*transparent/);
  assert.doesNotMatch(popCss, /rgba\(30,\s*20,\s*18,\s*0\.3/);

  // D. four common models
  for (const id of CHAT_PINNED_MODEL_IDS) {
    assert.match(html, new RegExp(`>${id}<`));
  }

  // E. effort entry
  assert.match(html, /思考强度/);
  assert.match(html, />高</);

  // F. more models entry
  assert.match(html, /更多模型/);
  assert.match(html, /查看全部可用模型/);

  // G. effort / more models switch in the same popover
  assert.match(effortHtml, /chat-model-pop /);
  assert.match(moreHtml, /chat-model-pop /);
  assert.match(effortHtml, /‹/);
  assert.match(moreHtml, /‹/);
  assert.doesNotMatch(effortHtml, /chat-model-sheet/);
  assert.doesNotMatch(moreHtml, /chat-model-sheet/);

  // H. selected model uses rosebg
  assert.match(popCss, /\.chat-model-pop-row\.is-current \{[\s\S]*background:\s*var\(--rosebg\)/);
  assert.match(html, /is-current/);

  // I. no blue accent
  assert.doesNotMatch(popCss, /#007|#0d6|#3b82f6|#2563eb|#0a84ff|#007aff/i);
  assert.doesNotMatch(html, /#007aff|#0a84ff|#3b82f6/i);
  assert.doesNotMatch(effortHtml, /#007aff|#0a84ff|#3b82f6/i);
  assert.match(popCss, /color:\s*var\(--mut\)/);
  assert.match(popCss, /color:\s*var\(--ink\)/);

  // J. model id still visible
  assert.match(html, />claude-opus-5-5</);
  assert.match(html, />claude-haiku-4-5-20251001</);
  assert.match(moreHtml, />claude-sonnet-4-6</);

  // K. color dots
  assert.match(html, /chat-model-pop-dot/);
  assert.match(html, /#c4a07a/);
  assert.match(moreHtml, /chat-model-pop-dot/);

  // L. composer pill unchanged
  assert.match(screen, /className="hstack hstack-6 chat-model-capsule"/);
  assert.match(screen, /padding: '9px 13px'/);
  assert.match(screen, /borderRadius: 999/);
  assert.match(screen, /background: 'var\(--card2\)'/);

  assert.doesNotMatch(html, /能力最强/);
  assert.doesNotMatch(html, /选择模型/);
  assert.match(html, />模型<\/span>/);
  assert.match(html, />MODELS</);
}

const apiPath = path.join(root, 'src/lib/api.ts');
const api = fs.readFileSync(apiPath, 'utf8');

// R2.1 / R2.2 Main menu directory rows have no left icons or icon slots.
{
  const html = renderSheet();
  assert.doesNotMatch(html, /chat-model-pop-ico/);
  assert.doesNotMatch(html, /◷/);
  assert.doesNotMatch(html, /•••/);
  assert.match(html, /data-picker-dir="effort"/);
  assert.match(html, /data-picker-dir="more"/);
  assert.match(html, /chat-model-pop-dir/);
  assert.match(css, /\.chat-model-pop-dir \{[\s\S]*padding:\s*9px 10px 9px 28px/);
  assert.doesNotMatch(css, /\.chat-model-pop-ico/);
}

// R2.3 Effort levels still come from allowedEfforts.
{
  const html = renderSheet({
    panel: 'effort',
    allowedEfforts: ['low', 'max'],
  });
  assert.match(html, />低</);
  assert.match(html, />最大</);
  assert.doesNotMatch(html, />中</);
  assert.match(screen, /allowedEfforts\.map/);
}

// R2.4 Opus 5.5 thinking is ON / native_locked and not clickable.
{
  assert.equal(thinkingControlForPicker('explicit', 'claude-opus-5-5'), 'native_locked');
  assert.deepEqual(thinkingRowCopy('native_locked'), {
    title: '思考',
    note: '模型原生思考',
    checked: true,
  });
  const html = renderSheet({
    panel: 'effort',
    currentModel: 'claude-opus-5-5',
    modelMode: 'explicit',
    authoredPromptEffective: false,
  });
  assert.match(html, /data-thinking-control="native_locked"/);
  assert.match(html, /模型原生思考/);
  assert.match(html, /aria-checked="true"/);
  assert.match(html, /disabled=""/);
  assert.doesNotMatch(html, /🔒|chat-model-pop-lock/);
}

// R2.5 Other models cannot fake a thinking on/off switch.
{
  assert.equal(thinkingControlForPicker('explicit', 'claude-sonnet-5'), 'unavailable');
  assert.equal(thinkingControlForPicker('default', ''), 'unavailable');
  const html = renderSheet({
    panel: 'effort',
    currentModel: 'claude-sonnet-5',
    modelMode: 'explicit',
  });
  assert.match(html, /data-thinking-control="unavailable"/);
  assert.match(html, /当前 Claude Code 未提供独立开关/);
  assert.match(html, />--</);
  assert.doesNotMatch(html, /--thinking/);
  assert.doesNotMatch(screen, /--thinking/);
  assert.doesNotMatch(screen, /thinking_enabled/);
  assert.doesNotMatch(api, /thinking_enabled/);
}

// R2.6–R2.10 Authored prompt switch follows effective injection, not raw configured mode.
{
  const opusAuto = renderSheet({
    panel: 'effort',
    currentModel: 'claude-opus-5-5',
    modelMode: 'explicit',
    authoredPromptEffective: false,
  });
  assert.match(opusAuto, /data-authored-prompt="off"/);
  assert.match(opusAuto, /使用原生思考，无需提示词/);
  assert.deepEqual(authoredPromptRowCopy({
    authoredPromptEffective: false,
    thinkingControl: 'native_locked',
  }), {
    title: '思考提示词',
    note: '使用原生思考，无需提示词',
    checked: false,
  });

  const sonnetAuto = renderSheet({
    panel: 'effort',
    currentModel: 'claude-sonnet-5',
    modelMode: 'explicit',
    authoredPromptEffective: true,
  });
  assert.match(sonnetAuto, /data-authored-prompt="on"/);
  assert.match(sonnetAuto, /用于可见思绪；下一条消息起生效/);
  assert.deepEqual(authoredPromptRowCopy({
    authoredPromptEffective: true,
    thinkingControl: 'unavailable',
  }), {
    title: '思考提示词',
    note: '用于可见思绪；下一条消息起生效',
    checked: true,
  });

  const sonnetNative = renderSheet({
    panel: 'effort',
    currentModel: 'claude-sonnet-5',
    modelMode: 'explicit',
    authoredPromptEffective: false,
  });
  assert.match(sonnetNative, /data-authored-prompt="off"/);
  assert.match(sonnetNative, /给非原生思考模型注入可见思绪提示词/);

  let toggled;
  renderSheet({
    panel: 'effort',
    currentModel: 'claude-sonnet-5',
    modelMode: 'explicit',
    authoredPromptEffective: true,
    onToggleAuthoredPrompt(enabled) { toggled = enabled; },
  });
  assert.equal(toggled, undefined);
  assert.match(screen, /setDisplayThinkingAuthoredPrompt\(enabled\)/);
  assert.match(api, /authored_prompt_enabled: enabled/);
  assert.match(api, /'\/api\/config\/display-thinking'/);
  assert.doesNotMatch(api, /mode:\s*['"]auto['"]/);
}

// R2.11 Model switch refreshes effective display-thinking state.
{
  const selectStart = screen.indexOf('const selectChatModel =');
  const selectEnd = screen.indexOf('const selectChatEffort =', selectStart);
  const selectBody = screen.slice(selectStart, selectEnd);
  assert.match(selectBody, /getDisplayThinkingConfig\(\)/);
  assert.match(selectBody, /applyDisplayThinking\(displayThinking\)/);
  assert.match(screen, /Promise\.all\(\[\s*ensureModelCatalog\(\),\s*getChatEffort\(\),\s*getDisplayThinkingConfig\(\),/);
}

// R2.12 Display-thinking write failure keeps chat/runtime untouched.
{
  const promptStart = screen.indexOf('const selectAuthoredPrompt =');
  const promptEnd = screen.indexOf('useEffect(() => {', promptStart);
  const promptBody = screen.slice(promptStart, promptEnd);
  assert.match(promptBody, /思考提示词设置失败/);
  assert.match(promptBody, /if \(!result\.ok\)/);
  assert.doesNotMatch(promptBody, /sendChatMessage|streamChatReply|abortRef|forceUnlock|rollback|setChatModel/);
}

// R2.13 Chrome 78 + palette
{
  const popCss = css.slice(css.indexOf('.chat-model-pop {'), css.indexOf('.contacts-monopoly-card'));
  assert.doesNotMatch(popCss, /\bgap\s*:/);
  assert.doesNotMatch(popCss, /:has\(/);
  assert.doesNotMatch(popCss, /\bmin\(/);
  assert.doesNotMatch(popCss, /\bclamp\(/);
  assert.doesNotMatch(popCss, /Object\.hasOwn/);
  assert.doesNotMatch(popCss, /#007|#0d6|#3b82f6|#2563eb|#0a84ff|#007aff/i);
  assert.match(popCss, /background:\s*#E4D8D3/);
  assert.match(popCss, /background:\s*var\(--rosebg\)/);
  assert.match(popCss, /background:\s*var\(--rose\)/);
  assert.match(popCss, /color:\s*var\(--ghost\)/);
  for (const forbidden of ['Array.prototype.at', 'Object.hasOwn', 'replaceAll', 'structuredClone']) {
    assert.equal(screen.includes(forbidden), false, `Chrome 78 builtin: ${forbidden}`);
    assert.equal(api.includes(forbidden), false, `Chrome 78 builtin api: ${forbidden}`);
  }
}

console.log('chat model picker contract: PASS');
