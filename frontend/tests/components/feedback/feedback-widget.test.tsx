import React from "react";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import FeedbackWidget from "@/components/feedback/FeedbackWidget";
import { apiFetch } from "@/lib/api";

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return { ...actual, apiFetch: vi.fn() };
});

const mockedApiFetch = vi.mocked(apiFetch);

function setLocation(href: string) {
  // jsdom does not allow direct assignment to window.location, but it
  // does honor a Property-defined replacement via Object.defineProperty.
  Object.defineProperty(window, "location", {
    writable: true,
    value: new URL(href),
  });
}

describe("FeedbackWidget", () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    setLocation("http://localhost/dashboard");
  });

  // -------------------------------------------------------------------
  // Closed-state contract
  // -------------------------------------------------------------------

  it("renders nothing when closed", () => {
    const { container } = render(
      <FeedbackWidget open={false} onClose={() => {}} />,
    );
    expect(container.textContent).toBe("");
  });

  // -------------------------------------------------------------------
  // Open-state shape
  // -------------------------------------------------------------------

  it("shows the form with category radios, message field, and identity opt-in OFF by default", () => {
    render(<FeedbackWidget open onClose={() => {}} />);

    expect(
      screen.getByRole("dialog", { name: /send feedback/i }),
    ).toBeInTheDocument();

    expect(screen.getByLabelText(/bug/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/feature/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/other/i)).toBeInTheDocument();

    const includeIdentity = screen.getByTestId(
      "feedback-include-identity",
    ) as HTMLInputElement;
    expect(includeIdentity.checked).toBe(false);
  });

  it("captures and discloses auto-context (URL, viewport, theme) without query strings", () => {
    setLocation("http://localhost/login?token=SECRET");
    render(<FeedbackWidget open onClose={() => {}} />);

    const details = screen.getByTestId("feedback-context-details");
    expect(details.textContent).toMatch(/Page/i);
    expect(details.textContent).toMatch(/Viewport/i);
    expect(details.textContent).toMatch(/Theme/i);
    // Query string MUST be stripped before disclosure.
    expect(details.textContent).not.toMatch(/SECRET/);
    expect(details.textContent).not.toMatch(/token=/);
  });

  // -------------------------------------------------------------------
  // Submission flow
  // -------------------------------------------------------------------

  it("submits anonymously when identity opt-in is unchecked", async () => {
    mockedApiFetch.mockResolvedValue({ id: 1 });
    render(<FeedbackWidget open onClose={() => {}} />);

    fireEvent.change(screen.getByTestId("feedback-message"), {
      target: { value: "Dashboard chart looks off" },
    });
    fireEvent.click(screen.getByTestId("feedback-submit"));

    await waitFor(() => expect(mockedApiFetch).toHaveBeenCalledTimes(1));

    const [path, options] = mockedApiFetch.mock.calls[0];
    expect(path).toBe("/api/v1/feedback");
    expect(options?.method).toBe("POST");
    const body = JSON.parse(options?.body as string);
    expect(body.message).toBe("Dashboard chart looks off");
    expect(body.category).toBe("bug");
    expect(body.include_identity).toBe(false);
    expect(body.context.url).toBe("http://localhost/dashboard");
  });

  it("submits with identity when the opt-in is checked", async () => {
    mockedApiFetch.mockResolvedValue({ id: 1 });
    render(<FeedbackWidget open onClose={() => {}} />);

    fireEvent.change(screen.getByTestId("feedback-message"), {
      target: { value: "Please add export" },
    });
    fireEvent.click(screen.getByTestId("feedback-include-identity"));
    fireEvent.click(screen.getByLabelText(/feature/i));
    fireEvent.click(screen.getByTestId("feedback-submit"));

    await waitFor(() => expect(mockedApiFetch).toHaveBeenCalledTimes(1));
    const body = JSON.parse(
      mockedApiFetch.mock.calls[0][1]?.body as string,
    );
    expect(body.include_identity).toBe(true);
    expect(body.category).toBe("feature");
  });

  it("strips query strings off the URL before it reaches the API", async () => {
    setLocation("http://localhost/import/123/reconcile?session=ABC");
    mockedApiFetch.mockResolvedValue({ id: 1 });
    render(<FeedbackWidget open onClose={() => {}} />);

    fireEvent.change(screen.getByTestId("feedback-message"), {
      target: { value: "Importer crashed" },
    });
    fireEvent.click(screen.getByTestId("feedback-submit"));

    await waitFor(() => expect(mockedApiFetch).toHaveBeenCalledTimes(1));
    const body = JSON.parse(
      mockedApiFetch.mock.calls[0][1]?.body as string,
    );
    expect(body.context.url).toBe(
      "http://localhost/import/123/reconcile",
    );
    expect(body.context.url).not.toMatch(/ABC/);
    expect(body.context.url).not.toMatch(/session/);
  });

  it("shows a success message after a successful submit", async () => {
    mockedApiFetch.mockResolvedValue({ id: 1 });
    render(<FeedbackWidget open onClose={() => {}} />);

    fireEvent.change(screen.getByTestId("feedback-message"), {
      target: { value: "x" },
    });
    fireEvent.click(screen.getByTestId("feedback-submit"));

    expect(
      await screen.findByTestId("feedback-success"),
    ).toBeInTheDocument();
  });

  it("disables submit when the message is empty", () => {
    render(<FeedbackWidget open onClose={() => {}} />);
    const submit = screen.getByTestId("feedback-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
  });

  it("shows an inline error when the API call fails", async () => {
    mockedApiFetch.mockRejectedValue(new Error("Network down"));
    render(<FeedbackWidget open onClose={() => {}} />);

    fireEvent.change(screen.getByTestId("feedback-message"), {
      target: { value: "x" },
    });
    fireEvent.click(screen.getByTestId("feedback-submit"));

    expect(await screen.findByTestId("feedback-error")).toBeInTheDocument();
  });

  // -------------------------------------------------------------------
  // Privacy default: re-opening the panel resets opt-in
  // -------------------------------------------------------------------

  it("resets identity opt-in to OFF every time the panel reopens", async () => {
    const { rerender } = render(
      <FeedbackWidget open onClose={() => {}} />,
    );
    fireEvent.click(screen.getByTestId("feedback-include-identity"));
    expect(
      (screen.getByTestId(
        "feedback-include-identity",
      ) as HTMLInputElement).checked,
    ).toBe(true);

    rerender(<FeedbackWidget open={false} onClose={() => {}} />);
    rerender(<FeedbackWidget open onClose={() => {}} />);

    expect(
      (screen.getByTestId(
        "feedback-include-identity",
      ) as HTMLInputElement).checked,
    ).toBe(false);
  });
});
