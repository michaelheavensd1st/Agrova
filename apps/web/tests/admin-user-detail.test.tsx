import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

const { routerPush, stableRouter } = vi.hoisted(() => {
  const push = vi.fn();
  return { routerPush: push, stableRouter: { push, replace: vi.fn() } };
});

vi.mock('next/navigation', () => ({
  useRouter: () => stableRouter,
  useSearchParams: () => new URLSearchParams(window.location.search),
}));

vi.mock('next/link', () => ({
  default: ({ href, children, ...rest }: any) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, apiFetch } from '@/lib/api';
import { AdminUserDetail } from '@/components/admin-users/admin-user-detail';
import type { AdminUser } from '@/lib/admin-users';
import type { CurrentUser } from '@/lib/types';

const mockedApiFetch = vi.mocked(apiFetch);
const VIEWER: CurrentUser = {
  id: 'admin-1',
  email: 'admin@example.test',
  full_name: 'Admin',
  is_active: true,
  is_verified: true,
  is_superuser: false,
  permissions: ['platform.admin'],
  permission_scopes: [],
};

function target(overrides: Partial<AdminUser> = {}): AdminUser {
  return {
    id: 'target-1',
    email: 'target@example.test',
    full_name: 'Target User',
    is_active: true,
    is_verified: true,
    is_superuser: false,
    created_at: '2026-08-01T10:00:00Z',
    updated_at: '2026-08-01T10:00:00Z',
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function bootstrap(user = target(), viewer = VIEWER) {
  mockedApiFetch.mockImplementation((path: string) => {
    if (path === '/v1/auth/me') return Promise.resolve(viewer as never);
    if (path === `/v1/admin/users/${user.id}`) return Promise.resolve(user as never);
    throw new Error(`Unexpected ${path}`);
  });
}

describe('Sprint 5.6 admin user detail and actions', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    window.history.replaceState(
      {},
      '',
      '/admin/users/target-1?returnTo=%2Fadmin%2Fusers%3Fstatus%3Dactive',
    );
  });

  it('loads detail, preserves a safe directory return URL, and renders bounded fields', async () => {
    bootstrap();
    render(<AdminUserDetail userId="target-1" />);
    expect(await screen.findByText('target@example.test')).toBeVisible();
    expect(screen.getByRole('link', { name: /back to users/i })).toHaveAttribute(
      'href',
      '/admin/users?status=active',
    );
    expect(screen.getByText('Active')).toBeVisible();
    expect(screen.queryByText(/token|password|permission/i)).not.toBeInTheDocument();
  });

  it('suppresses self-disable and self-session-revoke while retaining server defense', async () => {
    const self = target({ id: VIEWER.id, email: VIEWER.email });
    bootstrap(self);
    render(<AdminUserDetail userId={VIEWER.id} />);
    expect(await screen.findByTestId('admin-self-action-note')).toBeVisible();
    expect(screen.queryByTestId('admin-disable-user')).not.toBeInTheDocument();
    expect(screen.queryByTestId('admin-revoke-sessions')).not.toBeInTheDocument();
  });

  it('requires a reason, treats disable as destructive, blocks duplicates, and reconciles', async () => {
    const post = deferred<AdminUser>();
    let detailReads = 0;
    mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (path === '/v1/auth/me') return Promise.resolve(VIEWER as never);
      if (path === '/v1/admin/users/target-1' && !init?.method) {
        detailReads += 1;
        return Promise.resolve(target({ is_active: detailReads === 1 }) as never);
      }
      if (path === '/v1/admin/users/target-1/disable') return post.promise as never;
      throw new Error(`Unexpected ${path}`);
    });
    render(<AdminUserDetail userId="target-1" />);
    fireEvent.click(await screen.findByTestId('admin-disable-user'));
    expect(screen.getByTestId('admin-user-action-confirm')).toHaveAttribute(
      'data-destructive',
      'true',
    );
    expect(screen.getByLabelText(/administrative reason/i)).toHaveFocus();
    fireEvent.click(screen.getByTestId('admin-user-action-confirm'));
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Enter a reason between 1 and 500 characters.',
    );
    fireEvent.change(screen.getByLabelText(/administrative reason/i), {
      target: { value: '  Security incident  ' },
    });
    fireEvent.click(screen.getByTestId('admin-user-action-confirm'));
    fireEvent.click(screen.getByTestId('admin-user-action-confirm'));
    expect(mockedApiFetch.mock.calls.filter(([path]) => path.endsWith('/disable'))).toHaveLength(1);
    expect(mockedApiFetch).toHaveBeenCalledWith('/v1/admin/users/target-1/disable', {
      method: 'POST',
      body: JSON.stringify({ reason: 'Security incident' }),
    });
    await act(async () => post.resolve(target({ is_active: false })));
    expect(await screen.findByTestId('admin-action-success')).toHaveTextContent('User disabled');
    expect(screen.getByTestId('admin-action-success')).toHaveFocus();
    expect(screen.getByTestId('admin-enable-user')).toBeVisible();
  });

  it('treats enable as restorative and reason-required', async () => {
    const disabled = target({ is_active: false });
    let reads = 0;
    mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (path === '/v1/auth/me') return Promise.resolve(VIEWER as never);
      if (path === '/v1/admin/users/target-1' && !init?.method) {
        reads += 1;
        return Promise.resolve((reads === 1 ? disabled : target()) as never);
      }
      if (path.endsWith('/enable')) return Promise.resolve(target() as never);
      throw new Error(`Unexpected ${path}`);
    });
    render(<AdminUserDetail userId="target-1" />);
    fireEvent.click(await screen.findByTestId('admin-enable-user'));
    expect(screen.getByTestId('admin-user-action-confirm')).toHaveAttribute(
      'data-destructive',
      'false',
    );
    expect(screen.getByText(/does not restore revoked sessions/i)).toBeVisible();
    fireEvent.change(screen.getByLabelText(/administrative reason/i), {
      target: { value: 'Access restored' },
    });
    fireEvent.click(screen.getByTestId('admin-user-action-confirm'));
    expect(await screen.findByTestId('admin-action-success')).toHaveTextContent('User enabled');
  });

  it('uses destructive confirmation and reports the authoritative revoked-session count', async () => {
    bootstrap();
    mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (path === '/v1/auth/me') return Promise.resolve(VIEWER as never);
      if (path === '/v1/admin/users/target-1' && !init?.method)
        return Promise.resolve(target() as never);
      if (path.endsWith('/sessions/revoke')) {
        return Promise.resolve({ user: target(), revoked_sessions: 2 } as never);
      }
      throw new Error(`Unexpected ${path}`);
    });
    render(<AdminUserDetail userId="target-1" />);
    fireEvent.click(await screen.findByTestId('admin-revoke-sessions'));
    expect(screen.getByTestId('admin-user-action-confirm')).toHaveAttribute(
      'data-destructive',
      'true',
    );
    expect(screen.getByText(/does not change their password/i)).toBeVisible();
    fireEvent.change(screen.getByLabelText(/administrative reason/i), {
      target: { value: 'Device compromise' },
    });
    fireEvent.click(screen.getByTestId('admin-user-action-confirm'));
    expect(await screen.findByTestId('admin-action-success')).toHaveTextContent(
      '2 active sessions revoked.',
    );
  });

  it('bounds server validation and action errors without rendering hostile payloads', async () => {
    bootstrap();
    mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (path === '/v1/auth/me') return Promise.resolve(VIEWER as never);
      if (path === '/v1/admin/users/target-1' && !init?.method)
        return Promise.resolve(target() as never);
      if (path.endsWith('/disable')) {
        return Promise.reject(
          new ApiError(422, { detail: [{ msg: 'SQLSTATE stack identifier' }] } as never),
        );
      }
      throw new Error(`Unexpected ${path}`);
    });
    render(<AdminUserDetail userId="target-1" />);
    fireEvent.click(await screen.findByTestId('admin-disable-user'));
    fireEvent.change(screen.getByLabelText(/administrative reason/i), {
      target: { value: 'Reason' },
    });
    fireEvent.click(screen.getByTestId('admin-user-action-confirm'));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Enter a reason between 1 and 500 characters.',
    );
    expect(document.body.textContent).not.toContain('SQLSTATE');
  });

  it('terminates an unauthorized mutation without replay and returns only navigation context', async () => {
    const targetId = '11111111-1111-4111-8111-111111111111';
    const targetUser = target({ id: targetId });
    mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (path === '/v1/auth/me') return Promise.resolve(VIEWER as never);
      if (path === `/v1/admin/users/${targetId}` && !init?.method) {
        return Promise.resolve(targetUser as never);
      }
      if (path === `/v1/admin/users/${targetId}/disable`) {
        return Promise.reject(new ApiError(401, { detail: 'expired' }));
      }
      throw new Error(`Unexpected ${path}`);
    });
    render(<AdminUserDetail userId={targetId} />);
    fireEvent.click(await screen.findByTestId('admin-disable-user'));
    fireEvent.change(screen.getByLabelText(/administrative reason/i), {
      target: { value: 'Fresh confirmation only' },
    });
    fireEvent.click(screen.getByTestId('admin-user-action-confirm'));
    expect(await screen.findByTestId('admin-action-error')).toHaveTextContent(
      'Your session has expired.',
    );
    expect(
      mockedApiFetch.mock.calls.filter(([path]) => path === `/v1/admin/users/${targetId}/disable`),
    ).toHaveLength(1);
    expect(routerPush).toHaveBeenCalledWith(
      `/login?returnTo=${encodeURIComponent(`/admin/users/${targetId}`)}`,
    );
  });

  it('traps focus, closes with Escape, and restores focus to the action trigger', async () => {
    bootstrap();
    render(<AdminUserDetail userId="target-1" />);
    const trigger = await screen.findByTestId('admin-disable-user');
    trigger.focus();
    fireEvent.click(trigger);
    const reason = screen.getByLabelText(/administrative reason/i);
    const confirm = screen.getByTestId('admin-user-action-confirm');
    expect(reason).toHaveFocus();
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true });
    expect(confirm).toHaveFocus();
    fireEvent.keyDown(window, { key: 'Tab' });
    expect(reason).toHaveFocus();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('admin-user-action-dialog')).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it('discards stale detail data when the target identity changes', async () => {
    const stale = deferred<AdminUser>();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(VIEWER as never);
      if (path === '/v1/admin/users/target-1') return stale.promise as never;
      if (path === '/v1/admin/users/target-2')
        return Promise.resolve(target({ id: 'target-2', email: 'new@example.test' }) as never);
      throw new Error(`Unexpected ${path}`);
    });
    const rendered = render(<AdminUserDetail userId="target-1" />);
    rendered.rerender(<AdminUserDetail userId="target-2" />);
    expect(await screen.findByText('new@example.test')).toBeVisible();
    await act(async () => stale.resolve(target({ email: 'stale@example.test' })));
    expect(screen.queryByText('stale@example.test')).not.toBeInTheDocument();
  });

  it('renders bounded forbidden, not-found, and failure states', async () => {
    for (const [status, testId] of [
      [403, 'admin-user-detail-forbidden'],
      [404, 'admin-user-not-found'],
      [500, 'admin-user-detail-error'],
    ] as const) {
      mockedApiFetch.mockReset();
      mockedApiFetch.mockImplementation((path: string) =>
        path === '/v1/auth/me'
          ? Promise.resolve(VIEWER as never)
          : Promise.reject(new ApiError(status, { detail: 'driver://secret stack' })),
      );
      const rendered = render(<AdminUserDetail userId="target-1" />);
      expect(await screen.findByTestId(testId)).toBeVisible();
      expect(document.body.textContent).not.toContain('driver://secret');
      rendered.unmount();
    }
  });
});
