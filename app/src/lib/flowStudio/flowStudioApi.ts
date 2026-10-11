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
  readonly currentRuntimeVersion: number | null;

  constructor(payload: Record<string, unknown>) {
    super(
      409,
      String(payload.code || 'EDITOR_REVISION_CONFLICT'),
      String(payload.error || '服务器草稿已被其他窗口修改'),
      payload,
    );
    this.name = 'FlowStudioConflictError';
    this.currentDocument = isFlowStudioData(payload.currentDocument)
      ? {
          ...cloneFlowStudioData(payload.currentDocument),
          version: 0,
          runtimeVersion: typeof payload.currentRuntimeVersion === 'number'
            ? payload.currentRuntimeVersion
            : 0,
          savedDraft: true,
          published: false,
          savedAt: typeof (payload.currentDocument as FlowStudioData).savedAt === 'string'
            ? (payload.currentDocument as FlowStudioData).savedAt
            : '',
        }
      : null;
    this.currentRevision = typeof payload.currentRevision === 'number'
      ? payload.currentRevision
      : null;
    this.currentRuntimeVersion = typeof payload.currentRuntimeVersion === 'number'
      ? payload.currentRuntimeVersion
      : null;
  }
}

function isFlowStudioData(value: unknown): value is FlowStudioData {
  return Boolean(
    value
    && typeof value === 'object'
    && Array.isArray((value as FlowStudioData).stages)
    && Array.isArray((value as FlowStudioData).pools)
    && Array.isArray((value as FlowStudioData).cues)
    && Array.isArray((value as FlowStudioData).dimensions),
  );
}

function documentForWorkspace(payload: Record<string, unknown>): FlowStudioData {
  if (!isFlowStudioData(payload.document)) {
    throw new FlowStudioApiError(
      502,
      'EDITOR_RESPONSE_INVALID',
      '服务器返回的 Flow Studio 文档不可用',
      payload,
    );
  }
  const document = cloneFlowStudioData(payload.document);
  const runtime = payload.runtime && typeof payload.runtime === 'object'
    ? payload.runtime as Record<string, unknown>
    : {};
  return {
    ...document,
    // Runtime config version is not the editable draft revision.
    version: 0,
    runtimeVersion: typeof runtime.version === 'number' ? runtime.version : 0,
    editorRevision: typeof payload.editorRevision === 'number' ? payload.editorRevision : 0,
    savedDraft: payload.source === 'editor',
    published: runtime.published === true,
    savedAt: typeof payload.savedAt === 'string' ? payload.savedAt : document.savedAt,
  };
}

function editorDocumentForRequest(next: FlowStudioData): FlowStudioData {
  const document = cloneFlowStudioData(next) as FlowStudioData & Record<string, unknown>;
  delete (document as Record<string, unknown>).version;
  delete (document as Record<string, unknown>).runtimeVersion;
  delete (document as Record<string, unknown>).editorRevision;
  delete (document as Record<string, unknown>).savedDraft;
  delete (document as Record<string, unknown>).published;
  delete (document as Record<string, unknown>).savedAt;
  return document;
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
  let runtimeVersion = 0;

  return {
    async load() {
      const payload = await requestJson(
        '/api/flow-studio/editor/' + encodeURIComponent(flowId),
      );
      revision = typeof payload.editorRevision === 'number' ? payload.editorRevision : 0;
      const document = documentForWorkspace(payload);
      runtimeVersion = document.runtimeVersion ?? 0;
      return document;
    },

    async save(next) {
      const payload = await requestJson(
        '/api/flow-studio/editor/' + encodeURIComponent(flowId),
        {
          method: 'PUT',
          body: JSON.stringify({
            expectedRevision: revision,
            document: editorDocumentForRequest(next),
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
      revision = typeof payload.editorRevision === 'number' ? payload.editorRevision : revision + 1;
      const document = documentForWorkspace(payload);
      runtimeVersion = document.runtimeVersion ?? runtimeVersion;
      return document;
    },

    async publish() {
      const payload = await requestJson(
        '/api/flow-studio/editor/' + encodeURIComponent(flowId) + '/publish',
        {
          method: 'POST',
          body: JSON.stringify({
            expectedRevision: revision,
            expectedRuntimeVersion: runtimeVersion,
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
      const published = documentForWorkspace(payload);
      const confirmed = documentForWorkspace(await requestJson(
        '/api/flow-studio/editor/' + encodeURIComponent(flowId),
      ));
      if (
        !confirmed.published
        || confirmed.runtimeVersion !== published.runtimeVersion
        || confirmed.editorRevision !== published.editorRevision
      ) {
        throw new FlowStudioApiError(
          502,
          'FLOW_PUBLISH_VERIFY_FAILED',
          '发布后服务器配置校验未通过',
          { published, confirmed },
        );
      }
      revision = confirmed.editorRevision ?? revision;
      runtimeVersion = confirmed.runtimeVersion ?? runtimeVersion;
      return confirmed;
    },

    acceptRevision(nextRevision) {
      if (Number.isInteger(nextRevision) && nextRevision >= 0) {
        revision = nextRevision;
      }
    },
  };
}
