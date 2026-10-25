import { http } from './http';

export const DISPLAY_THINKING_PROMPT_MAX_CHARS = 12000;
export const SELF_STATE_OPEN_TAG = '[[自我状态]]';
export const SELF_STATE_CLOSE_TAG = '[[/自我状态]]';
export const LEGACY_SELF_STATE_OPEN_TAG = '<思绪>';
export const LEGACY_SELF_STATE_CLOSE_TAG = '</思绪>';

// Compatibility aliases for the existing prompt settings surface.
export const DISPLAY_THINKING_OPEN_TAG = SELF_STATE_OPEN_TAG;
export const DISPLAY_THINKING_CLOSE_TAG = SELF_STATE_CLOSE_TAG;

export type DisplayThinkingPromptResponse = {
  ok?: boolean;
  prompt?: string;
  default_prompt?: string;
  overridden?: boolean;
  error?: string;
};

export function validateDisplayThinkingPrompt(value: string): string | null {
  if (typeof value !== 'string' || !value.trim()) return '可见自我状态 prompt 不能为空';
  if (Array.from(value.trim()).length > DISPLAY_THINKING_PROMPT_MAX_CHARS) {
    return '可见自我状态 prompt 不能超过 '
      + DISPLAY_THINKING_PROMPT_MAX_CHARS + ' 个字符';
  }
  const hasNew = value.includes(SELF_STATE_OPEN_TAG) && value.includes(SELF_STATE_CLOSE_TAG);
  const hasLegacy = value.includes(LEGACY_SELF_STATE_OPEN_TAG)
    && value.includes(LEGACY_SELF_STATE_CLOSE_TAG);
  if (!hasNew && !hasLegacy) {
    return '可见自我状态 prompt 必须包含 [[自我状态]]/[[/自我状态]] 或旧 <思绪>/</思绪>';
  }
  const openTag = hasNew ? SELF_STATE_OPEN_TAG : LEGACY_SELF_STATE_OPEN_TAG;
  const closeTag = hasNew ? SELF_STATE_CLOSE_TAG : LEGACY_SELF_STATE_CLOSE_TAG;
  if (value.indexOf(closeTag) < value.indexOf(openTag)) {
    return '可见自我状态 prompt 的结束标签必须位于开始标签之后';
  }
  return null;
}

export function fetchDisplayThinkingPrompt(): Promise<DisplayThinkingPromptResponse> {
  return http.get<DisplayThinkingPromptResponse>('/api/profile/display-thinking-prompt');
}

export function saveDisplayThinkingPrompt(prompt: string): Promise<DisplayThinkingPromptResponse> {
  return http.put<DisplayThinkingPromptResponse>('/api/profile/display-thinking-prompt', { prompt });
}

export function resetDisplayThinkingPrompt(): Promise<DisplayThinkingPromptResponse> {
  return http.del<DisplayThinkingPromptResponse>('/api/profile/display-thinking-prompt');
}
