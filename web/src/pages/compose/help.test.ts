/**
 * The (i) text: every input format is explained, and a param the table has not caught up with
 * still gets an (i) that states its type and default rather than none.
 *
 * Coverage of the *real* catalogue's params is not asserted here: the test catalogue is a
 * fixture with params of its own, so pinning prose against it would test the fixture.
 */
import { describe, expect, it } from "vitest";

import { modules } from "@/test/fixtures";
import { FORMAT_HELP, paramHelp } from "@/pages/compose/help";

describe("compose help text", () => {
  it("explains every input format a normalizer declares", () => {
    for (const normalizer of modules.normalizers) {
      for (const format of normalizer.formats) {
        expect(FORMAT_HELP[format], format).toBeTruthy();
      }
    }
  });

  it("appends the default, or says the param is optional", () => {
    expect(paramHelp("detector", "stub_threshold", { name: "threshold", type: "float", default: 80, required: false })).toMatch(
      /Default: 80\.$/,
    );
    expect(
      paramHelp("detector", "flow_burst", { name: "threshold", type: "float", default: null, required: false }),
    ).toMatch(/Optional\.$/);
  });

  it("falls back to the param's type for a param it does not know", () => {
    expect(paramHelp("detector", "new_one", { name: "k", type: "int", default: 3, required: false })).toBe(
      "A setting of new_one; expects a whole number. Default: 3.",
    );
    expect(
      paramHelp("detector", "new_one", { name: "mode", type: "str", default: null, required: true, choices: ["a", "b"] }),
    ).toBe("A setting of new_one; expects one of a, b. Required.");
  });
});
