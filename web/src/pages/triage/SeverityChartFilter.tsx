/**
 * Which severities the Triage chart draws — one checkbox per band, all ticked to start.
 *
 * It narrows the CHART only: the queue keeps its own single-severity filter, which asks the
 * server. Hiding a band here is a display choice (e.g. hide 9,000 lows to see 2 criticals),
 * so it is page state and is not remembered across reloads — a chart that reopens silently
 * narrowed would be read as the whole queue.
 */
import { Checkbox } from "@/components/ui/controls";
import { InfoTip } from "@/components/ui/info";
import type { AlertSeverity } from "@/lib/api/types";

/** Highest first, as the options read; the chart keeps its own axis order. */
export const CHART_SEVERITIES: readonly AlertSeverity[] = ["emergency", "critical", "high", "medium", "low"];

export function SeverityChartFilter({
  value,
  onValueChange,
  info,
}: {
  readonly value: readonly AlertSeverity[];
  readonly onValueChange: (value: AlertSeverity[]) => void;
  readonly info?: React.ReactNode;
}) {
  const toggle = (severity: AlertSeverity) =>
    onValueChange(
      CHART_SEVERITIES.filter((item) => (item === severity ? !value.includes(item) : value.includes(item))),
    );
  return (
    <fieldset className="min-w-0" aria-labelledby="triage_chart_severity_label">
      <legend className="field-label flex items-center gap-1">
        <span id="triage_chart_severity_label">Chart severities</span>
        {info ? <InfoTip label="Chart severities">{info}</InfoTip> : null}
      </legend>
      <div className="flex flex-wrap gap-x-4 gap-y-1.5 rounded border border-line bg-surface px-3 py-2">
        {CHART_SEVERITIES.map((severity) => (
          <Checkbox
            key={severity}
            label={severity}
            checked={value.includes(severity)}
            onChange={() => toggle(severity)}
          />
        ))}
      </div>
    </fieldset>
  );
}
