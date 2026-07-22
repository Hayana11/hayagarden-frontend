import { useMemo, useState } from 'react';
import type { ParsedBoardPlayer, PendingDecision } from '../../lib/monopolyTypes';

function stripCardEmoji(value: string): string {
  return value.replace(/^\p{Extended_Pictographic}\s*/u, '').trim();
}

export function HandDock({
  player, pending, reminder, busy, guess, onGuess, onAction,
}: {
  player: ParsedBoardPlayer;
  pending: PendingDecision | null;
  reminder: string;
  busy: boolean;
  guess: '大' | '小' | null;
  onGuess: (value: '大' | '小' | null) => void;
  onAction: (action: string, args?: Record<string, unknown>) => void;
}) {
  const [open, setOpen] = useState(true);
  const [skillText, setSkillText] = useState('');
  const identity = player.identity || '未发放';
  const isGambler = identity.includes('赌徒');
  const isCat = identity.includes('猫猫');
  const isHungry = identity.includes('不知餍足');
  const isMark = identity.includes('淫纹');
  const isPersona = identity.includes('背德');
  const activeSkills = useMemo(() => [isCat, isHungry, isMark, isPersona].filter(Boolean).length, [isCat, isHungry, isMark, isPersona]);

  return (
    <section className="mono-hand-dock">
      <button type="button" className="mono-hand-toggle" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        <span>HAND DOCK</span><strong>手牌 {player.hand.length} · 身份技 {activeSkills || ''}</strong><i className={open ? 'open' : ''}>⌄</i>
      </button>
      {open && (
        <div className="mono-hand-content">
          <div className="mono-hand-scroll">
            <article className="mono-playing-card identity-card">
              <span className="mono-card-index">身</span>
              <div className="mono-playing-card-copy">
                <strong>{identity}</strong><small>IDENTITY</small>
                {isGambler ? <p>掷骰前猜大小<br />中 +2 币 · 错 −1 币</p> : <p>{reminder || '身份提醒会随引擎回合更新。'}</p>}
              </div>
              {isGambler && <div className="mono-card-buttons"><button className={guess === '大' ? 'active' : ''} onClick={() => onGuess(guess === '大' ? null : '大')}>大</button><button className={guess === '小' ? 'active' : ''} onClick={() => onGuess(guess === '小' ? null : '小')}>小</button></div>}
              <button type="button" className="mono-card-text-action" disabled={busy} onClick={() => onAction('reroll_identity')}>重抽身份 · 每局一次</button>
            </article>
            {player.hand.map((card, index) => (
              <article className="mono-playing-card item-card" key={`${card}-${index}`}>
                <span className="mono-card-index">功</span>
                <div className="mono-playing-card-copy"><strong>{stripCardEmoji(card)}</strong><small>ITEM · #{index}</small><p>引擎手牌 · 使用条件由棋盘真值校验</p></div>
                <div className="mono-card-buttons">
                  <button disabled={busy} onClick={() => onAction('use_card', { index })}>用</button>
                  <button disabled={busy} onClick={() => onAction('discard', { index })}>弃</button>
                </div>
              </article>
            ))}
            {player.hand.length === 0 && <div className="mono-empty-card">手牌空<br />商店可摸功能卡</div>}
          </div>
          <div className="mono-skill-panel">
            <div className="mono-skill-quick">
              <button type="button" disabled={busy} onClick={() => onAction('buy_card')}>商店摸一张</button>
              {isCat && <button type="button" disabled={busy || pending?.actor !== 'haya'} onClick={() => onAction('reroll_task')}>🐱 重抽当前任务</button>}
              {isHungry && <button type="button" disabled={busy} onClick={() => onAction('extra_task')}>➕ 加餐</button>}
              <button type="button" disabled={busy} onClick={() => onAction('id_event', { event: 'first_climax' })}>记录首次高潮</button>
              <button type="button" disabled={busy} onClick={() => onAction('id_event', { event: 'say_banned' })}>记录说出禁词</button>
              <button type="button" disabled={busy} onClick={() => onAction('id_event', { event: 'no_kiss_2turns' })}>记录两轮未接吻</button>
            </div>
            {(isMark || isPersona) && (
              <div className="mono-skill-input">
                <input value={skillText} onChange={(event) => setSkillText(event.target.value)} placeholder={isMark ? '猜淫纹部位，例如：腰侧' : '宣布背德身份，例如：邻居'} />
                <button type="button" disabled={busy || !skillText.trim()} onClick={() => { onAction(isMark ? 'guess_mark' : 'declare_persona', isMark ? { part: skillText.trim() } : { persona: skillText.trim() }); setSkillText(''); }}>提交</button>
              </div>
            )}
          </div>
          <small className="mono-hand-note">功能卡、身份与金币都只显示引擎状态；按钮不会在浏览器里擅自改棋盘。</small>
        </div>
      )}
    </section>
  );
}
