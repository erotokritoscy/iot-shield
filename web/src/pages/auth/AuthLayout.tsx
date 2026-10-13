/**
 * The frame around the two pages a logged-out visitor can reach: a centred card, outside the
 * app shell, because the shell's navigation leads only to pages they cannot open yet.
 */
import type * as React from "react";
import { Navigate, useLocation } from "react-router-dom";

import { ThemeToggle } from "@/components/ThemeToggle";
import { Card, Spinner } from "@/components/ui/surfaces";
import { safeRedirect, useAuth, type LoginRedirect } from "@/lib/auth";

export function AuthLayout({
  title,
  description,
  children,
}: {
  readonly title: string;
  readonly description: React.ReactNode;
  readonly children: React.ReactNode;
}) {
  const auth = useAuth();
  const location = useLocation();
  // Already in (or no login in this deployment): these pages have nothing to offer, so go
  // where the visitor was headed. This is the ONE post-login redirect: a successful sign-in
  // only updates the auth state and lands here. A second `navigate()` in the form raced this
  // one in a real browser and lost, returning every login to Triage.
  if (auth.data?.kind === "user" || auth.data?.kind === "disabled") {
    const target = safeRedirect((location.state as LoginRedirect | null)?.from);
    return <Navigate to={target} replace />;
  }

  return (
    <div className="flex min-h-full items-start justify-center px-4 py-16">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center justify-between">
          <span className="text-sm font-semibold tracking-tight text-ink">iotshield</span>
          <ThemeToggle />
        </div>
        <Card className="p-6">
          <h1 className="text-lg font-semibold tracking-tight text-ink">{title}</h1>
          <p className="caption mb-5 mt-1">{description}</p>
          {auth.isPending ? <Spinner label="Checking your login…" /> : children}
        </Card>
      </div>
    </div>
  );
}
