/**
 * Cookie-first API client for the Next.js web app.
 *
 * Auth cookies are always httpOnly; the Secure attribute depends on the
 * environment. The browser attaches them when ``credentials: 'include'`` is
 * set. We never touch localStorage/sessionStorage for tokens.
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? '/api-proxy';

export type ApiErrorPayload = { detail?: string; [key: string]: unknown };

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly payload: ApiErrorPayload,
  ) {
    super(
      typeof payload.detail === 'string' ? payload.detail : `Request failed with status ${status}`,
    );
    this.name = 'ApiError';
  }
}

export interface ApiFetchResult<T> {
  data: T;
  response: Response;
}

let refreshPromise: Promise<boolean> | null = null;

const NO_REFRESH_PATHS = new Set([
  '/v1/auth/login',
  '/v1/auth/register',
  '/v1/auth/refresh',
  '/v1/auth/verify',
  '/v1/auth/resend-verification',
  '/v1/auth/recovery/request',
  '/v1/auth/recovery/reset',
  '/v1/auth/logout',
]);

function requestInit(init: RequestInit): RequestInit {
  const headers = new Headers(init.headers);
  if (!headers.has('Content-Type') && typeof init.body === 'string') {
    headers.set('Content-Type', 'application/json');
  }
  return {
    ...init,
    credentials: 'include',
    headers,
  };
}

function mayRetry(init: RequestInit): boolean {
  const method = (init.method ?? 'GET').toUpperCase();
  if (method === 'GET' || method === 'HEAD' || method === 'OPTIONS') return true;
  return new Headers(init.headers).has('Idempotency-Key');
}

async function refreshSession(): Promise<boolean> {
  if (refreshPromise) return refreshPromise;
  const pending = (async () => {
    try {
      const response = await fetch(`${API_URL}/v1/auth/refresh`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      });
      return response.ok;
    } catch {
      return false;
    }
  })();
  refreshPromise = pending;
  try {
    return await pending;
  } finally {
    if (refreshPromise === pending) refreshPromise = null;
  }
}

async function parseResponse<T>(response: Response): Promise<ApiFetchResult<T>> {
  const isJson = response.headers.get('content-type')?.includes('application/json');
  const body = isJson ? await response.json() : undefined;

  if (!response.ok) {
    throw new ApiError(response.status, (body as ApiErrorPayload) ?? {});
  }
  return { data: body as T, response };
}

export async function apiFetchResult<T>(
  path: string,
  init: RequestInit = {},
): Promise<ApiFetchResult<T>> {
  const normalized = requestInit(init);
  let response = await fetch(`${API_URL}${path}`, normalized);

  if (response.status === 401 && !NO_REFRESH_PATHS.has(path)) {
    const refreshed = await refreshSession();
    if (refreshed && mayRetry(normalized)) {
      response = await fetch(`${API_URL}${path}`, normalized);
    }
  }

  return parseResponse<T>(response);
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  return (await apiFetchResult<T>(path, init)).data;
}
