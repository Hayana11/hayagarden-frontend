import {
  cloneFlowStudioData,
  type FlowStudioData,
  type FlowStudioDataAdapter,
  type FlowStudioPublishResult,
  type FlowStudioValidationResult,
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
    editorRevision: typeof payload.editorRevision === 'number' ? payload.editorRevision : 0,
    runtimeVersion: typeof runtime.version === 'number' ? runtime.version : 0,
    published: runtime.published === true,
    savedAt: typeof payload.savedAt === 'string' ? payload.savedAt : document.savedAt,
  };
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) {
    return '[' + value.map((item) => stableJson(item)).join(',') + ']';
  }
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return '{' + Object.keys(record).sort().map((key) => (
      JSON.stringify(key) + ':' + stableJson(record[key])
    )).join(',') + '}';
  }
  return JSON.stringify(value);
}

function editorDocumentForRequest(next: FlowStudioData): FlowStudioData {
  const document = cloneFlowStudioData(next) as FlowStudioData & Record<string, unknown>;
  delete (document as Record<string, unknown>).version;
  delete (document as Record<string, unknown>).editorRevision;
  delete (document as Record<string, unknown>).savedAt;
  delete (document as Record<string, unknown>).runtimeVersion;
  delete (document as Record<string, unknown>).published;
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

  return {
    async load() {
      const payload = await requestJson(
        '/api/flow-studio/editor/' + encodeURIComponent(flowId),
      );
      revision = typeof payload.editorRevision === 'number' ? payload.editorRevision : 0;
      return documentForWorkspace(payload);
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
      return documentForWorkspace(payload);
    },

    async validate(next) {
      const payload = await requestJson(
        '/api/flow-studio/editor/' + encodeURIComponent(flowId) + '/validate',
        {
          method: 'POST',
          body: JSON.stringify({
            document: editorDocumentForRequest(next),
          }),
        },
      );
      const list = (value: unknown): string[] => Array.isArray(value)
        ? value.filter((item): item is string => typeof item === 'string')
        : [];
      return {
        valid: payload.valid === true,
        publishable: payload.publishable === true,
        errors: list(payload.errors),
        unsupportedFields: list(payload.unsupportedFields),
        displayOnlyFields: list(payload.displayOnlyFields),
        unsafeFields: list(payload.unsafeFields),
      };
    },

    async publish(next): Promise<FlowStudioPublishResult> {
      const expectedRuntimeVersion = next.runtimeVersion;
      if (!Number.isInteger(expectedRuntimeVersion) || (expectedRuntimeVersion as number) < 0) {
        throw new FlowStudioApiError(
          409,
          'RUNTIME_VERSION_REQUIRED',
          '缺少当前运行配置版本，请先重新读取服务器草稿',
          {},
        );
      }
      let payload: Record<string, unknown>;
      try {
        payload = await requestJson(
          '/api/flow-studio/editor/' + encodeURIComponent(flowId) + '/publish',
          {
            method: 'POST',
            body: JSON.stringify({
              expectedRevision: revision,
              expectedRuntimeVersion,
              document: editorDocumentForRequest(next),
            }),
          },
        );
      } catch (error: unknown) {
        if (error instanceof FlowStudioApiError && error.status === 409) {
          throw new FlowStudioConflictError(
            (error.payload && typeof error.payload === 'object')
              ? error.payload as Record<string, unknown>
              : {},
          );
        }
        throw error;
      }

      const confirmed = await requestJson(
        '/api/flow-studio/editor/' + encodeURIComponent(flowId),
      );
      const confirmedDocument = documentForWorkspace(confirmed);
      const publishedRuntime = payload.runtime && typeof payload.runtime === 'object'
        ? payload.runtime as Record<string, unknown>
        : {};
      const confirmedRuntime = confirmed.runtime && typeof confirmed.runtime === 'object'
        ? confirmed.runtime as Record<string, unknown>
        : {};
      if (
        publishedRuntime.version !== confirmedRuntime.version
        || confirmedRuntime.published !== true
        || stableJson(editorDocumentForRequest(confirmedDocument))
          !== stableJson(editorDocumentForRequest(next))
      ) {
        throw new FlowStudioApiError(
          502,
          'PUBLISH_CONFIRMATION_FAILED',
          '服务器发布结果未通过重新读取确认',
          confirmed,
        );
      }
      revision = typeof confirmed.editorRevision === 'number'
        ? confirmed.editorRevision
        : revision;
      return {
        document: confirmedDocument,
        displayOnlyFields: Array.isArray(payload.displayOnlyFields)
          ? payload.displayOnlyFields.filter(
              (item): item is string => typeof item === 'string',
            )
          : [],
      };
    },

    acceptRevision(nextRevision) {
      if (Number.isInteger(nextRevision) && nextRevision >= 0) {
        revision = nextRevision;
      }
    },
  };
}
