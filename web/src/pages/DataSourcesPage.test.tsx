/**
 * The organization's IT/OT data sources (ADR-0041): the page that keeps the register, and the
 * Compose source card that reads it.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConnectionInfo, DataSourceInfo, MeResponse } from "@/lib/api/types";
import * as seed from "@/test/fixtures";
import { PIPELINE, server } from "@/test/msw";
import { renderApp } from "@/test/render";

const CAMERAS: DataSourceInfo = {
  source_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  name: "Camera network",
  type: "IT",
  description: "Zeek conn.log from the camera VLAN",
  normalizer: "flow",
  source_format: "zeek-conn",
  connection: null,
  created_at: "2026-10-21T09:00:00+00:00",
};
const BIOLOGY: DataSourceInfo = {
  source_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  name: "Biology",
  type: "OT",
  description: null,
  normalizer: null,
  source_format: null,
  connection: null,
  created_at: "2026-10-21T09:05:00+00:00",
};

const METERS: DataSourceInfo = {
  source_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  name: "SensiPro meters",
  type: "OT",
  description: "Operational Data",
  normalizer: "meter",
  source_format: "sensipro-hourly-csv",
  connection: "sensipro",
  created_at: "2026-10-21T09:10:00+00:00",
};
const SENSIPRO: ConnectionInfo = {
  name: "sensipro",
  connector: "sensipro",
  description: "SensiPro utility meters: hourly cumulative readings (OT).",
  normalizer: "meter",
  source_format: "sensipro-hourly-csv",
  params: [
    {
      name: "utility",
      type: "str",
      default: "electricity",
      required: false,
      choices: ["electricity", "water"],
    },
    {
      name: "devices",
      type: "list",
      default: [],
      required: false,
      choices: null,
    },
  ],
  has_devices: true,
};
const PULLED_CSV =
  "device_id,from_timestamp,last_value,unit\nw1,2023-07-01 00:00:00,100.0,m3\nw1,2023-07-01 01:00:00,100.25,m3\n";

const sent: {
  posted: unknown[];
  put: unknown[];
  deleted: string[];
  pulls: unknown[];
  deviceQueries: string[];
} = {
  posted: [],
  put: [],
  deleted: [],
  pulls: [],
  deviceQueries: [],
};

/**
 * The server declares the SensiPro connection, lists two water meters, and serves pulls — and
 * its catalogue has the `meter` normalizer, which the shared fixture leaves out on purpose.
 */
function connections(): void {
  const meter = {
    name: "meter",
    kind: "normalizer",
    consumes: ["format:eoa-meter-csv", "format:sensipro-hourly-csv"],
    produces: ["lane:telemetry", "payload:meter"],
    formats: ["eoa-meter-csv", "sensipro-hourly-csv"],
    params: [],
    artifact_param: null,
  };
  server.use(
    http.get(`${PIPELINE}/modules`, () =>
      HttpResponse.json({
        ...seed.modules,
        normalizers: [...seed.modules.normalizers, meter],
      }),
    ),
    http.get(`${PIPELINE}/data-sources/connections`, () => HttpResponse.json([SENSIPRO])),
    http.get(`${PIPELINE}/data-sources/:id/devices`, ({ request }) => {
      sent.deviceQueries.push(new URL(request.url).search);
      return HttpResponse.json([
        { id: "w1", label: "w1 — Kitchen" },
        { id: "w2", label: "w2 — Garden" },
      ]);
    }),
    http.post(`${PIPELINE}/data-sources/:id/pull`, async ({ request }) => {
      sent.pulls.push(await request.json());
      return HttpResponse.json({
        content: PULLED_CSV,
        normalizer: "meter",
        source_format: "sensipro-hourly-csv",
        rows: 2,
        origin: "SensiPro meters 2023-07-01..2023-07-02",
      });
    }),
  );
}

/** The server's register, starting from `rows`; writes go through and are remembered. */
function register(rows: DataSourceInfo[]): void {
  let current = [...rows];
  server.use(
    http.get(`${PIPELINE}/data-sources`, () => HttpResponse.json(current)),
    http.post(`${PIPELINE}/data-sources`, async ({ request }) => {
      const body = (await request.json()) as Omit<DataSourceInfo, "source_id" | "created_at">;
      sent.posted.push(body);
      const created = {
        ...body,
        source_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        created_at: "2026-10-21T10:00:00+00:00",
      };
      current = [...current, created as DataSourceInfo];
      return HttpResponse.json(created, { status: 201 });
    }),
    http.put(`${PIPELINE}/data-sources/:id`, async ({ request, params }) => {
      const body = (await request.json()) as Omit<DataSourceInfo, "source_id" | "created_at">;
      sent.put.push(body);
      const before = current.find((row) => row.source_id === params.id);
      const updated = { ...before, ...body } as DataSourceInfo;
      current = current.map((row) => (row.source_id === params.id ? updated : row));
      return HttpResponse.json(updated);
    }),
    http.delete(`${PIPELINE}/data-sources/:id`, ({ params }) => {
      sent.deleted.push(String(params.id));
      current = current.filter((row) => row.source_id !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
  );
}

describe("Data sources page", () => {
  beforeEach(() => {
    sent.posted = [];
    sent.put = [];
    sent.deleted = [];
    sent.pulls = [];
    sent.deviceQueries = [];
  });
  afterEach(() => vi.restoreAllMocks());

  it("lists the sources with their type and how they are read", async () => {
    register([CAMERAS, BIOLOGY]);
    renderApp("/data-sources");
    const row = (await screen.findByText("Camera network")).closest("tr");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("IT")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("flow · zeek-conn")).toBeInTheDocument();
    const biology = screen.getByText("Biology").closest("tr") as HTMLElement;
    expect(within(biology).getByText("OT")).toBeInTheDocument();
    expect(within(biology).getByText("not set")).toBeInTheDocument();
  });

  it("says so when there are none yet", async () => {
    register([]);
    renderApp("/data-sources");
    expect(await screen.findByTestId("ds_empty")).toHaveTextContent("No data sources yet");
  });

  it("adds an OT source with a reader, and lists it", async () => {
    register([]);
    renderApp("/data-sources");
    await userEvent.type(await screen.findByTestId("ds_name"), "  Water meters ");
    await userEvent.selectOptions(screen.getByTestId("ds_type"), "OT");
    await userEvent.type(screen.getByTestId("ds_description"), "Hourly export");
    await userEvent.selectOptions(screen.getByTestId("ds_normalizer"), "flow");
    // flow reads two formats, so the format is asked for; one-format readers are not.
    await userEvent.selectOptions(screen.getByTestId("ds_format"), "zeek-conn");
    await userEvent.click(screen.getByTestId("ds_save"));

    await waitFor(() =>
      expect(sent.posted).toEqual([
        {
          name: "Water meters",
          type: "OT",
          description: "Hourly export",
          normalizer: "flow",
          source_format: "zeek-conn",
          connection: null,
        },
      ]),
    );
    expect(await screen.findByText("Water meters")).toBeInTheDocument();
    expect(screen.getByTestId("ds_name")).toHaveValue("");
  });

  it("sends a source with no reader as not set", async () => {
    register([]);
    renderApp("/data-sources");
    await userEvent.type(await screen.findByTestId("ds_name"), "Biology");
    expect(screen.queryByTestId("ds_format")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("ds_save"));
    await waitFor(() =>
      expect(sent.posted).toEqual([
        {
          name: "Biology",
          type: "IT",
          description: null,
          normalizer: null,
          source_format: null,
          connection: null,
        },
      ]),
    );
  });

  it("edits a source in the same form", async () => {
    register([CAMERAS]);
    renderApp("/data-sources");
    await userEvent.click(await screen.findByTestId("ds_edit_Camera network"));
    expect(screen.getByText("Edit “Camera network”")).toBeInTheDocument();
    expect(screen.getByTestId("ds_name")).toHaveValue("Camera network");
    expect(screen.getByTestId("ds_format")).toHaveValue("zeek-conn");
    await userEvent.clear(screen.getByTestId("ds_name"));
    await userEvent.type(screen.getByTestId("ds_name"), "Camera VLAN");
    await userEvent.click(screen.getByTestId("ds_save"));
    await waitFor(() => expect(sent.put).toHaveLength(1));
    expect(sent.put[0]).toMatchObject({
      name: "Camera VLAN",
      normalizer: "flow",
      source_format: "zeek-conn",
    });
    expect(await screen.findByText("Camera VLAN")).toBeInTheDocument();
    expect(screen.getByText("Add a data source")).toBeInTheDocument();
  });

  it("removes a source after confirmation", async () => {
    register([CAMERAS]);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderApp("/data-sources");
    await userEvent.click(await screen.findByTestId("ds_remove_Camera network"));
    await waitFor(() => expect(sent.deleted).toEqual([CAMERAS.source_id]));
    expect(await screen.findByTestId("ds_empty")).toBeInTheDocument();
  });

  it("shows the server's refusal, e.g. a duplicate name", async () => {
    register([CAMERAS]);
    server.use(
      http.post(`${PIPELINE}/data-sources`, () =>
        HttpResponse.json({ detail: "a data source named 'Camera network' already exists" }, { status: 409 }),
      ),
    );
    renderApp("/data-sources");
    await userEvent.type(await screen.findByTestId("ds_name"), "Camera network");
    await userEvent.click(screen.getByTestId("ds_save"));
    expect(await screen.findByText(/already exists/)).toBeInTheDocument();
  });

  it("gives a member the list but no form and no buttons", async () => {
    const member: MeResponse = {
      user_id: "22222222-2222-4222-8222-222222222222",
      username: "bob",
      role: "member",
      organization: { org_id: "acme", name: "Acme" },
    };
    server.use(http.get(`${PIPELINE}/auth/me`, () => HttpResponse.json(member)));
    register([CAMERAS]);
    renderApp("/data-sources");
    expect(await screen.findByText("Camera network")).toBeInTheDocument();
    expect(screen.getByText("Only an organization admin can add or change data sources.")).toBeInTheDocument();
    expect(screen.queryByTestId("source_form")).not.toBeInTheDocument();
    expect(screen.queryByTestId("ds_edit_Camera network")).not.toBeInTheDocument();
  });

  it("ties a source to a connection, which then decides how it is read", async () => {
    register([]);
    connections();
    renderApp("/data-sources");
    await userEvent.type(await screen.findByTestId("ds_name"), "SensiPro meters");
    await userEvent.selectOptions(screen.getByTestId("ds_type"), "OT");
    await userEvent.selectOptions(await screen.findByTestId("ds_connection"), "sensipro");
    // The reader is the connector's, so the form stops asking for one.
    expect(screen.queryByTestId("ds_normalizer")).not.toBeInTheDocument();
    expect(screen.getByText(/Read by meter \(sensipro-hourly-csv\)/)).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("ds_save"));
    await waitFor(() =>
      expect(sent.posted).toEqual([
        {
          name: "SensiPro meters",
          type: "OT",
          description: null,
          normalizer: null,
          source_format: null,
          connection: "sensipro",
        },
      ]),
    );
  });

  it("is in the sidebar under Operate", async () => {
    register([]);
    renderApp("/data-sources");
    expect(await screen.findByRole("link", { name: "Data sources" })).toHaveAttribute("href", "/data-sources");
  });
});

describe("Compose — picking a data source", () => {
  it("fills in the source's normalizer and format, and says which it is", async () => {
    register([CAMERAS, BIOLOGY]);
    renderApp("/compose");
    const picker = await screen.findByTestId("src_0_source");
    expect(screen.getByTestId("src_0_norm")).toHaveValue("stub");

    await userEvent.selectOptions(picker, CAMERAS.source_id);
    expect(screen.getByTestId("src_0_norm")).toHaveValue("flow");
    expect(screen.getByTestId("src_0_fmt")).toHaveValue("zeek-conn");
    expect(screen.getByText("IT · read by flow (zeek-conn).")).toBeInTheDocument();

    // Choosing another reader by hand detaches it from the data source.
    await userEvent.selectOptions(screen.getByTestId("src_0_norm"), "syslog");
    expect(picker).toHaveValue("");
  });

  it("leaves the reader alone for a source that names none", async () => {
    register([BIOLOGY]);
    renderApp("/compose");
    await userEvent.selectOptions(await screen.findByTestId("src_0_source"), BIOLOGY.source_id);
    expect(screen.getByTestId("src_0_norm")).toHaveValue("stub");
    expect(screen.getByText("OT · no normalizer set for it — choose one below.")).toBeInTheDocument();
  });

  it("pulls a date range from a connected source into the card, narrowed to chosen meters", async () => {
    register([METERS]);
    connections();
    renderApp("/compose");
    await userEvent.selectOptions(await screen.findByTestId("src_0_source"), METERS.source_id);
    expect(screen.getByTestId("src_0_norm")).toHaveValue("meter");

    const panel = await screen.findByTestId("src_0_pull");
    expect(within(panel).getByText("sensipro")).toBeInTheDocument();
    expect(screen.getByTestId("src_0_pull_go")).toBeDisabled(); // no dates yet
    await userEvent.type(screen.getByTestId("src_0_pull_start"), "2023-07-01");
    await userEvent.type(screen.getByTestId("src_0_pull_end"), "2023-07-02");
    await userEvent.selectOptions(screen.getByTestId("src_0_pull_utility"), "water");
    // The meter list follows the utility.
    await waitFor(() => expect(sent.deviceQueries).toContain("?utility=water"));
    await userEvent.type(screen.getByTestId("src_0_pull_filter"), "kitchen");
    expect(within(screen.getByTestId("src_0_pull_devices")).queryByText("w2 — Garden")).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByTestId("src_0_pull_devices"), "w1");
    await userEvent.click(screen.getByTestId("src_0_pull_go"));

    await waitFor(() =>
      expect(sent.pulls).toEqual([
        {
          start: "2023-07-01",
          end: "2023-07-02",
          params: { utility: "water", devices: ["w1"] },
        },
      ]),
    );
    expect(await screen.findByText("Pulled 2 rows into this source.")).toBeInTheDocument();
    // Filled like an uploaded file: content, its origin, and the connector's reader.
    expect(screen.getByTestId("src_0_tab")).toHaveTextContent("SensiPro meters 2023-07-01..2023-07-02");
    expect(screen.getByTestId("src_0_preview")).toHaveTextContent("w1,2023-07-01 00:00:00,100.0,m3");
    expect(screen.getByTestId("src_0_fmt")).toHaveValue("sensipro-hourly-csv");
  });

  it("shows why a pull was refused", async () => {
    register([METERS]);
    connections();
    server.use(
      http.post(`${PIPELINE}/data-sources/:id/pull`, () =>
        HttpResponse.json(
          {
            detail: "more than 100,000 rows in this window; pick fewer days or devices",
          },
          { status: 422 },
        ),
      ),
    );
    renderApp("/compose");
    await userEvent.selectOptions(await screen.findByTestId("src_0_source"), METERS.source_id);
    await userEvent.type(await screen.findByTestId("src_0_pull_start"), "2023-07-01");
    await userEvent.type(screen.getByTestId("src_0_pull_end"), "2023-07-31");
    await userEvent.click(screen.getByTestId("src_0_pull_go"));
    expect(await screen.findByText(/more than 100,000 rows/)).toBeInTheDocument();
    expect(screen.getByTestId("src_0_text")).toHaveValue("");
  });

  it("offers no pull for a source without a connection", async () => {
    register([CAMERAS]);
    connections();
    renderApp("/compose");
    await userEvent.selectOptions(await screen.findByTestId("src_0_source"), CAMERAS.source_id);
    expect(screen.queryByTestId("src_0_pull")).not.toBeInTheDocument();
  });

  it("shows no picker while the organization has no data sources", async () => {
    register([]);
    renderApp("/compose");
    await screen.findByTestId("src_0_text");
    expect(screen.queryByTestId("src_0_source")).not.toBeInTheDocument();
  });
});
