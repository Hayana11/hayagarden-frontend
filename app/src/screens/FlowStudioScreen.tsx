import { createFlowStudioApiAdapter } from '../lib/flowStudio/flowStudioApi';
import { FlowStudioWorkspace } from './FlowStudioWorkspace';

const productionApiAdapter = createFlowStudioApiAdapter('intimacy-v1');

/**
 * Formal Flow Studio entry point.
 *
 * The formal editor uses the owner-authenticated server document API. Preview
 * keeps its separate Mock Adapter and never reaches this adapter.
 */
export function FlowStudioScreen() {
  return (
    <FlowStudioWorkspace
      dataAdapter={productionApiAdapter}
      backPath="/chat"
      surfaceLabel="正式编辑器 · 服务器草稿"
      modeLabel="正式页面 · 未发布"
      persistenceLabel="已保存至服务器"
      storageKicker="CURRENT SERVER STATE"
    />
  );
}
