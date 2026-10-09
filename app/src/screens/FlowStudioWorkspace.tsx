import { useEffect, useMemo, useState, type KeyboardEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  cloneFlowStudioData,
  type FlowStudioCue,
  type FlowStudioData,
  type FlowStudioDataAdapter,
  type FlowStudioEntry,
  type FlowStudioPool,
  type FlowStudioStage,
} from '../lib/flowStudio/flowStudioMock';
import './FlowStudioSoftGlowScreen.css';

type FlowTab = 'overview' | 'stages' | 'library';
type LibraryTab = 'pools' | 'cues';
type DeleteTarget = { kind: 'stage' | 'pool'; id: string } | null;

function uid(prefix: string): string {
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
}

function Switch({ checked, onChange, label }: { checked: boolean; onChange: () => void; label: string }) {
  return (
    <button type="button" className={`flow-switch ${checked ? 'is-on' : ''}`} role="switch" aria-checked={checked} aria-label={label} onClick={onChange}>
      <span />
    </button>
  );
}

function Icon({ name }: { name: 'spark' | 'book' | 'search' | 'plus' | 'chevron' | 'edit' | 'close' | 'undo' | 'adjust' | 'trash' }) {
  const paths: Record<string, string> = {
    spark: 'M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z',
    book: 'M5 5h9a3 3 0 0 1 3 3v11H8a3 3 0 0 1-3-3V5Zm0 0v11a3 3 0 0 0 3 3m3-10h4',
    search: 'm20 20-3.7-3.7M10.8 17a6.2 6.2 0 1 0 0-12.4 6.2 6.2 0 0 0 0 12.4Z',
    plus: 'M12 5v14M5 12h14',
    chevron: 'm6 9 6 6 6-6',
    edit: 'm14.5 5.5 4 4L9 19H5v-4l9.5-9.5ZM13 7l4 4',
    close: 'M6 6l12 12M18 6 6 18',
    undo: 'M9 7H4v5m0-5 5 5m-5-5c2.8-3.2 7.9-3.8 11.4-1.1a7.5 7.5 0 0 1 .8 10.7',
    adjust: 'M4 7h16M4 12h16M4 17h16M8 5v4m8-2v4m-5 3v4',
    trash: 'M4 7h16M10 11v6m4-6v6M6 7l1 12h10l1-12M9 7V4h6v3',
  };
  return <svg className="flow-icon" viewBox="0 0 24 24" aria-hidden="true"><path d={paths[name]} /></svg>;
}

export type FlowStudioWorkspaceProps = {
  dataAdapter: FlowStudioDataAdapter;
  backPath: string;
  surfaceLabel: string;
  modeLabel: string;
};

export function FlowStudioWorkspace({
  dataAdapter,
  backPath,
  surfaceLabel,
  modeLabel,
}: FlowStudioWorkspaceProps) {
  const navigate = useNavigate();
  const [tab, setTab] = useState<FlowTab>('overview');
  const [libraryTab, setLibraryTab] = useState<LibraryTab>('pools');
  const [saved, setSaved] = useState<FlowStudioData>(() => dataAdapter.load());
  const [draft, setDraft] = useState<FlowStudioData>(() => dataAdapter.load());
  const [history, setHistory] = useState<FlowStudioData[]>([]);
  const [openStage, setOpenStage] = useState<string | null>(null);
  const [reorderingStages, setReorderingStages] = useState(false);
  const [openPool, setOpenPool] = useState<string | null>('pool-a');
  const [openPoolSettings, setOpenPoolSettings] = useState<string | null>(null);
  const [openCue, setOpenCue] = useState<string | null>('cue-1');
  const [editingEntry, setEditingEntry] = useState<string | null>(null);
  const [editingText, setEditingText] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget>(null);
  const [showDraw, setShowDraw] = useState(false);
  const [drawStageId, setDrawStageId] = useState('stage-2');
  const [drawCueIds, setDrawCueIds] = useState<string[]>(['cue-1']);
  const [drawTurn, setDrawTurn] = useState(1);
  const [drawn, setDrawn] = useState(false);
  const [showGuide, setShowGuide] = useState(false);
  const [toast, setToast] = useState('');
  const [saving, setSaving] = useState(false);

  const dirty = useMemo(() => JSON.stringify(saved) !== JSON.stringify(draft), [saved, draft]);
  const enabledEntries = draft.pools.reduce((total, pool) => total + pool.entries.filter((entry) => entry.enabled).length, 0);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(''), 2600);
    return () => window.clearTimeout(timer);
  }, [toast]);

  const notify = (message: string) => setToast(message);

  const mutate = (message: string, recipe: (next: FlowStudioData) => void) => {
    const next = cloneFlowStudioData(draft);
    recipe(next);
    setHistory((current) => [...current, cloneFlowStudioData(draft)].slice(-24));
    setDraft(next);
    notify(message);
  };

  const updateStage = (id: string, recipe: (stage: FlowStudioStage, data: FlowStudioData) => void, message = '阶段草稿已更新') => {
    mutate(message, (next) => {
      const stage = next.stages.find((item) => item.id === id);
      if (stage) recipe(stage, next);
    });
  };

  const updatePool = (id: string, recipe: (pool: FlowStudioPool, data: FlowStudioData) => void, message = '灵感池草稿已更新') => {
    mutate(message, (next) => {
      const pool = next.pools.find((item) => item.id === id);
      if (pool) recipe(pool, next);
    });
  };

  const updateCue = (id: string, recipe: (cue: FlowStudioCue, data: FlowStudioData) => void, message = '情境词草稿已更新') => {
    mutate(message, (next) => {
      const cue = next.cues.find((item) => item.id === id);
      if (cue) recipe(cue, next);
    });
  };

  const saveDraft = () => {
    setSaving(true);
    window.setTimeout(() => {
      const persisted = dataAdapter.save(draft);
      setSaved(persisted);
      setDraft(cloneFlowStudioData(persisted));
      setHistory([]);
      setSaving(false);
      notify('已保存到本地 Mock 草稿');
    }, 220);
  };

  const discardDraft = () => {
    setDraft(cloneFlowStudioData(saved));
    setHistory([]);
    setEditingEntry(null);
    notify('已取消未保存修改');
  };

  const undo = () => {
    const previous = history[history.length - 1];
    if (!previous) return;
    setDraft(cloneFlowStudioData(previous));
    setHistory((current) => current.slice(0, -1));
    notify('已撤销上一步');
  };

  const addStage = () => {
    const id = uid('stage');
    mutate('已添加新阶段', (next) => {
      const terminalIndex = next.stages.findIndex((stage) => stage.terminal);
      const stage: FlowStudioStage = { id, name: '新阶段', minTurns: 1, terminal: false, poolIds: [], nextStageId: null };
      if (terminalIndex >= 0) next.stages.splice(terminalIndex, 0, stage);
      else next.stages.push(stage);
    });
    setOpenStage(id);
    setTab('stages');
  };

  const addPool = () => {
    const id = uid('pool');
    mutate('已添加新灵感池', (next) => {
      next.pools.push({ id, name: '新灵感池', enabled: true, mode: 'perTurn', count: 1, entries: [] });
    });
    setSearchQuery('');
    setOpenPool(id);
    setOpenPoolSettings(null);
    setTab('library');
    setLibraryTab('pools');
  };

  const addCue = () => {
    const id = uid('cue');
    mutate('已添加新情境词', (next) => {
      next.cues.push({ id, key: '新情境词', kind: '地点', enabled: true, poolIds: [] });
    });
    setSearchQuery('');
    setOpenCue(id);
    setTab('library');
    setLibraryTab('cues');
  };

  const reorderStage = (id: string, direction: 'up' | 'down') => {
    mutate(direction === 'up' ? '阶段已上移' : '阶段已下移', (next) => {
      const currentIndex = next.stages.findIndex((stage) => stage.id === id);
      const targetIndex = direction === 'up' ? currentIndex - 1 : currentIndex + 1;
      if (currentIndex < 0 || targetIndex < 0 || targetIndex >= next.stages.length) return;
      [next.stages[currentIndex], next.stages[targetIndex]] = [next.stages[targetIndex], next.stages[currentIndex]];
    });
  };

  const requestDelete = (kind: 'stage' | 'pool', id: string) => setDeleteTarget({ kind, id });

  const confirmDelete = () => {
    if (!deleteTarget) return;
    const { kind, id } = deleteTarget;
    mutate(kind === 'stage' ? '阶段已移入删除草稿' : '灵感池已移入删除草稿', (next) => {
      if (kind === 'stage') {
        next.stages = next.stages.filter((stage) => stage.id !== id);
        next.stages.forEach((stage) => { if (stage.nextStageId === id) stage.nextStageId = null; });
      } else {
        next.pools = next.pools.filter((pool) => pool.id !== id);
        next.stages.forEach((stage) => { stage.poolIds = stage.poolIds.filter((poolId) => poolId !== id); });
        next.cues.forEach((cue) => { cue.poolIds = cue.poolIds.filter((poolId) => poolId !== id); });
      }
    });
    setDeleteTarget(null);
    setOpenStage(null);
    setOpenPool(null);
  };

  const beginEntryEdit = (entry: FlowStudioEntry) => {
    setEditingEntry(entry.id);
    setEditingText(entry.text);
  };

  const saveEntryEdit = (poolId: string, entryId: string) => {
    updatePool(poolId, (pool) => {
      const entry = pool.entries.find((item) => item.id === entryId);
      if (entry) entry.text = editingText;
    }, '灵感条目已更新');
    setEditingEntry(null);
    setEditingText('');
  };

  const cancelEntryEdit = (poolId: string, entryId: string) => {
    const pool = draft.pools.find((item) => item.id === poolId);
    const entry = pool?.entries.find((item) => item.id === entryId);
    if (entry && !entry.text.trim()) {
      mutate('已取消空白条目', (next) => {
        const targetPool = next.pools.find((item) => item.id === poolId);
        if (targetPool) targetPool.entries = targetPool.entries.filter((item) => item.id !== entryId);
      });
    }
    setEditingEntry(null);
    setEditingText('');
  };

  const addEntry = (poolId: string) => {
    const id = uid('entry');
    mutate('已添加灵感条目', (next) => {
      const pool = next.pools.find((item) => item.id === poolId);
      pool?.entries.push({ id, text: '', enabled: true });
    });
    setEditingEntry(id);
    setEditingText('');
  };

  const deleteEntry = (poolId: string, entryId: string) => {
    mutate('灵感条目已移入删除草稿', (next) => {
      const pool = next.pools.find((item) => item.id === poolId);
      if (pool) pool.entries = pool.entries.filter((entry) => entry.id !== entryId);
    });
    setEditingEntry(null);
  };

  const searchHits = useMemo(() => {
    const query = searchQuery.trim().toLocaleLowerCase();
    if (!query) return [];
    const hits: Array<{ kind: string; title: string; detail: string; poolId?: string; cueId?: string }> = [];
    draft.pools.forEach((pool) => {
      if (pool.name.toLocaleLowerCase().includes(query)) hits.push({ kind: '灵感池', title: pool.name, detail: `${pool.entries.length} 条灵感`, poolId: pool.id });
      pool.entries.forEach((entry) => {
        if (entry.text.toLocaleLowerCase().includes(query)) hits.push({ kind: '灵感', title: entry.text, detail: `在「${pool.name}」`, poolId: pool.id });
      });
    });
    draft.cues.forEach((cue) => {
      if (cue.key.toLocaleLowerCase().includes(query)) hits.push({ kind: '情境词', title: cue.key, detail: cue.kind, cueId: cue.id });
    });
    return hits.slice(0, 8);
  }, [draft, searchQuery]);

  const filteredPools = useMemo(() => {
    const query = searchQuery.trim().toLocaleLowerCase();
    if (!query) return draft.pools;
    return draft.pools.filter((pool) => pool.name.toLocaleLowerCase().includes(query) || pool.entries.some((entry) => entry.text.toLocaleLowerCase().includes(query)));
  }, [draft.pools, searchQuery]);

  const visibleCues = useMemo(() => {
    const query = searchQuery.trim().toLocaleLowerCase();
    if (!query) return draft.cues;
    return draft.cues.filter((cue) => cue.key.toLocaleLowerCase().includes(query) || cue.kind.toLocaleLowerCase().includes(query));
  }, [draft.cues, searchQuery]);

  const drawResults = useMemo(() => {
    const stage = draft.stages.find((item) => item.id === drawStageId) || draft.stages[0];
    if (!stage) return [];
    const poolIds = new Set(stage.poolIds);
    draft.cues.filter((cue) => drawCueIds.includes(cue.id)).forEach((cue) => cue.poolIds.forEach((id) => poolIds.add(id)));
    return draft.pools
      .filter((pool) => pool.enabled && poolIds.has(pool.id))
      .flatMap((pool) => pool.entries.filter((entry) => entry.enabled && entry.text.trim()).slice(0, pool.count).map((entry) => ({ ...entry, poolName: pool.name, mode: pool.mode === 'perTurn' ? '每轮重抽' : '整段固定' })))
      .slice(0, 5);
  }, [draft, drawCueIds, drawStageId]);

  const drawGuide = useMemo(() => {
    const stage = draft.stages.find((item) => item.id === drawStageId) || draft.stages[0];
    const cueNames = draft.cues.filter((cue) => drawCueIds.includes(cue.id)).map((cue) => cue.key).join('、') || '无';
    return ['LOCAL FLOW GUIDANCE', `Current stage: ${stage?.name || '未选择'}`, `Current stage turn: ${drawTurn}`, `Context cues: ${cueNames}`, 'The next turn should keep the selected creative direction.', 'No model request is made by this local mock.'].join('\n');
  }, [draft, drawCueIds, drawStageId, drawTurn]);

  const statusTitle = draft.enabled ? 'Standby' : '已暂停';
  const statusBody = draft.enabled
    ? '这是一个只改变本地 Mock 草稿的编辑空间。可以放心试错，任何保存都不会触达真实 Flow。'
    : '这份沙盒配置暂时停用。停用只影响当前浏览器里的 Mock 状态，不会改变真实运行总闸。';

  const renderStage = (stage: FlowStudioStage, index: number) => {
    const isOpen = openStage === stage.id;
    const nextOptions = draft.stages.filter((candidate) => candidate.id !== stage.id);
    return (
      <article className={`flow-stage-row ${isOpen ? 'is-open' : ''}`} key={stage.id}>
        <div className="flow-stage-head-row">
          <button type="button" className="flow-stage-head" onClick={() => setOpenStage(isOpen ? null : stage.id)} aria-expanded={isOpen}>
            <span className={`flow-stage-index ${stage.terminal ? 'is-terminal' : ''}`}>{stage.terminal ? '✦' : index + 1}</span>
            <span className="flow-stage-head-copy"><span className="flow-stage-title-line"><strong>{stage.name || '未命名阶段'}</strong>{stage.terminal ? <em>结束阶段</em> : null}</span><span className="flow-stage-subtitle">最低 {stage.minTurns} 轮 · {stage.poolIds.length} 个关联池</span></span>
            {!reorderingStages ? <Icon name="chevron" /> : null}
          </button>
          {reorderingStages ? <div className="flow-stage-reorder" aria-label={`${stage.name || '阶段'}调整顺序`}><button type="button" className="flow-reorder-button" disabled={index === 0} onClick={() => reorderStage(stage.id, 'up')}>↑ 上移</button><button type="button" className="flow-reorder-button" disabled={index === draft.stages.length - 1} onClick={() => reorderStage(stage.id, 'down')}>↓ 下移</button></div> : null}
        </div>
        {isOpen ? (
          <div className="flow-stage-editor">
            <label className="flow-field"><span>名称</span><input value={stage.name} onChange={(event) => updateStage(stage.id, (target) => { target.name = event.target.value; }, '阶段名称已更新')} placeholder="为这一阶段命名" /></label>
            <div className="flow-editor-grid">
              <div className="flow-field"><span>最低轮数 <small>1–99</small></span><div className="flow-stepper"><button type="button" onClick={() => updateStage(stage.id, (target) => { target.minTurns = Math.max(1, target.minTurns - 1); }, '最低轮数已调整')}>−</button><b>{stage.minTurns}</b><button type="button" onClick={() => updateStage(stage.id, (target) => { target.minTurns = Math.min(99, target.minTurns + 1); }, '最低轮数已调整')}>＋</button></div></div>
              <label className="flow-field"><span>推进至</span><select value={stage.nextStageId || ''} disabled={stage.terminal} onChange={(event) => updateStage(stage.id, (target) => { target.nextStageId = event.target.value || null; }, '推进关系已更新')}><option value="">按顺序推进</option>{nextOptions.map((candidate) => <option value={candidate.id} key={candidate.id}>{candidate.name}</option>)}</select></label>
            </div>
            <div className="flow-toggle-row"><div><strong>设为结束阶段</strong><small>完成最低轮数后，沙盒流程在这里收束</small></div><Switch checked={stage.terminal} label={`将${stage.name || '本阶段'}设为结束阶段`} onChange={() => mutate('结束阶段状态已更新', (next) => { next.stages.forEach((candidate) => { candidate.terminal = candidate.id === stage.id ? !stage.terminal : false; }); })} /></div>
            <div className="flow-field"><span>关联灵感池</span><div className="flow-choice-wrap">{draft.pools.map((pool) => { const selected = stage.poolIds.includes(pool.id); return <button type="button" className={`flow-choice flow-stage-pool-choice ${selected ? 'is-selected' : ''}`} key={pool.id} onClick={() => updateStage(stage.id, (target) => { target.poolIds = selected ? target.poolIds.filter((id) => id !== pool.id) : [...target.poolIds, pool.id]; }, '阶段关联已更新')}><span className="flow-choice-symbol">{selected ? '✓' : '+'}</span>{pool.name}</button>; })}</div></div>
            <div className="flow-row-actions"><button type="button" className="flow-soft-button is-danger" onClick={() => requestDelete('stage', stage.id)}>删除阶段</button></div>
          </div>
        ) : null}
      </article>
    );
  };

  const renderEntry = (pool: FlowStudioPool, entry: FlowStudioEntry, index: number) => {
    const isEditing = editingEntry === entry.id;
    if (isEditing) {
      const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') saveEntryEdit(pool.id, entry.id); };
      return <div className="flow-entry-edit" key={entry.id}><span className={`flow-entry-dot ${entry.enabled ? 'is-on' : ''}`} /><textarea autoFocus value={editingText} onChange={(event) => setEditingText(event.target.value)} onKeyDown={onKeyDown} rows={2} placeholder="写一条只给下一轮模型看的指引" /><div className="flow-entry-edit-actions"><button type="button" className="flow-icon-button" aria-label="取消编辑" onClick={() => cancelEntryEdit(pool.id, entry.id)}><Icon name="close" /></button><button type="button" className="flow-icon-button is-primary" aria-label="保存条目" onClick={() => saveEntryEdit(pool.id, entry.id)}>✓</button></div></div>;
    }
    return <div className={`flow-entry-row ${entry.enabled ? '' : 'is-disabled'}`} key={entry.id}><button type="button" className={`flow-entry-toggle ${entry.enabled ? 'is-on' : ''}`} aria-label={entry.enabled ? '停用条目' : '启用条目'} onClick={() => updatePool(pool.id, (target) => { const item = target.entries.find((candidate) => candidate.id === entry.id); if (item) item.enabled = !item.enabled; }, entry.enabled ? '条目已停用' : '条目已启用')}><span /></button><div className="flow-entry-copy"><span>{entry.text || '空白条目'}</span>{entry.enabled ? null : <small>· 已停用</small>}</div><div className="flow-entry-actions" onClick={() => beginEntryEdit(entry)}><button type="button" className="flow-icon-button flow-entry-edit-button" aria-label={`编辑第${index + 1}条灵感`} onClick={() => beginEntryEdit(entry)}><Icon name="edit" /></button><button type="button" className="flow-entry-delete" aria-label="删除条目" onClick={(event) => { event.stopPropagation(); deleteEntry(pool.id, entry.id); }}>删除</button></div></div>;
  };

  const renderPool = (pool: FlowStudioPool) => {
    const isOpen = openPool === pool.id;
    const settingsOpen = openPoolSettings === pool.id;
    const activeCount = pool.entries.filter((entry) => entry.enabled).length;
    return (
      <article className={`flow-pool-card ${isOpen ? 'is-open' : ''} ${pool.enabled ? '' : 'is-disabled'}`} key={pool.id}>
        <div className="flow-pool-head"><button type="button" className="flow-pool-toggle" onClick={() => setOpenPool(isOpen ? null : pool.id)} aria-expanded={isOpen}><span className="flow-pool-icon"><Icon name={pool.id === 'pool-b' ? 'book' : 'spark'} /></span><span className="flow-pool-copy"><strong>{pool.name || '未命名灵感池'}</strong><small>{pool.enabled ? `${activeCount} 条启用 · ${pool.mode === 'perTurn' ? '每轮抽取' : '整段固定'} ${pool.count} 条` : `已停用 · ${pool.entries.length} 条灵感`}</small></span><Icon name="chevron" /></button><Switch checked={pool.enabled} label={`启用${pool.name || '灵感池'}`} onChange={() => updatePool(pool.id, (target) => { target.enabled = !target.enabled; }, pool.enabled ? '灵感池已停用' : '灵感池已启用')} /></div>
        {isOpen ? (
          <div className="flow-pool-body">
            <div className="flow-entry-list">{pool.entries.map((entry, index) => renderEntry(pool, entry, index))}</div>
            <button type="button" className="flow-add-entry-row" onClick={() => addEntry(pool.id)}><Icon name="plus" />添加一条灵感</button>
            <button type="button" className="flow-pool-settings-toggle" aria-expanded={settingsOpen} onClick={() => setOpenPoolSettings(settingsOpen ? null : pool.id)}><span><Icon name="adjust" />抽取设置</span><span><small>{pool.mode === 'perTurn' ? '每轮重抽' : '整段固定'} · {pool.count} 条</small><Icon name="chevron" /></span></button>
            {settingsOpen ? <div className="flow-settings-panel"><div className="flow-settings-row"><div><strong>抽取方式</strong><small>这一池如何参与生成</small></div><div className="flow-segment"><button type="button" className={pool.mode === 'perTurn' ? 'is-active' : ''} onClick={() => updatePool(pool.id, (target) => { target.mode = 'perTurn'; }, '抽取方式已改为每轮重抽')}>每轮重抽</button><button type="button" className={pool.mode === 'perStage' ? 'is-active' : ''} onClick={() => updatePool(pool.id, (target) => { target.mode = 'perStage'; }, '抽取方式已改为整段固定')}>整段固定</button></div></div><div className="flow-settings-row"><div><strong>每次抽取</strong><small>可选 1–5 条</small></div><div className="flow-stepper is-compact"><button type="button" onClick={() => updatePool(pool.id, (target) => { target.count = Math.max(1, target.count - 1); }, '抽取数量已调整')}>−</button><b>{pool.count}</b><button type="button" onClick={() => updatePool(pool.id, (target) => { target.count = Math.min(5, target.count + 1); }, '抽取数量已调整')}>＋</button></div></div><div className="flow-pool-delete-row"><span>管理这组灵感</span><button type="button" className="flow-text-button is-danger" onClick={() => requestDelete('pool', pool.id)}>删除灵感池</button></div></div> : null}
          </div>
        ) : null}
      </article>
    );
  };

  const renderCue = (cue: FlowStudioCue) => {
    const isOpen = openCue === cue.id;
    const poolNames = cue.poolIds.map((poolId) => draft.pools.find((pool) => pool.id === poolId)?.name).filter(Boolean).join('、');
    const summary = `${cue.kind} · ${poolNames ? `追加 ${poolNames}` : '未关联灵感池'}${cue.enabled ? '' : ' · 已停用'}`;
    return (
      <article className={`flow-cue-card ${isOpen ? 'is-open' : ''} ${cue.enabled ? '' : 'is-disabled'}`} key={cue.id}>
        <button type="button" className="flow-cue-head" onClick={() => setOpenCue(isOpen ? null : cue.id)} aria-expanded={isOpen}>
          <span className="flow-cue-dot"><span /></span>
          <span className="flow-cue-copy"><strong>{cue.key || '未命名情境词'}</strong><small>{summary}</small></span>
          <Icon name="chevron" />
        </button>
        {isOpen ? (
          <div className="flow-cue-editor">
            <div className="flow-cue-name-row">
              <label className="flow-field flow-cue-name-field"><span>名称</span><input value={cue.key} onChange={(event) => updateCue(cue.id, (target) => { target.key = event.target.value; }, '情境词名称已更新')} placeholder="为这个情境词命名" /></label>
              <div className="flow-cue-enable"><span>启用</span><Switch checked={cue.enabled} label={'启用' + (cue.key || '情境词')} onChange={() => updateCue(cue.id, (target) => { target.enabled = !target.enabled; }, cue.enabled ? '情境词已停用' : '情境词已启用')} /></div>
              <button type="button" className="flow-cue-delete-button" aria-label="删除情境词" title="删除情境词" onClick={() => mutate('情境词已移入删除草稿', (next) => { next.cues = next.cues.filter((item) => item.id !== cue.id); })}><Icon name="trash" /></button>
            </div>
            <div className="flow-cue-kind-tabs">{(['地点', '氛围', '其他'] as const).map((kind) => <button type="button" className={cue.kind === kind ? 'is-active' : ''} key={kind} onClick={() => updateCue(cue.id, (target) => { target.kind = kind; }, '情境词类型已更新')}>{kind}</button>)}</div>
            <div className="flow-cue-field-label">关联灵感池</div>
            <div className="flow-choice-wrap flow-cue-pool-choices">{draft.pools.map((pool) => { const selected = cue.poolIds.includes(pool.id); return <button type="button" className={'flow-choice flow-stage-pool-choice flow-cue-pool-choice ' + (selected ? 'is-selected' : '')} key={pool.id} onClick={() => updateCue(cue.id, (target) => { target.poolIds = selected ? target.poolIds.filter((id) => id !== pool.id) : [...target.poolIds, pool.id]; }, '情境词关联已更新')}><span className="flow-choice-symbol">{selected ? '✓' : '+'}</span>{pool.name}</button>; })}</div>
          </div>
        ) : null}
      </article>
    );
  };

  const statusBar = dirty ? <div className="flow-save-bar"><span className="flow-save-dot" /><span className="flow-save-copy">有未保存的沙盒修改</span><button type="button" className="flow-save-link" onClick={undo} disabled={!history.length}><Icon name="undo" />撤销</button><button type="button" className="flow-save-link" onClick={discardDraft}>取消</button><button type="button" className="flow-save-button" onClick={saveDraft} disabled={saving}>{saving ? '保存中…' : '保存模拟数据'}</button></div> : null;

  return (
    <main className="flow-studio-page">
      <div className="flow-studio-glow flow-studio-glow-left" /><div className="flow-studio-glow flow-studio-glow-right" />
      <div className="flow-studio-content">
        <header className="flow-studio-header"><button type="button" className="flow-back-button" aria-label="返回" onClick={() => navigate(backPath)}>‹</button><div className="flow-studio-title"><div className="flow-title-row"><h1>Flow Studio</h1></div><div className="flow-save-state"><span className={`flow-status-dot ${dirty ? 'is-dirty' : ''}`} />{surfaceLabel} · {dirty ? '有未保存修改' : `已保存 · v${draft.version}`}</div></div></header>
        <div className="flow-tabbar" role="tablist" aria-label="Flow Studio 页面">{([['overview', '概览'], ['stages', '阶段'], ['library', '灵感库']] as Array<[FlowTab, string]>).map(([key, label]) => <button type="button" role="tab" aria-selected={tab === key} className={tab === key ? 'is-active' : ''} onClick={() => setTab(key)} key={key}>{label}</button>)}</div>

        {tab === 'overview' ? (
          <section className="flow-screen flow-overview" aria-label="概览">
            <article className="flow-status-card"><div className="flow-status-card-main"><div className="flow-kicker">CURRENT MOCK STATE</div><div className="flow-status-title"><h2>{statusTitle}</h2><span /></div><p>{statusBody}</p><div className="flow-status-chips"><span className="is-rose">本地 Mock</span><span>不调用模型</span><span>不写入后端</span></div></div><div className="flow-status-meta"><span>Draft v{draft.version}</span><span>{modeLabel}</span></div></article>
            <article className="flow-control-card"><div className="flow-control-row"><div><strong>运行总闸</strong><small>真实服务器级开关 · 本页只读</small></div><span className="flow-readonly-pill">生产隔离</span></div><div className="flow-control-row"><div><strong>启用这份沙盒配置</strong><small>{draft.enabled ? '仅改变当前 Mock 状态，关闭不会删除内容' : '已停用，保存后仍只留在沙盒内'}</small></div><Switch checked={draft.enabled} label="启用沙盒配置" onChange={() => mutate('沙盒启用状态已更新', (next) => { next.enabled = !next.enabled; })} /></div><div className="flow-control-row"><div><strong>会话记录</strong><small>没有连接真实聊天，不会产生运行记录</small></div><span className="flow-muted-value">无</span></div></article>
            <div className="flow-section-heading"><span>流程 · {draft.stages.length} 个阶段</span><button type="button" onClick={() => setTab('stages')}>编辑 ›</button></div>
            <article className="flow-stage-chain"><div className="flow-chain-line" />{draft.stages.map((stage, index) => <button type="button" className="flow-chain-node" key={stage.id} onClick={() => { setTab('stages'); setOpenStage(stage.id); }}><span className={stage.terminal ? 'is-terminal' : ''}>{stage.terminal ? '✦' : index + 1}</span><b>{stage.name || '未命名'}</b></button>)}<p>每个阶段默认停留；达到最低轮数后，也要明确推进才会前进。</p></article>
            <div className="flow-action-grid"><button type="button" className="flow-action-card" onClick={() => { setShowDraw(true); setDrawn(false); }}><span className="flow-action-icon is-rose"><Icon name="spark" /></span><strong>试抽一次</strong><small>本地模拟抽取 · 不调用模型</small></button><button type="button" className="flow-action-card" onClick={() => { setTab('library'); setLibraryTab('pools'); }}><span className="flow-action-icon is-violet"><Icon name="book" /></span><strong>灵感库</strong><small>{draft.pools.length} 池 · {enabledEntries} 条启用</small></button></div>
            <div className="flow-section-heading flow-dimension-heading"><span>未来创作维度</span><small>先做视觉结构，后续再接数据源</small></div><div className="flow-dimension-grid">{draft.dimensions.map((dimension) => <article className={`flow-dimension-card is-${dimension.tone}`} key={dimension.id}><span>{dimension.label.slice(0, 1)}</span><div><strong>{dimension.label}</strong><small>{dimension.detail}</small></div><em>设计中</em></article>)}</div>
          </section>
        ) : null}

        {tab === 'stages' ? <section className="flow-screen" aria-label="阶段"><div className="flow-screen-intro"><span>点击阶段展开编辑。流程从上往下进行。</span><button type="button" className={`flow-text-button ${reorderingStages ? 'is-active' : ''}`} onClick={() => setReorderingStages((value) => !value)}>{reorderingStages ? '完成排序' : '调整顺序'}</button></div><div className="flow-stage-list">{draft.stages.map(renderStage)}</div><button type="button" className="flow-add-row" onClick={addStage}><Icon name="plus" />新增阶段</button></section> : null}

        {tab === 'library' ? (
          <section className="flow-screen" aria-label="灵感库">
            <div className="flow-library-tabs" role="tablist" aria-label="灵感库分类"><button type="button" className={libraryTab === 'pools' ? 'is-active' : ''} onClick={() => setLibraryTab('pools')}>灵感池 <b>{draft.pools.length}</b></button><button type="button" className={libraryTab === 'cues' ? 'is-active' : ''} onClick={() => setLibraryTab('cues')}>情境词 <b>{draft.cues.length}</b></button></div>
            <label className="flow-search"><Icon name="search" /><input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="搜索灵感池、灵感或情境词" /><button type="button" aria-label="清除搜索" onClick={() => setSearchQuery('')}><Icon name="close" /></button></label>
            {searchQuery.trim() ? <div className="flow-search-summary">找到 {searchHits.length} 个相关结果</div> : null}
            {searchQuery.trim() && searchHits.length ? <div className="flow-search-results">{searchHits.map((hit, index) => <button type="button" key={`${hit.kind}-${hit.title}-${index}`} onClick={() => {
              setSearchQuery('');
              if (hit.poolId) {
                setLibraryTab('pools');
                setOpenPool(hit.poolId);
              } else if (hit.cueId) {
                setLibraryTab('cues');
                setOpenCue(hit.cueId);
              }
            }}><span>{hit.kind}</span><strong>{hit.title}</strong><small>{hit.detail}</small></button>)}</div> : null}
            {libraryTab === 'pools' ? (
              <>
                <div className="flow-pool-list">{filteredPools.map(renderPool)}</div>
                {!filteredPools.length ? <div className="flow-empty">没有匹配的灵感池。换一个关键词试试。</div> : null}
                <button type="button" className="flow-add-row" onClick={addPool}><Icon name="plus" />新增灵感池</button>
              </>
            ) : (
              <>
                <div className="flow-library-heading flow-cue-heading"><div><small>情境词是场景线索，出现时追加相关的灵感池，不会单独开始流程。</small></div></div>
                <div className="flow-cue-list">{visibleCues.map(renderCue)}</div>
                <button type="button" className="flow-add-row" onClick={addCue}><Icon name="plus" />新增情境词</button>
              </>
            )}
          </section>
        ) : null}

        {deleteTarget ? <div className="flow-inline-confirm"><span>确定将这个{deleteTarget.kind === 'stage' ? '阶段' : '灵感池'}移入删除草稿吗？保存前仍可撤销。</span><button type="button" onClick={() => setDeleteTarget(null)}>取消</button><button type="button" className="is-danger" onClick={confirmDelete}>确认删除</button></div> : null}
        {statusBar}
      </div>

      {toast ? <div className="flow-toast" role="status">{toast}</div> : null}
      {showDraw ? <div className="flow-draw-layer"><button type="button" className="flow-draw-backdrop" aria-label="关闭试抽" onClick={() => setShowDraw(false)} /><section className="flow-draw-sheet" role="dialog" aria-modal="true" aria-labelledby="flow-draw-title"><div className="flow-sheet-handle" /><header className="flow-sheet-header"><div><h2 id="flow-draw-title">试抽一次</h2><p>不调用模型 · 不改变聊天 · 使用未保存草稿</p></div><button type="button" className="flow-close-button" onClick={() => setShowDraw(false)}><Icon name="close" /></button></header><div className="flow-sheet-scroll"><div className="flow-sheet-field"><span>阶段</span><div className="flow-choice-wrap">{draft.stages.map((stage) => <button type="button" className={`flow-choice ${drawStageId === stage.id ? 'is-selected' : ''}`} key={stage.id} onClick={() => { setDrawStageId(stage.id); setDrawn(false); }}>{stage.name}</button>)}</div></div><div className="flow-sheet-field"><span>情境词 <small>最多 4 个</small></span><div className="flow-choice-wrap">{draft.cues.filter((cue) => cue.enabled).map((cue) => { const selected = drawCueIds.includes(cue.id); return <button type="button" className={`flow-choice ${selected ? 'is-selected' : ''}`} key={cue.id} onClick={() => { setDrawCueIds((current) => selected ? current.filter((id) => id !== cue.id) : current.length < 4 ? [...current, cue.id] : current); setDrawn(false); }}>{selected ? '✓ ' : ''}{cue.key}</button>; })}</div></div><div className="flow-turn-row"><span>假设本阶段第几轮</span><div className="flow-stepper is-compact"><button type="button" onClick={() => setDrawTurn((value) => Math.max(1, value - 1))}>−</button><b>{drawTurn}</b><button type="button" onClick={() => setDrawTurn((value) => value + 1)}>＋</button></div></div>{drawn ? <div className="flow-draw-result"><span className="flow-result-kicker">LOCAL MOCK DRAW · {drawResults.length} 条</span><div className="flow-result-list">{drawResults.map((entry) => <div className="flow-result-row" key={entry.id}><span /><div><strong>{entry.text}</strong><small>{entry.poolName} · {entry.mode}</small></div></div>)}</div>{!drawResults.length ? <p>当前选择没有可用的本地灵感。</p> : null}<button type="button" className="flow-guide-toggle" onClick={() => setShowGuide((value) => !value)}>查看本地模拟引导 <Icon name="chevron" /></button>{showGuide ? <pre className="flow-guide">{drawGuide}</pre> : null}</div> : null}</div><footer><button type="button" className="flow-run-button" onClick={() => { setDrawn(true); setShowGuide(false); }}>{drawn ? '重新本地试抽' : '开始本地试抽'}</button></footer></section></div> : null}
    </main>
  );
}
