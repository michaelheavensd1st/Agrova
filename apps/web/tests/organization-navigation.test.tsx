import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('next/navigation', () => ({
  useParams: () => ({ id: 'org-1' }),
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

import { apiFetch } from '@/lib/api';
import OrganizationDetail from '@/app/organizations/[id]/page';

const mockedApiFetch = vi.mocked(apiFetch);

describe('NAV-UAT-01 organization navigation', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();

    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations/org-1') {
        return Promise.resolve({
          id: 'org-1',
          name: 'UAT Organization',
          slug: 'uat-organization',
        } as never);
      }

      if (path === '/v1/organizations/org-1/farms') {
        return Promise.resolve([] as never);
      }

      return Promise.reject(new Error(`Unexpected API path: ${path}`));
    });
  });

  it('exposes Purchase Orders from the organization hub with organization scope preserved', async () => {
    render(<OrganizationDetail />);

    await screen.findByText('UAT Organization');

    expect(screen.getByTestId('organization-purchase-orders-link')).toHaveAttribute(
      'href',
      '/purchase-orders?organization_id=org-1',
    );

    expect(screen.getByTestId('organization-purchase-orders-link')).toHaveTextContent(
      'Purchase Orders',
    );
  });
});
