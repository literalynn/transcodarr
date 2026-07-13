import { cva, type VariantProps } from "class-variance-authority";
import type { HTMLAttributes } from "react";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium",
  {
    variants: {
      variant: {
        default: "bg-secondary text-secondary-foreground",
        success: "bg-success/15 text-success",
        destructive: "bg-destructive/15 text-destructive",
        warning: "bg-warning/15 text-warning",
        info: "bg-primary/15 text-primary",
        outline: "border border-input text-foreground",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

/** Badge d'état sémantique pour fichiers et jobs (vert/rouge/ambre, §7). */
export function StatusBadge({ status }: { status: string }) {
  const t = useT();
  const variant =
    status === "done" || status === "success"
      ? "success"
      : status === "failed" || status === "cancelled"
        ? "destructive"
        : status === "skipped_no_gain" || status === "skipped_policy" || status === "ignored"
          ? "warning"
          : status === "processing" || status === "running" || status === "validating" ||
              status === "replacing"
            ? "info"
            : "default";
  const labels: Record<string, string> = {
    pending: "en attente de scan",
    queued: "en file",
    processing: "en cours",
    done: "traité",
    failed: "échec",
    skipped_no_gain: "gain insuffisant",
    skipped_policy: "non traité (politique)",
    ignored: "ignoré",
    running: "encodage",
    validating: "validation",
    replacing: "remplacement",
    success: "succès",
    cancelled: "annulé",
  };
  return <Badge variant={variant}>{t(labels[status] ?? status)}</Badge>;
}
