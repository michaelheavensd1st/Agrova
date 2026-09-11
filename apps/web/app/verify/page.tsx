import Link from 'next/link';

export default async function VerifyPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>;
}) {
  const resolvedSearchParams = await searchParams;
  const token = resolvedSearchParams?.token;
  return (
    <main
      className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6 py-12"
      data-testid="verify-page"
    >
      <h1 className="font-display text-3xl">Verify your email</h1>
      <p className="mt-4 text-sm text-muted-foreground">
        {token
          ? 'Click the button below to confirm your address.'
          : 'Open the verification link that was sent to your inbox.'}
      </p>
      {token && <VerifyClient token={token} />}
      <Link
        href="/login"
        data-testid="verify-back-link"
        className="mt-8 text-sm text-primary hover:underline"
      >
        Back to sign in
      </Link>
    </main>
  );
}

// Client verify island — imported inline to keep the page RSC.
import { VerifyClient } from '@/components/verify-client';
