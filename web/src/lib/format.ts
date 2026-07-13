/** Formatage des valeurs techniques (mono dans l'UI, §7). */

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  const abs = Math.abs(bytes);
  if (abs >= 1024 ** 4) return `${(bytes / 1024 ** 4).toFixed(2)} Tio`;
  if (abs >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} Gio`;
  if (abs >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} Mio`;
  if (abs >= 1024) return `${(bytes / 1024).toFixed(0)} Kio`;
  return `${bytes} o`;
}

export function formatBitrate(kbps: number | null | undefined): string {
  if (kbps === null || kbps === undefined) return "—";
  if (kbps >= 1000) return `${(kbps / 1000).toFixed(1)} Mbps`;
  return `${kbps} kbps`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h${String(m).padStart(2, "0")}`;
  if (m > 0) return `${m}m${String(sec).padStart(2, "0")}`;
  return `${sec}s`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("fr-FR", {
    day: "2-digit",
    month: "2-digit",
    year: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(digits)} %`;
}

export function basename(path: string): string {
  return path.split("/").pop() ?? path;
}

export function hdrLabel(fmt: string | null | undefined): string {
  switch (fmt) {
    case "hdr10":
      return "HDR10";
    case "hdr10plus":
      return "HDR10+";
    case "dolby_vision":
      return "Dolby Vision";
    case "hlg":
      return "HLG";
    case "sdr":
      return "SDR";
    default:
      return "—";
  }
}
