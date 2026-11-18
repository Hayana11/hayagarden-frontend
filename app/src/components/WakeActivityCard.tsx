import type { WakeActivity } from '../lib/wakeActivity';

const labels: Record<string, string> = {
  xhs_read: '刷小红书', xhs_status: '查看小红书状态', x_read: '读推特',
  x_state: '查看推特状态', x_post: '发推或回复', diary_write: '写日记',
  memory_write: '存记忆', memory_search: '翻记忆', todo_read: '看待办',
  get_todos: '看待办', todo_write: '调整待办', add_todo: '加待办',
  countdown_read: '看倒计时', get_countdowns: '看倒计时', gallery_recall: '翻相册',
  WebSearch: '搜索网页', WebFetch: '读网页', ToolSearch: '查找工具',
};

export function WakeActivityCard({ activity }: { activity: WakeActivity }) {
  return (
    <article className="wake-activity-card">
      <header>他自己醒来 · <time dateTime={activity.woke_at.replace(' ', 'T') + '+08:00'}>{activity.woke_at.slice(11, 16)}</time></header>
      <strong>{activity.title || '自己待了一会儿'}</strong>
      {activity.summary && <p>{activity.summary}</p>}
      {activity.tools.length > 0 && (
        <details>
          <summary>工具记录（{activity.tools.length}）</summary>
          <p>{activity.tools.map(name => {
            const tool = name.split('__').pop() || name;
            return labels[tool] || tool;
          }).join(' · ')}</p>
        </details>
      )}
    </article>
  );
}
