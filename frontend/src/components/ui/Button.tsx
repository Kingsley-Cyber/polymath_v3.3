// Button — the shared action-button affordance.
//
// The app's default surface intentionally uses `transition-none` (flat /
// terminal aesthetic), which left action buttons feeling dead — no hover, no
// press feedback, weak signal that they DO something. This component is the
// deliberate exception for things-that-act (Save, Add, Validate, Delete):
// clear color by intent, a hover state, a tactile press (active:scale), a
// focus ring for keyboard/a11y, and proper disabled styling.
//
// Variants:
//   primary    green filled — affirmative actions (Save, Add, Create)
//   danger     red filled — destructive (Delete / confirm-delete)
//   secondary  outlined — neutral actions (Validate, Cancel, secondary)
//   ghost      transparent — icon buttons (edit, remove-row)
import { ButtonHTMLAttributes, forwardRef } from "react";

type Variant = "primary" | "danger" | "secondary" | "ghost";
type Size = "sm" | "md" | "icon";

const BASE =
  "inline-flex items-center justify-center gap-1.5 font-medium rounded select-none " +
  "cursor-pointer transition-[transform,background-color,border-color,color,box-shadow,opacity] " +
  "duration-150 ease-out active:scale-[0.96] " +
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 " +
  "focus-visible:ring-offset-transparent " +
  "disabled:opacity-45 disabled:pointer-events-none disabled:active:scale-100 disabled:shadow-none";

const SIZES: Record<Size, string> = {
  sm: "text-[11px] px-2.5 py-1 tracking-wide",
  md: "text-[13px] px-3.5 py-1.5",
  icon: "p-1.5",
};

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-emerald-600 text-white shadow-sm shadow-emerald-950/50 " +
    "hover:bg-emerald-500 hover:shadow-md hover:shadow-emerald-800/40 hover:-translate-y-px " +
    "focus-visible:ring-emerald-400/70",
  danger:
    "bg-red-600 text-white shadow-sm shadow-red-950/50 " +
    "hover:bg-red-500 hover:shadow-md hover:shadow-red-800/40 hover:-translate-y-px " +
    "focus-visible:ring-red-400/70",
  secondary:
    "border border-white/15 text-gray-300 bg-white/[0.02] " +
    "hover:border-emerald-400/50 hover:text-white hover:bg-emerald-400/10 " +
    "focus-visible:ring-emerald-400/40",
  ghost:
    "text-gray-500 hover:text-white hover:bg-white/10 focus-visible:ring-white/25",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
}

/** Action button with hover/press/focus affordance. Defaults to a neutral
 *  outlined `secondary` at `md` size. Pass `className` to extend (it wins
 *  over the variant via source order). */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ variant = "secondary", size = "md", className = "", type, ...props }, ref) => (
    <button
      ref={ref}
      type={type ?? "button"}
      className={`${BASE} ${SIZES[size]} ${VARIANTS[variant]} ${className}`}
      {...props}
    />
  ),
);
Button.displayName = "Button";

export default Button;
