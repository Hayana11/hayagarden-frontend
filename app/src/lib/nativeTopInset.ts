export type NativeTopInsetPayload = {
  schemaVersion?: unknown;
  available?: unknown;
  edgeToEdgeTop?: unknown;
  topInsetPx?: unknown;
  density?: unknown;
  topInsetCssPx?: unknown;
};

type NativeTopInsetBridge = {
  getTopInset?: () => unknown;
};

export type NativeTopInsetRuntime = {
  ElpisInsets?: NativeTopInsetBridge;
  document?: {
    documentElement?: {
      style?: {
        setProperty: (property: string, value: string) => void;
      };
      setAttribute?: (name: string, value: string) => void;
    };
  };
};

declare global {
  interface Window {
    ElpisInsets?: NativeTopInsetBridge;
  }
}

const attemptedRoots = new WeakSet<object>();
const MAX_REASONABLE_TOP_INSET_CSS_PX = 200;

type ValidNativeTopInsetPayload = NativeTopInsetPayload & {
  schemaVersion: 1;
  available: true;
  edgeToEdgeTop: true;
  topInsetPx: number;
  density: number;
  topInsetCssPx: number;
};

function parsePayload(raw: unknown): NativeTopInsetPayload | null {
  if (typeof raw === 'string') {
    try {
      raw = JSON.parse(raw);
    } catch {
      return null;
    }
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  return raw as NativeTopInsetPayload;
}

function isValidPayload(payload: NativeTopInsetPayload | null): payload is ValidNativeTopInsetPayload {
  if (!payload || payload.schemaVersion !== 1 || payload.available !== true || payload.edgeToEdgeTop !== true) {
    return false;
  }

  const values = [payload.topInsetPx, payload.density, payload.topInsetCssPx];
  if (!values.every((value) => typeof value === 'number' && Number.isFinite(value) && value > 0)) {
    return false;
  }

  return payload.topInsetCssPx <= MAX_REASONABLE_TOP_INSET_CSS_PX;
}

export function installNativeTopInset(runtime?: NativeTopInsetRuntime): boolean {
  const target = runtime ?? (typeof window === 'undefined' ? undefined : window);
  const root = target?.document?.documentElement;
  const bridge = target?.ElpisInsets;
  if (!root || !bridge || typeof bridge.getTopInset !== 'function') return false;
  if (attemptedRoots.has(root)) return false;
  attemptedRoots.add(root);

  let payload: NativeTopInsetPayload | null;
  try {
    payload = parsePayload(bridge.getTopInset());
  } catch {
    return false;
  }
  if (!isValidPayload(payload) || !root.style?.setProperty) return false;

  root.style.setProperty('--elpis-safe-top', `${payload.topInsetCssPx}px`);
  root.setAttribute?.('data-elpis-top-overlay', 'true');
  return true;
}
