import assert from 'node:assert/strict';
import fs from 'node:fs';

const screenPath = new URL('../src/screens/MemoryScreen.tsx', import.meta.url);
const apiPath = new URL('../src/lib/api.ts', import.meta.url);
const hookPath = new URL('../src/hooks/useMemoryLibrary.ts', import.meta.url);
const detailHookPath = new URL('../src/hooks/useMemoryEntryContent.ts', import.meta.url);
const coldStartPath = new URL('../src/lib/memoryColdStart.ts', import.meta.url);
const typesPath = new URL('../src/types.ts', import.meta.url);

const screen = fs.readFileSync(screenPath, 'utf8');
const api = fs.readFileSync(apiPath, 'utf8');
const hook = fs.readFileSync(hookPath, 'utf8');
const detailHook = fs.readFileSync(detailHookPath, 'utf8');
const coldStart = fs.readFileSync(coldStartPath, 'utf8');
const types = fs.readFileSync(typesPath, 'utf8');

// A. Memory initial uses index endpoint, not legacy full library
{
  assert.match(hook, /fetchMemoryLibraryIndex/);
  assert.doesNotMatch(hook, /fetchMemoryLibrary\(/);
  assert.doesNotMatch(screen, /fetchMemoryLibrary\(/);
}

// B. Index type excludes content
{
  const indexBlock = types.slice(types.indexOf('export interface MemoryIndexEntry'), types.indexOf('export interface MemoryLibraryIndex'));
  assert.match(indexBlock, /excerpt: string/);
  assert.doesNotMatch(indexBlock, /content: string/);
}

// C. Directory previews use preview/excerpt, not full content
{
  assert.match(screen, /preview: m\.preview/);
  assert.match(screen, /m\.excerpt/);
  assert.doesNotMatch(screen, /m\.content/);
}

// D. Detail drawer loads content on demand
{
  assert.match(screen, /useMemoryEntryContent/);
  assert.match(detailHook, /fetchMemoryEntryContent/);
  assert.match(api, /export function fetchMemoryEntryContent/);
  assert.match(screen, /正文读取中/);
}

// E. Memory search hits use server full-text search
{
  assert.match(screen, /searchMemoryEntries/);
  assert.match(api, /export function searchMemoryEntries/);
  assert.match(api, /\/api\/memories\/library\/search/);
}

// F. Search debounce + stale response guard
{
  assert.match(coldStart, /MEMORY_SEARCH_DEBOUNCE_MS = 200/);
  assert.match(screen, /MEMORY_SEARCH_DEBOUNCE_MS/);
  assert.match(screen, /searchGenRef/);
  assert.match(screen, /searchGenRef\.current !== gen/);
}

// G. Module cache: second mount can render from cache immediately
{
  assert.match(hook, /cachedMemoryIndex/);
  assert.match(hook, /useState<MemoryLibraryIndex \| null>\(\(\) => cachedMemoryIndex\)/);
}

// H. Revalidate does not clear cache first
{
  assert.match(hook, /memory_cache_hit/);
  assert.match(hook, /memory_revalidate_start/);
  assert.doesNotMatch(hook, /setLibrary\(null\)/);
}

// I. Index/detail request unmount guards
{
  assert.match(hook, /mountedRef\.current = true/);
  assert.match(hook, /mountedRef\.current = false/);
  assert.match(hook, /generationRef/);
  assert.match(detailHook, /mountedRef\.current/);
  assert.match(detailHook, /generationRef/);
}

// J. No persistent browser storage for Memory cache
{
  for (const src of [hook, screen, detailHook, coldStart]) {
    assert.doesNotMatch(src, /localStorage/);
    assert.doesNotMatch(src, /sessionStorage/);
    assert.doesNotMatch(src, /indexedDB/i);
  }
}

// Perf marks (DEV-only)
{
  assert.match(coldStart, /markMemoryPerf/);
  assert.match(hook, /markMemoryPerf\('memory_mount'\)/);
  assert.match(hook, /markMemoryPerf\('memory_index_start'\)/);
  assert.match(hook, /markMemoryPerf\('memory_index_ready'\)/);
  assert.match(hook, /markMemoryPerf\('memory_cache_hit'\)/);
  assert.match(hook, /markMemoryPerf\('memory_revalidate_start'\)/);
  assert.match(hook, /markMemoryPerf\('memory_revalidate_ready'\)/);
  assert.match(detailHook, /markMemoryPerf\('memory_detail_start'\)/);
  assert.match(detailHook, /markMemoryPerf\('memory_detail_ready'\)/);
  assert.match(screen, /markMemoryPerf\('memory_search_start'\)/);
  assert.match(screen, /markMemoryPerf\('memory_search_ready'\)/);
}

// In-flight dedupe
{
  assert.match(hook, /memoryIndexInflight/);
  assert.match(detailHook, /inflight\.get\(id\)/);
}

console.log('test:memory-cold-start — all checks passed');
