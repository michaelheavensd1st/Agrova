import Link from 'next/link';
import { ResetPasswordForm } from '@/components/password-recovery-forms';

export default async function ResetPasswordPage({
  searchParams,
}: {
  searchParams: Promise<{ token?: string }>;
}) {
  const resolvedSearchParams = await searchParams;
  const token = typeof resolvedSearchParams?.token === 'string' ? resolvedSearchParams.token : null;
  return (
    <main
      className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6 py-12"
      data-testid="reset-password-page"
    >
      <h1 className="font-display text-3xl">Reset your password</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        Choose a new password. Completing recovery signs out existing sessions.
      </p>
      <ResetPasswordForm initialToken={token} />
      <Link
        href="/login"
        className="mt-6 text-sm text-primary hover:underline"
        data-testid="reset-back-to-login"
      >
        Back to sign in
      </Link>
    </main>
  );
}
