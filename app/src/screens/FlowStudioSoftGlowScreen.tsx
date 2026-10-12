import { createFlowStudioMockAdapter } from '../lib/flowStudio/flowStudioMock';
import { FlowStudioWorkspace } from './FlowStudioWorkspace';

const previewMockAdapter = createFlowStudioMockAdapter('preview');

/**
 * Preview-only entry point for the reviewed Soft Glow experiment.
 * The workspace is shared, but this route and Mock scope remain Preview-only.
 */
export function FlowStudioSoftGlowScreen() {
  return (
    <FlowStudioWorkspace
      dataAdapter={previewMockAdapter}
      backPath="/dash/chat"
      surfaceLabel="Flow Studio · 视觉预览"
      persistenceLabel="已保存（预览）"
    />
  );
}
