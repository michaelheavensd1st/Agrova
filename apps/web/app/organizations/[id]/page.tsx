'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import { apiFetch, ApiError } from '@/lib/api';

interface Organization {
  id: string;
  name: string;
  slug: string;
}
interface Farm {
  id: string;
  name: string;
  code: string;
}

export default function OrganizationDetail() {
  const params = useParams<{ id: string }>();
  const orgId = params.id;
  const [org, setOrg] = useState<Organization | null>(null);
  const [farms, setFarms] = useState<Farm[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const [o, f] = await Promise.all([
          apiFetch<Organization>(`/v1/organizations/${orgId}`),
          apiFetch<Farm[]>(`/v1/organizations/${orgId}/farms`),
        ]);
        setOrg(o);
        setFarms(f);
      } catch (err) {
        setError(err instanceof ApiError ? (err.payload.detail ?? 'Failed to load') : String(err));
      }
    })();
  }, [orgId]);

  if (error)
    return (
      <main className="mx-auto max-w-3xl px-6 py-12" data-testid="organization-error">
        <p className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{error}</p>
      </main>
    );

  return (
    <main className="mx-auto max-w-3xl px-6 py-12" data-testid="organization-detail">
      <p className="text-xs uppercase tracking-widest text-muted-foreground">Organization</p>
      <h1 className="font-display text-3xl">{org?.name ?? 'Loading…'}</h1>

      <section className="mt-10">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-semibold">Farms</h2>
          <Link
            href={`/organizations/${orgId}/farms/new`}
            data-testid="organization-add-farm-link"
            className="rounded-md border border-border px-3 py-1.5 text-xs font-medium hover:bg-secondary"
          >
            + Add farm
          </Link>
        </div>
        {farms.length === 0 ? (
          <p
            className="rounded-2xl border border-dashed border-border p-8 text-center text-sm text-muted-foreground"
            data-testid="organization-empty-farms"
          >
            No farms yet.
          </p>
        ) : (
          <ul
            className="divide-y divide-border rounded-2xl border border-border"
            data-testid="organization-farm-list"
          >
            {farms.map((f) => (
              <li key={f.id} className="flex items-center justify-between px-4 py-3">
                <Link
                  href={`/farms/${f.id}`}
                  data-testid={`organization-farm-${f.code}`}
                  className="flex-1"
                >
                  <p className="text-sm font-medium">{f.name}</p>
                  <p className="text-xs text-muted-foreground">{f.code}</p>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="mt-10">
        <h2 className="text-lg font-semibold">Operations</h2>
        <div className="mt-4">
          <Link
            href={`/purchase-orders?organization_id=${encodeURIComponent(orgId)}`}
            data-testid="organization-purchase-orders-link"
            className="block rounded-2xl border border-border bg-card/60 p-4 transition hover:border-primary/40 hover:shadow-sm"
          >
            <p className="text-sm font-medium">Purchase Orders</p>
            <p className="mt-1 text-xs text-muted-foreground">
              Create, review, approve, and receive organization purchase orders.
            </p>
          </Link>
        </div>
      </section>

      <section className="mt-10">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Team</h2>
          <Link
            href={`/organizations/${orgId}/invitations/new`}
            data-testid="organization-invite-link"
            className="rounded-md border border-border px-3 py-1.5 text-xs font-medium hover:bg-secondary"
          >
            Invite a teammate
          </Link>
        </div>
      </section>
    </main>
  );
}
