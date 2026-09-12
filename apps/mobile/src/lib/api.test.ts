/// <reference types="jest" />

jest.mock('expo-constants', () => ({
  expoConfig: { extra: { apiUrl: 'http://localhost:8000/api' } },
}));
let mockPlatformOs = 'android';
jest.mock('react-native', () => ({
  Platform: {
    get OS() {
      return mockPlatformOs;
    },
  },
}));
jest.mock('./secure-storage', () => ({
  setTokens: jest.fn(),
  clearTokens: jest.fn(),
  getAccessToken: jest.fn(),
  getRefreshToken: jest.fn(),
}));

import easConfig from '../../eas.json';
import { authenticatedRequest, login, logout, refreshTokens, resolveApiUrl } from './api';
import * as secureStorage from './secure-storage';

const mockSetTokens = jest.mocked(secureStorage.setTokens);
const mockClearTokens = jest.mocked(secureStorage.clearTokens);
const mockGetAccessToken = jest.mocked(secureStorage.getAccessToken);
const mockGetRefreshToken = jest.mocked(secureStorage.getRefreshToken);

const tokenPair = (access: string, refresh: string) => ({
  access_token: access,
  refresh_token: refresh,
  token_type: 'bearer',
  expires_in: 900,
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('mobile API configuration and native auth transport', () => {
  let fetchMock: ReturnType<typeof jest.fn>;

  beforeEach(() => {
    jest.resetAllMocks();
    mockPlatformOs = 'android';
    fetchMock = jest.fn();
    globalThis.fetch = fetchMock as unknown as typeof fetch;
  });

  test('validation env overrides checked-in localhost while local development keeps localhost', () => {
    const validationUrl = easConfig.build['sdk56-validation'].env.EXPO_PUBLIC_API_URL;
    expect(resolveApiUrl(validationUrl, 'http://localhost:8000/api')).toBe(validationUrl);
    expect(resolveApiUrl(undefined, 'http://localhost:8000/api')).toBe('http://localhost:8000/api');
  });

  test.each(['android', 'ios'])(
    '%s login selects bearer transport and stores tokens',
    async (os) => {
      mockPlatformOs = os;
      fetchMock.mockResolvedValue(jsonResponse(tokenPair('access-1', 'refresh-1')) as never);

      await login('native@example.com', 'password');

      const init = fetchMock.mock.calls[0][1] as RequestInit;
      expect(init.headers).toMatchObject({ 'X-Agrovix-Auth-Transport': 'bearer' });
      expect(mockSetTokens).toHaveBeenCalledWith('access-1', 'refresh-1');
    },
  );

  test.each(['android', 'ios'])(
    '%s refresh selects bearer transport and atomically replaces the pair',
    async (os) => {
      mockPlatformOs = os;
      mockGetRefreshToken.mockResolvedValue('refresh-1');
      fetchMock.mockResolvedValue(jsonResponse(tokenPair('access-2', 'refresh-2')) as never);

      await refreshTokens();

      const init = fetchMock.mock.calls[0][1] as RequestInit;
      expect(init.headers).toMatchObject({ 'X-Agrovix-Auth-Transport': 'bearer' });
      expect(JSON.parse(init.body as string)).toEqual({ refresh_token: 'refresh-1' });
      expect(mockSetTokens).toHaveBeenCalledWith('access-2', 'refresh-2');
    },
  );

  test('Expo web login and refresh use cookies without entering bearer storage', async () => {
    mockPlatformOs = 'web';
    fetchMock.mockImplementation(
      async () => jsonResponse({ token_type: 'bearer', expires_in: 900 }) as never,
    );

    await login('browser@example.com', 'password');
    await refreshTokens();
    await authenticatedRequest('/v1/protected');

    for (const [, init] of fetchMock.mock.calls) {
      expect(init).toMatchObject({ credentials: 'include' });
      expect(init.headers).not.toHaveProperty('X-Agrovix-Auth-Transport');
    }
    expect(mockGetRefreshToken).not.toHaveBeenCalled();
    expect(mockGetAccessToken).not.toHaveBeenCalled();
    expect(mockSetTokens).not.toHaveBeenCalled();
  });

  test('concurrent authenticated failures share one refresh and retry with replacement access', async () => {
    let accessToken = 'access-1';
    let refreshRequests = 0;
    let releaseRefresh!: () => void;
    const refreshGate = new Promise<void>((resolve) => {
      releaseRefresh = resolve;
    });
    mockGetAccessToken.mockImplementation(async () => accessToken);
    mockGetRefreshToken.mockResolvedValue('refresh-1');
    mockSetTokens.mockImplementation(async (access) => {
      accessToken = access;
    });
    fetchMock.mockImplementation(async (url, init) => {
      if ((url as string).endsWith('/v1/auth/refresh')) {
        refreshRequests += 1;
        await refreshGate;
        return jsonResponse(tokenPair('access-2', 'refresh-2')) as never;
      }
      const authorization = (init as RequestInit).headers as Record<string, string>;
      return authorization.Authorization === 'Bearer access-1'
        ? (jsonResponse({ detail: 'Access token has expired.' }, 401) as never)
        : (jsonResponse({ ok: true }) as never);
    });

    const first = authenticatedRequest<{ ok: boolean }>('/v1/protected');
    const second = authenticatedRequest<{ ok: boolean }>('/v1/protected');
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(refreshRequests).toBe(1);
    releaseRefresh();

    await expect(Promise.all([first, second])).resolves.toEqual([{ ok: true }, { ok: true }]);
    expect(refreshRequests).toBe(1);
    const protectedCalls = fetchMock.mock.calls.filter(([url]) =>
      (url as string).endsWith('/v1/protected'),
    );
    expect(protectedCalls).toHaveLength(4);
    expect(
      protectedCalls
        .slice(2)
        .map(([, init]) => (init.headers as Record<string, string>).Authorization),
    ).toEqual(['Bearer access-2', 'Bearer access-2']);
  });

  test('logout uses the refresh-token body contract and always clears local tokens', async () => {
    mockGetRefreshToken.mockResolvedValue('refresh-2');
    fetchMock.mockResolvedValue(jsonResponse({ message: 'Logged out' }) as never);

    await logout();

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(init.body as string)).toEqual({ refresh_token: 'refresh-2' });
    expect(mockClearTokens).toHaveBeenCalledTimes(1);
  });

  test('logout clears tokens and preserves a refresh-token read failure', async () => {
    const readFailure = new Error('secure read failed');
    mockGetRefreshToken.mockRejectedValue(readFailure);

    await expect(logout()).rejects.toBe(readFailure);

    expect(fetchMock).not.toHaveBeenCalled();
    expect(mockClearTokens).toHaveBeenCalledTimes(1);
  });

  test('logout clears tokens and reports the original server failure', async () => {
    mockGetRefreshToken.mockResolvedValue('refresh-2');
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'server failed' }, 500) as never);
    mockClearTokens.mockRejectedValue(new Error('secure clear failed'));

    await expect(logout()).rejects.toMatchObject({ status: 500, detail: 'server failed' });

    expect(mockClearTokens).toHaveBeenCalledTimes(1);
  });
});
