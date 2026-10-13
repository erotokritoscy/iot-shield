/**
 * Login, registration and the organization pages (ADR-0039).
 *
 * Every other suite runs with the `/auth` router absent (the default `msw` world), which is
 * itself the first property here: a deployment without login looks exactly as before. These
 * tests switch login *on* by answering `/auth/me`.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import type { MeResponse } from "@/lib/api/types";
import { API, PIPELINE, server } from "@/test/msw";
import { renderApp } from "@/test/render";
import { safeRedirect } from "@/lib/auth";
import { registrationProblem } from "@/pages/auth/RegisterPage";

const admin: MeResponse = {
  user_id: "11111111-1111-4111-8111-111111111111",
  username: "alice",
  role: "admin",
  organization: { org_id: "acme-water", name: "Acme Water" },
};
const member: MeResponse = { ...admin, user_id: "22222222-2222-4222-8222-222222222222", username: "bob", role: "member" };

const captured: { login: unknown; register: unknown; logout: number; addUser: unknown; removed: string[] } = {
  login: null,
  register: null,
  logout: 0,
  addUser: null,
  removed: [],
};

/** Login is on. `who` is the session the browser starts with, or null for none. */
function loginRequired(who: MeResponse | null): { endSession: () => void } {
  let current = who;
  server.use(
    http.get(`${PIPELINE}/auth/me`, () =>
      current ? HttpResponse.json(current) : HttpResponse.json({ detail: "not logged in" }, { status: 401 }),
    ),
    http.post(`${PIPELINE}/auth/login`, async ({ request }) => {
      captured.login = await request.json();
      const body = captured.login as { password: string };
      if (body.password !== "right-password") {
        return HttpResponse.json({ detail: "invalid username or password" }, { status: 401 });
      }
      current = admin;
      return HttpResponse.json(admin);
    }),
    http.post(`${PIPELINE}/auth/register`, async ({ request }) => {
      captured.register = await request.json();
      current = admin;
      return HttpResponse.json(admin, { status: 201 });
    }),
    http.post(`${PIPELINE}/auth/logout`, () => {
      captured.logout += 1;
      current = null;
      return new HttpResponse(null, { status: 204 });
    }),
    http.get(`${PIPELINE}/auth/users`, () =>
      HttpResponse.json([
        { user_id: admin.user_id, username: "alice", role: "admin", created_at: "2026-10-13T08:00:00+00:00" },
        { user_id: member.user_id, username: "bob", role: "member", created_at: "2026-10-13T09:00:00+00:00" },
      ]),
    ),
    http.post(`${PIPELINE}/auth/users`, async ({ request }) => {
      captured.addUser = await request.json();
      const body = captured.addUser as { username: string; role: string };
      return HttpResponse.json(
        { user_id: "33333333-3333-4333-8333-333333333333", username: body.username, role: body.role, created_at: "2026-10-13T10:00:00+00:00" },
        { status: 201 },
      );
    }),
    http.delete(`${PIPELINE}/auth/users/:id`, ({ params }) => {
      captured.removed.push(String(params["id"]));
      return new HttpResponse(null, { status: 204 });
    }),
  );
  return {
    endSession: () => {
      current = null;
    },
  };
}

beforeEach(() => {
  captured.login = null;
  captured.register = null;
  captured.logout = 0;
  captured.addUser = null;
  captured.removed = [];
});

describe("without login (no IOTSHIELD_AUTH)", () => {
  it("opens straight onto the dashboard with no account section", async () => {
    renderApp("/");
    expect(await screen.findByTestId("stat-alerts")).toBeInTheDocument();
    expect(screen.queryByText("Organization")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign out/i })).not.toBeInTheDocument();
  });

  it("sends the login page back to the dashboard, since there is nothing to log into", async () => {
    renderApp("/login");
    expect(await screen.findByTestId("stat-alerts")).toBeInTheDocument();
  });
});

describe("with login required", () => {
  it("sends a visitor with no session to the login page", async () => {
    loginRequired(null);
    renderApp("/data-health");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByText("Data health")).not.toBeInTheDocument();
  });

  it("signs in and returns to the page that was asked for", async () => {
    loginRequired(null);
    renderApp("/data-health");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Username"), "alice");
    await user.type(screen.getByLabelText("Password"), "right-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { name: "Data health" })).toBeInTheDocument();
    expect(captured.login).toEqual({ username: "alice", password: "right-password" });
    expect(screen.getByText("Acme Water · admin")).toBeInTheDocument();
  });

  it("says so on a wrong password and stays on the form", async () => {
    loginRequired(null);
    renderApp("/");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Username"), "alice");
    await user.type(screen.getByLabelText("Password"), "nope-nope");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Wrong username or password.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("returns to the login page when a session dies mid-use", async () => {
    // An admin removed this user, or the session expired: the next read answers 401.
    const session = loginRequired(admin);
    server.use(
      http.get(`${API}/summary`, () => {
        session.endSession(); // the server forgot it: /auth/me now says 401 as well
        return HttpResponse.json({ detail: "not logged in" }, { status: 401 });
      }),
    );
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });

  it("signs out on the server and lands on the login page", async () => {
    loginRequired(admin);
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /sign out/i }));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(captured.logout).toBe(1);
  });

  it("registers an organization and lands its admin on the dashboard", async () => {
    loginRequired(null);
    renderApp("/register");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Organization name"), "Acme Water");
    await user.type(screen.getByLabelText("Your username"), "alice");
    await user.type(screen.getByLabelText("Password"), "long-enough");
    await user.type(screen.getByLabelText("Confirm password"), "long-enough");
    await user.click(screen.getByRole("button", { name: "Create organization" }));

    expect(await screen.findByTestId("stat-alerts")).toBeInTheDocument();
    expect(captured.register).toEqual({ organization: "Acme Water", username: "alice", password: "long-enough" });
  });

  it("refuses mismatched passwords without asking the server", async () => {
    loginRequired(null);
    renderApp("/register");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Organization name"), "Acme Water");
    await user.type(screen.getByLabelText("Your username"), "alice");
    await user.type(screen.getByLabelText("Password"), "long-enough");
    await user.type(screen.getByLabelText("Confirm password"), "long-enougH");
    await user.click(screen.getByRole("button", { name: "Create organization" }));
    expect(await screen.findByText("The two passwords do not match.")).toBeInTheDocument();
    expect(captured.register).toBeNull();
  });

  it("shows the server's reason when the organization exists", async () => {
    loginRequired(null);
    server.use(
      http.post(`${PIPELINE}/auth/register`, () =>
        HttpResponse.json({ detail: "an organization named 'Acme Water' already exists" }, { status: 409 }),
      ),
    );
    renderApp("/register");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Organization name"), "Acme Water");
    await user.type(screen.getByLabelText("Your username"), "alice");
    await user.type(screen.getByLabelText("Password"), "long-enough");
    await user.type(screen.getByLabelText("Confirm password"), "long-enough");
    await user.click(screen.getByRole("button", { name: "Create organization" }));
    expect(await screen.findByText("an organization named 'Acme Water' already exists")).toBeInTheDocument();
  });
});

describe("the organization pages", () => {
  it("lists users for an admin, adds one, and removes another", async () => {
    loginRequired(admin);
    renderApp("/users");
    const user = userEvent.setup();
    const table = await screen.findByRole("table");
    expect(within(table).getByText("bob")).toBeInTheDocument();
    expect(within(table).getByText("you")).toBeInTheDocument(); // no Remove on your own row

    await user.type(screen.getByLabelText("Username"), "carol");
    await user.type(screen.getByLabelText("Temporary password"), "temp-pass-1");
    await user.selectOptions(screen.getByLabelText("Role"), "admin");
    await user.click(screen.getByRole("button", { name: "Add user" }));
    expect(await screen.findByText("Added carol.")).toBeInTheDocument();
    expect(captured.addUser).toEqual({ username: "carol", password: "temp-pass-1", role: "admin" });

    const confirm = window.confirm;
    window.confirm = () => true;
    try {
      await user.click(within(table).getByRole("button", { name: "Remove" }));
      await waitFor(() => expect(captured.removed).toEqual([member.user_id]));
    } finally {
      window.confirm = confirm;
    }
  });

  it("shows a member no Users entry and no user list", async () => {
    loginRequired(member);
    renderApp("/users");
    expect(await screen.findByText("Only an organization admin can manage users.")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Users" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Account" })).toBeInTheDocument();
  });
});

describe("registrationProblem", () => {
  const ok = { organization: "Acme", username: "alice", password: "long-enough", confirm: "long-enough" };
  it.each([
    [{ ...ok }, null],
    [{ ...ok, organization: " " }, "Give your organization a name."],
    [{ ...ok, organization: "!!!" }, "The organization name needs a letter or digit."],
    [{ ...ok, username: "al" }, "Usernames are at least 3 characters."],
    [{ ...ok, password: "short", confirm: "short" }, "Passwords are at least 8 characters."],
    [{ ...ok, confirm: "different" }, "The two passwords do not match."],
  ])("%o -> %s", (form, problem) => {
    expect(registrationProblem(form)).toBe(problem);
  });
});

describe("safeRedirect", () => {
  it.each([
    [undefined, "/"],
    ["/data-health", "/data-health"],
    ["/assets?asset=pump-02", "/assets?asset=pump-02"],
    ["//evil.example/x", "/"],
    ["https://evil.example", "/"],
    ["/login", "/"],
  ])("%s -> %s", (from, target) => {
    expect(safeRedirect(from)).toBe(target);
  });
});
