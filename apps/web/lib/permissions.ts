import type { CurrentUser, UUID } from '@/lib/types';

export interface PermissionContext {
  organizationId: UUID;
  farmId?: UUID;
}

export function hasPlatformPermission(user: CurrentUser | null, permission: string): boolean {
  if (!user) return false;
  return (
    user.is_superuser || user.permissions.includes('*') || user.permissions.includes(permission)
  );
}

export function hasScopedPermission(
  user: CurrentUser | null,
  permission: string,
  context: PermissionContext | null,
): boolean {
  if (!user || !context) return false;
  if (
    user.is_superuser ||
    user.permissions.includes('*') ||
    user.permissions.includes(permission)
  ) {
    return true;
  }

  return (user.permission_scopes ?? []).some(
    (scope) =>
      scope.organization_id === context.organizationId &&
      (scope.farm_id === null || scope.farm_id === context.farmId) &&
      (scope.permissions.includes('*') || scope.permissions.includes(permission)),
  );
}
