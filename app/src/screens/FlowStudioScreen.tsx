import { createFlowStudioMockAdapter } from '../lib/flowStudio/flowStudioMock';
import { FlowStudioWorkspace } from './FlowStudioWorkspace';

const productionMockAdapter = createFlowStudioMockAdapter('production');

/**
 * Formal Flow Studio entry point.
 *
 * This wrapper intentionally owns a separate Mock Adapter instance from the
 * Preview entry point. Replacing this adapter later must not change the
 * reviewed Soft Glow visual shell or the Preview route.
 */
export function FlowStudioScreen() {
  return (
    <FlowStudioWorkspace
      dataAdapter={productionMockAdapter}
      backPath="/chat"
      surfaceLabel="正式编辑器 · 本地 Mock"
      modeLabel="正式页面 · Mock 数据"
    />
  );
}
