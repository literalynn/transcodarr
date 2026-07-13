/** Statistiques (§7.4) : courbes, donuts, progression, top 20, échecs. */

import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  useCodecStats,
  useFailures,
  useGainByBucket,
  useLibraryProgress,
  useSavingsOverTime,
  useThroughput,
  useTopRemaining,
} from "@/api/hooks";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { basename, formatBytes, formatPercent } from "@/lib/format";
import { useT } from "@/lib/i18n";

const COLORS = ["#3b82f6", "#22c55e", "#eab308", "#ef4444", "#a855f7", "#14b8a6", "#f97316"];

function ChartCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="h-64">{children}</CardContent>
    </Card>
  );
}

export function StatsPage() {
  const t = useT();
  const { data: savings } = useSavingsOverTime();
  const { data: codecs } = useCodecStats();
  const { data: gainByBucket } = useGainByBucket();
  const { data: throughput } = useThroughput();
  const { data: topRemaining } = useTopRemaining();
  const { data: failures } = useFailures();
  const { data: progress } = useLibraryProgress();

  const savingsData = (savings ?? []).map((p) => ({
    day: p.day.slice(0, 10),
    cumulative_gb: p.cumulative / 1024 ** 3,
  }));
  const codecData = Object.entries(codecs?.current ?? {}).map(([name, value]) => ({
    name,
    value,
  }));
  const gainData = (gainByBucket ?? []).map((g) => ({
    name: `${g.resolution ?? "?"} ${g.codec ?? ""}`,
    gain: g.avg_gain_percent ?? 0,
    count: g.count,
  }));
  const throughputData = (throughput?.per_day ?? []).map((d) => ({
    day: d.day.slice(0, 10),
    files: d.files,
    gpu_hours: d.gpu_hours,
  }));

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">{t("Statistiques")}</h1>

      <div className="grid gap-4 lg:grid-cols-2">
        <ChartCard title={t("Espace récupéré (cumulé, Gio)")}>
          <ResponsiveContainer>
            <AreaChart data={savingsData}>
              <CartesianGrid strokeDasharray="3 3" strokeOpacity={0.2} />
              <XAxis dataKey="day" fontSize={10} />
              <YAxis fontSize={10} />
              <Tooltip
                formatter={(v: number) => [`${v.toFixed(1)} Gio`, "cumulé"]}
                contentStyle={{ background: "hsl(224 18% 11%)", border: "1px solid #333" }}
              />
              <Area
                type="monotone"
                dataKey="cumulative_gb"
                stroke="#22c55e"
                fill="#22c55e"
                fillOpacity={0.25}
              />
            </AreaChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title={t("Répartition des codecs (bibliothèque actuelle)")}>
          <ResponsiveContainer>
            <PieChart>
              <Pie
                data={codecData}
                dataKey="value"
                nameKey="name"
                innerRadius="50%"
                outerRadius="80%"
                paddingAngle={2}
              >
                {codecData.map((_, i) => (
                  <Cell key={i} fill={COLORS[i % COLORS.length]} />
                ))}
              </Pie>
              <Legend />
              <Tooltip
                contentStyle={{ background: "hsl(224 18% 11%)", border: "1px solid #333" }}
              />
            </PieChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title={t("Gain moyen par bucket et codec cible (%)")}>
          <ResponsiveContainer>
            <BarChart data={gainData}>
              <CartesianGrid strokeDasharray="3 3" strokeOpacity={0.2} />
              <XAxis dataKey="name" fontSize={10} />
              <YAxis fontSize={10} />
              <Tooltip
                formatter={(v: number) => [`${v.toFixed(1)} %`, "gain moyen"]}
                contentStyle={{ background: "hsl(224 18% 11%)", border: "1px solid #333" }}
              />
              <Bar dataKey="gain" fill="#3b82f6" radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>

        <ChartCard title={t("Débit d'encodage (fichiers/jour)")}>
          <ResponsiveContainer>
            <BarChart data={throughputData}>
              <CartesianGrid strokeDasharray="3 3" strokeOpacity={0.2} />
              <XAxis dataKey="day" fontSize={10} />
              <YAxis fontSize={10} />
              <Tooltip
                contentStyle={{ background: "hsl(224 18% 11%)", border: "1px solid #333" }}
              />
              <Bar dataKey="files" name="fichiers" fill="#a855f7" radius={[3, 3, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </ChartCard>
      </div>

      {/* Progression par bibliothèque */}
      <Card>
        <CardHeader>
          <CardTitle>{t("Progression par bibliothèque (fichiers conformes)")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          {(progress ?? []).map((p) => (
            <div key={p.library_id}>
              <div className="mb-1 flex justify-between text-sm">
                <span>{p.name}</span>
                <span className="font-mono text-xs text-muted-foreground">
                  {p.conform}/{p.total} · {formatPercent(p.percent)}
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded bg-muted">
                <div className="h-full bg-success" style={{ width: `${p.percent}%` }} />
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        {/* Top 20 restants */}
        <Card>
          <CardHeader>
            <CardTitle>{t("Top fichiers les plus lourds non traités")}</CardTitle>
          </CardHeader>
          <CardContent className="max-h-80 overflow-auto">
            <table className="w-full text-sm">
              <tbody>
                {(topRemaining ?? []).map((f) => (
                  <tr key={f.id} className="border-b border-border/50">
                    <td className="max-w-xs truncate py-1 font-mono text-xs" title={f.path}>
                      {basename(f.path)}
                    </td>
                    <td className="py-1 font-mono text-xs">{f.video_codec ?? "—"}</td>
                    <td className="py-1 text-right font-mono text-xs">
                      {formatBytes(f.size_bytes)}
                    </td>
                    <td className="py-1 text-right font-mono text-xs text-success">
                      ~{formatBytes(f.estimated_gain_bytes)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>

        {/* Échecs */}
        <Card>
          <CardHeader>
            <CardTitle>
              {t("Échecs — taux :")} {formatPercent((failures?.failure_rate ?? 0) * 100)}
            </CardTitle>
          </CardHeader>
          <CardContent className="max-h-80 space-y-1.5 overflow-auto">
            {(failures?.top_errors ?? []).length === 0 && (
              <div className="text-sm text-muted-foreground">{t("Aucun échec. 🎉")}</div>
            )}
            {(failures?.top_errors ?? []).map((e, i) => (
              <div key={i} className="flex items-start justify-between gap-2 text-xs">
                <span className="font-mono text-destructive">{e.error}</span>
                <span className="shrink-0 font-mono text-muted-foreground">×{e.count}</span>
              </div>
            ))}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
