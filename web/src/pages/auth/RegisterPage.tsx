import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { Button, Field, TextInput } from "@/components/ui/controls";
import { Notice } from "@/components/ui/surfaces";
import { authApi } from "@/lib/api/client";
import { describeRefusal } from "@/lib/api/http";
import { useOpenRegistration, useSignedIn } from "@/lib/auth";
import { AuthLayout } from "@/pages/auth/AuthLayout";

/** Mirrors `api/auth.py: MIN_PASSWORD` — the server is the authority, this only saves a trip. */
export const MIN_PASSWORD = 8;

/** The first problem with the form, in words, or null when it may be submitted. */
export function registrationProblem(form: {
  organization: string;
  username: string;
  password: string;
  confirm: string;
}): string | null {
  if (form.organization.trim().length < 2) return "Give your organization a name.";
  if (!/[a-z0-9]/i.test(form.organization)) return "The organization name needs a letter or digit.";
  if (form.username.trim().length < 3) return "Usernames are at least 3 characters.";
  if (form.password.length < MIN_PASSWORD) return `Passwords are at least ${MIN_PASSWORD} characters.`;
  if (form.password !== form.confirm) return "The two passwords do not match.";
  return null;
}

export function RegisterPage() {
  const [form, setForm] = useState({ organization: "", username: "", password: "", confirm: "" });
  const [touched, setTouched] = useState(false);
  const signedIn = useSignedIn();
  const openRegistration = useOpenRegistration();
  const problem = registrationProblem(form);

  const register = useMutation({
    mutationFn: () => authApi.register(form.organization.trim(), form.username.trim(), form.password),
    onSuccess: signedIn, // `AuthLayout` redirects once the state says "user"
  });

  const set = (key: keyof typeof form) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setForm((current) => ({ ...current, [key]: event.target.value }));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (problem === null) register.mutate();
  };

  if (openRegistration === false) {
    return (
      <AuthLayout
        title="Registration is closed"
        description="New organizations are created by the platform administrator."
      >
        <Notice tone="info">Ask the platform administrator to create your organization.</Notice>
        <p className="caption mt-5">
          Already have an account? <Link className="text-accent hover:underline" to="/login">Sign in</Link>.
        </p>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout
      title="Register your organization"
      description="Creates the organization and you as its admin. You can add your team afterwards."
    >
      <form className="space-y-4" onSubmit={submit} noValidate>
        <Field label="Organization name" htmlFor="reg-org">
          <TextInput id="reg-org" autoFocus value={form.organization} onChange={set("organization")} />
        </Field>
        <Field label="Your username" htmlFor="reg-username">
          <TextInput id="reg-username" autoComplete="username" value={form.username} onChange={set("username")} />
        </Field>
        <Field label="Password" htmlFor="reg-password" hint={`At least ${MIN_PASSWORD} characters.`}>
          <TextInput
            id="reg-password"
            type="password"
            autoComplete="new-password"
            value={form.password}
            onChange={set("password")}
          />
        </Field>
        <Field label="Confirm password" htmlFor="reg-confirm">
          <TextInput
            id="reg-confirm"
            type="password"
            autoComplete="new-password"
            value={form.confirm}
            onChange={set("confirm")}
          />
        </Field>
        {touched && problem ? <Notice tone="warning">{problem}</Notice> : null}
        {register.isError ? <Notice tone="error">{describeRefusal(register.error)}</Notice> : null}
        <Button type="submit" variant="primary" className="w-full" disabled={register.isPending}>
          {register.isPending ? "Creating…" : "Create organization"}
        </Button>
      </form>
      <p className="caption mt-5">
        Already registered? <Link className="text-accent hover:underline" to="/login">Sign in</Link>.
      </p>
    </AuthLayout>
  );
}
