import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const { routerPush } = vi.hoisted(() => ({ routerPush: vi.fn() }));

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: routerPush }) }));
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return { ...actual, apiFetch: vi.fn() };
});

import { apiFetch, ApiError } from '@/lib/api';
import { AuthForm } from '@/components/auth-form';
import { safeAdminReturnTo } from '@/lib/admin-users';

const mockedApiFetch = vi.mocked(apiFetch);

async function submitLogin(returnTo?: string | null) {
  render(<AuthForm mode="login" returnTo={returnTo} />);
  fireEvent.change(screen.getByTestId('auth-email-input'), {
    target: { value: 'admin@example.test' },
  });
  fireEvent.change(screen.getByTestId('auth-password-input'), {
    target: { value: 'password123' },
  });
  fireEvent.submit(screen.getByTestId('login-form'));
  await waitFor(() => expect(mockedApiFetch).toHaveBeenCalledTimes(1));
}

describe('Sprint 5.6 administration login return destination', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    mockedApiFetch.mockResolvedValue({} as never);
    routerPush.mockReset();
  });

  it.each([
    ['/admin/users', '/admin/users'],
    ['/admin/users?offset=0', '/admin/users'],
    [
      '/admin/users?verified=false&search=Ada%20Lovelace&status=active&offset=50',
      '/admin/users?search=Ada+Lovelace&status=active&verified=false&offset=50',
    ],
    [
      '/admin/users/11111111-1111-4111-8111-111111111111',
      '/admin/users/11111111-1111-4111-8111-111111111111',
    ],
  ])('restores approved navigation context %s', async (returnTo, expected) => {
    await submitLogin(returnTo);
    expect(routerPush).toHaveBeenCalledWith(expected);
    expect(mockedApiFetch).toHaveBeenCalledWith('/v1/auth/login', expect.any(Object));
    expect(mockedApiFetch.mock.calls.some(([path]) => path.startsWith('/v1/admin/users'))).toBe(
      false,
    );
  });

  it.each([
    ['/admin/users?offset=0', '/admin/users'],
    ['/admin/users?offset=50', '/admin/users?offset=50'],
    ['/admin/users?offset=100', '/admin/users?offset=100'],
    ['/admin/users?offset=150', '/admin/users?offset=150'],
    ['/admin/users?offset=9007199254740950', '/admin/users?offset=9007199254740950'],
  ])('accepts canonical fixed-page offset %s', (returnTo, expected) => {
    expect(safeAdminReturnTo(returnTo)).toBe(expected);
  });

  it.each([
    '/admin/users?offset=1',
    '/admin/users?offset=49',
    '/admin/users?offset=51',
    '/admin/users?offset=-50',
    '/admin/users?offset=not-a-number',
    '/admin/users?offset=9007199254741000',
    '/admin/users?offset=50&offset=100',
    '/admin/users?offset=%E0%A4%A',
  ])('rejects noncanonical fixed-page offset %s', (returnTo) => {
    expect(safeAdminReturnTo(returnTo)).toBeNull();
  });

  it.each([
    ['https://attacker.example/admin/users'],
    ['//attacker.example/admin/users'],
    ['/%2Fattacker.example/admin/users'],
    ['javascript:alert(1)'],
    ['data:text/html,unsafe'],
    ['/inventory'],
    ['/admin/audit'],
    ['/admin/users#unsafe'],
    ['/admin/users?returnTo=%2Fadmin%2Fusers'],
    ['/admin/users?search=%E0%A4%A'],
    ['/admin/users/not-a-uuid'],
    [null],
  ])('falls back to the dashboard for an unsafe destination %#', async (returnTo) => {
    await submitLogin(returnTo);
    expect(routerPush).toHaveBeenCalledWith('/dashboard');
  });

  it('rejects duplicate, unknown, and invalid directory query state', () => {
    expect(safeAdminReturnTo('/admin/users?status=active&status=disabled')).toBeNull();
    expect(safeAdminReturnTo('/admin/users?unknown=value')).toBeNull();
    expect(safeAdminReturnTo('/admin/users?verified=yes')).toBeNull();
    expect(safeAdminReturnTo('/admin/users?offset=-1')).toBeNull();
  });

  it.each([
    [401, 'The email or password is incorrect.'],
    [422, 'Check the information you entered and try again.'],
    [429, 'Too many attempts. Please wait and try again.'],
    [503, 'The authentication service is unavailable. Please try again.'],
  ])('renders bounded authentication copy for HTTP %s', async (status, expected) => {
    const hostile = 'SQLSTATE password_hash internal-user-id stack trace';
    mockedApiFetch.mockRejectedValueOnce(
      new ApiError(status, { detail: hostile, validation: [{ message: hostile }] }),
    );

    await submitLogin();

    expect(await screen.findByRole('alert')).toHaveTextContent(expected);
    expect(screen.queryByText(new RegExp(hostile))).not.toBeInTheDocument();
    expect(routerPush).not.toHaveBeenCalled();
  });

  it('renders bounded operational copy for an arbitrary network exception', async () => {
    const hostile = 'fetch failed at internal.service.local with secret identifier';
    mockedApiFetch.mockRejectedValueOnce(new Error(hostile));

    await submitLogin();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The authentication service is unavailable. Please try again.',
    );
    expect(screen.queryByText(new RegExp(hostile))).not.toBeInTheDocument();
  });
});
