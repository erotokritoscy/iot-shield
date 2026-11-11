/** The Triage chart's severity checkboxes. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { describe, expect, it } from "vitest";

import type { AlertSeverity } from "@/lib/api/types";
import { CHART_SEVERITIES, SeverityChartFilter } from "@/pages/triage/SeverityChartFilter";

function Harness({ initial }: { readonly initial: readonly AlertSeverity[] }) {
  const [value, setValue] = React.useState<AlertSeverity[]>([...initial]);
  return (
    <>
      <SeverityChartFilter value={value} onValueChange={setValue} />
      <output data-testid="value">{value.join(",")}</output>
    </>
  );
}

const box = (name: string) => screen.getByRole("checkbox", { name });
const value = () => screen.getByTestId("value").textContent;

describe("SeverityChartFilter", () => {
  it("offers the bands highest first", () => {
    render(<Harness initial={CHART_SEVERITIES} />);
    const labels = screen.getAllByRole("checkbox").map((c) => (c as HTMLInputElement).labels?.[0]?.textContent);
    expect(labels).toEqual(["emergency", "critical", "high", "medium", "low"]);
    expect(screen.getByRole("group", { name: "Chart severities" })).toBeInTheDocument();
  });

  it("hides and shows a band", async () => {
    render(<Harness initial={CHART_SEVERITIES} />);
    await userEvent.click(box("low"));
    expect(box("low")).not.toBeChecked();
    expect(value()).toBe("emergency,critical,high,medium");
    await userEvent.click(box("low"));
    expect(value()).toBe("emergency,critical,high,medium,low");
  });

  it("keeps the offered order whatever the click order", async () => {
    render(<Harness initial={[]} />);
    await userEvent.click(box("low"));
    await userEvent.click(box("emergency"));
    expect(value()).toBe("emergency,low");
  });
});
