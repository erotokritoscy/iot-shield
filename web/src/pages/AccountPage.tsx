import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Button, Field, TextInput } from "@/components/ui/controls";
import { Card, Notice, PageHeader, Section } from "@/components/ui/surfaces";
import { authApi } from "@/lib/api/client";
import { describeRefusal } from "@/lib/api/http";
import { useAuth } from "@/lib/auth";
import { MIN_PASSWORD } from "@/pages/auth/RegisterPage";

/** Who you are, and the one thing every user may change about themselves: the password. */
export function AccountPage() {
  const auth = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");

  const change = useMutation({
    mutationFn: () => authApi.changePassword(current, next),
    onSuccess: () => {
      setCurrent("");
      setNext("");
      setConfirm("");
    },
  });

  if (auth.data?.kind !== "user") {
    return (
      <>
        <PageHeader title="Account" description="Your login and organization." />
        <Notice tone="info">This deployment has no login, so there is no account to manage.</Notice>
      </>
    );
  }
  const { me } = auth.data;

  const problem =
    next.length < MIN_PASSWORD
      ? `The new password needs at least ${MIN_PASSWORD} characters.`
      : next !== confirm
        ? "The two new passwords do not match."
        : null;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (problem === null && current) change.mutate();
  };

  return (
    <>
      <PageHeader title="Account" description="Your login and organization." />
      <div className="max-w-md space-y-8">
        <Card className="p-4">
          <dl className="grid grid-cols-[8rem_1fr] gap-y-1.5 text-sm">
            <dt className="text-muted">Username</dt>
            <dd className="text-ink">{me.username}</dd>
            <dt className="text-muted">Role</dt>
            <dd className="text-ink">{me.role}</dd>
            <dt className="text-muted">Organization</dt>
            <dd className="text-ink">
              {me.organization ? (
                <>
                  {me.organization.name}{" "}
                  <span className="font-mono text-xs text-muted">({me.organization.org_id})</span>
                </>
              ) : (
                <span className="text-muted">none — the super admin manages every organization</span>
              )}
            </dd>
          </dl>
        </Card>

        <Section
          title="Change password"
          description="Signs you out everywhere else. If an admin gave you a temporary password, replace it here."
        >
          <form className="space-y-4" onSubmit={submit} noValidate>
            <Field label="Current password" htmlFor="pw-current">
              <TextInput
                id="pw-current"
                type="password"
                autoComplete="current-password"
                value={current}
                onChange={(event) => setCurrent(event.target.value)}
              />
            </Field>
            <Field label="New password" htmlFor="pw-new" hint={`At least ${MIN_PASSWORD} characters.`}>
              <TextInput
                id="pw-new"
                type="password"
                autoComplete="new-password"
                value={next}
                onChange={(event) => setNext(event.target.value)}
              />
            </Field>
            <Field label="Confirm new password" htmlFor="pw-confirm">
              <TextInput
                id="pw-confirm"
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
              />
            </Field>
            {next && problem ? <Notice tone="warning">{problem}</Notice> : null}
            {change.isError ? <Notice tone="error">{describeRefusal(change.error)}</Notice> : null}
            {change.isSuccess ? <Notice tone="success">Password changed.</Notice> : null}
            <Button
              type="submit"
              variant="primary"
              disabled={change.isPending || problem !== null || !current}
            >
              {change.isPending ? "Saving…" : "Change password"}
            </Button>
          </form>
        </Section>
      </div>
    </>
  );
}
