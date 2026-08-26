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

function readValidTopInsetCssPx(payload: NativeTopInsetPayload | null): number | null {
  if (!payload || payload.schemaVersion !== 1 || payload.available !== true || payload.edgeToEdgeTop !== true) {
    return null;
  }

  const { topInsetPx, density, topInsetCssPx } = payload;
  const values = [topInsetPx, density, topInsetCssPx];
  if (!values.every((value) => typeof value === 'number' && Number.isFinite(value) && value > 0)) {
    return null;
  }

  return topInsetCssPx <= MAX_REASONABLE_TOP_INSET_CSS_PX ? topInsetCssPx : null;
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

  const topInsetCssPx = readValidTopInsetCssPx(payload);
  if (topInsetCssPx === null || !root.style?.setProperty) return false;

  root.style.setProperty('--elpis-safe-top', `${topInsetCssPx}px`);
  root.setAttribute?.('data-elpis-top-overlay', 'true');
  return true;
}
