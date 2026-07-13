/** Abonnement /ws/jobs/{id}/log : log FFmpeg ligne à ligne + progression. */

import { useEffect, useState } from "react";

export interface JobLogState {
  lines: string[];
  percent: number | null;
  fps: number | null;
  speed: number | null;
  etaS: number | null;
}

const MAX_LINES = 2000;

export function useJobLogSocket(jobId: number | null): JobLogState {
  const [state, setState] = useState<JobLogState>({
    lines: [],
    percent: null,
    fps: null,
    speed: null,
    etaS: null,
  });

  useEffect(() => {
    if (jobId === null) return;
    setState({ lines: [], percent: null, fps: null, speed: null, etaS: null });
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/jobs/${jobId}/log`);
    ws.onmessage = (msg: MessageEvent<string>) => {
      const event = JSON.parse(msg.data) as {
        type: string;
        lines?: string[];
        line?: string;
        percent?: number | null;
        fps?: number | null;
        speed?: number | null;
        eta_s?: number | null;
      };
      setState((prev) => {
        if (event.type === "backlog") {
          return { ...prev, lines: (event.lines ?? []).slice(-MAX_LINES) };
        }
        if (event.type === "log" && event.line !== undefined) {
          return { ...prev, lines: [...prev.lines, event.line].slice(-MAX_LINES) };
        }
        if (event.type === "progress") {
          return {
            ...prev,
            percent: event.percent ?? prev.percent,
            fps: event.fps ?? prev.fps,
            speed: event.speed ?? prev.speed,
            etaS: event.eta_s ?? prev.etaS,
          };
        }
        return prev;
      });
    };
    return () => ws.close();
  }, [jobId]);

  return state;
}
