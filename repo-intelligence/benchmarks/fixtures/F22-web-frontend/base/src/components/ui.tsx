import { forwardRef, type ButtonHTMLAttributes, type InputHTMLAttributes } from "react";

export const TextField = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement> & { label: string }>(
  function TextField({ label, ...props }, ref) {
    return (
      <label className="field">
        <span>{label}</span>
        <input ref={ref} {...props} />
      </label>
    );
  },
);

export function Button(props: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className="btn" {...props} />;
}
