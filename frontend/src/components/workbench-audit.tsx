"use client";

import { useMemo, useState } from "react";
import { formatDateTime, humanize } from "@/components/workbench-utils";
import type { AuditEvent } from "@/types/api";
import styles from "./workbench.module.css";

export function filterAuditEvents(events: AuditEvent[], query: string): AuditEvent[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return events;
  return events.filter((event) =>
    [event.action, event.object_type, event.object_id, event.actor_id, event.correlation_id]
      .join(" ")
      .toLowerCase()
      .includes(normalized),
  );
}

export function WorkbenchAudit({ events }: { events: AuditEvent[] }) {
  const [query, setQuery] = useState("");
  const filtered = useMemo(() => filterAuditEvents(events, query), [events, query]);
  const actors = new Set(events.map((event) => event.actor_id)).size;
  const actions = new Set(events.map((event) => event.action)).size;

  return (
    <section className={`${styles.auditPanel} panel`}>
      <div className={styles.auditHeader}>
        <div>
          <span className={styles.eyebrow}>Append-only history</span>
          <h2>Audit ledger</h2>
          <p>
            Inspect who changed what, request correlation, and recorded hash metadata. This browser
            view displays hashes but does not independently verify them.
          </p>
        </div>
        <div className={styles.auditStats}>
          <span>
            <strong>{events.length}</strong> events
          </span>
          <span>
            <strong>{actions}</strong> actions
          </span>
          <span>
            <strong>{actors}</strong> actors
          </span>
        </div>
      </div>
      <div className={styles.auditToolbar}>
        <label>
          <span className="sr-only">Search audit events</span>
          <input
            className="input"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search action, object, actor, or correlation ID"
          />
        </label>
        <span>{filtered.length} shown</span>
      </div>
      <div className={styles.auditList}>
        {filtered.map((event) => (
          <details key={event.id} className={styles.auditEvent}>
            <summary>
              <span className={styles.auditMarker} aria-hidden="true" />
              <span className={styles.auditTime}>{formatDateTime(event.created_at)}</span>
              <span className={styles.auditAction}>
                <strong>{humanize(event.action)}</strong>
                <small>
                  {humanize(event.object_type)} · {event.object_id.slice(0, 12)}…
                </small>
              </span>
              <span className={styles.chainState} data-linked={Boolean(event.previous_hash)}>
                {event.previous_hash ? "Previous hash recorded" : "No previous hash"}
              </span>
            </summary>
            <div className={styles.auditDetails}>
              <dl>
                <div>
                  <dt>Actor</dt>
                  <dd className="mono">{event.actor_id}</dd>
                </div>
                <div>
                  <dt>Correlation ID</dt>
                  <dd className="mono">{event.correlation_id}</dd>
                </div>
                <div>
                  <dt>Previous hash</dt>
                  <dd className="mono">{event.previous_hash ?? "Not recorded"}</dd>
                </div>
                <div>
                  <dt>Event hash</dt>
                  <dd className="mono">{event.event_hash}</dd>
                </div>
              </dl>
              <div className={styles.diffGrid}>
                <div>
                  <span className="field-label">Before</span>
                  <pre>{JSON.stringify(event.before, null, 2) ?? "null"}</pre>
                </div>
                <div>
                  <span className="field-label">After</span>
                  <pre>{JSON.stringify(event.after, null, 2) ?? "null"}</pre>
                </div>
              </div>
            </div>
          </details>
        ))}
        {!filtered.length ? (
          <div className={styles.noAuditEvents}>No audit events match this search.</div>
        ) : null}
      </div>
    </section>
  );
}
