import Link from 'next/link';
import { ForgotPasswordForm } from '@/components/password-recovery-forms';

export default function ForgotPasswordPage() {
  return (
    <main
      className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6 py-12"
      data-testid="forgot-password-page"
    >
      <h1 className="font-display text-3xl">Recover your account</h1>
      <p className="mt-2 text-sm text-muted-foreground">
        Enter your email and we’ll send recovery instructions if the account is eligible.
      </p>
      <ForgotPasswordForm />
      <Link
        href="/login"
        className="mt-6 text-sm text-primary hover:underline"
        data-testid="recovery-back-to-login"
      >
        Back to sign in
      </Link>
    </main>
  );
}
