"use client";

import { Eye, EyeOff } from "lucide-react";
import { forwardRef, useState } from "react";
import type { InputHTMLAttributes } from "react";

import { input as inputCls } from "@/lib/styles";

type Props = Omit<InputHTMLAttributes<HTMLInputElement>, "type">;

/**
 * Password input with a built-in show/hide toggle button.
 *
 * Accepts the same props as a normal text <input>. Renders a controlled
 * input typed as "password" by default; the eye button toggles to "text".
 *
 * Notes:
 * - The input remains controlled by the parent. The component holds only
 *   the visibility flag, never the password value.
 * - The eye button is type="button" so it never submits the surrounding form.
 * - The input's value is sent to the server on submit identically regardless
 *   of visible/hidden state.
 * - autoComplete is forwarded as-is so "new-password" and "current-password"
 *   semantics are preserved.
 */
const PasswordInput = forwardRef<HTMLInputElement, Props>(function PasswordInput(
  { className, ...rest },
  ref,
) {
  const [visible, setVisible] = useState(false);
  const Icon = visible ? EyeOff : Eye;
  const label = visible ? "Hide password" : "Show password";

  return (
    <div className="relative">
      <input
        {...rest}
        ref={ref}
        type={visible ? "text" : "password"}
        className={`${className ?? inputCls} pr-10`}
      />
      <button
        type="button"
        onClick={() => setVisible((v) => !v)}
        aria-label={label}
        aria-pressed={visible}
        title={label}
        tabIndex={0}
        className="absolute inset-y-0 right-0 flex items-center px-3 text-text-muted hover:text-text-primary focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 rounded-md"
      >
        <Icon aria-hidden="true" className="h-4 w-4" />
      </button>
    </div>
  );
});

export default PasswordInput;
