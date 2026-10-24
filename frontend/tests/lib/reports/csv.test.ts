import { downloadCsv, toCsv } from "@/lib/reports/csv";

describe("toCsv", () => {
  it("joins headers and rows with commas + CRLF line breaks", () => {
    const csv = toCsv(
      ["Category", "Amount"],
      [
        ["Food", 200],
        ["Transport", 80],
      ],
    );
    expect(csv).toBe("Category,Amount\r\nFood,200\r\nTransport,80");
  });

  it("quotes fields containing a comma", () => {
    const csv = toCsv(["Label", "Value"], [["Food, drink", 10]]);
    expect(csv).toBe('Label,Value\r\n"Food, drink",10');
  });

  it("quotes and doubles interior double-quotes", () => {
    const csv = toCsv(["Label"], [['He said "hi"']]);
    expect(csv).toBe('Label\r\n"He said ""hi"""');
  });

  it("quotes fields containing newlines", () => {
    const csv = toCsv(["Label"], [["line1\nline2"]]);
    expect(csv).toBe('Label\r\n"line1\nline2"');
  });

  it("renders numbers without quoting", () => {
    const csv = toCsv(["A", "B"], [[1, 2.5]]);
    expect(csv).toBe("A,B\r\n1,2.5");
  });

  it("renders null as an empty field", () => {
    const csv = toCsv(["A", "B"], [["x", null]]);
    expect(csv).toBe("A,B\r\nx,");
  });

  it("quotes a header that itself contains a comma", () => {
    const csv = toCsv(["Sum, total"], [[5]]);
    expect(csv).toBe('"Sum, total"\r\n5');
  });

  it("handles an empty row set (headers only)", () => {
    const csv = toCsv(["A", "B"], []);
    expect(csv).toBe("A,B");
  });
});

describe("toCsv formula neutralisation (FE-04)", () => {
  it.each([
    ["=HYPERLINK(\"x\")", "\"'=HYPERLINK(\"\"x\"\")\""],
    ["+1+2", "'+1+2"],
    ["-2+3", "'-2+3"],
    ["@SUM(A1)", "'@SUM(A1)"],
    ["\tTab", "\"'\tTab\""],
    ["\rReturn", "\"'\rReturn\""],
    ["\nFeed", "\"'\nFeed\""],
  ])("prefixes a text cell starting with a formula trigger: %j", (cell, expected) => {
    expect(toCsv(["Category"], [[cell]])).toBe(`Category\r\n${expected}`);
  });

  it("neutralises headers too (series headers carry user-named values)", () => {
    expect(toCsv(["=cmd", "Amount"], [])).toBe("'=cmd,Amount");
  });

  it("quotes a neutralised cell that also contains a comma", () => {
    expect(toCsv(["A"], [["=1,2"]])).toBe("A\r\n\"'=1,2\"");
  });

  it("leaves numeric cells untouched, including negatives", () => {
    expect(toCsv(["Category", "Amount"], [["Refund", -42.5], ["Fee", -0.01]])).toBe(
      "Category,Amount\r\nRefund,-42.5\r\nFee,-0.01",
    );
  });

  it.each(["-42.50", "+3", "-7", "-1e3", ".5", "-.5"])(
    "leaves a text cell shaped like a plain number untouched: %j",
    (cell) => {
      expect(toCsv(["A"], [[cell]])).toBe(`A\r\n${cell}`);
    },
  );

  it("leaves text that only contains a trigger character later untouched", () => {
    expect(toCsv(["A"], [["Food=Drink"], ["Self-care"]])).toBe("A\r\nFood=Drink\r\nSelf-care");
  });

  // Spreadsheets in some locales split on ";" (or a user-picked tab or "|")
  // instead of ",". Quoting keeps such a cell whole, so its tail can never
  // become a separate cell that starts with a formula trigger.
  it.each([
    ["foo;=HYPERLINK(1)", "\"foo;=HYPERLINK(1)\""],
    ["foo\t=1+1", "\"foo\t=1+1\""],
    ["foo|=1+1", "\"foo|=1+1\""],
    ["a;b", "\"a;b\""],
  ])("quotes a text cell containing another list separator: %j", (cell, expected) => {
    expect(toCsv(["A"], [[cell]])).toBe(`A\r\n${expected}`);
  });

  it("quotes and prefixes a cell that both starts with a trigger and holds a separator", () => {
    expect(toCsv(["A"], [["=1;=2"]])).toBe("A\r\n\"'=1;=2\"");
  });

  it("keeps negative numbers numeric next to separator-bearing text", () => {
    expect(toCsv(["Category", "Amount"], [["Fees; bank", -12.5]])).toBe(
      "Category,Amount\r\n\"Fees; bank\",-12.5",
    );
  });
});

describe("downloadCsv", () => {
  it("creates a blob anchor and triggers a click with the slug filename", () => {
    const createObjectURL = vi.fn((_blob: Blob) => "blob:mock-url");
    const revokeObjectURL = vi.fn((_url: string) => {});
    // jsdom doesn't implement URL.createObjectURL.
    (URL as unknown as { createObjectURL: typeof createObjectURL }).createObjectURL =
      createObjectURL;
    (URL as unknown as { revokeObjectURL: typeof revokeObjectURL }).revokeObjectURL =
      revokeObjectURL;

    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => {});

    downloadCsv("spend.csv", "A,B\r\n1,2");

    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const blob = createObjectURL.mock.calls[0][0];
    expect(blob).toBeInstanceOf(Blob);
    expect(blob.type).toContain("text/csv");
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock-url");

    clickSpy.mockRestore();
  });
});
