import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { QueryState } from "@/components/QueryState";
import { Button, Field, Select, TextInput } from "@/components/ui/controls";
import { Notice, PageHeader, Section } from "@/components/ui/surfaces";
import { DataTable, type Column } from "@/components/ui/table";
import { authApi } from "@/lib/api/client";
import { describeRefusal } from "@/lib/api/http";
import type { OrgRole, UserInfo } from "@/lib/api/types";
import { useAuth } from "@/lib/auth";
import { MIN_PASSWORD } from "@/pages/auth/RegisterPage";

export const usersKey = ["auth", "users"] as const;
const ROLES: readonly OrgRole[] = ["member", "admin"];

/**
 * Add a user with a temporary password. Shared by an org admin's Users page and the super
 * admin's organization page, which differ only in which endpoint adds and which list refreshes.
 */
export function AddUserForm({
  add: addUser,
  listKey,
}: {
  readonly add: (username: string, password: string, role: OrgRole) => Promise<UserInfo>;
  readonly listKey: readonly unknown[];
}) {
  const client = useQueryClient();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<OrgRole>("member");

  const add = useMutation({
    mutationFn: () => addUser(username.trim(), password, role),
    onSuccess: () => {
      setUsername("");
      setPassword("");
      setRole("member");
      void client.invalidateQueries({ queryKey: listKey });
    },
  });

  const tooShort = password.length > 0 && password.length < MIN_PASSWORD;
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (username.trim() && password.length >= MIN_PASSWORD) add.mutate();
  };

  return (
    <form className="space-y-4" onSubmit={submit} noValidate>
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Username" htmlFor="new-username">
          <TextInput
            id="new-username"
            autoComplete="off"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </Field>
        <Field
          label="Temporary password"
          htmlFor="new-password"
          hint={`At least ${MIN_PASSWORD} characters. Share it with them; they can change it.`}
        >
          <TextInput
            id="new-password"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </Field>
        <Field label="Role" htmlFor="new-role">
          <Select
            id="new-role"
            options={ROLES}
            labelFor={(option) => (option === "admin" ? "admin — can manage users" : "member")}
            value={role}
            onChange={(event) => setRole(event.target.value as OrgRole)}
          />
        </Field>
      </div>
      {tooShort ? <Notice tone="warning">{`Passwords are at least ${MIN_PASSWORD} characters.`}</Notice> : null}
      {add.isError ? <Notice tone="error">{describeRefusal(add.error)}</Notice> : null}
      {add.isSuccess ? <Notice tone="success">{`Added ${add.data.username}.`}</Notice> : null}
      <Button
        type="submit"
        variant="primary"
        disabled={add.isPending || !username.trim() || password.length < MIN_PASSWORD}
      >
        {add.isPending ? "Adding…" : "Add user"}
      </Button>
    </form>
  );
}

/** Admins only: the organization's users, adding one, and removing one. */
export function UsersPage() {
  const auth = useAuth();
  const client = useQueryClient();
  const me = auth.data?.kind === "user" ? auth.data.me : null;
  // An org admin, or a super admin at home in this organization (ADR-0040).
  const canManage = me !== null && me.organization !== null && (me.role === "admin" || me.role === "superadmin");
  const users = useQuery({
    queryKey: usersKey,
    queryFn: () => authApi.users(),
    enabled: canManage,
  });
  const remove = useMutation({
    mutationFn: (user: UserInfo) => authApi.removeUser(user.user_id),
    onSuccess: () => void client.invalidateQueries({ queryKey: usersKey }),
  });

  if (me === null || !canManage || me.organization === null) {
    return (
      <>
        <PageHeader title="Users" description="Who can sign in to your organization." />
        <Notice tone="info">Only an organization admin can manage users.</Notice>
      </>
    );
  }

  const columns: Column<UserInfo>[] = [
    { key: "username", header: "username" },
    { key: "role", header: "role" },
    {
      key: "created_at",
      header: "added (UTC)",
      render: (row) => <span className="tabular">{row.created_at.slice(0, 16).replace("T", " ")}</span>,
    },
    {
      key: "actions",
      header: "",
      render: (row) =>
        row.user_id === me.user_id ? (
          <span className="caption">you</span>
        ) : row.role === "superadmin" ? (
          // Managed from the server, never from an organization's page (the API refuses too).
          <span className="caption">platform super admin</span>
        ) : (
          <Button
            size="sm"
            variant="ghost"
            disabled={remove.isPending}
            onClick={() => {
              if (window.confirm(`Remove ${row.username}? They will be signed out immediately.`)) {
                remove.mutate(row);
              }
            }}
          >
            Remove
          </Button>
        ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Users"
        description={`Who can sign in to ${me.organization.name}. Everyone here sees the same alerts and can run pipelines; admins can also manage this list.`}
      />
      <div className="space-y-8">
        <Section title="Members">
          {remove.isError ? <Notice tone="error">{describeRefusal(remove.error)}</Notice> : null}
          <QueryState query={users} label="Loading users…">
            {(rows) => <DataTable columns={columns} rows={rows} rowKey={(row) => row.user_id} />}
          </QueryState>
        </Section>
        <Section title="Add a user">
          <AddUserForm add={authApi.addUser} listKey={usersKey} />
        </Section>
      </div>
    </>
  );
}
