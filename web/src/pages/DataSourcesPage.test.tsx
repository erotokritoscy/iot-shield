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
import { captured, PIPELINE, server } from "@/test/msw";
import { CREATE_SOURCE } from "@/pages/compose/SourceCard";
import { renderApp } from "@/test/render";

const CAMERAS: DataSourceInfo = {
  source_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  name: "Camera network",
  type: "IT",
  description: "Zeek conn.log from the camera VLAN",
  normalizer: "flow",
  source_format: "zeek-conn",
  connection: null,
  has_credentials: false,
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
  has_credentials: false,
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
  has_credentials: false,
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
        sample: PULLED_CSV,
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
      const body = (await request.json()) as Omit<DataSourceInfo, "source_id" | "created_at" | "has_credentials">;
      sent.posted.push(body);
      const created = {
        has_credentials: false,
        ...body,
        source_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        created_at: "2026-10-21T10:00:00+00:00",
      };
      current = [...current, created as DataSourceInfo];
      return HttpResponse.json(created, { status: 201 });
    }),
    http.put(`${PIPELINE}/data-sources/:id`, async ({ request, params }) => {
      const body = (await request.json()) as Omit<DataSourceInfo, "source_id" | "created_at" | "has_credentials">;
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

// Every describe below records into `sent`, so it starts empty for each test in the file.
beforeEach(() => {
  sent.posted = [];
  sent.put = [];
  sent.deleted = [];
  sent.pulls = [];
  sent.deviceQueries = [];
});

describe("Data sources page", () => {
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

/** Open Compose and choose how source 1's data arrives. */
async function composeWith(mode: "pull" | "sample" | "upload" | "paste"): Promise<void> {
  renderApp("/compose");
  await userEvent.click(await screen.findByTestId(`src_0_mode_${mode}`));
}

/** Pull 2023-07-01..`end` from the SensiPro source, on a card already set to pull. */
async function pullMeters(end = "2023-07-02"): Promise<void> {
  await userEvent.selectOptions(await screen.findByTestId("src_0_source"), METERS.source_id);
  await userEvent.type(await screen.findByTestId("src_0_pull_start"), "2023-07-01");
  await userEvent.type(screen.getByTestId("src_0_pull_end"), end);
  await userEvent.click(screen.getByTestId("src_0_pull_go"));
}

describe("Compose — picking a data source", () => {
  it("fills in the source's normalizer and format, and says which it is", async () => {
    register([CAMERAS, BIOLOGY]);
    await composeWith("paste");
    const picker = await screen.findByTestId("src_0_source");
    expect(screen.getByTestId("src_0_norm")).toHaveValue("stub");

    await userEvent.selectOptions(picker, CAMERAS.source_id);
    expect(screen.getByTestId("src_0_norm")).toHaveValue("flow");
    expect(screen.getByTestId("src_0_fmt")).toHaveValue("zeek-conn");
    expect(screen.getByText("IT · read by flow (zeek-conn)")).toBeInTheDocument();

    // Choosing another reader by hand detaches it from the data source.
    await userEvent.selectOptions(screen.getByTestId("src_0_norm"), "syslog");
    expect(picker).toHaveValue("");
  });

  it("leaves the reader alone for a source that names none", async () => {
    register([BIOLOGY]);
    await composeWith("upload");
    await userEvent.selectOptions(await screen.findByTestId("src_0_source"), BIOLOGY.source_id);
    expect(screen.getByTestId("src_0_norm")).toHaveValue("stub");
    expect(screen.getByText("OT · no normalizer set for it — choose one below.")).toBeInTheDocument();
  });

  it("pulls a date range from a connected source into the card, narrowed to chosen meters", async () => {
    register([METERS]);
    connections();
    await composeWith("pull");
    await userEvent.selectOptions(await screen.findByTestId("src_0_source"), METERS.source_id);
    // A pull brings the connector's reader: stated, not offered.
    expect(screen.queryByTestId("src_0_norm")).not.toBeInTheDocument();
    expect(screen.getByTestId("src_0_reader")).toHaveTextContent("Read by meter (sensipro-hourly-csv)");

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
    expect(await screen.findByText(/2 rows in this window\. The run reads them from/)).toBeInTheDocument();
    // The sample is shown and checked like a file, in the connector's reader and format.
    expect(screen.getByTestId("src_0_tab")).toHaveTextContent("SensiPro meters 2023-07-01..2023-07-02");
    expect(screen.getByTestId("src_0_preview")).toHaveTextContent("w1,2023-07-01 00:00:00,100.0,m3");
    expect(screen.getByTestId("src_0_pulled")).toHaveTextContent(
      "2 rows of SensiPro meters 2023-07-01..2023-07-02, read from the database when the run starts.",
    );
  });

  it("lists only connected sources to pull from", async () => {
    register([CAMERAS, METERS]);
    connections();
    await composeWith("pull");
    const options = within(await screen.findByTestId("src_0_source"))
      .getAllByRole("option")
      .map((option) => option.textContent);
    // The cameras have no connection, so only the meters are offered — then a way to add another.
    expect(options).toEqual(["— none —", "OT · SensiPro meters", "+ Create new data source…"]);
  });

  it("sends the run the pull to read, never the rows (ADR-0047)", async () => {
    // The point of the change: the browser holds a sample, the server reads the window. A run
    // that posted `content` would look identical on screen and be capped again.
    register([METERS]);
    connections();
    await composeWith("pull");
    await pullMeters();
    await screen.findByTestId("src_0_pulled");

    await userEvent.click(screen.getByTestId("step_4"));
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));
    await screen.findByText("Run complete.");
    const body = captured.runBody as { inputs: unknown[] };
    expect(body.inputs).toEqual([
      {
        pull: {
          source_id: METERS.source_id,
          start: "2023-07-01",
          end: "2023-07-02",
          params: {},
        },
        origin: "SensiPro meters 2023-07-01..2023-07-02",
      },
    ]);
  });

  it("drops the pull when the source switches to an upload", async () => {
    register([METERS]);
    connections();
    await composeWith("pull");
    await pullMeters();
    await screen.findByTestId("src_0_pulled");
    await userEvent.click(screen.getByTestId("src_0_mode_upload"));
    // Kept behind the upload's fields, the pull would still have been sent with the run.
    expect(screen.queryByTestId("src_0_pulled")).not.toBeInTheDocument();
    expect(screen.getByText(/Source 1 needs content/)).toBeInTheDocument();
    await userEvent.upload(
      screen.getByTestId("src_0_file"),
      new File([PULLED_CSV], "mine.csv", { type: "text/csv" }),
    );
    expect(await screen.findByText(/lines loaded from/)).toHaveTextContent("mine.csv");
  });

  it("shows why a pull was refused", async () => {
    register([METERS]);
    connections();
    server.use(
      http.post(`${PIPELINE}/data-sources/:id/pull`, () =>
        HttpResponse.json(
          {
            detail: "invalid pull parameters for sensipro: utility: Input should be 'electricity' or 'water'",
          },
          { status: 422 },
        ),
      ),
    );
    await composeWith("pull");
    await pullMeters("2023-07-31");
    expect(await screen.findByText(/invalid pull parameters for sensipro/)).toBeInTheDocument();
    expect(screen.queryByTestId("src_0_pulled")).not.toBeInTheDocument();
  });

  it("offers no pull while no data source has a connection", async () => {
    register([CAMERAS]);
    connections();
    renderApp("/compose");
    await screen.findByTestId("src_0_mode_upload");
    expect(screen.queryByTestId("src_0_mode_pull")).not.toBeInTheDocument();
  });

  it("shows no data-source picker while the organization has no data sources", async () => {
    register([]);
    await composeWith("paste");
    await screen.findByTestId("src_0_text");
    expect(screen.queryByTestId("src_0_source")).not.toBeInTheDocument();
    expect(screen.queryByTestId("src_0_mode_pull")).not.toBeInTheDocument();
  });
});

const LOG_API: DataSourceInfo = {
  source_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
  name: "Gateway logs",
  type: "IT",
  description: null,
  normalizer: "passthrough",
  source_format: "normalized-jsonl",
  connection: null,
  api: {
    url: "https://api.example.com/v1/logs",
    auth: "api_key",
    username: null,
    api_key_header: "Authorization",
    api_key_prefix: "Bearer ",
    start_param: "since",
    end_param: "until",
    window_format: "iso",
    next_path: "links.next",
    mapping: {
      lane: "log",
      records: "data",
      timestamp: "at",
      timestamp_format: "iso",
      timezone: null,
      asset_id: "host",
      metrics: {},
      message: "text",
      severity: "level",
    },
  },
  has_credentials: true,
  reader_params: { lane: "log" },
  created_at: "2026-11-09T09:00:00+00:00",
};

describe("Compose — creating a data source in a dialog", () => {
  const CREATED_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
  const dialog = () => screen.findByRole("dialog", { name: "Create a data source" });

  async function openDialog(mode: "pull" | "paste"): Promise<HTMLElement> {
    await composeWith(mode);
    const picker = await screen.findByTestId("src_0_source");
    await userEvent.selectOptions(picker, CREATE_SOURCE);
    return picker;
  }

  it("ends the picker with the entry and opens the form over Compose, keeping the choice", async () => {
    register([CAMERAS]);
    const picker = await openDialog("paste");
    expect(within(picker).getAllByRole("option").at(-1)).toHaveTextContent("+ Create new data source…");
    const form = await dialog();
    expect(within(form).getByTestId("source_form")).toBeInTheDocument();
    expect(within(form).getByTestId("ds_name")).toHaveFocus();
    expect(picker).toHaveValue(""); // the entry is an action, never a choice
    expect(screen.getByTestId("src_0_card")).toBeInTheDocument(); // Compose stayed put
  });

  it("saves the source, closes, and chooses it with the reader it names", async () => {
    register([CAMERAS]);
    const picker = await openDialog("paste");
    const form = await dialog();
    await userEvent.type(within(form).getByTestId("ds_name"), "Gateway logs");
    await userEvent.selectOptions(within(form).getByTestId("ds_normalizer"), "flow");
    await userEvent.selectOptions(within(form).getByTestId("ds_format"), "zeek-conn");
    await userEvent.click(within(form).getByTestId("ds_save"));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(sent.posted).toEqual([expect.objectContaining({ name: "Gateway logs", normalizer: "flow" })]);
    expect(picker).toHaveValue(CREATED_ID);
    expect(screen.getByTestId("src_0_norm")).toHaveValue("flow");
    expect(screen.getByTestId("src_0_fmt")).toHaveValue("zeek-conn");
  });

  it("chooses a new connected source for a pull and opens its pull panel", async () => {
    register([METERS]);
    connections();
    const picker = await openDialog("pull");
    const form = await dialog();
    await userEvent.type(within(form).getByTestId("ds_name"), "More meters");
    await userEvent.selectOptions(within(form).getByTestId("ds_connection"), "sensipro");
    await userEvent.click(within(form).getByTestId("ds_save"));

    await waitFor(() => expect(picker).toHaveValue(CREATED_ID));
    expect(await screen.findByTestId("src_0_pull_start")).toBeInTheDocument();
  });

  it("says why a new source with nothing to pull from is not chosen for a pull", async () => {
    register([METERS]);
    connections();
    const picker = await openDialog("pull");
    const form = await dialog();
    await userEvent.type(within(form).getByTestId("ds_name"), "Paper logbook");
    await userEvent.click(within(form).getByTestId("ds_save"));

    expect(await screen.findByTestId("src_0_created_note")).toHaveTextContent(
      "“Paper logbook” was added, but it has no connection or HTTP API, so there is nothing to pull from it.",
    );
    expect(picker).toHaveValue("");
  });

  it.each([
    ["Cancel", () => userEvent.click(screen.getByTestId("ds_cancel"))],
    ["the close button", () => userEvent.click(screen.getByTestId("create_source_close"))],
    ["Escape", () => userEvent.keyboard("{Escape}")],
  ])("closes on %s without saving anything", async (_how, close) => {
    register([CAMERAS]);
    const picker = await openDialog("paste");
    await userEvent.type(within(await dialog()).getByTestId("ds_name"), "Never mind");
    await close();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(sent.posted).toEqual([]);
    expect(picker).toHaveValue("");
  });

  it("is not offered to a member, who cannot add data sources", async () => {
    const member: MeResponse = {
      user_id: "22222222-2222-4222-8222-222222222222",
      username: "bob",
      role: "member",
      organization: { org_id: "acme", name: "Acme" },
    };
    server.use(http.get(`${PIPELINE}/auth/me`, () => HttpResponse.json(member)));
    register([CAMERAS]);
    await composeWith("paste");
    const options = within(await screen.findByTestId("src_0_source")).getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual(["— none —", "IT · Camera network"]);
  });
});

describe("Data sources page — an HTTP API (ADR-0049)", () => {
  it("registers an API with a key and a mapping, sending the key once", async () => {
    register([]);
    renderApp("/data-sources");
    await userEvent.type(await screen.findByTestId("ds_name"), "Plant API");
    await userEvent.selectOptions(screen.getByTestId("ds_type"), "OT");
    await userEvent.selectOptions(screen.getByTestId("ds_connection"), "(http-api)");
    // Neither a reader nor a format is asked for: the mapping decides how it is read.
    expect(screen.queryByTestId("ds_normalizer")).not.toBeInTheDocument();
    await userEvent.type(screen.getByTestId("ds_api_url"), "https://api.example.com/v1/readings");
    await userEvent.selectOptions(screen.getByTestId("ds_api_auth"), "api_key");
    await userEvent.type(screen.getByTestId("ds_api_key"), "sk-123");
    await userEvent.type(screen.getByTestId("ds_api_records"), "data.items");
    await userEvent.type(screen.getByTestId("ds_api_asset"), "device.id");
    // Saving waits for the mapping to name at least one metric.
    expect(screen.getByTestId("ds_save")).toBeDisabled();
    expect(screen.getByTestId("ds_api_problem")).toHaveTextContent("Name at least one metric.");
    await userEvent.type(screen.getByTestId("ds_api_metrics"), "Temperature = readings.temp");
    await userEvent.click(screen.getByTestId("ds_save"));

    await waitFor(() => expect(sent.posted).toHaveLength(1));
    expect(sent.posted[0]).toEqual({
      name: "Plant API",
      type: "OT",
      description: null,
      normalizer: null,
      source_format: null,
      connection: null,
      api: {
        url: "https://api.example.com/v1/readings",
        auth: "api_key",
        username: null,
        api_key_header: "Authorization",
        api_key_prefix: "Bearer ",
        start_param: "start",
        end_param: "end",
        window_format: "iso",
        next_path: null,
        mapping: {
          lane: "telemetry",
          records: "data.items",
          timestamp: "timestamp",
          timestamp_format: "iso",
          timezone: null,
          asset_id: "device.id",
          metrics: { Temperature: "readings.temp" },
          message: null,
          severity: null,
        },
      },
      credentials: { password: null, api_key: "sk-123" },
    });
  });

  it("lists an API source by its host and lane", async () => {
    register([LOG_API]);
    renderApp("/data-sources");
    expect(await screen.findByText("(pulled from api.example.com, log)")).toBeInTheDocument();
  });

  it("opens an edit with the key blank, and blank keeps the stored key", async () => {
    register([LOG_API]);
    renderApp("/data-sources");
    await userEvent.click(await screen.findByTestId("ds_edit_Gateway logs"));
    expect(screen.getByTestId("ds_connection")).toHaveValue("(http-api)");
    expect(screen.getByTestId("ds_api_url")).toHaveValue("https://api.example.com/v1/logs");
    expect(screen.getByTestId("ds_api_message")).toHaveValue("text");
    const key = screen.getByTestId("ds_api_key");
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("type", "password");
    expect(screen.getByText("Leave blank to keep the stored one.")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("ds_save"));
    await waitFor(() => expect(sent.put).toHaveLength(1));
    expect(sent.put[0]).toMatchObject({
      api: { url: "https://api.example.com/v1/logs", next_path: "links.next", mapping: { lane: "log" } },
      credentials: { password: null, api_key: null },
    });
  });

  it("turning an API source back into a plain one sends no api, which removes it", async () => {
    register([LOG_API]);
    renderApp("/data-sources");
    await userEvent.click(await screen.findByTestId("ds_edit_Gateway logs"));
    await userEvent.selectOptions(screen.getByTestId("ds_connection"), "");
    await userEvent.click(screen.getByTestId("ds_save"));
    await waitFor(() => expect(sent.put).toHaveLength(1));
    expect(sent.put[0]).not.toHaveProperty("api");
    expect(sent.put[0]).not.toHaveProperty("credentials");
  });
});

describe("Compose — pulling from an HTTP API source", () => {
  const passthrough = {
    name: "passthrough",
    kind: "normalizer",
    consumes: ["format:normalized-jsonl"],
    produces: ["lane:telemetry"],
    formats: ["normalized-jsonl"],
    params: [
      { name: "lane", type: "str", default: "telemetry", required: false, choices: ["flow", "log", "telemetry"] },
      { name: "payload", type: "str", default: "", required: false, choices: null },
    ],
    artifact_param: null,
  };
  const SAMPLE = '{"source_type": "log", "timestamp": "2026-11-01T10:00:00+00:00", "asset_id": "gw", "message": "boot"}\n';

  function apiServer(): void {
    server.use(
      http.get(`${PIPELINE}/modules`, () =>
        HttpResponse.json({ ...seed.modules, normalizers: [...seed.modules.normalizers, passthrough] }),
      ),
      http.post(`${PIPELINE}/data-sources/:id/pull`, async ({ request }) => {
        sent.pulls.push(await request.json());
        return HttpResponse.json({
          sample: SAMPLE,
          normalizer: "passthrough",
          source_format: "normalized-jsonl",
          rows: 1,
          origin: "Gateway logs 2026-11-01..2026-11-01",
        });
      }),
    );
  }

  it("offers an API source with no connection, and runs it read in its mapped lane", async () => {
    register([LOG_API]);
    apiServer();
    await composeWith("pull");
    await userEvent.selectOptions(await screen.findByTestId("src_0_source"), LOG_API.source_id);
    const panel = await screen.findByTestId("src_0_pull");
    expect(within(panel).getByText("api.example.com")).toBeInTheDocument();
    // An API takes no pull params and lists no devices: the window is the whole request.
    expect(screen.queryByTestId("src_0_pull_devices")).not.toBeInTheDocument();
    await userEvent.type(screen.getByTestId("src_0_pull_start"), "2026-11-01");
    await userEvent.type(screen.getByTestId("src_0_pull_end"), "2026-11-01");
    await userEvent.click(screen.getByTestId("src_0_pull_go"));
    await waitFor(() => expect(sent.pulls).toEqual([{ start: "2026-11-01", end: "2026-11-01", params: {} }]));
    expect(await screen.findByTestId("src_0_pulled")).toHaveTextContent("read from the API when the run starts.");

    await userEvent.click(screen.getByTestId("step_4"));
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));
    await screen.findByText("Run complete.");
    const body = captured.runBody as { spec: { sources: { normalizer: string; params: unknown }[] }; inputs: unknown[] };
    expect(body.spec.sources[0]).toMatchObject({ normalizer: "passthrough", params: { lane: "log" } });
    expect(body.inputs).toEqual([
      {
        pull: { source_id: LOG_API.source_id, start: "2026-11-01", end: "2026-11-01", params: {} },
        origin: "Gateway logs 2026-11-01..2026-11-01",
      },
    ]);
  });

  async function pullLogsAndReview(): Promise<void> {
    register([LOG_API]);
    apiServer();
    await composeWith("pull");
    await userEvent.selectOptions(await screen.findByTestId("src_0_source"), LOG_API.source_id);
    await userEvent.type(await screen.findByTestId("src_0_pull_start"), "2026-11-01");
    await userEvent.type(screen.getByTestId("src_0_pull_end"), "2026-11-01");
    await userEvent.click(screen.getByTestId("src_0_pull_go"));
    await screen.findByTestId("src_0_pulled");
    await userEvent.click(screen.getByTestId("step_4"));
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
  }

  it("saves the pulled chain as a scheduled job reading a rolling window (ADR-0050)", async () => {
    const jobs: unknown[] = [];
    server.use(
      http.post(`${PIPELINE}/jobs`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        jobs.push(body);
        return HttpResponse.json(
          {
            ...body,
            job_id: "j-1",
            created_by: "local",
            created_at: "2026-11-09T00:00:00+00:00",
            updated_at: "2026-11-09T00:00:00+00:00",
            schedule_text: "every day at 03:30 UTC",
            next_run_at: "2026-11-09T03:30:00+00:00",
            last: null,
          },
          { status: 201 },
        );
      }),
    );
    await pullLogsAndReview();
    await userEvent.click(screen.getByText(/Save as a job/));
    expect(screen.getByTestId("job_create")).toBeDisabled(); // no name yet
    expect(screen.getByTestId("job_problem")).toHaveTextContent("Name the job.");
    await userEvent.type(screen.getByTestId("job_name"), "Nightly gateway logs");
    await userEvent.selectOptions(screen.getByTestId("job_window_kind_0"), "rolling");
    await userEvent.clear(screen.getByTestId("job_days_0"));
    await userEvent.type(screen.getByTestId("job_days_0"), "1");
    await userEvent.selectOptions(screen.getByTestId("job_every"), "day");
    await userEvent.clear(screen.getByTestId("job_time"));
    await userEvent.type(screen.getByTestId("job_time"), "03:30");
    await userEvent.click(screen.getByTestId("job_create"));

    expect(await screen.findByTestId("job_saved")).toHaveTextContent("runs every day at 03:30 UTC");
    expect(jobs).toEqual([
      {
        name: "Nightly gateway logs",
        spec: expect.objectContaining({
          sources: [expect.objectContaining({ normalizer: "passthrough", params: { lane: "log" } })],
        }),
        sources: [{ source_id: LOG_API.source_id, params: {}, window: { kind: "rolling", days: 1, through: "yesterday" } }],
        schedule: { every: "day", hour: 3, minute: 30, weekday: 0 },
        enabled: true,
        notify_emails: [],
      },
    ]);
  });

  it("reopens a job on Compose and updates it in place", async () => {
    const JOB_ID = "abababab-abab-4bab-8bab-abababababab";
    const job = {
      job_id: JOB_ID,
      name: "Nightly gateway logs",
      created_by: "local",
      created_at: "2026-11-09T00:00:00+00:00",
      updated_at: "2026-11-09T00:00:00+00:00",
      spec: {
        sources: [{ normalizer: "passthrough", source_format: "normalized-jsonl", params: { lane: "log" } }],
        detectors: [{ name: "log_burst", params: {} }],
        alert_engine: { name: "stub", params: {} },
        sinks: [{ name: "postgres", params: {} }],
      },
      sources: [{ source_id: LOG_API.source_id, params: {}, window: { kind: "fixed", start: "2026-11-01", end: "2026-11-02" } }],
      schedule: null,
      schedule_text: null,
      enabled: true,
      next_run_at: null,
      notify_emails: ["ops@example.com"],
      last: null,
    };
    const puts: unknown[] = [];
    register([LOG_API]);
    apiServer();
    server.use(
      http.get(`${PIPELINE}/jobs/:id`, () => HttpResponse.json(job)),
      http.put(`${PIPELINE}/jobs/:id`, async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        puts.push(body);
        return HttpResponse.json({ ...job, ...body });
      }),
    );
    renderApp(`/compose?job=${JOB_ID}`);
    expect(await screen.findByTestId("compose_reopened_job")).toHaveTextContent("Editing the job “Nightly gateway logs”");
    expect(screen.getByTestId("src_0_tab")).toHaveTextContent("Gateway logs 2026-11-01..2026-11-02");

    await userEvent.click(screen.getByTestId("step_4"));
    await waitFor(() => expect(screen.getByTestId("job_update")).toBeEnabled());
    expect(screen.getByTestId("job_name")).toHaveValue("Nightly gateway logs");
    await userEvent.click(screen.getByTestId("job_update"));
    await screen.findByTestId("job_saved");
    expect(puts).toEqual([
      expect.objectContaining({
        name: "Nightly gateway logs",
        sources: job.sources,
        schedule: null,
      }),
    ]);
  });

  it("will not save an upload as a job, and says why", async () => {
    await composeWith("paste");
    await userEvent.type(await screen.findByTestId("src_0_text"), "2026-07-07T12:00:00+00:00,pump-02,Pressure,45.0");
    await userEvent.click(screen.getByTestId("step_4"));
    await userEvent.click(await screen.findByText(/Save as a job/));
    expect(screen.getByTestId("job_blocker")).toHaveTextContent("source 1 is an upload or paste");
    expect(screen.queryByTestId("job_create")).not.toBeInTheDocument();
  });
});
