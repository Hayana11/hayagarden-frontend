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

export const CHAT_IMAGE_EXIF_SCAN_BYTES = 256 * 1024;

export type JpegExifOrientation = number | null | 'malformed' | 'unknown';

function isJpegFile(file: File): boolean {
  const mime = sourceMime(file);
  const ext = extensionOf(file.name || '');
  return mime === 'image/jpeg' || mime === 'image/jpg' || ext === 'jpg' || ext === 'jpeg';
}

function readU16(bytes: Uint8Array, offset: number, littleEndian: boolean): number {
  return littleEndian
    ? bytes[offset] | (bytes[offset + 1] << 8)
    : (bytes[offset] << 8) | bytes[offset + 1];
}

function readU32(bytes: Uint8Array, offset: number, littleEndian: boolean): number {
  return littleEndian
    ? (bytes[offset]
      | (bytes[offset + 1] << 8)
      | (bytes[offset + 2] << 16)
      | (bytes[offset + 3] << 24)) >>> 0
    : (((bytes[offset] << 24)
      | (bytes[offset + 1] << 16)
      | (bytes[offset + 2] << 8)
      | bytes[offset + 3]) >>> 0);
}

function parseExifTiff(bytes: Uint8Array, start: number, end: number): JpegExifOrientation {
  if (start + 8 > end) return 'malformed';
  const littleEndian = bytes[start] === 0x49 && bytes[start + 1] === 0x49;
  const bigEndian = bytes[start] === 0x4d && bytes[start + 1] === 0x4d;
  if (!littleEndian && !bigEndian) return 'malformed';
  if (readU16(bytes, start + 2, littleEndian) !== 42) return 'malformed';
  const ifdOffset = readU32(bytes, start + 4, littleEndian);
  const ifd = start + ifdOffset;
  if (ifd < start || ifd + 2 > end) return 'malformed';
  const entryCount = readU16(bytes, ifd, littleEndian);
  const entriesEnd = ifd + 2 + entryCount * 12;
  if (entriesEnd + 4 > end) return 'malformed';
  for (let index = 0; index < entryCount; index += 1) {
    const entry = ifd + 2 + index * 12;
    if (readU16(bytes, entry, littleEndian) !== 0x0112) continue;
    const type = readU16(bytes, entry + 2, littleEndian);
    const count = readU32(bytes, entry + 4, littleEndian);
    if (type !== 3 || count !== 1) return 'malformed';
    const orientation = readU16(bytes, entry + 8, littleEndian);
    return orientation >= 1 && orientation <= 8 ? orientation : 'malformed';
  }
  return null;
}

export function parseJpegExifOrientation(bytes: Uint8Array, complete = true): JpegExifOrientation {
  const incomplete = () => complete ? 'malformed' as const : 'unknown' as const;
  if (bytes.length < 2 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return 'malformed';
  let offset = 2;
  while (offset < bytes.length) {
    if (bytes[offset] !== 0xff) return 'malformed';
    while (offset < bytes.length && bytes[offset] === 0xff) offset += 1;
    if (offset >= bytes.length) return 'malformed';
    const marker = bytes[offset];
    offset += 1;
    if (marker === 0xd8 || (marker >= 0xd0 && marker <= 0xd7)) continue;
    if (marker === 0xd9 || marker === 0xda) return null;
    if (offset + 2 > bytes.length) return incomplete();
    const segmentLength = readU16(bytes, offset, false);
    if (segmentLength < 2) return 'malformed';
    if (offset + segmentLength > bytes.length) return incomplete();
    const payloadStart = offset + 2;
    const payloadEnd = offset + segmentLength;
    if (marker === 0xe1
      && payloadEnd - payloadStart >= 6
      && bytes[payloadStart] === 0x45
      && bytes[payloadStart + 1] === 0x78
      && bytes[payloadStart + 2] === 0x69
      && bytes[payloadStart + 3] === 0x66
      && bytes[payloadStart + 4] === 0
      && bytes[payloadStart + 5] === 0) {
      return parseExifTiff(bytes, payloadStart + 6, payloadEnd);
    }
    offset = payloadEnd;
  }
  return incomplete();
}

async function readBlobPrefix(blob: Blob): Promise<Uint8Array> {
  const candidate = blob as Blob & { arrayBuffer?: () => Promise<ArrayBuffer> };
  if (typeof candidate.arrayBuffer === 'function') {
    return new Uint8Array(await candidate.arrayBuffer());
  }
  if (typeof FileReader === 'undefined') throw new Error('exif-reader-unavailable');
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(reader.error || new Error('exif-reader-failed'));
    reader.readAsArrayBuffer(blob);
  });
}

async function preserveForJpegExif(file: File): Promise<string | null> {
  if (!isJpegFile(file)) return null;
  try {
    const prefix = await readBlobPrefix(file.slice(0, CHAT_IMAGE_EXIF_SCAN_BYTES));
    const parsed = parseJpegExifOrientation(prefix, file.size <= CHAT_IMAGE_EXIF_SCAN_BYTES);
    if (parsed === 'unknown') return 'preserve-exif-unknown';
    if (parsed === 'malformed') return 'preserve-malformed-exif';
    if (typeof parsed === 'number' && parsed >= 2 && parsed <= 8) return 'preserve-exif-orientation';
    return null;
  } catch {
    return 'preserve-exif-unknown';
  }
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
  const exifPreserved = await preserveForJpegExif(file);
  if (exifPreserved) return originalResult(file, 0, 0, exifPreserved);

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
