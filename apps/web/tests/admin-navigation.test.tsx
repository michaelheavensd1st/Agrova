import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

const { routerPush } = vi.hoisted(() => ({ routerPush: vi.fn() }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ push: routerPush }) }));
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

import { apiFetch } from '@/lib/api';
import DashboardPage from '@/app/dashboard/page';
import type { CurrentUser } from '@/lib/types';

const mockedApiFetch = vi.mocked(apiFetch);
const USER: CurrentUser = {
  id: 'user-1',
  email: 'user@example.test',
  full_name: null,
  is_active: true,
  is_verified: true,
  is_superuser: false,
  permissions: [],
  permission_scopes: [],
};

describe('Sprint 5.6 administration navigation', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
  });

  it.each([
    [{ ...USER, permissions: ['platform.admin'] }, true],
    [{ ...USER, permissions: ['*'] }, true],
    [{ ...USER, is_superuser: true }, true],
    [USER, false],
  ] as const)('gates the entry point by effective platform authority', async (viewer, visible) => {
    mockedApiFetch.mockImplementation((path: string) =>
      Promise.resolve(
        (path === '/v1/auth/me' ? viewer : [{ id: 'org-1', name: 'Org', slug: 'org' }]) as never,
      ),
    );
    render(<DashboardPage />);
    await screen.findByText('Org');
    if (visible) {
      expect(screen.getByTestId('dashboard-platform-admin-link')).toHaveAttribute(
        'href',
        '/admin/users',
      );
    } else {
      expect(screen.queryByTestId('dashboard-platform-admin-link')).not.toBeInTheDocument();
    }
  });
});
