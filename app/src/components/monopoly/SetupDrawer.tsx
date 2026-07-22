import { useEffect, useMemo, useState } from 'react';
import { REDLINE_OPTIONS } from '../../lib/monopolyRoom';
import type { MonopolySetupValues, SetupConfirmation } from '../../lib/monopolyTypes';

const DEFAULT_VALUES: MonopolySetupValues = {
  first: 'haya', flavor: 'light', gameLength: 12, identityMode: 'mixed', reverseChance: 0.25,
  redline: [], openAnal: { haya: false, cc: false }, noPenetration: { haya: false, cc: false }, resetBlocklist: false,
};

function Segment<T extends string | number>({ label, value, options, onChange }: {
  label: string;
  value: T;
  options: Array<{ label: string; value: T }>;
  onChange: (value: T) => void;
}) {
  return (
    <label className="mono-setup-field">
      <span>{label}</span>
      <div className="mono-segment">
        {options.map((option) => <button type="button" className={value === option.value ? 'active' : ''} onClick={() => onChange(option.value)} key={String(option.value)}>{option.label}</button>)}
      </div>
    </label>
  );
}

function Toggle({ label, note, checked, onChange }: { label: string; note: string; checked: boolean; onChange: (checked: boolean) => void }) {
  return (
    <label className="mono-setup-toggle-row">
      <span><strong>{label}</strong><small>{note}</small></span>
      <button type="button" className={checked ? 'on' : ''} onClick={() => onChange(!checked)} aria-pressed={checked}><i /></button>
    </label>
  );
}

function readableLimits(value: unknown): string[] {
  if (!value) return ['引擎未返回额外限制文本'];
  if (typeof value === 'string') return [value];
  if (Array.isArray(value)) return value.map(String);
  if (typeof value === 'object') return Object.entries(value as Record<string, unknown>).map(([key, item]) => `${key}：${Array.isArray(item) ? item.join(' · ') : typeof item === 'object' ? JSON.stringify(item) : String(item)}`);
  return [String(value)];
}

export function SetupDrawer({
  open, locked, busy, confirmation, onClose, onSubmit,
}: {
  open: boolean;
  locked: boolean;
  busy: boolean;
  confirmation: SetupConfirmation | null;
  onClose: () => void;
  onSubmit: (values: MonopolySetupValues) => Promise<boolean>;
}) {
  const [values, setValues] = useState(DEFAULT_VALUES);
  const [step, setStep] = useState<1 | 2>(1);
  const limits = useMemo(() => readableLimits(confirmation?.activeLimits), [confirmation]);

  useEffect(() => {
    if (!open) setStep(1);
  }, [open]);

  if (!open) return null;
  const patch = <K extends keyof MonopolySetupValues>(key: K, value: MonopolySetupValues[K]) => setValues((current) => ({ ...current, [key]: value }));
  const submit = async () => {
    if (locked) { setStep(2); return; }
    if (await onSubmit(values)) setStep(2);
  };

  return (
    <div className="mono-setup-layer">
      <button type="button" className="mono-setup-backdrop" onClick={onClose} aria-label="关闭设置" />
      <section className="mono-setup-sheet" role="dialog" aria-modal="true" aria-label="开局设置">
        <header><i /><span>SETUP</span><strong>{step === 1 ? '开局设置' : '实际生效配置 · 请核对'}</strong><button type="button" onClick={onClose}>×</button></header>
        <div className="mono-setup-scroll">
          {step === 1 ? (
            <>
              {locked && <div className="mono-setup-locked">本房已经开局 · 设置仅供回看；重新配置请新开房间。</div>}
              <label className="mono-setup-field"><span>席位</span><div className="mono-seat-summary">玩家一 <b>哈娅</b> · 玩家二 <b>CC</b> · 观察席 <b>Codex</b></div></label>
              <Segment label="先手" value={values.first} onChange={(value) => patch('first', value)} options={[{ label: '哈娅', value: 'haya' }, { label: '对方（CC）', value: 'cc' }]} />
              <Segment label="强度 · flavor" value={values.flavor} onChange={(value) => patch('flavor', value)} options={['light', 'medium', 'heavy'].map((value) => ({ label: value, value: value as MonopolySetupValues['flavor'] }))} />
              <Segment label="局长 · game_length" value={values.gameLength} onChange={(value) => patch('gameLength', value)} options={[12, 18, 24].map((value) => ({ label: `${value} 回合`, value: value as 12 | 18 | 24 }))} />
              <Segment label="身份 · identity_mode" value={values.identityMode} onChange={(value) => patch('identityMode', value)} options={['off', 'mixed', 'nsfw_only'].map((value) => ({ label: value, value: value as MonopolySetupValues['identityMode'] }))} />
              <label className="mono-setup-field mono-range-field"><span>反转概率 <b>{Math.round(values.reverseChance * 100)}%</b></span><input type="range" min={0} max={1} step={0.05} value={values.reverseChance} onChange={(event) => patch('reverseChance', Number(event.target.value))} /></label>
              <label className="mono-setup-field"><span>红线（引擎合法开关 · 多选）</span><div className="mono-redline-grid">
                {REDLINE_OPTIONS.map(([value, label]) => {
                  const active = values.redline.includes(value);
                  return <button type="button" key={value} className={active ? 'active' : ''} onClick={() => patch('redline', active ? values.redline.filter((item) => item !== value) : [...values.redline, value])}><strong>{label}</strong><small>{value}</small></button>;
                })}
              </div></label>
              <div className="mono-toggle-list">
                <Toggle label="后庭 · 哈娅" note="open_anal · 逐人开关" checked={values.openAnal.haya} onChange={(checked) => patch('openAnal', { ...values.openAnal, haya: checked })} />
                <Toggle label="后庭 · CC" note="open_anal · 逐人开关" checked={values.openAnal.cc} onChange={(checked) => patch('openAnal', { ...values.openAnal, cc: checked })} />
                <Toggle label="纯 top · 哈娅" note="no_penetration" checked={values.noPenetration.haya} onChange={(checked) => patch('noPenetration', { ...values.noPenetration, haya: checked })} />
                <Toggle label="纯 top · CC" note="no_penetration" checked={values.noPenetration.cc} onChange={(checked) => patch('noPenetration', { ...values.noPenetration, cc: checked })} />
                <Toggle label="洗白黑名单" note="reset_blocklist · 清空本房换卡黑名单" checked={values.resetBlocklist} onChange={(checked) => patch('resetBlocklist', checked)} />
              </div>
              <div className="mono-safe-word-note">▣ 安全词固定为 <b>404</b> · 不可修改 · 服务端先于 AI 拦截</div>
              <button type="button" className="mono-setup-primary" disabled={busy} onClick={() => void submit()}>{locked ? '查看本房生效说明' : busy ? '正在创建真实棋局…' : '生成对局 · 核对生效配置'}</button>
            </>
          ) : (
            <>
              <div className="mono-active-limits-heading"><span>ACTIVE LIMITS</span><strong>以下才是引擎确认生效的安全设置</strong></div>
              {confirmation ? (
                <div className="mono-active-limits">{limits.map((line) => <p key={line}>{line}</p>)}</div>
              ) : locked ? (
                <div className="mono-active-limits"><p>本房已在运行；完整 active_limits 只在开局确认事件中播出。</p></div>
              ) : (
                <div className="mono-active-limits loading"><p>等待引擎确认事件…</p></div>
              )}
              {confirmation?.intensityNote && <div className="mono-intensity-note">{confirmation.intensityNote}</div>}
              <div className="mono-history-note">history_note · {confirmation?.historyNote || '等待跨局去重记录…'}</div>
              <div className="mono-limit-actions">
                {!locked && !confirmation && <button type="button" onClick={() => setStep(1)}>返回检查</button>}
                <button type="button" className="primary" disabled={!locked && !confirmation} onClick={onClose}>确认无误 · 进入游戏室</button>
              </div>
            </>
          )}
        </div>
      </section>
    </div>
  );
}
