import { http } from './http';

export type ToolCompanionItem = {
  capability_id: string;
  display_label: string;
  companion_hint: string;
  default_display_label: string;
  default_companion_hint: string;
  physical_boundary: string;
  status_label: string;
  kind: 'read' | 'write';
};

export type ToolCompanionGroup = {
  id: string;
  label: string;
  items: ToolCompanionItem[];
};

export type ToolCompanionHints = {
  ok: boolean;
  version: string;
  groups: ToolCompanionGroup[];
  prompt_preview: string;
  apply_mode: string;
  transparency: string;
  trial_mode: string;
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
