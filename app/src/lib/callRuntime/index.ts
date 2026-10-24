// Which call runtime this page uses. Only the WebView runtime exists today; the native
// call engine is selected here by capability once it lands (P2H-3E). Never assume native.
import type { CallRuntime, CallRuntimeKind } from './types';
import { useWebCallRuntime } from './useWebCallRuntime';

export type { CallRuntime, CallRuntimeKind, SpeechStatus } from './types';

export function callRuntimeKind(): CallRuntimeKind {
  return 'web';
}

export function useCallRuntime(): CallRuntime {
  return useWebCallRuntime();
}
