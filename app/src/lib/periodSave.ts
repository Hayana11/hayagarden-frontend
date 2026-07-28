/** Per-date save tokens so an older in-flight PUT cannot overwrite a newer one. */

export function nextSaveToken(map: Map<string, number>, date: string): number {
  const n = (map.get(date) || 0) + 1;
  map.set(date, n);
  return n;
}

export function isLatestSaveToken(map: Map<string, number>, date: string, token: number): boolean {
  return map.get(date) === token;
}
