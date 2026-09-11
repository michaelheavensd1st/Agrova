'use client';

import { Suspense } from 'react';
import { AdminUserDirectory } from '@/components/admin-users/admin-user-directory';

export default function AdminUsersPage() {
  return (
    <Suspense
      fallback={
        <main className="mx-auto max-w-6xl px-6 py-10" data-testid="admin-users-route-loading">
          Loading platform administration…
        </main>
      }
    >
      <AdminUserDirectory />
    </Suspense>
  );
}
