import { renderWithSWR, fireEvent, screen, waitFor } from "../../../utils/render-with-swr";

import TagFilter from "@/components/reports/filters/TagFilter";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

const TAGS = [
  { id: 1, name: "groceries", name_normalized: "groceries", usage_count: 5 },
  { id: 2, name: "essentials", name_normalized: "essentials", usage_count: 2 },
];

describe("TagFilter", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
  });

  it("fetches tags on mount and renders chips", async () => {
    apiFetchMock.mockResolvedValueOnce(TAGS);

    renderWithSWR(
      <TagFilter value={[]} match="all" onChange={() => {}} />,
    );

    expect(
      await screen.findByTestId("tag-filter-chip-groceries"),
    ).toBeInTheDocument();
    expect(
      screen.getByTestId("tag-filter-chip-essentials"),
    ).toBeInTheDocument();
    expect(apiFetchMock).toHaveBeenCalledWith("/api/v1/tags");
  });

  it("toggles a chip on and reports the new value", async () => {
    apiFetchMock.mockResolvedValueOnce(TAGS);
    const onChange = vi.fn();

    renderWithSWR(
      <TagFilter value={[]} match="all" onChange={onChange} />,
    );

    const chip = await screen.findByTestId("tag-filter-chip-groceries");
    fireEvent.click(chip);
    expect(onChange).toHaveBeenCalledWith({
      tag_names: ["groceries"],
      tag_match: "all",
    });
  });

  it("toggles a chip off when already selected", async () => {
    apiFetchMock.mockResolvedValueOnce(TAGS);
    const onChange = vi.fn();

    renderWithSWR(
      <TagFilter
        value={["groceries", "essentials"]}
        match="all"
        onChange={onChange}
      />,
    );

    const chip = await screen.findByTestId("tag-filter-chip-groceries");
    fireEvent.click(chip);
    expect(onChange).toHaveBeenCalledWith({
      tag_names: ["essentials"],
      tag_match: "all",
    });
  });

  it("flips tag_match between all and any via the radios", async () => {
    apiFetchMock.mockResolvedValueOnce(TAGS);
    const onChange = vi.fn();

    renderWithSWR(
      <TagFilter
        value={["groceries"]}
        match="all"
        onChange={onChange}
      />,
    );

    await screen.findByTestId("tag-filter-chip-groceries");
    fireEvent.click(screen.getByTestId("tag-filter-match-any"));
    expect(onChange).toHaveBeenLastCalledWith({
      tag_names: ["groceries"],
      tag_match: "any",
    });
  });

  it("renders an error state when the fetch fails", async () => {
    apiFetchMock.mockRejectedValueOnce(new Error("boom"));

    renderWithSWR(
      <TagFilter value={[]} match="all" onChange={() => {}} />,
    );

    await waitFor(() =>
      expect(screen.getByTestId("tag-filter-error")).toBeInTheDocument(),
    );
  });
});
