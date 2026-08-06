import { useLayoutEffect } from 'react';
import { applyLegacyNativeCompat } from '../lib/legacyNativeCompat';

/** Mount-level body zoom parity for legacy Capacitor WebViews (flex-gap unsupported). */
export function useLegacyNativeCompat() {
  useLayoutEffect(() => applyLegacyNativeCompat(), []);
}
