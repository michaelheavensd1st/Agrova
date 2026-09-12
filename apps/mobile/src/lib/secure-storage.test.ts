/// <reference types="jest" />

jest.mock('react-native', () => ({ Platform: { OS: 'android' } }));
jest.mock('expo-secure-store', () => ({
  WHEN_UNLOCKED_THIS_DEVICE_ONLY: 'when-unlocked',
  setItemAsync: jest.fn(),
  getItemAsync: jest.fn(),
  deleteItemAsync: jest.fn(),
}));

import * as SecureStore from 'expo-secure-store';
import { clearTokens, getAccessToken, getRefreshToken, setTokens } from './secure-storage';

const secureValues = new Map<string, string>();
const mockSetItemAsync = jest.mocked(SecureStore.setItemAsync);
const mockGetItemAsync = jest.mocked(SecureStore.getItemAsync);
const mockDeleteItemAsync = jest.mocked(SecureStore.deleteItemAsync);

describe('native SecureStore token lifecycle', () => {
  beforeEach(async () => {
    jest.clearAllMocks();
    secureValues.clear();
    mockSetItemAsync.mockImplementation(async (key, value) => {
      secureValues.set(key, value);
    });
    mockGetItemAsync.mockImplementation(async (key) => secureValues.get(key) ?? null);
    mockDeleteItemAsync.mockImplementation(async (key) => {
      secureValues.delete(key);
    });
    await clearTokens();
    jest.clearAllMocks();
  });

  test('writes and replaces a token pair as one SecureStore value', async () => {
    await setTokens('access-1', 'refresh-1');
    await setTokens('access-2', 'refresh-2');

    expect(await getAccessToken()).toBe('access-2');
    expect(await getRefreshToken()).toBe('refresh-2');
    expect(secureValues.get('agrovix.token_pair')).toBe(
      JSON.stringify({ accessToken: 'access-2', refreshToken: 'refresh-2' }),
    );
    expect(mockSetItemAsync).toHaveBeenCalledTimes(2);
  });

  test('a failed pair write leaves the complete old pair readable', async () => {
    await setTokens('access-1', 'refresh-1');
    mockSetItemAsync.mockRejectedValueOnce(new Error('write failed'));

    await expect(setTokens('access-2', 'refresh-2')).rejects.toThrow('write failed');

    expect(await getAccessToken()).toBe('access-1');
    expect(await getRefreshToken()).toBe('refresh-1');
  });

  test('clear attempts every credential deletion and reports a failure', async () => {
    secureValues.set('agrovix.token_pair', 'pair');
    secureValues.set('agrovix.access_token', 'legacy-access');
    secureValues.set('agrovix.refresh_token', 'legacy-refresh');
    mockDeleteItemAsync.mockRejectedValueOnce(new Error('delete failed'));

    await expect(clearTokens()).rejects.toThrow('delete failed');

    expect(mockDeleteItemAsync).toHaveBeenCalledTimes(3);
    expect(secureValues.has('agrovix.access_token')).toBe(false);
    expect(secureValues.has('agrovix.refresh_token')).toBe(false);
  });

  test('serialized legacy migration cannot overwrite a newer queued pair', async () => {
    secureValues.set('agrovix.access_token', 'legacy-access');
    secureValues.set('agrovix.refresh_token', 'legacy-refresh');

    const migration = getAccessToken();
    const replacement = setTokens('new-access', 'new-refresh');

    await expect(migration).resolves.toBe('legacy-access');
    await replacement;
    expect(await getAccessToken()).toBe('new-access');
    expect(await getRefreshToken()).toBe('new-refresh');
    expect(secureValues.has('agrovix.access_token')).toBe(false);
    expect(secureValues.has('agrovix.refresh_token')).toBe(false);
  });
});
