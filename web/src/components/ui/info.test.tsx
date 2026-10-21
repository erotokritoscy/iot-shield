/**
 * The (i) beside a label: hover and focus show it, a click pins it, Escape and an outside
 * click close a pinned one — and it never joins the control's accessible name.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Field, MultiSelect } from "@/components/ui/controls";
import { InfoTip } from "@/components/ui/info";

describe("InfoTip", () => {
  it("shows on hover and hides when the pointer leaves", async () => {
    render(<InfoTip label="Normalizer">Reads the file.</InfoTip>);
    const button = screen.getByRole("button", { name: "About Normalizer" });
    expect(screen.queryByRole("tooltip")).toBeNull();
    await userEvent.hover(button);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Reads the file.");
    expect(button).toHaveAccessibleDescription("Reads the file.");
    await userEvent.unhover(button);
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("shows on keyboard focus", async () => {
    render(<InfoTip label="Origin">Where it came from.</InfoTip>);
    await userEvent.tab();
    expect(screen.getByRole("tooltip")).toHaveTextContent("Where it came from.");
  });

  it("stays open once clicked, and closes on Escape or a click elsewhere", async () => {
    render(
      <div>
        <InfoTip label="Sinks">Where results go.</InfoTip>
        <p>outside</p>
      </div>,
    );
    const button = screen.getByRole("button", { name: "About Sinks" });
    await userEvent.click(button);
    await userEvent.unhover(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("tooltip")).toBeInTheDocument();

    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).toBeNull();

    await userEvent.click(button);
    await userEvent.click(screen.getByText("outside"));
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});

describe("the info prop", () => {
  it("puts an (i) beside a Field without changing the control's name", () => {
    render(
      <Field label="Normalizer" htmlFor="n" info="Reads the file.">
        <input id="n" />
      </Field>,
    );
    expect(screen.getByLabelText("Normalizer")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "About Normalizer" })).toBeInTheDocument();
  });

  it("renders no (i) when a Field has no info", () => {
    render(
      <Field label="Normalizer" htmlFor="n">
        <input id="n" />
      </Field>,
    );
    expect(screen.queryByRole("button", { name: /About/ })).toBeNull();
  });

  it("keeps a MultiSelect group named by its label alone", () => {
    render(<MultiSelect label="Sinks" options={["postgres"]} value={[]} onValueChange={() => {}} info="x" />);
    expect(screen.getByRole("group", { name: "Sinks" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "About Sinks" })).toBeInTheDocument();
  });
});
