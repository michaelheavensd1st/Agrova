import { describe, expect, it } from 'vitest';

import { hasPlatformPermission, hasScopedPermission } from '@/lib/permissions';
import type { CurrentUser } from '@/lib/types';

const USER: CurrentUser = {
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
      farm_id: null,
      permissions: ['production_unit.create'],
    },
    {
      organization_id: 'org-1',
      farm_id: 'farm-1',
      permissions: ['production_batch.create'],
    },
  ],
};

describe('hasScopedPermission', () => {
  it('applies organization permissions only within that organization', () => {
    expect(
      hasScopedPermission(USER, 'production_unit.create', {
        organizationId: 'org-1',
        farmId: 'farm-2',
      }),
    ).toBe(true);
    expect(
      hasScopedPermission(USER, 'production_unit.create', {
        organizationId: 'org-2',
        farmId: 'farm-2',
      }),
    ).toBe(false);
  });

  it('applies farm permissions only to the exact farm', () => {
    expect(
      hasScopedPermission(USER, 'production_batch.create', {
        organizationId: 'org-1',
        farmId: 'farm-1',
      }),
    ).toBe(true);
    expect(
      hasScopedPermission(USER, 'production_batch.create', {
        organizationId: 'org-1',
        farmId: 'farm-2',
      }),
    ).toBe(false);
  });

  it('does not grant access for empty or absent permission scopes', () => {
    expect(
      hasScopedPermission({ ...USER, permission_scopes: [] }, 'production_unit.create', {
        organizationId: 'org-1',
        farmId: 'farm-1',
      }),
    ).toBe(false);
    expect(
      hasScopedPermission({ ...USER, permission_scopes: undefined }, 'production_unit.create', {
        organizationId: 'org-1',
        farmId: 'farm-1',
      }),
    ).toBe(false);
  });

  it('uses refreshed user scopes without retaining a previous authorization result', () => {
    const context = { organizationId: 'org-1', farmId: 'farm-1' };
    expect(hasScopedPermission(USER, 'production_batch.create', context)).toBe(true);
    expect(
      hasScopedPermission({ ...USER, permission_scopes: [] }, 'production_batch.create', context),
    ).toBe(false);
  });
});

describe('hasPlatformPermission', () => {
  it('accepts the exact platform capability, wildcard, or superuser flag', () => {
    expect(
      hasPlatformPermission({ ...USER, permissions: ['platform.admin'] }, 'platform.admin'),
    ).toBe(true);
    expect(hasPlatformPermission({ ...USER, permissions: ['*'] }, 'platform.admin')).toBe(true);
    expect(hasPlatformPermission({ ...USER, is_superuser: true }, 'platform.admin')).toBe(true);
  });

  it('does not treat organization permissions or ownership-like scopes as platform authority', () => {
    expect(hasPlatformPermission(USER, 'platform.admin')).toBe(false);
    expect(hasPlatformPermission(null, 'platform.admin')).toBe(false);
  });
});
