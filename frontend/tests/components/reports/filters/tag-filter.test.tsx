import { renderWithSWR, act, fireEvent, screen, waitFor } from "../../../utils/render-with-swr";

import TagFilter from "@/components/reports/filters/TagFilter";
import { useTags } from "@/lib/hooks/use-tags";
import { useAuth } from "@/components/auth/AuthProvider";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", () => ({
  apiFetch: vi.fn(),
}));

vi.mock("@/components/auth/AuthProvider", () => ({ useAuth: vi.fn() }));

const TAGS = [
  { id: 1, name: "groceries", name_normalized: "groceries", usage_count: 5 },
  { id: 2, name: "essentials", name_normalized: "essentials", usage_count: 2 },
];

describe("TagFilter", () => {
  const apiFetchMock = vi.mocked(apiFetch);

  beforeEach(() => {
    apiFetchMock.mockReset();
    vi.mocked(useAuth).mockReturnValue({ user: { id: 1 }, loading: false } as never);
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

  it("shares the bare-path tags key (no duplicate ?for=reports-filter fetch)", async () => {
    apiFetchMock.mockResolvedValue(TAGS as never);

    function Harness() {
      useTags(true);
      return <TagFilter value={[]} match="all" onChange={() => {}} />;
    }

    renderWithSWR(<Harness />);

    await screen.findByTestId("tag-filter-chip-groceries");
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20));
    });

    const tagsCalls = apiFetchMock.mock.calls.filter(
      ([url]) => url === "/api/v1/tags",
    );
    expect(tagsCalls).toHaveLength(1);
  });

  it("shows the loading skeleton (not the empty state) while auth is gated off", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: null, loading: true } as never);
    apiFetchMock.mockResolvedValue(TAGS as never);

    renderWithSWR(
      <TagFilter value={[]} match="all" onChange={() => {}} />,
    );

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId("tag-filter-loading")).toBeInTheDocument();
    expect(screen.queryByText("No tags yet")).not.toBeInTheDocument();
  });

  it("does not fetch while auth is still loading (auth gate)", async () => {
    vi.mocked(useAuth).mockReturnValue({ user: null, loading: true } as never);
    apiFetchMock.mockResolvedValue(TAGS as never);

    renderWithSWR(
      <TagFilter value={[]} match="all" onChange={() => {}} />,
    );

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(apiFetchMock).not.toHaveBeenCalled();
  });
});
