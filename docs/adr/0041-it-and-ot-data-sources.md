# ADR-0041: IT and OT data sources

- **Status:** accepted
- **Date:** 2026-10-21
- **Related:** ADR-0039 (organizations; every row carries `org_id`), ADR-0019 (a run's
  `sources` list), ADR-0017 (a source is tagged, never sniffed)
- **Scope:** storage (one new table), the Pipeline API (a `/data-sources` router), the dashboard
  (a Data sources page, a picker on Compose's source card). **No contract changes**, and the
  pipeline does not read the new table.

## Context

The earlier iotShield prototype (a Laravel app) kept a per-tenant register of data sources, each
typed **IT** or **OT**. In its seed data, SensiPro LTD's "Operational Data" (utility meters) is
OT and Biology's "TCP Dump" (labelled network flows) is IT. Its dashboards are organized by that
split. We are bringing both sources and their charts over in four phases:

1. the register
2. reading the two live databases
3. a meter data-quality detector
4. the charts

This ADR covers the register only.

iotshield had no notion of a source outside a run. A run's `SourceSpec` names a normalizer and
a format and exists only for that run, so "where does this organization's data come from" was
not written down anywhere.

## Decisions

### D1 — A register, owned by the organization, read by nobody in the pipeline

`data_sources` stores the following, and `org_id` is required on every repository function
(the ADR-0039 rule):

- `org_id` and `name`. The name is unique within an organization; two organizations may both
  have a "Camera network".
- `type`: `IT` or `OT`, enforced by a check constraint.
- an optional `description`.
- an optional `normalizer` and `source_format`.

It is **not a projection of any contract**, like the account tables. A run never reads it, so
`PipelineSpec` and every adapter are unchanged. The register describes sources; it does not
configure runs.

### D2 — The two types are exactly IT and OT

The two types are named after the split the prototype drew and the industry draws:

- **IT** is information technology: network traffic and logs.
- **OT** is operational technology: the physical process — meters, sensors, controllers.

A closed vocabulary rather than free text, because Phase 4's charts filter on it and a typo
would quietly make a source disappear from a page.

We did not derive the type from the lane, even though the two usually line up (flow and log
are IT, telemetry is OT). Syslog from a PLC is a log-lane source on the OT side. Only the
person registering a source knows which side it belongs to.

### D3 — The reader is checked against the registry the run uses

A source may name how its data is read. The API checks that pairing the same way the
compatibility guard checks a run:

- the normalizer must exist;
- the format must be one that normalizer reads;
- a format named without a normalizer is refused;
- a normalizer that reads several formats must have one named, because it is never guessed
  (ADR-0017).

When a normalizer reads exactly one format, that format is filled in. Saving an unusable
pairing would only push the refusal into Compose, where the guard would then reject the run.

### D4 — Anyone in the organization reads; its admins write

This is the same split as the organization's users. With auth off, every request is the
default organization's admin, as on every other route. The router is mounted on the Pipeline
API because it writes; ADR-0007 keeps the read API write-free.

A super admin without a home organization is refused here by `AccountDep`, as on every data
route. Deleting an organization (`accounts.delete_organization`) deletes its data sources too.

### D5 — Compose offers the register; it does not require it

Compose's source card shows a **Data source** picker when the organization has any data
sources. Picking one fills in the normalizer and format it names. Choosing a different
normalizer by hand clears the selection, because the source is then no longer read as the
register says. The content is still uploaded or pasted. Pulling it from the source itself is
Phase 2.

## Consequences

- One new table. `create_schema` (`create_all`) adds it to an existing database, so there is
  no `down -v` this time.
- **A run still does not record which data source it came from** (ADR-0042 decided it need not:
  the charts' IT/OT split comes from what the events are). The selection lives only on
  the Compose page and is not sent. Phase 2 (live database pulls) is where a run first starts
  *from* a data source, and so where stamping events with their source belongs. The IT/OT
  filter the charts need depends on it.
- `normalizer`/`source_format` are checked when the source is saved, not again later. If a
  normalizer is removed from the registry, a stored source naming it becomes stale. Compose
  then leaves the reader alone, as it does for a source that names none.
