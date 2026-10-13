import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { QueryState } from "@/components/QueryState";
import { Button, Field, Select, TextInput } from "@/components/ui/controls";
import { Card, Notice, PageHeader, Section } from "@/components/ui/surfaces";
import { DataTable, StatTile, type Column } from "@/components/ui/table";
import { adminApi } from "@/lib/api/client";
import { useAuth } from "@/lib/auth";
import { describeRefusal } from "@/lib/api/http";
import type { OrganizationAdminInfo, OrgRole, UserInfo } from "@/lib/api/types";
import { organizationsKey, StatusBadge } from "@/pages/admin/OrganizationsPage";
import { MIN_PASSWORD } from "@/pages/auth/RegisterPage";
import { AddUserForm } from "@/pages/UsersPage";

const ORG_ROLES: readonly OrgRole[] = ["member", "admin"];
const orgKey = (orgId: string) => ["admin", "organization", orgId] as const;
const orgUsersKey = (orgId: string) => ["admin", "organization", orgId, "users"] as const;

function useRefreshOrg(orgId: string): () => void {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: orgKey(orgId) });
    void client.invalidateQueries({ queryKey: organizationsKey });
  };
}

function Users({ orgId }: { readonly orgId: string }) {
  const refreshOrg = useRefreshOrg(orgId);
  const client = useQueryClient();
  const users = useQuery({ queryKey: orgUsersKey(orgId), queryFn: () => adminApi.users(orgId) });
  const refresh = () => {
    void client.invalidateQueries({ queryKey: orgUsersKey(orgId) });
    refreshOrg();
  };
  const [notice, setNotice] = useState<string | null>(null);

  const setRole = useMutation({
    mutationFn: ({ user, role }: { user: UserInfo; role: OrgRole }) =>
      adminApi.setRole(orgId, user.user_id, role),
    onSuccess: (user) => {
      setNotice(`${user.username} is now ${user.role === "admin" ? "an admin" : "a member"}.`);
      refresh();
    },
  });
  const resetPassword = useMutation({
    mutationFn: ({ user, password }: { user: UserInfo; password: string }) =>
      adminApi.resetPassword(orgId, user.user_id, password),
    onSuccess: (_, { user }) =>
      setNotice(`New temporary password set for ${user.username}; they were signed out everywhere.`),
  });
  const remove = useMutation({
    mutationFn: (user: UserInfo) => adminApi.removeUser(orgId, user.user_id),
    onSuccess: (_, user) => {
      setNotice(`Removed ${user.username}.`);
      refresh();
    },
  });
  const failure = setRole.error ?? resetPassword.error ?? remove.error;

  const askForPassword = (user: UserInfo) => {
    const password = window.prompt(
      `New temporary password for ${user.username} (at least ${MIN_PASSWORD} characters):`,
    );
    if (password === null) return;
    if (password.length < MIN_PASSWORD) {
      setNotice(`Not changed: passwords are at least ${MIN_PASSWORD} characters.`);
      return;
    }
    resetPassword.mutate({ user, password });
  };

  const columns: Column<UserInfo>[] = [
    { key: "username", header: "username" },
    {
      key: "role",
      header: "role",
      render: (row) =>
        row.role === "superadmin" ? (
          // A super admin at home here: managed from the server, not by role changes.
          <span className="text-xs text-muted">super admin</span>
        ) : (
          <Select
            aria-label={`Role of ${row.username}`}
            className="h-7 w-32 py-0 text-xs"
            options={ORG_ROLES}
            value={row.role}
            disabled={setRole.isPending}
            onChange={(event) => setRole.mutate({ user: row, role: event.target.value as OrgRole })}
          />
        ),
    },
    {
      key: "created_at",
      header: "added (UTC)",
      render: (row) => <span className="tabular">{row.created_at.slice(0, 16).replace("T", " ")}</span>,
    },
    {
      key: "actions",
      header: "",
      render: (row) =>
        row.role === "superadmin" ? null : (
          <div className="flex justify-end gap-1">
            <Button size="sm" variant="ghost" onClick={() => askForPassword(row)}>
              Reset password
            </Button>
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
          </div>
        ),
    },
  ];

  return (
    <div className="space-y-8">
      <Section title="Users">
        {failure ? <Notice tone="error">{describeRefusal(failure)}</Notice> : null}
        {!failure && notice ? <Notice tone="success">{notice}</Notice> : null}
        <QueryState query={users} label="Loading users…">
          {(rows) => <DataTable columns={columns} rows={rows} rowKey={(row) => row.user_id} />}
        </QueryState>
      </Section>
      <Section title="Add a user">
        <AddUserForm
          add={(username, password, role) => adminApi.addUser(orgId, username, password, role)}
          listKey={orgUsersKey(orgId)}
        />
      </Section>
    </div>
  );
}

/** Whether this organization is the signed-in super admin's own home (ADR-0040). */
function useIsMyHome(orgId: string): boolean {
  const auth = useAuth();
  return auth.data?.kind === "user" && auth.data.me.organization?.org_id === orgId;
}

function Status({ org }: { readonly org: OrganizationAdminInfo }) {
  const refresh = useRefreshOrg(org.org_id);
  const home = useIsMyHome(org.org_id);
  const toggle = useMutation({
    mutationFn: () => adminApi.setSuspended(org.org_id, !org.suspended),
    onSuccess: refresh,
  });
  const onClick = () => {
    if (
      org.suspended ||
      window.confirm(
        `Suspend ${org.name}? Its ${org.users} user(s) are signed out and cannot log in until you reactivate it. Nothing is deleted.`,
      )
    ) {
      toggle.mutate();
    }
  };
  return (
    <Section
      title="Status"
      description={
        home
          ? "Your home organization. It cannot be suspended: that would sign you out of the platform."
          : org.suspended
            ? "Suspended: its users cannot sign in. Its data is kept."
            : "Active. Suspending signs its users out and blocks logins without deleting anything."
      }
      actions={
        <Button
          variant={org.suspended ? "primary" : "secondary"}
          disabled={toggle.isPending || (home && !org.suspended)}
          onClick={onClick}
        >
          {org.suspended ? "Reactivate" : "Suspend"}
        </Button>
      }
    >
      {toggle.isError ? <Notice tone="error">{describeRefusal(toggle.error)}</Notice> : null}
    </Section>
  );
}

function DangerZone({ org }: { readonly org: OrganizationAdminInfo }) {
  const home = useIsMyHome(org.org_id);
  const client = useQueryClient();
  const navigate = useNavigate();
  const [typed, setTyped] = useState("");
  const remove = useMutation({
    mutationFn: () => adminApi.deleteOrganization(org.org_id, typed),
    onSuccess: () => {
      client.removeQueries({ queryKey: orgKey(org.org_id) });
      void client.invalidateQueries({ queryKey: organizationsKey });
      navigate("/admin/organizations", { replace: true });
    },
  });
  if (home) {
    return (
      <Section title="Delete organization">
        <Notice tone="info">
          This is your home organization. Deleting it would delete your own account, so it cannot be
          deleted while you are its super admin.
        </Notice>
      </Section>
    );
  }
  return (
    <Section title="Delete organization">
      <Card className="space-y-3 border-critical/40 p-4">
        <p className="text-sm text-ink">
          Permanently deletes <strong>{org.name}</strong>, its {org.users} user(s) and all of its data —{" "}
          {org.events.toLocaleString("en-US")} events and {org.alerts.toLocaleString("en-US")} alerts. This
          cannot be undone.
        </p>
        <Field label={`Type the organization id (${org.org_id}) to confirm`} htmlFor="delete-confirm">
          <TextInput
            id="delete-confirm"
            autoComplete="off"
            className="max-w-xs font-mono"
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
          />
        </Field>
        {remove.isError ? <Notice tone="error">{describeRefusal(remove.error)}</Notice> : null}
        <Button
          className="border-critical bg-critical text-accent-ink hover:bg-critical/90"
          disabled={typed !== org.org_id || remove.isPending}
          onClick={() => remove.mutate()}
        >
          {remove.isPending ? "Deleting…" : "Delete organization and all its data"}
        </Button>
      </Card>
    </Section>
  );
}

/** One organization, as the super admin manages it: status, users, and deletion. */
export function OrganizationPage() {
  const { orgId = "" } = useParams();
  const org = useQuery({ queryKey: orgKey(orgId), queryFn: () => adminApi.organization(orgId) });

  return (
    <>
      <p className="caption mb-2">
        <Link className="text-accent hover:underline" to="/admin/organizations">
          ← All organizations
        </Link>
      </p>
      <QueryState query={org} label="Loading organization…">
        {(data) => (
          <>
            <PageHeader
              title={data.name}
              description={
                <span className="inline-flex items-center gap-2">
                  <span className="font-mono">{data.org_id}</span> <StatusBadge suspended={data.suspended} />
                </span>
              }
            />
            <div className="space-y-8">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <StatTile label="Users" value={data.users} />
                <StatTile label="Admins" value={data.admins} />
                <StatTile label="Events" value={data.events.toLocaleString("en-US")} />
                <StatTile label="Alerts" value={data.alerts.toLocaleString("en-US")} />
              </div>
              <Status org={data} />
              <Users orgId={data.org_id} />
              <DangerZone org={data} />
            </div>
          </>
        )}
      </QueryState>
    </>
  );
}
