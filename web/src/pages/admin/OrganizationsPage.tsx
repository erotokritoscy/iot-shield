import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { QueryState } from "@/components/QueryState";
import { Button, Field, TextInput } from "@/components/ui/controls";
import { Notice, PageHeader, Section } from "@/components/ui/surfaces";
import { DataTable, type Column } from "@/components/ui/table";
import { adminApi } from "@/lib/api/client";
import { describeRefusal } from "@/lib/api/http";
import type { OrganizationAdminInfo } from "@/lib/api/types";
import { MIN_PASSWORD, registrationProblem } from "@/pages/auth/RegisterPage";

export const organizationsKey = ["admin", "organizations"] as const;

export function StatusBadge({ suspended }: { readonly suspended: boolean }) {
  return suspended ? (
    <span className="rounded border border-warning/40 bg-warning/10 px-1.5 py-0.5 text-2xs font-medium text-warning">
      suspended
    </span>
  ) : (
    <span className="rounded border border-success/40 bg-success/10 px-1.5 py-0.5 text-2xs font-medium text-success">
      active
    </span>
  );
}

const count = (value: number) => <span className="tabular">{value.toLocaleString("en-US")}</span>;

const columns: Column<OrganizationAdminInfo>[] = [
  {
    key: "name",
    header: "organization",
    render: (row) => (
      <Link className="font-medium text-accent hover:underline" to={`/admin/organizations/${encodeURIComponent(row.org_id)}`}>
        {row.name}
      </Link>
    ),
  },
  { key: "org_id", header: "id", render: (row) => <span className="font-mono text-xs">{row.org_id}</span> },
  { key: "status", header: "status", render: (row) => <StatusBadge suspended={row.suspended} /> },
  { key: "users", header: "users", align: "right", render: (row) => count(row.users) },
  { key: "admins", header: "admins", align: "right", render: (row) => count(row.admins) },
  { key: "events", header: "events", align: "right", render: (row) => count(row.events) },
  { key: "alerts", header: "alerts", align: "right", render: (row) => count(row.alerts) },
  {
    key: "created_at",
    header: "created (UTC)",
    render: (row) => <span className="tabular">{row.created_at.slice(0, 16).replace("T", " ")}</span>,
  },
];

function CreateOrganizationForm() {
  const client = useQueryClient();
  const [form, setForm] = useState({ organization: "", username: "", password: "" });
  const [touched, setTouched] = useState(false);
  // The same rules as public registration, minus the confirmation field: the super admin is
  // setting a temporary password to hand over, not choosing their own.
  const problem = registrationProblem({ ...form, confirm: form.password });

  const create = useMutation({
    mutationFn: () =>
      adminApi.createOrganization(form.organization.trim(), form.username.trim(), form.password),
    onSuccess: () => {
      setForm({ organization: "", username: "", password: "" });
      setTouched(false);
      void client.invalidateQueries({ queryKey: organizationsKey });
    },
  });

  const set = (key: keyof typeof form) => (event: React.ChangeEvent<HTMLInputElement>) =>
    setForm((current) => ({ ...current, [key]: event.target.value }));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (problem === null) create.mutate();
  };

  return (
    <form className="space-y-4" onSubmit={submit} noValidate>
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Organization name" htmlFor="new-org-name">
          <TextInput id="new-org-name" value={form.organization} onChange={set("organization")} />
        </Field>
        <Field label="First admin's username" htmlFor="new-org-admin">
          <TextInput id="new-org-admin" autoComplete="off" value={form.username} onChange={set("username")} />
        </Field>
        <Field
          label="Temporary password"
          htmlFor="new-org-password"
          hint={`At least ${MIN_PASSWORD} characters. Share it with the admin; they can change it.`}
        >
          <TextInput
            id="new-org-password"
            type="password"
            autoComplete="new-password"
            value={form.password}
            onChange={set("password")}
          />
        </Field>
      </div>
      {touched && problem ? <Notice tone="warning">{problem}</Notice> : null}
      {create.isError ? <Notice tone="error">{describeRefusal(create.error)}</Notice> : null}
      {create.isSuccess ? (
        <Notice tone="success">{`Created ${create.data.name} (${create.data.org_id}).`}</Notice>
      ) : null}
      <Button type="submit" variant="primary" disabled={create.isPending}>
        {create.isPending ? "Creating…" : "Create organization"}
      </Button>
    </form>
  );
}

/** The super admin's home: every organization on the platform (ADR-0040). */
export function OrganizationsPage() {
  const organizations = useQuery({ queryKey: organizationsKey, queryFn: () => adminApi.organizations() });

  return (
    <>
      <PageHeader
        title="Organizations"
        description="Every organization on this platform. You manage them and their users; their alerts and data stay private to them — the counts below are all you see."
      />
      <div className="space-y-8">
        <Section title="All organizations">
          <QueryState query={organizations} label="Loading organizations…">
            {(rows) => (
              <DataTable
                columns={columns}
                rows={rows}
                rowKey={(row) => row.org_id}
                empty={<Notice tone="info">No organizations yet. Create the first one below.</Notice>}
              />
            )}
          </QueryState>
        </Section>
        <Section title="Create an organization" description="Works even when public registration is closed.">
          <CreateOrganizationForm />
        </Section>
      </div>
    </>
  );
}
