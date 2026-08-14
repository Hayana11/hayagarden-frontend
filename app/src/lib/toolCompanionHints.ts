import { http } from './http';

export type ToolCompanionTool = {
  capability_id: string;
  display_label: string;
  companion_hint: string;
  default_display_label: string;
  default_companion_hint: string;
  physical_boundary: string;
  status_label: string;
};

export type ToolCompanionGroup = {
  id: string;
  label: string;
  tools: ToolCompanionTool[];
};

export type ToolCompanionHints = {
  ok: boolean;
  version: string;
  groups: ToolCompanionGroup[];
  prompt_preview: string;
  apply_mode: string;
  transparency: string;
  trial_mode: string;
  error?: string;
};

export function fetchToolCompanionHints(): Promise<ToolCompanionHints> {
  return http.get<ToolCompanionHints>('/api/tools/companion-hints');
}

export function patchToolCompanionHint(payload: {
  capability_id: string;
  display_label?: string;
  companion_hint?: string;
  reset?: boolean;
}): Promise<ToolCompanionHints> {
  return http.patch<ToolCompanionHints>('/api/tools/companion-hints', payload);
}
