import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';

const { routerPush, routerReplace, stableRouter, urlListeners } = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  const replace = vi.fn((url: string) => {
    window.history.replaceState({}, '', url);
    listeners.forEach((listener) => listener());
  });
  const push = vi.fn();
  return {
    routerPush: push,
    routerReplace: replace,
    stableRouter: { push, replace },
    urlListeners: listeners,
  };
});

vi.mock('next/navigation', async () => {
  const React = await vi.importActual<typeof import('react')>('react');
  return {
    useRouter: () => stableRouter,
    useSearchParams: () => {
      const [search, setSearch] = React.useState(window.location.search);
      React.useEffect(() => {
        const listener = () => setSearch(window.location.search);
        urlListeners.add(listener);
        return () => {
          urlListeners.delete(listener);
        };
      }, []);
      return new URLSearchParams(search);
    },
  };
});

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
import { AdminUserDirectory } from '@/components/admin-users/admin-user-directory';
import { normalizeAdminReason, type AdminUser, type AdminUserPage } from '@/lib/admin-users';

const mockedApiFetch = vi.mocked(apiFetch);

function user(id: string, overrides: Partial<AdminUser> = {}): AdminUser {
  return {
    id,
    email: `${id}@example.test`,
    full_name: `User ${id}`,
    is_active: true,
    is_verified: true,
    is_superuser: false,
    created_at: '2026-08-01T10:00:00Z',
    updated_at: '2026-08-01T10:00:00Z',
    ...overrides,
  };
}

function page(items: AdminUser[], total = items.length, offset = 0): AdminUserPage {
  return { items, total, limit: 50, offset };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe('Sprint 5.6 admin user directory', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    window.history.replaceState({}, '', '/admin/users');
  });

  it('loads the fixed server page and renders bounded account fields and badges', async () => {
    mockedApiFetch.mockResolvedValue(page([user('one', { is_superuser: true })]));
    render(<AdminUserDirectory />);
    expect(await screen.findByText('one@example.test')).toBeVisible();
    expect(mockedApiFetch).toHaveBeenCalledWith('/v1/admin/users?limit=50');
    const badges = within(screen.getByTestId('admin-user-badges'));
    expect(badges.getByText('Active')).toBeVisible();
    expect(badges.getByText('Verified')).toBeVisible();
    expect(badges.getByText('Superuser')).toBeVisible();
    expect(screen.getByRole('link', { name: 'View details' })).toHaveAttribute(
      'href',
      '/admin/users/one?returnTo=%2Fadmin%2Fusers',
    );
  });

  it('serializes normalized search and frozen filters into the URL and API query', async () => {
    mockedApiFetch.mockResolvedValue(page([]));
    render(<AdminUserDirectory />);
    await screen.findByText('No users available');
    fireEvent.change(screen.getByLabelText('Email or full name'), { target: { value: '  Ada  ' } });
    fireEvent.submit(screen.getByTestId('admin-user-filter-form'));
    await waitFor(() => expect(routerReplace).toHaveBeenCalledWith('/admin/users?search=Ada'));
    fireEvent.change(screen.getByLabelText('Account status'), { target: { value: 'disabled' } });
    await waitFor(() =>
      expect(routerReplace).toHaveBeenCalledWith('/admin/users?search=Ada&status=disabled'),
    );
    fireEvent.change(screen.getByLabelText('Verification'), { target: { value: 'false' } });
    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenLastCalledWith(
        '/v1/admin/users?search=Ada&status=disabled&verified=false&limit=50',
      ),
    );
  });

  it('discards a stale response after the filter identity changes', async () => {
    const stale = deferred<AdminUserPage>();
    mockedApiFetch
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce(page([user('disabled', { is_active: false })]));
    render(<AdminUserDirectory />);
    fireEvent.change(screen.getByLabelText('Account status'), { target: { value: 'disabled' } });
    expect(await screen.findByText('disabled@example.test')).toBeVisible();
    await act(async () => stale.resolve(page([user('stale')])));
    expect(screen.queryByText('stale@example.test')).not.toBeInTheDocument();
  });

  it('paginates by the frozen offset and preserves current filters', async () => {
    window.history.replaceState({}, '', '/admin/users?search=Ada&status=active');
    mockedApiFetch.mockResolvedValue(page([user('one')], 51, 0));
    render(<AdminUserDirectory />);
    await screen.findByText('Showing 1–1 of 51');
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(routerReplace).toHaveBeenCalledWith('/admin/users?search=Ada&status=active&offset=50');
    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenLastCalledWith(
        '/v1/admin/users?search=Ada&status=active&limit=50&offset=50',
      ),
    );
  });

  it('recovers an out-of-range page to the nearest valid offset with filters intact', async () => {
    window.history.replaceState(
      {},
      '',
      '/admin/users?search=Ada&status=disabled&verified=false&offset=150',
    );
    mockedApiFetch
      .mockResolvedValueOnce(page([], 51, 150))
      .mockResolvedValueOnce(page([user('last')], 51, 50));
    render(<AdminUserDirectory />);
    await waitFor(() =>
      expect(routerReplace).toHaveBeenCalledWith(
        '/admin/users?search=Ada&status=disabled&verified=false&offset=50',
      ),
    );
    expect(await screen.findByText('last@example.test')).toBeVisible();
  });

  it('does not redirect an exact final valid page', async () => {
    window.history.replaceState({}, '', '/admin/users?offset=50');
    mockedApiFetch.mockResolvedValue(page([user('last')], 51, 50));
    render(<AdminUserDirectory />);
    expect(await screen.findByText('Showing 51–51 of 51')).toBeVisible();
    expect(routerReplace).not.toHaveBeenCalled();
  });

  it('does not let a stale empty page trigger recovery navigation', async () => {
    window.history.replaceState({}, '', '/admin/users?offset=100');
    const stale = deferred<AdminUserPage>();
    mockedApiFetch
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce(page([user('active')], 1, 0));
    render(<AdminUserDirectory />);
    fireEvent.change(screen.getByLabelText('Account status'), { target: { value: 'active' } });
    expect(await screen.findByText('active@example.test')).toBeVisible();
    routerReplace.mockClear();
    await act(async () => stale.resolve(page([], 51, 100)));
    expect(routerReplace).not.toHaveBeenCalled();
    expect(screen.getByText('active@example.test')).toBeVisible();
  });

  it('renders distinct no-results and authorization states without backend detail', async () => {
    window.history.replaceState({}, '', '/admin/users?search=missing');
    mockedApiFetch.mockResolvedValueOnce(page([]));
    const { unmount } = render(<AdminUserDirectory />);
    expect(await screen.findByText('No matching users')).toBeVisible();
    unmount();

    window.history.replaceState({}, '', '/admin/users');
    mockedApiFetch.mockRejectedValueOnce(new ApiError(403, { detail: 'SQLSTATE secret' }));
    render(<AdminUserDirectory />);
    expect(await screen.findByTestId('admin-users-forbidden')).toHaveTextContent(
      'You do not have access to platform administration.',
    );
    expect(document.body.textContent).not.toContain('SQLSTATE secret');
  });

  it('redirects expired authentication and bounds arbitrary failures', async () => {
    mockedApiFetch.mockRejectedValueOnce(new ApiError(401, { detail: 'raw cookie detail' }));
    const { unmount } = render(<AdminUserDirectory />);
    await waitFor(() =>
      expect(routerPush).toHaveBeenCalledWith('/login?returnTo=%2Fadmin%2Fusers'),
    );
    unmount();
    mockedApiFetch.mockRejectedValueOnce(
      new ApiError(500, { detail: ['stack', 'identifier'] } as never),
    );
    render(<AdminUserDirectory />);
    expect(await screen.findByTestId('admin-users-error')).toHaveTextContent(
      'Unable to load the user directory. Try again.',
    );
    expect(document.body.textContent).not.toContain('identifier');
  });

  it('normalizes the frozen 1–500 character administrative reason contract', () => {
    expect(normalizeAdminReason('   ')).toBeNull();
    expect(normalizeAdminReason(`  ${'x'.repeat(500)}  `)).toBe('x'.repeat(500));
    expect(normalizeAdminReason('x'.repeat(501))).toBeNull();
  });
});
