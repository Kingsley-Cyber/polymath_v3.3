/**
 * Button — premium research-grade primitive.
 *
 * Five variants × three sizes × five states (default / hover / active /
 * focus-visible / disabled). Wraps the CSS classes defined in
 * `index.css` so colors, spacing, type, and motion come from the
 * token system — not from ad-hoc Tailwind utilities.
 *
 * Use this everywhere a button appears in the graph workspace. The
 * legacy `btn-primary` Tailwind class still works for non-graph chrome
 * (chat, settings) but new graph-builder code should use <Button />.
 */

import { forwardRef, type ButtonHTMLAttributes } from "react";
import type { ButtonSize, ButtonVariant } from "../../lib/design-tokens";

export interface ButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "type"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  type?: "button" | "submit" | "reset";
  /** Visually active toggle state (e.g. tab is selected). */
  active?: boolean;
  /** Icon-only button — square hit area, content stays compact. */
  iconOnly?: boolean;
}

const variantClass: Record<ButtonVariant, string> = {
  primary: "gbtn--primary",
  secondary: "gbtn--secondary",
  tertiary: "gbtn--tertiary",
  ghost: "gbtn--ghost",
  danger: "gbtn--danger",
};

const sizeClass: Record<ButtonSize, string> = {
  sm: "gbtn--sm",
  md: "gbtn--md",
  lg: "gbtn--lg",
  icon: "gbtn--md",
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    {
      variant = "secondary",
      size = "md",
      type = "button",
      active = false,
      iconOnly = false,
      className = "",
      children,
      ...rest
    },
    ref,
  ) {
    const cls = [
      "gbtn",
      variantClass[variant],
      sizeClass[size],
      iconOnly ? "gbtn--icon" : "",
      active ? "is-active" : "",
      className,
    ]
      .filter(Boolean)
      .join(" ");

    const style: React.CSSProperties | undefined = active
      ? {
          background: "var(--accent-soft)",
          color: "var(--accent-main)",
          borderColor: "var(--accent-main)",
        }
      : undefined;

    return (
      <button
        ref={ref}
        type={type}
        className={cls}
        style={style}
        {...rest}
      >
        {children}
      </button>
    );
  },
);

export interface IconButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "type"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  type?: "button" | "submit" | "reset";
  active?: boolean;
  label: string;
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(
  function IconButton(
    {
      variant = "ghost",
      size = "md",
      type = "button",
      active = false,
      label,
      className = "",
      children,
    },
    ref,
  ) {
    return (
      <Button
        ref={ref}
        type={type}
        variant={variant}
        size={size}
        active={active}
        iconOnly
        title={label}
        aria-label={label}
        className={`gbtn--icon ${className}`}
      >
        {children}
      </Button>
    );
  },
);