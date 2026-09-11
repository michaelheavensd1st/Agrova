import type { AdminUser } from '@/lib/admin-users';

function Badge({ children, tone = 'neutral' }: { children: React.ReactNode; tone?: string }) {
  const colors =
    tone === 'good'
      ? 'bg-primary/10 text-primary'
      : tone === 'bad'
        ? 'bg-destructive/10 text-destructive'
        : 'bg-secondary text-secondary-foreground';
  return (
    <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${colors}`}>{children}</span>
  );
}

export function AdminUserBadges({ user }: { user: AdminUser }) {
  return (
    <span
      className="flex flex-wrap gap-2"
      aria-label="Account status"
      data-testid="admin-user-badges"
    >
      <Badge tone={user.is_active ? 'good' : 'bad'}>{user.is_active ? 'Active' : 'Disabled'}</Badge>
      <Badge tone={user.is_verified ? 'good' : 'neutral'}>
        {user.is_verified ? 'Verified' : 'Unverified'}
      </Badge>
      {user.is_superuser && <Badge>Superuser</Badge>}
    </span>
  );
}
