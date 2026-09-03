export interface ChatMediaItem {
  url: string;
  alt?: string;
}

export type ChatMediaLayout = 'none' | 'single' | 'double' | 'collage' | 'stack';

/** Keep presentation policy independent from the message/database boundary. */
export function chatMediaLayoutForCount(count: number): ChatMediaLayout {
  if (count <= 0) return 'none';
  if (count === 1) return 'single';
  if (count === 2) return 'double';
  if (count === 3) return 'collage';
  return 'stack';
}

export function clampChatMediaIndex(index: number, count: number): number {
  if (count <= 0) return 0;
  return Math.min(Math.max(index, 0), count - 1);
}

export function nextChatMediaIndex(index: number, count: number, direction: -1 | 1): number {
  return clampChatMediaIndex(index + direction, count);
}

/**
 * PhotoStack owns only a small sliding window. The order is previous/current/
 * next/nextNext so a right swipe can reveal the previous card without mounting
 * every image in a large attachment group.
 */
export function visibleChatMediaIndices(count: number, currentIndex: number): number[] {
  if (count <= 0) return [];
  const current = clampChatMediaIndex(currentIndex, count);
  return [-1, 0, 1, 2]
    .map((offset) => current + offset)
    .filter((index, position, indices) => index >= 0 && index < count && indices.indexOf(index) === position);
}

export function chatMediaImageUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const url = value.trim();
  if (!url) return null;
  if (url.startsWith('data:image/')) return url;
  if (url.startsWith('/') || /^https?:\/\//i.test(url)) return url;
  return null;
}

/** Extract only image-shaped tool output; ordinary links remain tool text. */
export function imageUrlsFromToolValue(value: unknown, limit = 8): string[] {
  const found: string[] = [];
  const seen = new Set<string>();
  const visit = (node: unknown, imageContext = false): void => {
    if (found.length >= limit || node == null) return;
    if (typeof node === 'string') {
      const url = imageContext ? chatMediaImageUrl(node) : null;
      if (url && !seen.has(url)) {
        seen.add(url);
        found.push(url);
      }
      if (!imageContext && /^[[{]/.test(node.trim())) {
        try {
          visit(JSON.parse(node));
        } catch {
          // Ordinary tool text is not an image payload.
        }
      }
      return;
    }
    if (Array.isArray(node)) {
      node.forEach((item) => visit(item, imageContext));
      return;
    }
    if (typeof node !== 'object') return;
    const record = node as Record<string, unknown>;
    const typedImage = record.type === 'image' || record.kind === 'image' || record.media_type?.toString().startsWith('image/');
    Object.entries(record).forEach(([key, child]) => {
      const imageKey = /^(image|image_url|imageUrl|image_uri|imageUri|src)$/i.test(key);
      const imageListKey = /^(images|image_urls|imageUrls|media)$/i.test(key);
      const typedImageKey = typedImage && /^(url|src|source|data)$/i.test(key);
      visit(child, imageContext || imageKey || imageListKey || typedImageKey);
    });
  };
  visit(value);
  return found;
}

