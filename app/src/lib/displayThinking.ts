import { http } from './http';

export const DISPLAY_THINKING_PROMPT_MAX_CHARS = 12000;
export const DISPLAY_THINKING_OPEN_TAG = '<思绪>';
export const DISPLAY_THINKING_CLOSE_TAG = '</思绪>';

export type DisplayThinkingPromptResponse = {
  ok?: boolean;
  prompt?: string;
  default_prompt?: string;
  overridden?: boolean;
  error?: string;
};

export function validateDisplayThinkingPrompt(value: string): string | null {
  if (typeof value !== 'string' || !value.trim()) return '可见思绪 prompt 不能为空';
  if (Array.from(value.trim()).length > DISPLAY_THINKING_PROMPT_MAX_CHARS) {
    return '可见思绪 prompt 不能超过 '
      + DISPLAY_THINKING_PROMPT_MAX_CHARS + ' 个字符';
  }
  if (!value.includes(DISPLAY_THINKING_OPEN_TAG) || !value.includes(DISPLAY_THINKING_CLOSE_TAG)) {
    return '可见思绪 prompt 必须同时包含 <思绪> 和 </思绪>';
  }
  if (value.indexOf(DISPLAY_THINKING_CLOSE_TAG) < value.indexOf(DISPLAY_THINKING_OPEN_TAG)) {
    return '可见思绪 prompt 中 </思绪> 必须位于 <思绪> 之后';
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
