export const CHAT_IMAGE_MAX_LONG_EDGE = 1568;
export const CHAT_IMAGE_FALLBACK_LONG_EDGE = 1280;
export const CHAT_IMAGE_TARGET_BYTES = 100 * 1024;
export const CHAT_IMAGE_SOFT_MAX_BYTES = 220 * 1024;
export const CHAT_IMAGE_QUALITY_MAX = 0.88;
export const CHAT_IMAGE_QUALITY_MIN = 0.58;

export const CHAT_IMAGE_QUALITY_STEPS = [
  0.88, 0.82, 0.76, 0.70, 0.64, 0.58,
] as const;

type CompressionPass = {
  width: number;
  height: number;
  longEdge: number;
  qualities: readonly number[];
};

export type ChatImageCompressionPlan = {
  passthrough: boolean;
  reason: string;
  sourceWidth: number;
  sourceHeight: number;
  outputWidth: number;
  outputHeight: number;
  passes: CompressionPass[];
};

export type ChatImageCompressionResult = {
  file: File;
  originalBytes: number;
  outputBytes: number;
  originalWidth: number;
  originalHeight: number;
  outputWidth: number;
  outputHeight: number;
  compressed: boolean;
  reason: string;
};

export type PendingChatImage = {
  id: string;
  file: File;
  previewUrl: string;
  status: 'compressing' | 'ready';
  originalBytes: number;
  outputBytes?: number;
};

let pendingChatImageSequence = 0;

export function createPendingChatImage(file: File): PendingChatImage {
  const sequence = pendingChatImageSequence;
  pendingChatImageSequence += 1;
  return {
    id: `chat-image-${Date.now()}-${sequence}`,
    file,
    previewUrl: URL.createObjectURL(file),
    status: 'compressing',
    originalBytes: file.size,
  };
}

export function revokePendingChatImagePreview(image: PendingChatImage): void {
  if (image.previewUrl && typeof URL !== 'undefined' && typeof URL.revokeObjectURL === 'function') {
    URL.revokeObjectURL(image.previewUrl);
  }
}

export function mergePendingChatImages(
  current: PendingChatImage[],
  settled: PendingChatImage[],
): PendingChatImage[] {
  const settledById = new Map(settled.map((image) => [image.id, image]));
  return current.map((image) => settledById.get(image.id) || image);
}

function positiveInteger(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 0;
  return Math.floor(value);
}

function dimensionsForLongEdge(width: number, height: number, longEdge: number): { width: number; height: number } {
  const scale = Math.min(1, longEdge / Math.max(width, height));
  return {
    width: Math.max(1, Math.floor(width * scale)),
    height: Math.max(1, Math.floor(height * scale)),
  };
}

export function buildChatImageCompressionPlan(input: {
  originalBytes: number;
  width: number;
  height: number;
}): ChatImageCompressionPlan {
  const sourceWidth = positiveInteger(input.width);
  const sourceHeight = positiveInteger(input.height);
  if (!sourceWidth || !sourceHeight) {
    return {
      passthrough: true,
      reason: 'invalid-dimensions',
      sourceWidth,
      sourceHeight,
      outputWidth: sourceWidth,
      outputHeight: sourceHeight,
      passes: [],
    };
  }

  const originalBytes = Number(input.originalBytes);
  const sourceLongEdge = Math.max(sourceWidth, sourceHeight);
  const primary = dimensionsForLongEdge(sourceWidth, sourceHeight, CHAT_IMAGE_MAX_LONG_EDGE);
  if (Number.isFinite(originalBytes)
    && originalBytes <= CHAT_IMAGE_TARGET_BYTES
    && sourceLongEdge <= CHAT_IMAGE_MAX_LONG_EDGE) {
    return {
      passthrough: true,
      reason: 'small-image',
      sourceWidth,
      sourceHeight,
      outputWidth: sourceWidth,
      outputHeight: sourceHeight,
      passes: [],
    };
  }

  const passes: CompressionPass[] = [{
    ...primary,
    longEdge: Math.max(primary.width, primary.height),
    qualities: CHAT_IMAGE_QUALITY_STEPS,
  }];
  if (Math.max(primary.width, primary.height) > CHAT_IMAGE_FALLBACK_LONG_EDGE) {
    const fallback = dimensionsForLongEdge(sourceWidth, sourceHeight, CHAT_IMAGE_FALLBACK_LONG_EDGE);
    passes.push({
      ...fallback,
      longEdge: Math.max(fallback.width, fallback.height),
      qualities: CHAT_IMAGE_QUALITY_STEPS,
    });
  }

  return {
    passthrough: false,
    reason: 'encode-bounded-search',
    sourceWidth,
    sourceHeight,
    outputWidth: primary.width,
    outputHeight: primary.height,
    passes,
  };
}

function extensionOf(name: string): string {
  const match = /\.([^.\\/]+)$/.exec(name);
  return match ? match[1].toLowerCase() : '';
}

function baseNameOf(name: string): string {
  const clean = name.replace(/[\\/]+/g, '/').split('/').pop() || 'image';
  return clean.replace(/\.[^.]+$/, '') || 'image';
}

function sourceMime(file: File): string {
  return String(file.type || '').trim().toLowerCase();
}

function shouldPreserveWithoutDecode(file: File): string | null {
  const mime = sourceMime(file);
  const ext = extensionOf(file.name || '');
  if (mime === 'image/svg+xml' || ext === 'svg') return 'preserve-svg';
  if (mime === 'image/gif' || ext === 'gif') return 'preserve-gif';
  return null;
}

function originalResult(file: File, width: number, height: number, reason: string): ChatImageCompressionResult {
  return {
    file, originalBytes: file.size, outputBytes: file.size,
    originalWidth: width, originalHeight: height,
    outputWidth: width, outputHeight: height,
    compressed: false, reason,
  };
}

function loadImage(file: File): Promise<{ image: HTMLImageElement; width: number; height: number }> {
  return new Promise((resolve, reject) => {
    if (typeof Image === 'undefined' || typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') {
      reject(new Error('image-decode-unavailable'));
      return;
    }
    const image = new Image();
    const objectUrl = URL.createObjectURL(file);
    const cleanup = () => URL.revokeObjectURL(objectUrl);
    image.onload = () => {
      cleanup();
      const width = positiveInteger(image.naturalWidth || image.width);
      const height = positiveInteger(image.naturalHeight || image.height);
      if (!width || !height) reject(new Error('invalid-decoded-dimensions'));
      else resolve({ image, width, height });
    };
    image.onerror = () => { cleanup(); reject(new Error('image-decode-failed')); };
    image.src = objectUrl;
  });
}

function canvasToBlob(canvas: HTMLCanvasElement, mime: string, quality: number): Promise<Blob | null> {
  return new Promise((resolve) => {
    try {
      if (typeof canvas.toBlob !== 'function') { resolve(null); return; }
      canvas.toBlob((blob) => resolve(blob), mime, quality);
    } catch { resolve(null); }
  });
}

async function selectOutputMime(canvas: HTMLCanvasElement, file: File): Promise<string | null> {
  const webp = await canvasToBlob(canvas, 'image/webp', CHAT_IMAGE_QUALITY_MAX);
  if (webp && webp.type === 'image/webp') return 'image/webp';
  const source = sourceMime(file);
  const fallback = source === 'image/png' ? 'image/png' : 'image/jpeg';
  const encoded = await canvasToBlob(canvas, fallback, CHAT_IMAGE_QUALITY_MAX);
  if (encoded && encoded.type === fallback) return fallback;
  return null;
}

function outputName(file: File, mime: string): string {
  const extension = mime === 'image/webp' ? 'webp' : mime === 'image/png' ? 'png' : 'jpg';
  return baseNameOf(file.name || 'image') + '.' + extension;
}

async function encodeAtQuality(canvas: HTMLCanvasElement, mime: string, quality: number): Promise<Blob | null> {
  const blob = await canvasToBlob(canvas, mime, quality);
  return blob && blob.type === mime ? blob : null;
}

export async function compressChatImage(file: File): Promise<ChatImageCompressionResult> {
  const preserved = shouldPreserveWithoutDecode(file);
  if (preserved) return originalResult(file, 0, 0, preserved);

  let decoded: { image: HTMLImageElement; width: number; height: number };
  try { decoded = await loadImage(file); }
  catch { return originalResult(file, 0, 0, 'decode-failed-original'); }

  const plan = buildChatImageCompressionPlan({
    originalBytes: file.size, width: decoded.width, height: decoded.height,
  });
  if (plan.passthrough) return originalResult(file, decoded.width, decoded.height, plan.reason);

  let canvas: HTMLCanvasElement;
  try { canvas = document.createElement('canvas'); }
  catch { return originalResult(file, decoded.width, decoded.height, 'canvas-unavailable-original'); }

  let best: { blob: Blob; width: number; height: number; passIndex: number } | null = null;
  let selected: { blob: Blob; width: number; height: number; passIndex: number; target: boolean } | null = null;

  for (let passIndex = 0; passIndex < plan.passes.length; passIndex += 1) {
    const pass = plan.passes[passIndex];
    canvas.width = pass.width;
    canvas.height = pass.height;
    const context = canvas.getContext('2d');
    if (!context) return originalResult(file, decoded.width, decoded.height, 'canvas-context-unavailable-original');
    try {
      context.clearRect(0, 0, pass.width, pass.height);
      context.drawImage(decoded.image, 0, 0, pass.width, pass.height);
    } catch { return originalResult(file, decoded.width, decoded.height, 'canvas-draw-failed-original'); }

    const mime = await selectOutputMime(canvas, file);
    if (!mime) return originalResult(file, decoded.width, decoded.height, 'codec-unavailable-original');

    let passBest: { blob: Blob; width: number; height: number; passIndex: number } | null = null;
    for (const quality of pass.qualities) {
      const blob = await encodeAtQuality(canvas, mime, quality);
      if (!blob) continue;
      const candidate = { blob, width: pass.width, height: pass.height, passIndex };
      if (!passBest || blob.size < passBest.blob.size) passBest = candidate;
      if (blob.size <= CHAT_IMAGE_TARGET_BYTES) {
        selected = { ...candidate, target: true };
        break;
      }
    }
    if (!passBest) continue;
    if (!best || passBest.blob.size < best.blob.size) best = passBest;
    if (!selected && passBest.blob.size <= CHAT_IMAGE_SOFT_MAX_BYTES) {
      selected = { ...passBest, target: false };
    }
    if (selected) break;
  }

  const chosen = selected
    ? selected
    : best
      ? { ...best, target: false }
      : null;
  if (!chosen || chosen.blob.size >= file.size) {
    return originalResult(file, decoded.width, decoded.height, 'encoded-not-smaller-original');
  }

  try {
    const output = new File(
      [chosen.blob], outputName(file, chosen.blob.type),
      { type: chosen.blob.type, lastModified: file.lastModified },
    );
    return {
      file: output, originalBytes: file.size, outputBytes: output.size,
      originalWidth: decoded.width, originalHeight: decoded.height,
      outputWidth: chosen.width, outputHeight: chosen.height, compressed: true,
      reason: chosen.target ? 'compressed-target'
        : chosen.passIndex > 0 ? 'compressed-fallback-dimensions' : 'compressed-soft-target',
    };
  } catch { return originalResult(file, decoded.width, decoded.height, 'file-construction-failed-original'); }
}
