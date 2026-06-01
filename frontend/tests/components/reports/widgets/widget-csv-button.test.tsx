import { fireEvent, render, screen } from "@testing-library/react";

import WidgetCsvButton from "@/components/reports/widgets/WidgetCsvButton";
import { downloadCsv } from "@/lib/reports/csv";

vi.mock("@/lib/reports/csv", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/reports/csv")>();
  return { ...actual, downloadCsv: vi.fn() };
});

describe("WidgetCsvButton", () => {
  const downloadMock = vi.mocked(downloadCsv);

  beforeEach(() => downloadMock.mockReset());

  it("renders in view mode", () => {
    render(
      <WidgetCsvButton
        title="Spend by category"
        dataset={{ headers: ["A"], rows: [["x"]] }}
      />,
    );
    expect(screen.getByTestId("widget-csv-export")).toBeInTheDocument();
  });

  it("hides in edit mode", () => {
    render(
      <WidgetCsvButton
        title="Spend"
        editMode
        dataset={{ headers: ["A"], rows: [["x"]] }}
      />,
    );
    expect(screen.queryByTestId("widget-csv-export")).toBeNull();
  });

  it("is disabled when there are no rows", () => {
    render(
      <WidgetCsvButton title="Spend" dataset={{ headers: ["A"], rows: [] }} />,
    );
    expect(screen.getByTestId("widget-csv-export")).toBeDisabled();
  });

  it("builds CSV from the dataset and downloads with a slugified filename", () => {
    render(
      <WidgetCsvButton
        title="Spend by Category"
        dataset={{
          headers: ["Category", "Amount"],
          rows: [
            ["Food", 200],
            ["Transport", 80],
          ],
        }}
      />,
    );

    fireEvent.click(screen.getByTestId("widget-csv-export"));

    expect(downloadMock).toHaveBeenCalledTimes(1);
    const [filename, csv] = downloadMock.mock.calls[0];
    expect(filename).toBe("spend-by-category.csv");
    expect(csv).toBe("Category,Amount\r\nFood,200\r\nTransport,80");
  });

  it("does not download when there are no rows (button disabled)", () => {
    render(<WidgetCsvButton title="X" dataset={{ headers: ["A"], rows: [] }} />);
    fireEvent.click(screen.getByTestId("widget-csv-export"));
    expect(downloadMock).not.toHaveBeenCalled();
  });
});
