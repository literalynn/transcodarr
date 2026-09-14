/** Badges de diagnostic : cause d'échec, stratégie, tentative. */

import { AlertTriangle, RotateCcw, Sparkles } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { failureMeta, strategyMeta } from "@/lib/failures";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export function FailureBadge({
  category,
  className,
}: {
  category: string | null | undefined;
  className?: string;
}) {
  const t = useT();
  if (!category) return null;
  const meta = failureMeta(category);
  return (
    <Badge
      variant="destructive"
      className={cn("gap-1 whitespace-nowrap", className)}
      title={`${t(meta.explanation)}\n→ ${t(meta.remedy)}`}
    >
      <AlertTriangle className="h-3 w-3" />
      {t(meta.label)}
    </Badge>
  );
}

export function StrategyBadge({
  strategy,
  hideStandard = true,
}: {
  strategy: string | null | undefined;
  hideStandard?: boolean;
}) {
  const t = useT();
  if (!strategy || (hideStandard && strategy === "standard")) return null;
  const meta = strategyMeta(strategy);
  return (
    <Badge variant="info" className="gap-1 whitespace-nowrap" title={t(meta.description)}>
      <Sparkles className="h-3 w-3" />
      {t(meta.label)}
    </Badge>
  );
}

export function AttemptBadge({ attempt }: { attempt: number | null | undefined }) {
  const t = useT();
  if (!attempt || attempt <= 1) return null;
  return (
    <Badge variant="outline" className="gap-1 whitespace-nowrap" title={t("Numéro de tentative")}>
      <RotateCcw className="h-3 w-3" />
      {t("essai")} {attempt}
    </Badge>
  );
}
