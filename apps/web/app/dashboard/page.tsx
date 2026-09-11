'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { apiFetch, ApiError } from '@/lib/api';
import { hasPlatformPermission } from '@/lib/permissions';
import type { CurrentUser } from '@/lib/types';

interface Organization {
  id: string;
  name: string;
  slug: string;
}

export default function DashboardPage() {
  const router = useRouter();
  const [orgs, setOrgs] = useState<Organization[] | null>(null);
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [list, viewer] = await Promise.all([
          apiFetch<Organization[]>('/v1/organizations'),
          apiFetch<CurrentUser>('/v1/auth/me'),
        ]);
        setOrgs(list);
        setCurrentUser(viewer);
        if (list.length === 0 && !hasPlatformPermission(viewer, 'platform.admin')) {
          router.push('/onboarding');
        }
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          router.push('/login');
        } else {
          setError('Unable to load the dashboard. Try again.');
        }
      }
    })();
  }, [router]);

  return (
    <main className="mx-auto max-w-6xl px-6 py-12" data-testid="dashboard-page">
      <div className="mb-8 flex items-center justify-between">
        <div>
          <p className="text-xs uppercase tracking-widest text-muted-foreground">Sprint 1</p>
          <h1 className="font-display text-3xl">Your organizations</h1>
        </div>
        <div className="flex flex-wrap gap-2">
          {hasPlatformPermission(currentUser, 'platform.admin') && (
            <Link
              href="/admin/users"
              data-testid="dashboard-platform-admin-link"
              className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary"
            >
              Platform administration
            </Link>
          )}
          <Link
            href="/onboarding"
            data-testid="dashboard-new-org-link"
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary"
          >
            + New organization
          </Link>
        </div>
      </div>

      {error && (
        <p
          className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive"
          data-testid="dashboard-error"
        >
          {error}
        </p>
      )}

      {orgs && orgs.length > 0 ? (
        <ul className="grid gap-4 sm:grid-cols-2" data-testid="dashboard-org-list">
          {orgs.map((o) => (
            <li key={o.id}>
              <Link
                href={`/organizations/${o.id}`}
                data-testid={`dashboard-org-${o.slug}`}
                className="block rounded-2xl border border-border bg-card/60 p-6 transition hover:border-primary/40 hover:shadow-md"
              >
                <p className="text-sm text-muted-foreground">{o.slug}</p>
                <p className="mt-1 text-lg font-semibold">{o.name}</p>
              </Link>
            </li>
          ))}
        </ul>
      ) : (
        <div
          data-testid="dashboard-empty-state"
          className="rounded-2xl border border-dashed border-border bg-card/40 p-10 text-center"
        >
          <p className="font-display text-xl">
            {hasPlatformPermission(currentUser, 'platform.admin')
              ? 'No organizations available.'
              : 'Redirecting to onboarding…'}
          </p>
        </div>
      )}
    </main>
  );
}
