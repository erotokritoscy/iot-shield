import { useMutation } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { Button, Field, TextInput } from "@/components/ui/controls";
import { Notice } from "@/components/ui/surfaces";
import { authApi } from "@/lib/api/client";
import { ApiError, describeRefusal } from "@/lib/api/http";
import { useOpenRegistration, useSignedIn } from "@/lib/auth";
import { AuthLayout } from "@/pages/auth/AuthLayout";

export function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const signedIn = useSignedIn();
  const openRegistration = useOpenRegistration();

  // On success only the auth state changes; `AuthLayout` then sends the user where they were
  // headed — one redirect, so there is nothing to race.
  const login = useMutation({
    mutationFn: () => authApi.login(username, password),
    onSuccess: signedIn,
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    login.mutate();
  };

  return (
    <AuthLayout title="Sign in" description="Sign in to see your organization's alerts.">
      <form className="space-y-4" onSubmit={submit} noValidate>
        <Field label="Username" htmlFor="login-username">
          <TextInput
            id="login-username"
            autoComplete="username"
            autoFocus
            required
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </Field>
        <Field label="Password" htmlFor="login-password">
          <TextInput
            id="login-password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>
        {login.isError ? (
          <Notice tone="error">
            {login.error instanceof ApiError && login.error.status === 401
              ? "Wrong username or password."
              : describeRefusal(login.error)}
          </Notice>
        ) : null}
        <Button
          type="submit"
          variant="primary"
          className="w-full"
          disabled={login.isPending || !username || !password}
        >
          {login.isPending ? "Signing in…" : "Sign in"}
        </Button>
      </form>
      <p className="caption mt-5">
        {openRegistration ? (
          <>
            New organization? <Link className="text-accent hover:underline" to="/register">Register it</Link>.{" "}
          </>
        ) : null}
        Joining an existing one? Ask its admin to add you.
      </p>
    </AuthLayout>
  );
}
