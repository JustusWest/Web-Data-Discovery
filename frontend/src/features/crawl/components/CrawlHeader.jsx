import { Activity, Cpu, Globe, Search } from 'lucide-react';
import React from 'react';
import { CONNECTION_STATES } from '../clients/crawlerClientContract';

const CONNECTION_LABELS = {
  [CONNECTION_STATES.CONNECTING]: 'Connecting',
  [CONNECTION_STATES.CONNECTED]: 'Connected',
  [CONNECTION_STATES.RECONNECTING]: 'Reconnecting',
  [CONNECTION_STATES.DISCONNECTED]: 'Disconnected',
  [CONNECTION_STATES.ERROR]: 'Connection error',
};

function getConnectionClass(connectionState) {
  if (connectionState === CONNECTION_STATES.CONNECTED) {
    return 'connected';
  }

  if (connectionState === CONNECTION_STATES.ERROR) {
    return 'error';
  }

  if (connectionState === CONNECTION_STATES.CONNECTING || connectionState === CONNECTION_STATES.RECONNECTING) {
    return 'connecting';
  }

  return 'disconnected';
}

export function CrawlHeader({ stats, connectionState }) {
  const connectionLabel = CONNECTION_LABELS[connectionState] || CONNECTION_LABELS[CONNECTION_STATES.DISCONNECTED];
  const hasErrorMetrics =
    Number.isFinite(stats.urlErrors) &&
    Number.isFinite(stats.errorRate) &&
    Number.isFinite(stats.urlsAttempted);

  return (
    <header className="app-header">
      <div className="brand">
        <span className="brand-icon" aria-hidden="true">
          <Activity size={18} />
        </span>
        <div>
          <h1>LLM Focused Crawl</h1>
          <p>Topic-guided page discovery with human feedback</p>
        </div>
      </div>

      <div className="header-right">
        <div className="stats-row" aria-label="crawl stats">
          <span>
            <Globe size={14} /> {stats.pagesScanned} scanned
          </span>
          <span>
            <Search size={14} /> {stats.relevantFound} candidates
          </span>
          <span>
            <Cpu size={14} /> {stats.tokensUsed.toLocaleString()} tokens
          </span>
          {hasErrorMetrics && (
            <span>
              <Activity size={14} /> {stats.urlErrors} errors ({(stats.errorRate * 100).toFixed(1)}%)
            </span>
          )}
        </div>

        <span className={`connection-state ${getConnectionClass(connectionState)}`}>{connectionLabel}</span>
      </div>
    </header>
  );
}
