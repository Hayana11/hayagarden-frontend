export type PoolMode = 'perTurn' | 'perStage';

export type FlowStudioEntry = {
  id: string;
  text: string;
  enabled: boolean;
};

export type FlowStudioPool = {
  id: string;
  name: string;
  enabled: boolean;
  mode: PoolMode;
  count: number;
  entries: FlowStudioEntry[];
};

export type FlowStudioStage = {
  id: string;
  name: string;
  minTurns: number;
  terminal: boolean;
  poolIds: string[];
  nextStageId: string | null;
};

export type FlowStudioCue = {
  id: string;
  key: string;
  kind: '地点' | '氛围' | '动作' | '其他';
  enabled: boolean;
  poolIds: string[];
};

export type FlowStudioData = {
  version: number;
  savedAt: string;
  enabled: boolean;
  stages: FlowStudioStage[];
  pools: FlowStudioPool[];
  cues: FlowStudioCue[];
  dimensions: Array<{ id: string; label: string; detail: string; tone: 'rose' | 'violet' | 'green' }>;
};

/**
 * View-model adapter seam for the future real source.
 * A future API adapter can normalize async/server data before handing the page
 * this same front-end shape; no API, database, or runtime state belongs here.
 */
export type FlowStudioDataAdapter = {
  load(): FlowStudioData;
  save(next: FlowStudioData): FlowStudioData;
};

export type FlowStudioMockScope = 'preview' | 'production' | 'standalone';

export type FlowStudioMockAdapter = FlowStudioDataAdapter & {
  readonly scope: FlowStudioMockScope;
};

export function cloneFlowStudioData<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

const MOCK_DATA: FlowStudioData = {
  version: 7,
  savedAt: '10/08 13:42',
  enabled: true,
  dimensions: [
    { id: 'speed', label: '速度', detail: '快慢与停顿', tone: 'rose' },
    { id: 'posture', label: '姿势', detail: '靠近与退后', tone: 'violet' },
    { id: 'process', label: '过程', detail: '进入与收束', tone: 'green' },
  ],
  stages: [
    { id: 'stage-1', name: '试探', minTurns: 1, terminal: false, poolIds: ['pool-a'], nextStageId: null },
    { id: 'stage-2', name: '靠近', minTurns: 3, terminal: false, poolIds: ['pool-a', 'pool-b'], nextStageId: null },
    { id: 'stage-3', name: '渐深', minTurns: 3, terminal: false, poolIds: ['pool-b', 'pool-c'], nextStageId: null },
    { id: 'stage-4', name: '临界', minTurns: 2, terminal: false, poolIds: ['pool-b', 'pool-c'], nextStageId: null },
    { id: 'stage-5', name: '终章', minTurns: 1, terminal: true, poolIds: ['pool-d'], nextStageId: null },
  ],
  pools: [
    {
      id: 'pool-a',
      name: '氛围与环境',
      enabled: true,
      mode: 'perTurn',
      count: 2,
      entries: [
        { id: 'entry-a1', text: '让环境里的一个细节参与进来：光线、温度或声音', enabled: true },
        { id: 'entry-a2', text: '放慢叙述，用一句很短的句子停顿', enabled: true },
        { id: 'entry-a3', text: '描写一次呼吸节奏的变化', enabled: true },
        { id: 'entry-a4', text: '让窗外的天气轻轻映照情绪', enabled: true },
        { id: 'entry-a5', text: '以一件随手的物件作为过渡', enabled: false },
      ],
    },
    {
      id: 'pool-b',
      name: '语言与回应',
      enabled: true,
      mode: 'perTurn',
      count: 1,
      entries: [
        { id: 'entry-b1', text: '用一句低声的确认代替陈述', enabled: true },
        { id: 'entry-b2', text: '回应对方上一句话里的某个词', enabled: true },
        { id: 'entry-b3', text: '留一个没有说完的句子', enabled: true },
        { id: 'entry-b4', text: '以提问把主动权交还给对方', enabled: true },
      ],
    },
    {
      id: 'pool-c',
      name: '整段基调',
      enabled: true,
      mode: 'perStage',
      count: 1,
      entries: [
        { id: 'entry-c1', text: '基调：温柔而克制', enabled: true },
        { id: 'entry-c2', text: '基调：带一点玩笑的亲昵', enabled: true },
        { id: 'entry-c3', text: '基调：安静，像深夜的长谈', enabled: true },
      ],
    },
    {
      id: 'pool-d',
      name: '收束',
      enabled: true,
      mode: 'perTurn',
      count: 1,
      entries: [
        { id: 'entry-d1', text: '把注意力落回彼此的名字', enabled: true },
        { id: 'entry-d2', text: '以一个安静的细节收尾', enabled: true },
      ],
    },
  ],
  cues: [
    { id: 'cue-1', key: '雨夜', kind: '地点', enabled: true, poolIds: ['pool-a'] },
    { id: 'cue-2', key: '书房', kind: '地点', enabled: true, poolIds: ['pool-a'] },
    { id: 'cue-3', key: '晚安', kind: '氛围', enabled: true, poolIds: ['pool-c'] },
    { id: 'cue-4', key: '旧照片', kind: '其他', enabled: true, poolIds: ['pool-b'] },
    { id: 'cue-5', key: '海边', kind: '地点', enabled: false, poolIds: ['pool-a'] },
  ],
};

export function createFlowStudioMockAdapter(scope: FlowStudioMockScope = 'standalone'): FlowStudioMockAdapter {
  let saved = cloneFlowStudioData(MOCK_DATA);
  return {
    scope,
    load: () => cloneFlowStudioData(saved),
    save: (next) => {
      saved = cloneFlowStudioData(next);
      return cloneFlowStudioData(saved);
    },
  };
}
