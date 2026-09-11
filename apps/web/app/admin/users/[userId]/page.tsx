'use client';

import { Suspense } from 'react';
import { useParams } from 'next/navigation';
import { AdminUserDetail } from '@/components/admin-users/admin-user-detail';

function AdminUserDetailRoute() {
  const params = useParams<{ userId: string }>();
  return <AdminUserDetail userId={params.userId} />;
}

export default function AdminUserDetailPage() {
  return (
    <Suspense
      fallback={
        <main
          className="mx-auto max-w-4xl px-6 py-10"
          data-testid="admin-user-detail-route-loading"
        >
          Loading platform user…
        </main>
      }
    >
      <AdminUserDetailRoute />
    </Suspense>
  );
}
