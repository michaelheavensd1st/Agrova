import Constants from 'expo-constants';
import { Platform } from 'react-native';
import { clearTokens, getAccessToken, getRefreshToken, setTokens } from './secure-storage';

export function resolveApiUrl(publicUrl?: string, configuredUrl?: string): string {
  return publicUrl || configuredUrl || 'http://localhost:8000/api';
}

const API_URL = resolveApiUrl(
  process.env.EXPO_PUBLIC_API_URL,
  Constants.expoConfig?.extra?.apiUrl as string | undefined,
);
const NATIVE_AUTH_HEADERS = { 'X-Agrovix-Auth-Transport': 'bearer' } as const;
let refreshPromise: Promise<void> | null = null;

function isNativePlatform(): boolean {
  return Platform.OS === 'android' || Platform.OS === 'ios';
}

export interface RegisterPayload {
  email: string;
  password: string;
  full_name: string | null;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: 'bearer';
  expires_in: number;
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(detail);
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  auth = false,
  mayRefresh = true,
): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((init.headers as Record<string, string>) ?? {}),
  };
  if (auth && isNativePlatform()) {
    const token = await getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }
  const res = await fetch(`${API_URL}${path}`, {
    ...init,
    ...(Platform.OS === 'web' ? { credentials: 'include' as const } : {}),
    headers,
  });
  if (res.status === 401 && auth && mayRefresh && isNativePlatform()) {
    await refreshTokens();
    return request<T>(path, init, true, false);
  }
  const isJson = res.headers.get('content-type')?.includes('application/json');
  const body = isJson ? await res.json() : undefined;
  if (!res.ok) {
    const detail = (body as { detail?: string } | undefined)?.detail ?? 'Request failed';
    throw new ApiError(res.status, detail);
  }
  return body as T;
}

export async function register(payload: RegisterPayload): Promise<void> {
  await request('/v1/auth/register', { method: 'POST', body: JSON.stringify(payload) });
}

export async function login(email: string, password: string): Promise<void> {
  const native = isNativePlatform();
  const response = await request<TokenPair>('/v1/auth/login', {
    method: 'POST',
    ...(native ? { headers: NATIVE_AUTH_HEADERS } : {}),
    body: JSON.stringify({ email, password }),
  });
  if (native) await setTokens(response.access_token, response.refresh_token);
}

async function performRefresh(): Promise<void> {
  if (!isNativePlatform()) {
    await request('/v1/auth/refresh', { method: 'POST', body: '{}' });
    return;
  }
  const refreshToken = await getRefreshToken();
  if (!refreshToken) throw new ApiError(401, 'Missing refresh token.');
  const tokens = await request<TokenPair>('/v1/auth/refresh', {
    method: 'POST',
    headers: NATIVE_AUTH_HEADERS,
    body: JSON.stringify({ refresh_token: refreshToken }),
  });
  await setTokens(tokens.access_token, tokens.refresh_token);
}

export async function refreshTokens(): Promise<void> {
  if (refreshPromise) return refreshPromise;
  const pending = performRefresh();
  refreshPromise = pending;
  try {
    await pending;
  } finally {
    if (refreshPromise === pending) refreshPromise = null;
  }
}

export async function authenticatedRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  return request<T>(path, init, true);
}

export async function logout(): Promise<void> {
  let originalError: unknown;
  try {
    const refreshToken = isNativePlatform() ? await getRefreshToken() : null;
    await request('/v1/auth/logout', {
      method: 'POST',
      body: JSON.stringify(refreshToken ? { refresh_token: refreshToken } : {}),
    });
  } catch (error) {
    originalError = error;
  }
  try {
    await clearTokens();
  } catch (error) {
    if (originalError === undefined) originalError = error;
  }
  if (originalError !== undefined) throw originalError;
}
