import { formatTime } from "../api";
import type { EventRecord } from "../types";

export function EventLog({ events }: { events: EventRecord[] }) {
  if (events.length === 0) {
    return <div className="empty-state">暂无事件记录。</div>;
  }
  return (
    <div className="event-list">
      {events.map((event) => (
        <div key={event.id} className={`event-row ${event.level}`}>
          <span className="time">{formatTime(event.created_at)}</span>
          <span className={`code mono ${event.level}`}>
            {event.level === "error" ? "错误" : event.level === "warn" ? "警告" : "信息"}
          </span>
          <div>
            <div className="msg">{event.message}</div>
            <div className="hint mono">
              {event.code}
              {event.package_id ? ` · 交付包 #${event.package_id}` : ""}
              {event.batch_id ? ` · 批次 #${event.batch_id}` : ""}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
