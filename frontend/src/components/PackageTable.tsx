import { formatBytes, formatTime } from "../api";
import { failureLabel } from "../labels";
import { StatusPill } from "./StatusPill";
import type { PackageSummary } from "../types";

interface Props {
  packages: PackageSummary[];
  selectedId: number | null;
  onSelect: (pkg: PackageSummary) => void;
}

export function PackageTable({ packages, selectedId, onSelect }: Props) {
  if (packages.length === 0) {
    return (
      <div className="empty-state">
        <h3>还没有发现交付目录</h3>
        扫描到子目录后，这里会显示每个交付包的接收状态。
      </div>
    );
  }

  return (
    <div className="table-wrap">
      <table className="grid">
        <thead>
          <tr>
            <th>交付目录</th>
            <th>交付编号</th>
            <th>状态</th>
            <th>校验</th>
            <th>压缩包</th>
            <th>最近更新</th>
          </tr>
        </thead>
        <tbody>
          {packages.map((pkg) => (
            <tr
              key={pkg.id}
              className={pkg.id === selectedId ? "selected" : ""}
              onClick={() => onSelect(pkg)}
              style={{ cursor: "pointer" }}
            >
              <td>
                <div className="path-cell">{pkg.relative_path}</div>
                {pkg.failure_code && (
                  <div className="hint" style={{ color: "var(--error)", marginTop: 3 }}>
                    {failureLabel(pkg.failure_code)}
                    {pkg.reason && (
                      <span className="faint"> · {pkg.reason.slice(0, 80)}</span>
                    )}
                  </div>
                )}
              </td>
              <td className="mono nowrap">{pkg.package_code ?? "-"}</td>
              <td>
                <StatusPill state={pkg.state} label={pkg.state_label} />
                {pkg.state === "waiting_stable" && (
                  <div className="hint" style={{ marginTop: 3 }}>
                    大小连续两次不变后接收
                  </div>
                )}
              </td>
              <td className="nowrap">
                {pkg.checks.total > 0 ? (
                  <>
                    <span style={{ color: pkg.checks.failed ? "var(--error)" : "var(--ok)" }}>
                      {pkg.checks.ok}/{pkg.checks.total}
                    </span>
                    <div className="hint">文件校验通过</div>
                  </>
                ) : (
                  <span className="faint">-</span>
                )}
              </td>
              <td className="mono nowrap">
                {pkg.zip_bytes ? formatBytes(pkg.zip_bytes) : "-"}
                {pkg.member_count > 0 && (
                  <div className="hint">{pkg.member_count} 个文件</div>
                )}
              </td>
              <td className="nowrap muted">{formatTime(pkg.updated_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
