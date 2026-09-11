import { beforeEach, describe, expect, it, vi } from 'vitest';

const API = '/api-proxy';

function jsonResponse(status: number, body: unknown = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('apiFetch session refresh', () => {
  beforeEach(() => {
    vi.resetModules();
    vi.restoreAllMocks();
  });

  it('refreshes an expired session and retries the original request once', async () => {
    const refreshedUser = {
      id: 'user-1',
      email: 'manager@example.com',
      full_name: 'Farm Manager',
      is_active: true,
      is_verified: true,
      is_superuser: false,
      permissions: [],
      permission_scopes: [
        {
          organization_id: 'org-1',
          farm_id: 'farm-1',
          permissions: ['production_batch.create'],
        },
      ],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }))
      .mockResolvedValueOnce(jsonResponse(200, { token_type: 'bearer' }))
      .mockResolvedValueOnce(jsonResponse(200, refreshedUser));
    vi.stubGlobal('fetch', fetchMock);
    const { apiFetch } = await import('@/lib/api');
    const { hasScopedPermission } = await import('@/lib/permissions');

    const user = await apiFetch<typeof refreshedUser>('/v1/auth/me');
    expect(user).toEqual(refreshedUser);
    expect(
      hasScopedPermission(user, 'production_batch.create', {
        organizationId: 'org-1',
        farmId: 'farm-1',
      }),
    ).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[1][0]).toBe(`${API}/v1/auth/refresh`);
    expect(fetchMock.mock.calls[2][0]).toBe(`${API}/v1/auth/me`);
  });

  it('preserves method, headers, body, credentials, and idempotency key on retry', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(401))
      .mockResolvedValueOnce(jsonResponse(200))
      .mockResolvedValueOnce(jsonResponse(201, { id: 'event-1' }));
    vi.stubGlobal('fetch', fetchMock);
    const { apiFetch } = await import('@/lib/api');
    const body = JSON.stringify({ event_type: 'SAMPLING', data: { sample_size: 2 } });

    await apiFetch('/v1/batches/b1/events', {
      method: 'POST',
      headers: { 'Idempotency-Key': 'event-key-1', 'X-Test': 'preserved' },
      body,
    });

    const first = fetchMock.mock.calls[0][1] as RequestInit;
    const retry = fetchMock.mock.calls[2][1] as RequestInit;
    expect(retry.method).toBe('POST');
    expect(retry.body).toBe(body);
    expect(retry.credentials).toBe('include');
    expect(retry.headers).toEqual(first.headers);
    expect(new Headers(retry.headers).get('Idempotency-Key')).toBe('event-key-1');
    expect(new Headers(retry.headers).get('X-Test')).toBe('preserved');
  });

  it.each([
    ['plain object', { Authorization: 'Bearer object', 'Idempotency-Key': 'object-key' }],
    [
      'Headers instance',
      new Headers({ Authorization: 'Bearer headers', 'Idempotency-Key': 'headers-key' }),
    ],
    [
      'tuple array',
      [
        ['Authorization', 'Bearer tuples'],
        ['Idempotency-Key', 'tuple-key'],
      ] as [string, string][],
    ],
  ])('normalizes %s headers and preserves them across refresh retry', async (_label, headers) => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(401))
      .mockResolvedValueOnce(jsonResponse(200))
      .mockResolvedValueOnce(jsonResponse(201, { id: 'event-1' }));
    vi.stubGlobal('fetch', fetchMock);
    const { apiFetch } = await import('@/lib/api');

    await apiFetch('/v1/batches/b1/events', {
      method: 'POST',
      headers,
      body: JSON.stringify({ event_type: 'SAMPLING' }),
    });

    const initial = fetchMock.mock.calls[0][1] as RequestInit;
    const retry = fetchMock.mock.calls[2][1] as RequestInit;
    expect(initial.headers).toBeInstanceOf(Headers);
    expect(retry.headers).toBe(initial.headers);
    expect(new Headers(retry.headers).get('Authorization')).toMatch(/^Bearer /);
    expect(new Headers(retry.headers).get('Idempotency-Key')).toMatch(/-key$/);
    expect(new Headers(retry.headers).get('Content-Type')).toBe('application/json');
  });

  it('does not add a JSON content type to a bodyless request or replace a caller content type', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(200))
      .mockResolvedValueOnce(jsonResponse(200));
    vi.stubGlobal('fetch', fetchMock);
    const { apiFetch } = await import('@/lib/api');

    await apiFetch('/v1/auth/me', { headers: [['X-Test', 'bodyless']] });
    await apiFetch('/v1/import', {
      method: 'POST',
      headers: new Headers({ 'Content-Type': 'text/plain' }),
      body: 'raw',
    });

    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).has('Content-Type')).toBe(false);
    expect(new Headers(fetchMock.mock.calls[1][1]?.headers).get('Content-Type')).toBe('text/plain');
  });

  it('uses one shared refresh for concurrent 401 responses', async () => {
    let targetCalls = 0;
    let refreshCalls = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith('/v1/auth/refresh')) {
        refreshCalls += 1;
        await Promise.resolve();
        return jsonResponse(200);
      }
      targetCalls += 1;
      return targetCalls <= 2 ? jsonResponse(401) : jsonResponse(200, { ok: true });
    });
    vi.stubGlobal('fetch', fetchMock);
    const { apiFetch } = await import('@/lib/api');

    await Promise.all([apiFetch('/v1/organizations'), apiFetch('/v1/auth/me')]);
    expect(refreshCalls).toBe(1);
    expect(targetCalls).toBe(4);
  });

  it('propagates 401 when refresh fails so existing login handling runs', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(401, { detail: 'expired' }))
      .mockResolvedValueOnce(jsonResponse(401, { detail: 'invalid refresh' }));
    vi.stubGlobal('fetch', fetchMock);
    const { apiFetch } = await import('@/lib/api');

    await expect(apiFetch('/v1/auth/me')).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('retries the original request no more than once', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(401))
      .mockResolvedValueOnce(jsonResponse(200))
      .mockResolvedValueOnce(jsonResponse(401));
    vi.stubGlobal('fetch', fetchMock);
    const { apiFetch } = await import('@/lib/api');

    await expect(apiFetch('/v1/auth/me')).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('does not recursively refresh the refresh endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(401));
    vi.stubGlobal('fetch', fetchMock);
    const { apiFetch } = await import('@/lib/api');

    await expect(
      apiFetch('/v1/auth/refresh', { method: 'POST', body: '{}' }),
    ).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each(['/v1/auth/recovery/request', '/v1/auth/recovery/reset'])(
    'does not refresh the public recovery endpoint %s',
    async (path) => {
      const fetchMock = vi.fn().mockResolvedValue(jsonResponse(401, { detail: 'rejected' }));
      vi.stubGlobal('fetch', fetchMock);
      const { apiFetch } = await import('@/lib/api');

      await expect(apiFetch(path, { method: 'POST', body: '{}' })).rejects.toMatchObject({
        status: 401,
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0][0]).toBe(`${API}${path}`);
    },
  );

  it('does not refresh non-401 responses or retry unsafe requests without an idempotency key', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(422, { detail: 'invalid' }))
      .mockResolvedValueOnce(jsonResponse(401))
      .mockResolvedValueOnce(jsonResponse(200));
    vi.stubGlobal('fetch', fetchMock);
    const { apiFetch } = await import('@/lib/api');

    await expect(apiFetch('/v1/organizations')).rejects.toMatchObject({ status: 422 });
    await expect(
      apiFetch('/v1/units/u1/batches', { method: 'POST', body: '{}' }),
    ).rejects.toMatchObject({ status: 401 });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls[2][0]).toBe(`${API}/v1/auth/refresh`);
  });
});
