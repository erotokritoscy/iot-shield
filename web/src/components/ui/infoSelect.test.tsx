/**
 * InfoSelect: a dropdown with an (i) on every option. The (i) explains without choosing, and the
 * keyboard and an outside click behave like a select.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { Field, InfoSelect } from "@/components/ui/controls";

const HELP: Record<string, string> = {
  flow_burst: "Counts peers.",
  log_burst: "Counts lines.",
  rules: "Matches signatures.",
};

function Harness({ start = "log_burst" }: { readonly start?: string }) {
  const [value, setValue] = useState(start);
  return (
    <>
      <Field label="Detector" htmlFor="det">
        <InfoSelect
          id="det"
          data-testid="det"
          aria-label={`Detector: ${value}`}
          value={value}
          onChange={setValue}
          options={Object.keys(HELP)}
          labelFor={(option) => (option === "rules" ? "rules — no source feeds it" : option)}
          infoFor={(option) => HELP[option]}
        />
      </Field>
      <button type="button">elsewhere</button>
    </>
  );
}

const trigger = () => screen.getByTestId("det");
const option = (name: string) => screen.getByTestId(`det_option_${name}`);
const info = (name: string) => screen.getByRole("button", { name: `About ${name}` });

describe("InfoSelect", () => {
  it("shows the current value, and hides the list until opened", () => {
    render(<Harness />);
    expect(trigger()).toHaveTextContent("log_burst");
    expect(trigger()).toHaveAccessibleName("Detector: log_burst");
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("opens from its label", async () => {
    render(<Harness />);
    await userEvent.click(screen.getByText("Detector"));
    expect(screen.getByRole("list")).toBeInTheDocument();
  });

  it("explains each option on hover, without choosing it", async () => {
    render(<Harness />);
    await userEvent.click(trigger());
    for (const [name, text] of Object.entries(HELP)) {
      await userEvent.hover(info(name));
      expect(screen.getByRole("tooltip")).toHaveTextContent(text);
      await userEvent.unhover(info(name));
      expect(screen.queryByRole("tooltip")).toBeNull();
    }
    expect(trigger()).toHaveTextContent("log_burst");
  });

  it("does not choose an option or close when its (i) is clicked", async () => {
    render(<Harness />);
    await userEvent.click(trigger());
    await userEvent.click(info("rules"));
    expect(screen.getByRole("tooltip")).toHaveTextContent("Matches signatures.");
    expect(screen.getByRole("list")).toBeInTheDocument();
    expect(trigger()).toHaveTextContent("log_burst");
  });

  it("chooses by name, closes, and hands focus back", async () => {
    render(<Harness />);
    await userEvent.click(trigger());
    await userEvent.click(option("flow_burst"));
    expect(trigger()).toHaveTextContent("flow_burst");
    expect(screen.queryByRole("list")).toBeNull();
    expect(trigger()).toHaveFocus();
  });

  it("carries an option's label, and marks the current one", async () => {
    render(<Harness />);
    await userEvent.click(trigger());
    expect(option("rules")).toHaveTextContent("rules — no source feeds it");
    expect(option("log_burst")).toHaveAttribute("aria-current", "true");
    expect(option("rules")).not.toHaveAttribute("aria-current");
  });

  it("works from the keyboard: arrows move, Tab reaches the (i), Enter picks", async () => {
    render(<Harness />);
    trigger().focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(option("log_burst")).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}");
    expect(option("rules")).toHaveFocus();
    await userEvent.keyboard("{ArrowDown}"); // already last: stays put
    expect(option("rules")).toHaveFocus();
    await userEvent.tab();
    expect(info("rules")).toHaveFocus();
    expect(screen.getByRole("tooltip")).toHaveTextContent("Matches signatures.");
    await userEvent.keyboard("{ArrowUp}"); // from the (i), relative to its own row
    expect(option("log_burst")).toHaveFocus();
    await userEvent.keyboard("{ArrowUp}{Enter}");
    expect(trigger()).toHaveTextContent("flow_burst");
  });

  it("lets Escape close the tip first, then the list", async () => {
    render(<Harness />);
    await userEvent.click(trigger());
    await userEvent.tab();
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(screen.getByRole("list")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("list")).toBeNull();
    expect(trigger()).toHaveFocus();
  });

  it("closes on a click elsewhere, leaving the value alone", async () => {
    render(<Harness />);
    await userEvent.click(trigger());
    await userEvent.click(screen.getByRole("button", { name: "elsewhere" }));
    expect(screen.queryByRole("list")).toBeNull();
    expect(trigger()).toHaveTextContent("log_burst");
  });

  it("closes when focus tabs out of the list", async () => {
    render(<Harness />);
    await userEvent.click(trigger());
    option("rules").focus();
    await userEvent.tab(); // its (i)
    await userEvent.tab(); // out
    expect(screen.queryByRole("list")).toBeNull();
    expect(screen.getByRole("button", { name: "elsewhere" })).toHaveFocus();
  });

  it("still shows a value that is not among the options", () => {
    // A saved run restored into Compose may name a detector this deployment does not register.
    render(<Harness start="retired_detector" />);
    expect(trigger()).toHaveTextContent("retired_detector");
  });
});
