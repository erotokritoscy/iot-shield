/**
 * Rendering a benchmark report (E11–E15).
 *
 * Head-to-head, per-case variance, one case's timeline, the per-case table — the four views
 * the Streamlit page drew, with their explanatory captions carried over near-verbatim.
 * Those captions are not decoration: they are what stops `event_f1`, an empty cell and a
 * flat sweep line from being misread.
 */
import * as React from "react";

import { Chart, useChartPalette } from "@/components/Chart";
import { Field, Select } from "@/components/ui/controls";
import { Notice, Section } from "@/components/ui/surfaces";
import { DynamicTable } from "@/components/ui/table";
import { caseTimeline, caseVariance, parseSweep, specScores, sweepCurve, type ScoreRow } from "@/charts/specs";
import { TIMELINE_SPAN_CAP } from "@/lib/api/client";
import type { EvalCaseReport, EvalReport, EvalSpecReport } from "@/lib/api/schemas";
import { formatUtc } from "@/lib/format";

const casesFitted = (spec: EvalSpecReport): number =>
  spec.cases.filter((item) => item.fitted).length;

function CaseTimelineView({ report }: { readonly report: EvalReport }) {
  const palette = useChartPalette();
  const [specName, setSpecName] = React.useState<string | null>(null);
  const spec = report.specs.find((item) => item.spec === specName) ?? report.specs[0];
  const [caseName, setCaseName] = React.useState<string | null>(null);
  const chosen = spec?.cases.find((item) => item.case === caseName) ?? spec?.cases[0];

  if (spec === undefined || chosen === undefined) return null;
  const timeline = chosen.timeline;

  return (
    <Section title="What actually happened">
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Spec" htmlFor="eval_timeline_spec">
          <Select
            id="eval_timeline_spec"
            data-testid="eval_timeline_spec"
            options={report.specs.map((item) => item.spec)}
            value={spec.spec}
            onChange={(event) => {
              setSpecName(event.target.value);
              setCaseName(null);
            }}
          />
        </Field>
        <Field label="Case" htmlFor="eval_timeline_case">
          <Select
            id="eval_timeline_case"
            data-testid="eval_timeline_case"
            options={spec.cases.map((item) => item.case)}
            value={chosen.case}
            onChange={(event) => setCaseName(event.target.value)}
          />
        </Field>
      </div>

      {timeline === null || timeline === undefined ? (
        <Notice tone="info">This report carries no timelines — re-run with them enabled.</Notice>
      ) : (
        (() => {
          const spec2 = caseTimeline(timeline.truth, timeline.detections, timeline.alerts, palette);
          return (
            <>
              {spec2 === null ? (
                <Notice tone="info">
                  Nothing to draw: no faults, no detections and no alerts on this case.
                </Notice>
              ) : (
                <Chart spec={spec2} label={`Truth, detections and alerts for ${chosen.case}`} />
              )}
              {timeline.truncated ? (
                <Notice tone="warning">
                  Truncated at {TIMELINE_SPAN_CAP} spans per lane — this detector emits more
                  detections than a chart can show, which is itself the finding.
                </Notice>
              ) : null}
              <p className="caption">
                <b>truth</b> is where the fault really was; <b>detections</b> are what the detector
                emitted; <b>alerts</b> are what the correlation stage compressed them into. The gap
                between the truth band&apos;s left edge and the first detection is the latency.
              </p>
            </>
          );
        })()
      )}
    </Section>
  );
}

export function ReportView({ report }: { readonly report: EvalReport }) {
  const palette = useChartPalette();

  // One construction: `pooled` is the whole row as the report serialized it, and the chart
  // is a slice of it. Columns are whatever the report carries, so a metric the harness adds
  // appears here without a code change.
  const summaryRows = report.specs.map((spec) => ({
    spec: spec.spec,
    detectors: spec.detectors.join(", "),
    ...spec.pooled,
    fitted: `${casesFitted(spec)}/${spec.cases.length}`,
  }));
  const scores: ScoreRow[] = report.specs.map((spec) => ({
    spec: spec.spec,
    event_f1: spec.pooled["event_f1"] ?? null,
  }));

  // A sweep gets a curve, everything else gets bars: over a quantitative axis the reader
  // sees whether the parameter matters and where it peaks, which is the question the sweep
  // was run to answer and the one a bar chart of decoded labels hides (ADR-0022).
  const isSweep = parseSweep(scores) !== null;
  const chart = isSweep ? sweepCurve(scores, palette) : specScores(scores, palette);

  // `lanes` is a counts-of-counts matrix — several hundred characters of JSON in one cell,
  // which blows out the row and buries every column beside it. `DynamicTable` now drops
  // non-cell-shaped columns by SHAPE, so it needs no name here; it is not lost, it is rendered
  // below as what it actually is, a table. (`lane_recall` stays inline: one level of scalars
  // is cell-shaped, and `formatCell` gives its nulls the same em-dash every undefined gets.)
  const perCase = report.specs.flatMap((spec) =>
    spec.cases.map((item) => {
      const { precision_at_k: _p, timeline: _t, ...rest } = item as EvalCaseReport &
        Record<string, unknown>;
      return { spec: spec.spec, ...rest };
    }),
  );
  const laneRows = report.specs.flatMap((spec) =>
    spec.cases.flatMap((item) => {
      const lanes = (item as unknown as { lanes?: Record<string, Record<string, unknown>> }).lanes;
      return Object.entries(lanes ?? {}).map(([lane, counts]) => ({
        spec: spec.spec,
        case: item.case,
        lane,
        ...counts,
      }));
    }),
  );
  const notes = [
    ...new Set(report.specs.flatMap((spec) => spec.cases.flatMap((item) => item.notes))),
  ].sort();

  const varianceRows = report.specs.flatMap((spec) =>
    spec.cases.map((item) => ({
      spec: spec.spec,
      case: item.case,
      event_f1: item.event_f1,
    })),
  );
  const variance = caseVariance(varianceRows, palette);
  const totalCases = varianceRows.length;
  const scoredCases = varianceRows.filter(
    (row) => row.event_f1 !== null && row.event_f1 !== undefined,
  ).length;
  const unscored = totalCases - scoredCases;

  return (
    <div className="space-y-8">
      <p className="caption">
        <code className="font-mono">{report.corpus}</code> — {report.corpus_description} ·{" "}
        {report.cases} case(s), {report.incidents} incident(s) · fit protocol{" "}
        <b>{report.fit_protocol}</b> · generated {formatUtc(report.generated_at, "minute")} UTC
      </p>

      <Section title="Head to head">
        {chart === null ? null : (
          <Chart spec={chart} label="Event F1 per spec" />
        )}
        {isSweep ? (
          <p className="caption">
            One point per swept value, all other settings held fixed. A flat line means the
            hyperparameter does not matter on this corpus — a real result, and a cheaper one than
            tuning it.
          </p>
        ) : null}
        <DynamicTable
          rows={summaryRows}
          leading={["spec", "detectors"]}
          caption={
            <>
              <code>event_f1</code> is what an analyst experiences (one finding per fault);{" "}
              <code>point_f1</code> is what papers report (per-row coverage).{" "}
              <code>compression_ratio</code> is detections-per-incident over alerts-per-incident —
              what the correlation stage absorbed. An empty cell means <i>undefined</i>, not zero —
              a clean recording has no faults to recall.
            </>
          }
        />
      </Section>

      {report.cases > 1 ? (
        <Section title="Consistency across cases">
          {/* The caption used to render unconditionally, so a corpus of only clean recordings
              produced a heading and an explanation with nothing between them. */}
          {variance === null ? (
            <Notice tone="info">
              No recording in this corpus has a defined event F1, so there is nothing to plot
              here — every case is clean (see the per-case table for false alarms).
            </Notice>
          ) : (
            <>
              <Chart spec={variance} label="Event F1 per case, per spec" />
              <p className="caption">
                One dot per <i>scored</i> recording. A mean hides the shape: a detector scoring
                1.0 on most cases and 0 on a few is a different problem from one that is
                mediocre everywhere, and only this view tells them apart.
                {/* The filter is right — F1 is undefined with no incidents, and a dot at 0
                    would claim the detector scored nothing rather than that nothing was
                    measurable (ADR-0021 D2). What was wrong is that it was silent, and the
                    omitted cases are exactly where a false-alarming spec differs from a quiet
                    one, so a reader counting dots would conclude the corpus is smaller than
                    it is. Ties also coincide exactly, which the count makes recoverable. */}
                {" "}
                {unscored > 0 ? (
                  <>
                    <b>
                      {scoredCases} of {totalCases} recordings are plotted
                    </b>
                    : the other {unscored} are clean, so event F1 is undefined for them and a
                    dot would invent a score.
                  </>
                ) : (
                  <>All {totalCases} recordings are plotted.</>
                )}{" "}
                Dots at the same value sit on top of one another.
              </p>
            </>
          )}
        </Section>
      ) : null}

      <CaseTimelineView report={report} />

      {/* Every spec, not just the first: comparing them is the page's whole purpose, and a
          table pinned to `specs[0]` silently hid the second detector's cases and notes. */}
      <Section title="Per case">
        <DynamicTable rows={perCase} leading={["spec", "case"]} />
        <div className="space-y-2">
          {notes.map((note) => (
            <Notice key={note} tone="warning">
              {note}
            </Notice>
          ))}
        </div>
      </Section>

      {laneRows.length > 0 ? (
        <Section
          title="Per case, per lane"
          description="The counts behind each case's lane columns — a matrix, so it gets a table rather than a cell."
        >
          <DynamicTable rows={laneRows} leading={["spec", "case", "lane"]} />
        </Section>
      ) : null}
    </div>
  );
}
