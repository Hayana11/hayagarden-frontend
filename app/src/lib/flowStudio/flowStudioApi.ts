import {
  cloneFlowStudioData,
  type FlowStudioData,
  type FlowStudioDataAdapter,
} from './flowStudioMock';

export class FlowStudioApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly payload: unknown;

  constructor(status: number, code: string, message: string, payload: unknown) {
    super(message);
    this.name = 'FlowStudioApiError';
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

export class FlowStudioConflictError extends FlowStudioApiError {
  readonly currentDocument: FlowStudioData | null;
  readonly currentRevision: number | null;

  constructor(payload: Record<string, unknown>) {
    super(
      409,
      String(payload.code || 'EDITOR_REVISION_CONFLICT'),
      String(payload.error || '服务器草稿已被其他窗口修改'),
      payload,
    );
    this.name = 'FlowStudioConflictError';
    this.currentDocument = isFlowStudioData(payload.currentDocument)
      ? payload.currentDocument
      : null;
    this.currentRevision = typeof payload.currentRevision === 'number'
      ? payload.currentRevision
      : null;
  }
}

function isFlowStudioData(value: unknown): value is FlowStudioData {
  return Boolean(
    value
    && typeof value === 'object'
    && Array.isArray((value as FlowStudioData).stages)
    && Array.isArray((value as FlowStudioData).pools)
    && Array.isArray((value as FlowStudioData).cues),
  );
}

async function requestJson(
  url: string,
  init?: RequestInit,
): Promise<Record<string, unknown>> {
  const response = await fetch(url, {
    credentials: 'same-origin',
    cache: 'no-store',
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init?.headers || {}),
    },
  });
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok || !payload || typeof payload !== 'object') {
    const body = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {};
    throw new FlowStudioApiError(
      response.status,
      String(body.code || 'EDITOR_REQUEST_FAILED'),
      String(body.error || ('Flow Studio 请求失败（' + response.status + '）')),
      body,
    );
  }
  return payload as Record<string, unknown>;
}

export function createFlowStudioApiAdapter(flowId: string): FlowStudioDataAdapter {
  let revision = 0;

  return {
    async load() {
      const payload = await requestJson(
        '/api/flow-studio/editor/' + encodeURIComponent(flowId),
      );
      if (!isFlowStudioData(payload.document)) {
        throw new FlowStudioApiError(
          502,
          'EDITOR_RESPONSE_INVALID',
          '服务器返回的 Flow Studio 文档不可用',
          payload,
        );
      }
      revision = typeof payload.editorRevision === 'number' ? payload.editorRevision : 0;
      return cloneFlowStudioData(payload.document);
    },

    async save(next) {
      const payload = await requestJson(
        '/api/flow-studio/editor/' + encodeURIComponent(flowId),
        {
          method: 'PUT',
          body: JSON.stringify({
            expectedRevision: revision,
            document: next,
          }),
        },
      ).catch((error: unknown) => {
        if (error instanceof FlowStudioApiError && error.status === 409) {
          throw new FlowStudioConflictError(
            (error.payload && typeof error.payload === 'object')
              ? error.payload as Record<string, unknown>
              : {},
          );
        }
        throw error;
      });
      if (!isFlowStudioData(payload.document)) {
        throw new FlowStudioApiError(
          502,
          'EDITOR_RESPONSE_INVALID',
          '服务器保存响应中的文档不可用',
          payload,
        );
      }
      revision = typeof payload.editorRevision === 'number' ? payload.editorRevision : revision + 1;
      return cloneFlowStudioData(payload.document);
    },
  };
}
