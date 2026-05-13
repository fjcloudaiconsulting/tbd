import React from "react";
import { render, screen } from "@testing-library/react";

import FeedbackTrigger from "@/components/feedback/FeedbackTrigger";

const useAuthMock = vi.fn();

vi.mock("@/components/auth/AuthProvider", () => ({
  useAuth: () => useAuthMock(),
}));

describe("FeedbackTrigger", () => {
  beforeEach(() => {
    useAuthMock.mockReset();
  });

  it("renders nothing when there is no authenticated user", () => {
    useAuthMock.mockReturnValue({ user: null, loading: false });
    const { container } = render(<FeedbackTrigger />);
    expect(container.textContent).toBe("");
    expect(
      screen.queryByTestId("feedback-trigger"),
    ).not.toBeInTheDocument();
  });

  it("renders the trigger when the user is authenticated", () => {
    useAuthMock.mockReturnValue({
      user: { id: 1, username: "tester", email: "t@x.io" },
      loading: false,
    });
    render(<FeedbackTrigger />);
    expect(
      screen.getByRole("button", { name: /give feedback/i }),
    ).toBeInTheDocument();
  });
});
