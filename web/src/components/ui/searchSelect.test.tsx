/**
 * SearchSelect: a dropdown whose open list starts with a search box. Typing filters the
 * options by every word of the label; the keyboard and an outside click behave like a select.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { Field, SearchSelect } from "@/components/ui/controls";

const NAMES: Record<string, string> = {
  "pump-02": "Feed pump 2 [pump-02]",
  "cam-07": "Lobby camera [cam-07]",
  "gw-11": "Edge gateway [gw-11]",
};

function Harness({ start = "pump-02" }: { readonly start?: string }) {
  const [value, setValue] = useState(start);
  return (
    <>
      <Field label="Asset" htmlFor="asset">
        <SearchSelect
          id="asset"
          data-testid="asset"
          value={value}
          onChange={setValue}
          options={Object.keys(NAMES)}
          labelFor={(id) => NAMES[id] ?? id}
        />
      </Field>
      <p data-testid="chosen">{value}</p>
      <button type="button">elsewhere</button>
    </>
  );
}

const options = () => screen.getAllByRole("option").map((option) => option.textContent);

describe("SearchSelect", () => {
  it("is named by its label and shows the chosen option's label, closed", () => {
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Asset" });
    expect(trigger).toHaveTextContent("Feed pump 2 [pump-02]");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("opens on the search box and filters by every word, case-insensitively", async () => {
    render(<Harness />);
    await userEvent.click(screen.getByTestId("asset"));
    const search = screen.getByTestId("asset_search");
    expect(search).toHaveFocus();
    expect(options()).toHaveLength(3);
    await userEvent.type(search, "CAM");
    expect(options()).toEqual(["Lobby camera [cam-07]"]);
    await userEvent.clear(search);
    await userEvent.type(search, "edge 11");
    expect(options()).toEqual(["Edge gateway [gw-11]"]);
    await userEvent.clear(search);
    await userEvent.type(search, "nothing like this");
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(screen.getByText("No matches.")).toBeInTheDocument();
  });

  it("picks with a click, closes, and opens again with an empty search", async () => {
    render(<Harness />);
    await userEvent.click(screen.getByTestId("asset"));
    await userEvent.type(screen.getByTestId("asset_search"), "gate");
    await userEvent.click(screen.getByRole("option", { name: "Edge gateway [gw-11]" }));
    expect(screen.getByTestId("chosen")).toHaveTextContent("gw-11");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByTestId("asset")).toHaveFocus();
    await userEvent.click(screen.getByTestId("asset"));
    expect(screen.getByTestId("asset_search")).toHaveValue("");
    expect(screen.getByRole("option", { name: "Edge gateway [gw-11]" })).toHaveAttribute("aria-selected", "true");
  });

  it("works from the keyboard: arrows move, Enter picks, Escape closes without picking", async () => {
    render(<Harness />);
    screen.getByTestId("asset").focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    // Opens on the chosen option; one down is the next one.
    await userEvent.keyboard("{ArrowDown}{Enter}");
    expect(screen.getByTestId("chosen")).toHaveTextContent("cam-07");
    await userEvent.click(screen.getByTestId("asset"));
    await userEvent.keyboard("{ArrowDown}{Escape}");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByTestId("chosen")).toHaveTextContent("cam-07");
  });

  it("closes on a click elsewhere, keeping the value", async () => {
    render(<Harness />);
    await userEvent.click(screen.getByTestId("asset"));
    await userEvent.click(screen.getByRole("button", { name: "elsewhere" }));
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByTestId("chosen")).toHaveTextContent("pump-02");
  });
});
