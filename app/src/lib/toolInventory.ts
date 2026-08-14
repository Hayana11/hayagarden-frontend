import { http } from './http';

export type ToolInventoryTool = {
  tool_name: string; display_label: string; available: boolean; status_label: string; reason_code: string; provider: string | null;
};
export type ToolInventoryGroup = {
  id: string; label: string; total: number; available: number; tools: ToolInventoryTool[];
};
export type ToolInventoryResponse = {
  ok: boolean; version: string; total: number; available_count: number; unavailable_count: number; groups: ToolInventoryGroup[]; error?: string;
};
export function fetchToolInventory(): Promise<ToolInventoryResponse> {
  return http.get<ToolInventoryResponse>('/api/tools/inventory');
}
