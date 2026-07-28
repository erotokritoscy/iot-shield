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

  const perCase = report.specs.flatMap((spec) =>
    spec.cases.map((item) => {
      const { precision_at_k: _p, timeline: _t, ...rest } = item as EvalCaseReport &
        Record<string, unknown>;
      return { spec: spec.spec, ...rest };
    }),
  );
  const notes = [
    ...new Set(report.specs.flatMap((spec) => spec.cases.flatMap((item) => item.notes))),
  ].sort();

  const variance = caseVariance(
    report.specs.flatMap((spec) =>
      spec.cases.map((item) => ({
        spec: spec.spec,
        case: item.case,
        event_f1: item.event_f1,
      })),
    ),
    palette,
  );

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
          {variance === null ? null : <Chart spec={variance} label="Event F1 per case, per spec" />}
          <p className="caption">
            One dot per recording. A mean hides the shape: a detector scoring 1.0 on most cases and
            0 on a few is a different problem from one that is mediocre everywhere, and only this
            view tells them apart.
          </p>
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
    </div>
  );
}
