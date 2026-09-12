/**
 * Secure token storage helpers.
 *
 * Uses Expo SecureStore (Keychain on iOS, EncryptedSharedPreferences on
 * Android). On web, SecureStore is not available — we intentionally
 * fall back to sessionStorage there so tokens NEVER touch localStorage.
 * The canonical web client (apps/web) uses httpOnly cookies exclusively.
 */

import * as SecureStore from 'expo-secure-store';
import { Platform } from 'react-native';

const TOKEN_PAIR_KEY = 'agrovix.token_pair';
const LEGACY_ACCESS_KEY = 'agrovix.access_token';
const LEGACY_REFRESH_KEY = 'agrovix.refresh_token';

interface StoredTokenPair {
  accessToken: string;
  refreshToken: string;
}

let storageQueue: Promise<void> = Promise.resolve();

function withStorageLock<T>(operation: () => Promise<T>): Promise<T> {
  const result = storageQueue.then(operation, operation);
  storageQueue = result.then(
    () => undefined,
    () => undefined,
  );
  return result;
}

async function setItem(key: string, value: string): Promise<void> {
  if (Platform.OS === 'web') {
    if (typeof window !== 'undefined') window.sessionStorage.setItem(key, value);
    return;
  }
  await SecureStore.setItemAsync(key, value, {
    keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
  });
}

async function getItem(key: string): Promise<string | null> {
  if (Platform.OS === 'web') {
    if (typeof window === 'undefined') return null;
    return window.sessionStorage.getItem(key);
  }
  return SecureStore.getItemAsync(key);
}

async function deleteItem(key: string): Promise<void> {
  if (Platform.OS === 'web') {
    if (typeof window !== 'undefined') window.sessionStorage.removeItem(key);
    return;
  }
  await SecureStore.deleteItemAsync(key);
}

async function setTokensUnlocked(access: string, refresh: string): Promise<void> {
  await setItem(TOKEN_PAIR_KEY, JSON.stringify({ accessToken: access, refreshToken: refresh }));
  await Promise.all([deleteItem(LEGACY_ACCESS_KEY), deleteItem(LEGACY_REFRESH_KEY)]);
}

export function setTokens(access: string, refresh: string): Promise<void> {
  return withStorageLock(() => setTokensUnlocked(access, refresh));
}

export function clearTokens(): Promise<void> {
  return withStorageLock(async () => {
    const results = await Promise.allSettled([
      deleteItem(TOKEN_PAIR_KEY),
      deleteItem(LEGACY_ACCESS_KEY),
      deleteItem(LEGACY_REFRESH_KEY),
    ]);
    const failure = results.find((result) => result.status === 'rejected');
    if (failure?.status === 'rejected') throw failure.reason;
  });
}

async function getTokensUnlocked(): Promise<StoredTokenPair | null> {
  const value = await getItem(TOKEN_PAIR_KEY);
  if (value) {
    try {
      const tokens = JSON.parse(value) as Partial<StoredTokenPair>;
      return typeof tokens.accessToken === 'string' && typeof tokens.refreshToken === 'string'
        ? (tokens as StoredTokenPair)
        : null;
    } catch {
      return null;
    }
  }

  const [accessToken, refreshToken] = await Promise.all([
    getItem(LEGACY_ACCESS_KEY),
    getItem(LEGACY_REFRESH_KEY),
  ]);
  if (!accessToken || !refreshToken) return null;
  await setTokensUnlocked(accessToken, refreshToken);
  return { accessToken, refreshToken };
}

export function getAccessToken(): Promise<string | null> {
  return withStorageLock(async () => (await getTokensUnlocked())?.accessToken ?? null);
}

export function getRefreshToken(): Promise<string | null> {
  return withStorageLock(async () => (await getTokensUnlocked())?.refreshToken ?? null);
}
