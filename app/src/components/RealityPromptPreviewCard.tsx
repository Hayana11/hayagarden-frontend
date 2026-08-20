import { useSyncExternalStore } from 'react';
import type {
  CompiledRealityPrompt,
  RealityPromptSegment,
} from '../lib/reality/realityContextCompiler';
import { realityPromptProjection } from '../lib/reality/realityPromptProjection';

function renderSegment(segment: RealityPromptSegment, index: number) {
  if (segment.kind === 'dynamic') {
    return <strong key={`${segment.key}-${index}`}>{segment.text}</strong>;
  }

  return <span key={`literal-${index}`}>{segment.text}</span>;
}

export function RealityPromptPreview({ prompt }: { prompt: CompiledRealityPrompt }) {
  return (
    <section className="config-card config-current reality-prompt-preview" aria-label="现实 Prompt 预览">
      <div className="config-card-heading">
        <h2>现实 Prompt 预览</h2>
        <span className="config-current-badge">尚未接入 Chat</span>
      </div>
      {prompt.text === ''
        ? <p className="reality-prompt-empty">暂无可用的设备现实状态</p>
        : <p className="reality-prompt-text" aria-live="polite">{prompt.segments.map(renderSegment)}</p>}
      <small>仅在语义状态变化时更新</small>
    </section>
  );
}

export function RealityPromptPreviewCard() {
  const prompt = useSyncExternalStore(
    (listener) => realityPromptProjection.subscribe(listener),
    () => realityPromptProjection.getSnapshot(),
    () => realityPromptProjection.getSnapshot(),
  );

  return <RealityPromptPreview prompt={prompt} />;
}
