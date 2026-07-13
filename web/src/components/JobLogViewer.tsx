/** Visionneuse de log FFmpeg avec recherche (§7.3), live via WebSocket. */

import { Download, Search } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Input } from "@/components/ui/input";
import { useT } from "@/lib/i18n";
import { useJobLogSocket } from "@/ws/useJobLogSocket";

export function JobLogViewer({ jobId, live = true }: { jobId: number; live?: boolean }) {
  const t = useT();
  const { lines, percent, fps, speed } = useJobLogSocket(live ? jobId : null);
  const [staticLines, setStaticLines] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (live) return;
    let cancelled = false;
    void fetch(`/api/v1/jobs/${jobId}/log`)
      .then((r) => r.text())
      .then((text) => {
        if (!cancelled) setStaticLines(text.split("\n"));
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, live]);

  const allLines = live ? lines : staticLines;
  const visible = useMemo(
    () =>
      search
        ? allLines.filter((l) => l.toLowerCase().includes(search.toLowerCase()))
        : allLines,
    [allLines, search],
  );

  useEffect(() => {
    if (live) bottomRef.current?.scrollIntoView({ behavior: "instant" as ScrollBehavior });
  }, [visible.length, live]);

  return (
    <div className="flex h-full flex-col gap-2">
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <Search className="absolute left-2 top-2 h-4 w-4 text-muted-foreground" />
          <Input
            className="pl-7"
            placeholder={t("Rechercher dans le log…")}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        {live && percent !== null && (
          <span className="font-mono text-xs text-muted-foreground">
            {percent.toFixed(1)} % · {fps?.toFixed(0) ?? "—"} fps · {speed?.toFixed(2) ?? "—"}x
          </span>
        )}
        <a
          href={`/api/v1/jobs/${jobId}/log?download=true`}
          className="flex items-center gap-1 rounded-md border px-2 py-1.5 text-xs hover:bg-accent"
        >
          <Download className="h-3.5 w-3.5" /> {t("Télécharger")}
        </a>
      </div>
      <div className="log-viewer max-h-96 flex-1 overflow-auto rounded-md border bg-black/40 p-2 font-mono text-xs leading-relaxed">
        {visible.length === 0 ? (
          <span className="text-muted-foreground">{t("(log vide)")}</span>
        ) : (
          visible.map((line, i) => (
            <div key={i} className="whitespace-pre-wrap break-all">
              {line}
            </div>
          ))
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
