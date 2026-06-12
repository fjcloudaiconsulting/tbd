import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { SeriesTooltip } from "@/components/charts/SeriesTooltip";

const fmt = (v: number) => `$${v.toFixed(2)}`;

describe("SeriesTooltip", () => {
  it("renders nothing when inactive", () => {
    const { container } = render(
      <SeriesTooltip
        active={false}
        payload={[{ dataKey: "planned", value: 10 }]}
        resolve={() => ({ label: "Planned", color: "var(--color-accent)" })}
        format={fmt}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the title plus a label + formatted value per resolved series", () => {
    render(
      <SeriesTooltip
        active
        label="Housing"
        payload={[
          { dataKey: "planned", value: 3800 },
          {
            dataKey: "actual",
            value: 3758.06,
            payload: { planned: 3800, actual: 3758.06 },
          },
        ]}
        resolve={(e) =>
          e.dataKey === "planned"
            ? { label: "Planned", color: "var(--color-accent)" }
            : { label: "Actual", color: "var(--color-success)" }
        }
        format={fmt}
      />,
    );
    expect(screen.getByText("Housing")).toBeInTheDocument();
    expect(screen.getByText("Planned")).toBeInTheDocument();
    expect(screen.getByText("$3800.00")).toBeInTheDocument();
    expect(screen.getByText("Actual")).toBeInTheDocument();
    expect(screen.getByText("$3758.06")).toBeInTheDocument();
  });

  it("omits rows whose resolve returns null", () => {
    render(
      <SeriesTooltip
        active
        label="X"
        payload={[
          { dataKey: "spent", value: 5 },
          { dataKey: "ghost", value: 9 },
        ]}
        resolve={(e) =>
          e.dataKey === "spent"
            ? { label: "Spent", color: "var(--color-accent)" }
            : null
        }
        format={fmt}
      />,
    );
    expect(screen.getByText("Spent")).toBeInTheDocument();
    expect(screen.queryByText("$9.00")).not.toBeInTheDocument();
  });
});
