import { PLAYER_NAMES } from '../../lib/monopolyRoom';
import type { PendingDecision } from '../../lib/monopolyTypes';

const KIND_META = {
  task: { icon: '✦', char: '任', title: '任务悬账', state: 'TASK_PENDING' },
  truth: { icon: '❦', char: '真', title: '真心话悬账', state: 'TRUTH_PENDING' },
  duel: { icon: '⚔', char: '决', title: '对决悬账', state: 'DUEL_PENDING' },
  toll: { icon: '⛁', char: '费', title: '过路费挂账', state: 'TOLL_PENDING' },
  super: { icon: '★', char: '超', title: '超级任务悬账', state: 'SUPER_PENDING' },
} as const;

const META_COPY = {
  task: '默认「做完」· 决定随下一次掷骰结清',
  truth: '默认「答了」· 决定随下一次掷骰结清',
  duel: '无默认 · 不选出胜者任何人不得掷骰',
  toll: '默认「交钱」· 随下一次掷骰结清',
  super: '默认「做完」· 随下一次掷骰结清',
} as const;

export function PendingCard({ pending, text }: { pending: PendingDecision; text: string }) {
  const meta = KIND_META[pending.kind];
  return (
    <section className={`mono-pending-wrap ${pending.kind}`} aria-live="polite">
      <article className="mono-pending-card">
        <div className="mono-card-corner top"><span>{meta.icon}</span><b>{meta.char}</b></div>
        <div className="mono-card-corner bottom"><span>{meta.icon}</span><b>{meta.char}</b></div>
        <span className="mono-hourglass">⏳</span>
        <div className="mono-pending-heading">
          <strong>{meta.title} · {PLAYER_NAMES[pending.actor]}</strong>
          <span>{meta.state}</span>
        </div>
        <p>{text}</p>
        <small>{META_COPY[pending.kind]}</small>
        {pending.chosen && <em>决定已保存 · 等下一枚骰子结算</em>}
      </article>
    </section>
  );
}
