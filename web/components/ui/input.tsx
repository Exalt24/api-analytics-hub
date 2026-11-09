import * as React from "react";
import { cn } from "@/lib/utils";

export function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "border-border bg-background text-foreground h-9 w-full min-w-0 rounded-md border",
        "placeholder:text-muted-foreground px-3 py-1 text-sm shadow-xs outline-none",
        "focus-visible:ring-primary/50 focus-visible:ring-[3px] disabled:opacity-50",
        "disabled:cursor-not-allowed aria-invalid:border-destructive",
        "file:text-foreground file:mr-3 file:border-0 file:bg-transparent file:text-sm file:font-medium",
        className,
      )}
      {...props}
    />
  );
}
