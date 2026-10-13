/**
 * The super admin's dashboard (ADR-0040): organization management, and nothing of any
 * organization's data.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import type { MeResponse, OrganizationAdminInfo, UserInfo } from "@/lib/api/types";
import { PIPELINE, server } from "@/test/msw";
import { renderApp } from "@/test/render";

const root: MeResponse = {
  user_id: "00000000-0000-4000-8000-000000000001",
  username: "root",
  role: "superadmin",
  organization: null,
};
const acme: OrganizationAdminInfo = {
  org_id: "acme-water",
  name: "Acme Water",
  suspended: false,
  created_at: "2026-10-13T08:00:00+00:00",
  users: 2,
  admins: 1,
  events: 110,
  alerts: 2,
};
const alice: UserInfo = {
  user_id: "11111111-1111-4111-8111-111111111111",
  username: "alice",
  role: "admin",
  created_at: "2026-10-13T08:00:00+00:00",
};
const bob: UserInfo = { ...alice, user_id: "22222222-2222-4222-8222-222222222222", username: "bob", role: "member" };

const calls: { created: unknown; suspended: string[]; deleted: string[]; roles: unknown[]; resets: unknown[] } = {
  created: null,
  suspended: [],
  deleted: [],
  roles: [],
  resets: [],
};

function asSuperAdmin(me: MeResponse = root) {
  let org = { ...acme };
  server.use(
    http.get(`${PIPELINE}/auth/me`, () => HttpResponse.json(me)),
    http.get(`${PIPELINE}/auth/options`, () => HttpResponse.json({ open_registration: true })),
    http.get(`${PIPELINE}/admin/organizations`, () => HttpResponse.json([org])),
    http.post(`${PIPELINE}/admin/organizations`, async ({ request }) => {
      calls.created = await request.json();
      return HttpResponse.json({ ...acme, org_id: "initech", name: "Initech", users: 1, events: 0, alerts: 0 }, { status: 201 });
    }),
    http.get(`${PIPELINE}/admin/organizations/:id`, () => HttpResponse.json(org)),
    http.post(`${PIPELINE}/admin/organizations/:id/suspend`, ({ params }) => {
      calls.suspended.push(String(params["id"]));
      org = { ...org, suspended: true };
      return HttpResponse.json(org);
    }),
    http.delete(`${PIPELINE}/admin/organizations/:id`, ({ params, request }) => {
      const confirm = new URL(request.url).searchParams.get("confirm");
      if (confirm !== params["id"]) return HttpResponse.json({ detail: "confirm" }, { status: 400 });
      calls.deleted.push(String(params["id"]));
      return new HttpResponse(null, { status: 204 });
    }),
    http.get(`${PIPELINE}/admin/organizations/:id/users`, () => HttpResponse.json([alice, bob])),
    http.patch(`${PIPELINE}/admin/organizations/:id/users/:user`, async ({ request, params }) => {
      const body = (await request.json()) as { role: "admin" | "member" };
      calls.roles.push({ user: params["user"], ...body });
      if (params["user"] === alice.user_id && body.role === "member") {
        return HttpResponse.json({ detail: "'alice' is the organization's only admin" }, { status: 409 });
      }
      return HttpResponse.json({ ...bob, role: body.role });
    }),
    http.post(`${PIPELINE}/admin/organizations/:id/users/:user/password`, async ({ request, params }) => {
      calls.resets.push({ user: params["user"], ...((await request.json()) as object) });
      return new HttpResponse(null, { status: 204 });
    }),
  );
}

beforeEach(() => {
  calls.created = null;
  calls.suspended = [];
  calls.deleted = [];
  calls.roles = [];
  calls.resets = [];
});

describe("the super admin", () => {
  it("is sent from any data page to Organizations, and offered no data planes", async () => {
    asSuperAdmin();
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Organizations" })).toBeInTheDocument();
    for (const name of ["Triage", "Compose & run", "Benchmarks", "Users"]) {
      expect(screen.queryByRole("link", { name })).not.toBeInTheDocument();
    }
    expect(screen.getByText("super admin")).toBeInTheDocument();
  });

  it.each(["/data-health", "/compose", "/evaluate", "/users"])("cannot open %s", async (path) => {
    asSuperAdmin();
    renderApp(path);
    expect(await screen.findByRole("heading", { name: "Organizations" })).toBeInTheDocument();
  });

  it("lists organizations with counts and creates one", async () => {
    asSuperAdmin();
    renderApp("/admin/organizations");
    const table = await screen.findByRole("table");
    expect(within(table).getByRole("link", { name: "Acme Water" })).toBeInTheDocument();
    expect(within(table).getByText("110")).toBeInTheDocument();
    expect(within(table).getByText("active")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Organization name"), "Initech");
    await user.type(screen.getByLabelText("First admin's username"), "dan");
    await user.type(screen.getByLabelText("Temporary password"), "dan-pass-12");
    await user.click(screen.getByRole("button", { name: "Create organization" }));
    expect(await screen.findByText("Created Initech (initech).")).toBeInTheDocument();
    expect(calls.created).toEqual({ organization: "Initech", username: "dan", password: "dan-pass-12" });
  });

  it("suspends an organization after confirming", async () => {
    asSuperAdmin();
    renderApp("/admin/organizations/acme-water");
    const user = userEvent.setup();
    const confirm = window.confirm;
    window.confirm = () => true;
    try {
      await user.click(await screen.findByRole("button", { name: "Suspend" }));
      expect(await screen.findByRole("button", { name: "Reactivate" })).toBeInTheDocument();
    } finally {
      window.confirm = confirm;
    }
    expect(calls.suspended).toEqual(["acme-water"]);
  });

  it("only deletes once the id is typed, then returns to the list", async () => {
    asSuperAdmin();
    renderApp("/admin/organizations/acme-water");
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "Delete organization and all its data" });
    expect(button).toBeDisabled();
    const input = screen.getByLabelText(/Type the organization id/);
    await user.type(input, "acme");
    expect(button).toBeDisabled();
    await user.type(input, "-water");
    expect(button).toBeEnabled();
    await user.click(button);
    expect(await screen.findByRole("heading", { name: "Organizations" })).toBeInTheDocument();
    expect(calls.deleted).toEqual(["acme-water"]);
  });

  it("changes a role, and shows the server's refusal for the last admin", async () => {
    asSuperAdmin();
    renderApp("/admin/organizations/acme-water");
    const user = userEvent.setup();
    await user.selectOptions(await screen.findByLabelText("Role of bob"), "admin");
    expect(await screen.findByText("bob is now an admin.")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Role of alice"), "member");
    expect(await screen.findByText("'alice' is the organization's only admin")).toBeInTheDocument();
  });

  it("resets a password through a prompt", async () => {
    asSuperAdmin();
    renderApp("/admin/organizations/acme-water");
    const user = userEvent.setup();
    const table = await screen.findByRole("table");
    const prompt = window.prompt;
    window.prompt = () => "fresh-temp-2";
    try {
      const bobRow = within(table).getByText("bob").closest("tr") as HTMLElement;
      await user.click(within(bobRow).getByRole("button", { name: "Reset password" }));
      await waitFor(() => expect(calls.resets).toEqual([{ user: bob.user_id, password: "fresh-temp-2" }]));
    } finally {
      window.prompt = prompt;
    }
  });
});

describe("a super admin at home in UCY (the seeded account)", () => {
  const seeded: MeResponse = { ...root, username: "admin@admin.com", organization: { org_id: "ucy", name: "UCY" } };

  it("sees its home organization's pages and the Platform section", async () => {
    asSuperAdmin(seeded);
    renderApp("/");
    expect(await screen.findByTestId("stat-alerts")).toBeInTheDocument(); // Triage, not redirected
    expect(screen.getByRole("link", { name: "Triage" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Users" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Organizations" })).toBeInTheDocument();
    expect(screen.getByText("UCY · super admin")).toBeInTheDocument();
  });

  it("cannot suspend or delete its own home organization from the page", async () => {
    asSuperAdmin(seeded);
    server.use(
      http.get(`${PIPELINE}/admin/organizations/:id`, () =>
        HttpResponse.json({ ...acme, org_id: "ucy", name: "UCY" }),
      ),
    );
    renderApp("/admin/organizations/ucy");
    expect(await screen.findByRole("button", { name: "Suspend" })).toBeDisabled();
    expect(screen.getByText(/Deleting it would delete your own account/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete organization and all its data" }),
    ).not.toBeInTheDocument();
  });

  it("shows itself in the user list without role, reset or remove controls", async () => {
    asSuperAdmin(seeded);
    const self: UserInfo = { ...alice, user_id: seeded.user_id, username: "admin@admin.com", role: "superadmin" };
    server.use(http.get(`${PIPELINE}/admin/organizations/:id/users`, () => HttpResponse.json([alice, self])));
    renderApp("/admin/organizations/acme-water");
    const table = await screen.findByRole("table");
    const row = within(table).getByText("admin@admin.com").closest("tr") as HTMLElement;
    expect(within(row).getByText("super admin")).toBeInTheDocument();
    expect(within(row).queryByRole("combobox")).not.toBeInTheDocument();
    expect(within(row).queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("an organization admin", () => {
  it("cannot open the admin pages", async () => {
    asSuperAdmin({ ...root, role: "admin", organization: { org_id: "acme-water", name: "Acme Water" } });
    renderApp("/admin/organizations");
    expect(await screen.findByTestId("stat-alerts")).toBeInTheDocument(); // sent to Triage
    expect(screen.queryByRole("link", { name: "Organizations" })).not.toBeInTheDocument();
  });
});

describe("closed registration", () => {
  it("hides the register link and closes the register page", async () => {
    server.use(
      http.get(`${PIPELINE}/auth/me`, () => HttpResponse.json({ detail: "not logged in" }, { status: 401 })),
      http.get(`${PIPELINE}/auth/options`, () => HttpResponse.json({ open_registration: false })),
    );
    renderApp("/login");
    expect(await screen.findByText(/Ask its admin to add you/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Register it" })).not.toBeInTheDocument();

    renderApp("/register");
    expect(await screen.findByRole("heading", { name: "Registration is closed" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create organization" })).not.toBeInTheDocument();
  });

  it("offers registration when it is open", async () => {
    server.use(
      http.get(`${PIPELINE}/auth/me`, () => HttpResponse.json({ detail: "not logged in" }, { status: 401 })),
      http.get(`${PIPELINE}/auth/options`, () => HttpResponse.json({ open_registration: true })),
    );
    renderApp("/login");
    expect(await screen.findByRole("link", { name: "Register it" })).toBeInTheDocument();
  });
});
