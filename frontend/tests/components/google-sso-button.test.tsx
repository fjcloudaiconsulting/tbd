import { fireEvent, render, screen } from "@testing-library/react";

import GoogleSSOButton from "@/components/auth/GoogleSSOButton";

const ENV_KEY = "NEXT_PUBLIC_GOOGLE_SSO_ENABLED";

function withFlag(value: string | undefined, fn: () => void) {
  const original = process.env[ENV_KEY];
  if (value === undefined) {
    delete process.env[ENV_KEY];
  } else {
    process.env[ENV_KEY] = value;
  }
  try {
    fn();
  } finally {
    if (original === undefined) {
      delete process.env[ENV_KEY];
    } else {
      process.env[ENV_KEY] = original;
    }
  }
}

describe("GoogleSSOButton", () => {
  it("renders Google's official multi-color G mark when SSO is enabled", () => {
    withFlag("true", () => {
      render(<GoogleSSOButton onClick={() => {}} />);
      const button = screen.getByRole("button", { name: /Sign in with Google/i });
      const svg = button.querySelector("svg");
      expect(svg).not.toBeNull();
      const fills = Array.from(svg!.querySelectorAll("path")).map((p) =>
        p.getAttribute("fill"),
      );
      // Google's four official brand colors must all appear.
      expect(fills).toEqual(
        expect.arrayContaining(["#4285F4", "#34A853", "#FBBC05", "#EA4335"]),
      );
    });
  });

  it("renders the signin wordmark by default", () => {
    withFlag("true", () => {
      render(<GoogleSSOButton onClick={() => {}} />);
      expect(
        screen.getByRole("button", { name: "Sign in with Google" }),
      ).toBeTruthy();
    });
  });

  it("renders the signup wordmark when mode='signup'", () => {
    withFlag("true", () => {
      render(<GoogleSSOButton mode="signup" onClick={() => {}} />);
      expect(
        screen.getByRole("button", { name: "Sign up with Google" }),
      ).toBeTruthy();
    });
  });

  it("invokes onClick when clicked", () => {
    withFlag("true", () => {
      const onClick = vi.fn();
      render(<GoogleSSOButton onClick={onClick} />);
      fireEvent.click(screen.getByRole("button", { name: /Sign in with Google/i }));
      expect(onClick).toHaveBeenCalledTimes(1);
    });
  });

  it("renders nothing when NEXT_PUBLIC_GOOGLE_SSO_ENABLED is not 'true'", () => {
    withFlag(undefined, () => {
      const { container } = render(<GoogleSSOButton onClick={() => {}} />);
      expect(container.firstChild).toBeNull();
    });
  });

  it("renders nothing when the env flag is set to 'false'", () => {
    withFlag("false", () => {
      const { container } = render(<GoogleSSOButton onClick={() => {}} />);
      expect(container.firstChild).toBeNull();
    });
  });

  it("when showWhenDisabled is true and disabled, marks the button aria-disabled and exposes the reason via aria-describedby", () => {
    withFlag(undefined, () => {
      render(
        <GoogleSSOButton
          onClick={() => {}}
          showWhenDisabled
          disabledReason="Google sign-in is not configured"
        />,
      );
      const button = screen.getByRole("button", { name: /Sign in with Google/i });
      expect(button.getAttribute("aria-disabled")).toBe("true");
      const describedBy = button.getAttribute("aria-describedby");
      expect(describedBy).not.toBeNull();
      const helper = document.getElementById(describedBy!);
      expect(helper?.textContent).toBe("Google sign-in is not configured");
    });
  });

  it("carries the locked `gsi-button` surface class so theme overrides hit it", () => {
    withFlag("true", () => {
      render(<GoogleSSOButton onClick={() => {}} />);
      const button = screen.getByRole("button", { name: /Sign in with Google/i });
      // The light/dark surface colors are driven by globals.css selectors
      // scoped to `.gsi-button` and `[data-theme="light"] .gsi-button` so
      // we can't recolor the brand surface and it tracks the host theme
      // toggle without depending on Tailwind dark-mode plumbing.
      expect(button.className).toMatch(/\bgsi-button\b/);
    });
  });

  it("when loading, disables the button and sets aria-busy", () => {
    withFlag("true", () => {
      render(<GoogleSSOButton onClick={() => {}} loading />);
      const button = screen.getByRole("button", { name: /Sign in with Google/i });
      expect(button.getAttribute("aria-busy")).toBe("true");
      expect((button as HTMLButtonElement).disabled).toBe(true);
    });
  });
});
