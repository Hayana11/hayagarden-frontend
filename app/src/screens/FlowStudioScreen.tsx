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
      surfaceLabel="服务器配置"
      modeLabel="正式页面"
      persistenceLabel="已保存并应用到 Hidden Flow"
      storageKicker="CURRENT SERVER STATE"
    />
  );
}
