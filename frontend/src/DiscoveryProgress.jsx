import { useEffect, useRef, useState } from 'react';
import { fetchDiscoveryActive, fetchDiscoveryRequest } from './api.js';

export function DiscoveryProgress({ onBusyChange }) {
  const [snapshot, setSnapshot] = useState(null);
  const tracked = useRef(null);
  const pending = useRef(false);
  const sequence = useRef(0);

  useEffect(() => {
    let disposed = false;
    let timer;
    let polling = false;
    const track = (identifier) => {
      if (identifier !== tracked.current) {
        tracked.current = identifier;
        sequence.current = 0;
        setSnapshot(null);
      }
    };
    const poll = async () => {
      if (polling || disposed) return;
      polling = true;
      let active;
      try {
        const result = await fetchDiscoveryActive();
        if (disposed) return;
        active = result.active;
        onBusyChange(Boolean(active) || pending.current);
        if (active && !pending.current) track(active.request_id);
        if (tracked.current) {
          const identifier = tracked.current;
          const result = await fetchDiscoveryRequest(identifier, sequence.current);
          if (disposed || identifier !== tracked.current) return;
          sequence.current = result.last_seq || 0;
          setSnapshot((previous) => ({ ...result, logs: [
            ...(previous?.request_id === identifier ? previous.logs || [] : []), ...(result.logs || [])
          ].slice(-500) }));
        }
      } catch {
        // The POST may not have registered yet, or status may expire after a restart.
      } finally {
        polling = false;
        if (!disposed) timer = window.setTimeout(poll, active || pending.current ? 2000 : 5000);
      }
    };
    const onRequest = ({ detail }) => {
      if (detail.phase === 'start') {
        track(detail.request_id);
        pending.current = true;
        onBusyChange(true);
        setSnapshot({ request_id: detail.request_id, state: 'running', stage: 'starting', logs: [] });
      } else if (tracked.current === detail.request_id) {
        pending.current = false;
        onBusyChange(Boolean(detail.active_request_id));
        if (detail.active_request_id) track(detail.active_request_id);
        else setSnapshot((previous) => previous && ({ ...previous, state: detail.state }));
      }
      window.clearTimeout(timer);
      if (!polling) void poll();
    };
    window.addEventListener('discovery-request', onRequest);
    void poll();
    return () => {
      disposed = true;
      window.clearTimeout(timer);
      window.removeEventListener('discovery-request', onRequest);
    };
  }, [onBusyChange]);

  if (!snapshot) return null;
  return (
    <section className="discoveryProgress" aria-label="Discovery progress">
      <div className="discoveryProgressHeading" role="status" aria-live="polite">
        <strong>Discovery: {snapshot.state === 'running' ? snapshot.stage : snapshot.state}</strong>
        <span>{Math.floor(snapshot.elapsed_seconds || 0)}s elapsed</span>
      </div>
      <small>Request ID: <code>{snapshot.request_id}</code></small>
      {snapshot.state === 'running' && <p>One discovery request runs at a time. Other requests can start when this finishes.</p>}
      <div className="discoveryCounts">
        <span>Edges: {snapshot.completed_roots || 0}/{snapshot.total_roots || 0}</span>
        <span>Switches: {snapshot.completed_switches || 0}/{snapshot.discovered_switches || 0} discovered</span>
        <span>Upstream requests: {snapshot.upstream_calls || 0}</span>
        <span>Cache hits: {snapshot.cache_hits || 0}</span>
      </div>
      {snapshot.logs?.length > 0 && (
        <details>
          <summary>Progress logs ({snapshot.logs.length})</summary>
          <ol className="discoveryLogs">
            {snapshot.logs.map((event) => <li key={event.seq}>
              <time>{new Date(event.timestamp).toLocaleTimeString()}</time>{' '}{event.message}
            </li>)}
          </ol>
        </details>
      )}
    </section>
  );
}
