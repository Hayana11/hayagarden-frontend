import type { FlowStudioData } from './flowStudioMock';

export const FLOW_STUDIO_NON_EDITABLE_FIELDS = [
  'version',
  'editorRevision',
  'runtimeVersion',
  'savedAt',
  'published',
] as const;

const NON_EDITABLE_FIELDS = new Set<string>(FLOW_STUDIO_NON_EDITABLE_FIELDS);

function canonicalize(value: unknown, root = false): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => canonicalize(item));
  }
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return Object.keys(record)
      .filter((key) => !root || !NON_EDITABLE_FIELDS.has(key))
      .sort()
      .reduce<Record<string, unknown>>((result, key) => {
        result[key] = canonicalize(record[key]);
        return result;
      }, {});
  }
  return value;
}

export function editableFlowStudioDocument(document: FlowStudioData): unknown {
  return canonicalize(document, true);
}

export function areEditableFlowStudioDocumentsEqual(
  left: FlowStudioData,
  right: FlowStudioData,
): boolean {
  return JSON.stringify(editableFlowStudioDocument(left))
    === JSON.stringify(editableFlowStudioDocument(right));
}

export function hasEditableFlowStudioChanges(
  saved: FlowStudioData,
  draft: FlowStudioData,
): boolean {
  return !areEditableFlowStudioDocumentsEqual(saved, draft);
}
