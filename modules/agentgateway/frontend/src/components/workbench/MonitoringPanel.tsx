"use client";
import { useState, useEffect } from "react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import { AlertTriangle, ExternalLink, BarChart3, Clock, Activity, Zap } from "lucide-react";

interface MonitoringData {
  agent_id: number;
  request_count_24h: number;
  request_count_7d: number;
  request_count_30d: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  token_consumption: number;
  error_rate: number;
  status: string;
  langfuse_enabled?: boolean;
  langfuse_auth_ok?: boolean;
  langfuse_base_url?: string;
  credential_status?: Array<{ name: string; status: string }>;
}

interface TraceItem {
  trace_id: string;
  trace_url: string;
  duration_ms: number;
  status: string;
  node_count: number;
  created_at: string;
}

interface Props {
  agentId: number;
}

export default function MonitoringPanel({ agentId }: Props) {
  const [monitoring, setMonitoring] = useState<MonitoringData | null>(null);
  const [traces, setTraces] = useState<TraceItem[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      api.getMonitoring(agentId).catch(() => null),
      api.getMonitoringTraces(agentId).catch(() => ({ agent_id: agentId, traces: [] })),
    ]).then(([m, t]) => {
      setMonitoring(m);
      setTraces((t as any).traces || []);
      setLoading(false);
    });
  }, [agentId]);

  if (loading) {
    return <div className="p-4 text-sm text-muted-foreground text-center">加载监控数据中...</div>;
  }

  if (!monitoring) {
    return <div className="p-4 text-sm text-muted-foreground text-center">无法加载监控数据</div>;
  }

  return (
    <div className="p-4 space-y-4">
      {/* Degradation alert banner */}
      {monitoring.status === "degraded" && (
        <div className="flex items-center gap-2 p-3 rounded-md bg-destructive/10 border border-destructive/20 text-sm">
          <AlertTriangle className="w-4 h-4 text-destructive shrink-0" />
          <span className="text-destructive font-medium">智能体质量已降级</span>
          <span className="text-muted-foreground text-xs">
            近 100 次请求平均质量低于阈值，请检查提示词和模型配置
          </span>
        </div>
      )}

      {monitoring.status === "at_risk" && (
        <div className="flex items-center gap-2 p-3 rounded-md bg-amber-50 border border-amber-200 text-sm">
          <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0" />
          <span className="text-amber-800 font-medium">质量风险警告</span>
          <span className="text-amber-700 text-xs">
            近 100 次请求平均质量接近阈值，持续关注
          </span>
        </div>
      )}

      {/* Metrics cards */}
      <div className="grid grid-cols-4 gap-3">
        <Card>
          <CardHeader className="py-2">
            <CardTitle className="text-xs text-muted-foreground flex items-center gap-1">
              <Activity className="w-3 h-3" />24h 请求
            </CardTitle>
          </CardHeader>
          <CardContent className="pb-3">
            <span className="text-lg font-bold">{monitoring.request_count_24h}</span>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="py-2">
            <CardTitle className="text-xs text-muted-foreground flex items-center gap-1">
              <Clock className="w-3 h-3" />P50 延迟
            </CardTitle>
          </CardHeader>
          <CardContent className="pb-3">
            <span className="text-lg font-bold">{monitoring.p50_latency_ms.toFixed(0)}ms</span>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="py-2">
            <CardTitle className="text-xs text-muted-foreground flex items-center gap-1">
              <Zap className="w-3 h-3" />Token 消耗
            </CardTitle>
          </CardHeader>
          <CardContent className="pb-3">
            <span className="text-lg font-bold">{monitoring.token_consumption.toLocaleString()}</span>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="py-2">
            <CardTitle className="text-xs text-muted-foreground flex items-center gap-1">
              <BarChart3 className="w-3 h-3" />错误率
            </CardTitle>
          </CardHeader>
          <CardContent className="pb-3">
            <span className="text-lg font-bold">{(monitoring.error_rate * 100).toFixed(1)}%</span>
          </CardContent>
        </Card>
      </div>

      {/* Second row of metrics */}
      <div className="grid grid-cols-3 gap-3">
        <Card>
          <CardHeader className="py-2">
            <CardTitle className="text-xs text-muted-foreground">7d 请求</CardTitle>
          </CardHeader>
          <CardContent className="pb-3">
            <span className="text-lg font-bold">{monitoring.request_count_7d.toLocaleString()}</span>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="py-2">
            <CardTitle className="text-xs text-muted-foreground">30d 请求</CardTitle>
          </CardHeader>
          <CardContent className="pb-3">
            <span className="text-lg font-bold">{monitoring.request_count_30d.toLocaleString()}</span>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="py-2">
            <CardTitle className="text-xs text-muted-foreground">P95 延迟</CardTitle>
          </CardHeader>
          <CardContent className="pb-3">
            <span className="text-lg font-bold">{monitoring.p95_latency_ms.toFixed(0)}ms</span>
          </CardContent>
        </Card>
      </div>

      {/* Status badge */}
      <div className="flex items-center gap-2">
        <span className="text-sm text-muted-foreground">状态:</span>
        <Badge variant={monitoring.status === "healthy" ? "default" : "destructive"}>
          {monitoring.status === "healthy" ? "健康" : monitoring.status === "degraded" ? "已降级" : monitoring.status}
        </Badge>
      </div>

      {/* Langfuse health: enabled / auth_ok / base_url (no key) */}
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="text-muted-foreground">Langfuse:</span>
        {!monitoring.langfuse_enabled ? (
          <Badge variant="secondary" className="text-xs">未配置</Badge>
        ) : monitoring.langfuse_auth_ok ? (
          <Badge variant="default" className="text-xs">已连通 score 写回生效</Badge>
        ) : (
          <Badge variant="destructive" className="text-xs">已配置但鉴权失败 score 写回不生效</Badge>
        )}
        {monitoring.langfuse_base_url ? (
          <span className="text-xs font-mono text-muted-foreground truncate max-w-[260px]">
            {monitoring.langfuse_base_url}
          </span>
        ) : null}
      </div>

      {/* Credential health — names a placeholder/missing key so a 401/404 is
          attributable instead of mysterious. Never shows key values. */}
      {monitoring.credential_status && monitoring.credential_status.some((c) => c.status !== "ok") && (
        <div className="mb-4">
          <h4 className="text-sm font-semibold mb-2 flex items-center gap-2">
            <AlertTriangle className="w-4 h-4 text-amber-500" />
            凭据健康
          </h4>
          <div className="flex flex-wrap gap-1.5">
            {monitoring.credential_status
              .filter((c) => c.status !== "ok")
              .map((c) => (
                <Badge key={c.name} variant="destructive" className="text-xs">
                  {c.name}: {c.status === "placeholder" ? "占位/未配置" : "缺失"}
                </Badge>
              ))}
          </div>
          <p className="text-xs text-muted-foreground mt-1.5">
            这些 provider 的 key 未配置或仍是占位，相关模型会 401、langfuse trace 会 404。请在 backend/.env 填入有效 key。
          </p>
        </div>
      )}

      {/* Recent traces */}
      <div>
        <h4 className="text-sm font-semibold mb-2">最近 Trace 记录</h4>
        {traces.length === 0 ? (
          <p className="text-xs text-muted-foreground text-center py-4">暂无 trace 数据</p>
        ) : (
          <ScrollArea className="max-h-[200px]">
            <div className="space-y-1">
              {traces.map((t) => (
                <a
                  key={t.trace_id}
                  href={t.trace_url}
                  target="_blank"
                  rel="noreferrer"
                  className="flex items-center justify-between p-2 rounded hover:bg-accent text-sm"
                >
                  <div className="flex items-center gap-2">
                    <Badge variant={t.status === "completed" ? "default" : "secondary"} className="text-xs">
                      {t.status}
                    </Badge>
                    <span className="text-xs font-mono text-muted-foreground truncate max-w-[200px]">
                      {t.trace_id}
                    </span>
                  </div>
                  <div className="flex items-center gap-3 text-xs text-muted-foreground">
                    <span>{t.duration_ms}ms</span>
                    <span>{t.node_count} 节点</span>
                    <ExternalLink className="w-3 h-3" />
                  </div>
                </a>
              ))}
            </div>
          </ScrollArea>
        )}
      </div>
    </div>
  );
}
