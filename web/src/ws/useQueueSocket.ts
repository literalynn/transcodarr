/** Abonnement WebSocket /ws/queue : jobs actifs + progression temps réel. */

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type { ActiveJob } from "@/api/client";

export interface QueueEvent {
  type: string;
  [key: string]: unknown;
}

export function useQueueSocket(): { active: ActiveJob[]; connected: boolean } {
  const [active, setActive] = useState<ActiveJob[]>([]);
  const [connected, setConnected] = useState(false);
  const qc = useQueryClient();
  const activeRef = useRef<Map<number, ActiveJob>>(new Map());

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: number | undefined;

    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${window.location.host}/ws/queue`);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!closed) retry = window.setTimeout(connect, 3000);
      };
      ws.onmessage = (msg: MessageEvent<string>) => {
        const event = JSON.parse(msg.data) as QueueEvent;
        const map = activeRef.current;
        switch (event.type) {
          case "snapshot": {
            map.clear();
            for (const job of (event.active as ActiveJob[] | undefined) ?? []) {
              map.set(job.job_id, job);
            }
            break;
          }
          case "job_started": {
            const job = event.job as ActiveJob;
            map.set(job.job_id, job);
            break;
          }
          case "progress": {
            const id = event.job_id as number;
            const existing = map.get(id);
            if (existing) {
              map.set(id, {
                ...existing,
                percent: (event.percent as number | null) ?? existing.percent,
                fps: (event.fps as number | null) ?? existing.fps,
                speed: (event.speed as number | null) ?? existing.speed,
                eta_s: (event.eta_s as number | null) ?? existing.eta_s,
              });
            }
            break;
          }
          case "phase": {
            const id = event.job_id as number;
            const existing = map.get(id);
            if (existing) {
              map.set(id, { ...existing, phase: (event.phase as string) ?? null });
            }
            break;
          }
          case "job_finished":
          case "job_cancelled": {
            map.delete(event.job_id as number);
            void qc.invalidateQueries({ queryKey: ["jobs"] });
            void qc.invalidateQueries({ queryKey: ["queue"] });
            void qc.invalidateQueries({ queryKey: ["stats"] });
            void qc.invalidateQueries({ queryKey: ["files"] });
            break;
          }
          case "job_success":
          case "job_failed":
          case "job_no_gain":
          case "scan_finished": {
            void qc.invalidateQueries({ queryKey: ["files"] });
            void qc.invalidateQueries({ queryKey: ["queue"] });
            break;
          }
          default:
            break;
        }
        setActive([...map.values()]);
      };
    };
    connect();
    return () => {
      closed = true;
      if (retry !== undefined) window.clearTimeout(retry);
      ws?.close();
    };
  }, [qc]);

  return { active, connected };
}
