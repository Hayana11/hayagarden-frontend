import {
  cloneFlowStudioData,
  type FlowStudioData,
} from './flowStudioMock';

export type FlowStudioMergePreference = 'local' | 'remote';

export type FlowStudioMergeConflict = {
  path: string;
  base: unknown;
  local: unknown;
  remote: unknown;
};

export type FlowStudioMergeResult = {
  document: FlowStudioData;
  conflicts: FlowStudioMergeConflict[];
};

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function isEntityArray(value: unknown): value is Array<Record<string, unknown>> {
  return Array.isArray(value)
    && value.length > 0
    && value.every((item) => isRecord(item) && typeof item.id === 'string' && item.id);
}

function valueAt(value: unknown, key: string): unknown {
  return isRecord(value) ? value[key] : undefined;
}

function pathFor(parent: string, child: string): string {
  return parent ? `${parent}.${child}` : child;
}

function conflictValue(
  local: unknown,
  remote: unknown,
  preference: FlowStudioMergePreference | undefined,
): unknown {
  return cloneFlowStudioData(preference === 'local' ? local : remote);
}

function mergeEntityArray(
  base: Array<Record<string, unknown>>,
  local: Array<Record<string, unknown>>,
  remote: Array<Record<string, unknown>>,
  path: string,
  preference: FlowStudioMergePreference | undefined,
  conflicts: FlowStudioMergeConflict[],
): Array<Record<string, unknown>> {
  const baseById = new Map(base.map((item) => [String(item.id), item]));
  const localById = new Map(local.map((item) => [String(item.id), item]));
  const remoteById = new Map(remote.map((item) => [String(item.id), item]));
  const baseOrder = base.map((item) => String(item.id));
  const localOrder = local.map((item) => String(item.id));
  const remoteOrder = remote.map((item) => String(item.id));

  let order: string[];
  if (sameValue(localOrder, baseOrder)) {
    order = remoteOrder;
  } else if (sameValue(remoteOrder, baseOrder) || sameValue(localOrder, remoteOrder)) {
    order = localOrder;
  } else {
    conflicts.push({
      path: pathFor(path, 'order'),
      base: baseOrder,
      local: localOrder,
      remote: remoteOrder,
    });
    order = preference === 'local' ? localOrder : remoteOrder;
  }

  const allIds = [...order];
  for (const id of [...localOrder, ...remoteOrder, ...baseOrder]) {
    if (!allIds.includes(id)) allIds.push(id);
  }

  const merged: Array<Record<string, unknown>> = [];
  for (const id of allIds) {
    const baseItem = baseById.get(id);
    const localItem = localById.get(id);
    const remoteItem = remoteById.get(id);

    if (!localItem && !remoteItem) continue;
    if (!localItem && remoteItem) {
      if (baseItem && !sameValue(remoteItem, baseItem)) {
        conflicts.push({
          path: pathFor(path, id),
          base: baseItem,
          local: undefined,
          remote: remoteItem,
        });
        if (preference === 'local') continue;
      } else if (baseItem) {
        continue;
      }
      merged.push(cloneFlowStudioData(remoteItem));
      continue;
    }
    if (localItem && !remoteItem) {
      if (baseItem && !sameValue(localItem, baseItem)) {
        conflicts.push({
          path: pathFor(path, id),
          base: baseItem,
          local: localItem,
          remote: undefined,
        });
        if (preference !== 'local') continue;
      } else if (baseItem) {
        continue;
      }
      merged.push(cloneFlowStudioData(localItem));
      continue;
    }

    merged.push(mergeValue(baseItem, localItem, remoteItem, pathFor(path, id), preference, conflicts) as Record<string, unknown>);
  }
  return merged;
}

function mergeValue(
  base: unknown,
  local: unknown,
  remote: unknown,
  path: string,
  preference: FlowStudioMergePreference | undefined,
  conflicts: FlowStudioMergeConflict[],
): unknown {
  if (sameValue(local, base)) return cloneFlowStudioData(remote);
  if (sameValue(remote, base)) return cloneFlowStudioData(local);
  if (sameValue(local, remote)) return cloneFlowStudioData(local);

  if (isEntityArray(base) && isEntityArray(local) && isEntityArray(remote)) {
    return mergeEntityArray(base, local, remote, path, preference, conflicts);
  }

  if (isRecord(local) && isRecord(remote)) {
    const baseRecord = isRecord(base) ? base : {};
    const keys = new Set([
      ...Object.keys(baseRecord),
      ...Object.keys(local),
      ...Object.keys(remote),
    ]);
    const merged: Record<string, unknown> = {};
    for (const key of keys) {
      const hasLocal = Object.prototype.hasOwnProperty.call(local, key);
      const hasRemote = Object.prototype.hasOwnProperty.call(remote, key);
      const baseValue = valueAt(baseRecord, key);
      if (!hasLocal && !hasRemote) continue;
      if (!hasLocal && hasRemote) {
        if (Object.prototype.hasOwnProperty.call(baseRecord, key) && !sameValue(remote[key], baseValue)) {
          conflicts.push({
            path: pathFor(path, key),
            base: baseValue,
            local: undefined,
            remote: remote[key],
          });
          if (preference === 'local') continue;
        } else if (Object.prototype.hasOwnProperty.call(baseRecord, key)) {
          continue;
        }
        merged[key] = cloneFlowStudioData(remote[key]);
        continue;
      }
      if (hasLocal && !hasRemote) {
        if (Object.prototype.hasOwnProperty.call(baseRecord, key) && !sameValue(local[key], baseValue)) {
          conflicts.push({
            path: pathFor(path, key),
            base: baseValue,
            local: local[key],
            remote: undefined,
          });
          if (preference !== 'local') continue;
        } else if (Object.prototype.hasOwnProperty.call(baseRecord, key)) {
          continue;
        }
        merged[key] = cloneFlowStudioData(local[key]);
        continue;
      }
      merged[key] = mergeValue(baseValue, local[key], remote[key], pathFor(path, key), preference, conflicts);
    }
    return merged;
  }

  if (Array.isArray(local) && Array.isArray(remote)) {
    conflicts.push({ path, base, local, remote });
    return conflictValue(local, remote, preference);
  }

  conflicts.push({ path, base, local, remote });
  return conflictValue(local, remote, preference);
}

export function mergeFlowStudioDocuments(
  base: FlowStudioData,
  local: FlowStudioData,
  remote: FlowStudioData,
  preference?: FlowStudioMergePreference,
): FlowStudioMergeResult {
  const conflicts: FlowStudioMergeConflict[] = [];
  const document = mergeValue(base, local, remote, '', preference, conflicts) as FlowStudioData;
  return {
    document,
    conflicts,
  };
}
