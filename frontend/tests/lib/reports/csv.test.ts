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
