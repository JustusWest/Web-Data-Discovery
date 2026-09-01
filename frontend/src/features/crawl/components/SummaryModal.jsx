import { X } from 'lucide-react';
import React, { useEffect } from 'react';

function formatPercent(value) {
  if (!Number.isFinite(value)) return '0.0%';
  return `${(value * 100).toFixed(1)}%`;
}

export function SummaryModal({
  isOpen,
  isLoading,
  errorMessage,
  summaryData,
  onClose,
  onExportResults,
  canExport,
}) {
  useEffect(() => {
    if (!isOpen) return undefined;

    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        onClose();
      }
    };

    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  const stats = summaryData?.stats || {};
  const queryHistory = Array.isArray(summaryData?.queryHistory) ? summaryData.queryHistory : [];

  return (
    <div className="summary-modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="summary-modal"
        role="dialog"
        aria-modal="true"
        aria-label="Crawl summary"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="summary-modal-header">
          <div>
            <h2>Crawl Summary</h2>
            <p>Session-level narrative and key metrics</p>
          </div>
          <button type="button" className="summary-modal-close" onClick={onClose} aria-label="Close summary">
            <X size={16} />
          </button>
        </header>

        {isLoading && (
          <div className="summary-modal-loading">
            <p>Generating summary...</p>
          </div>
        )}

        {!isLoading && errorMessage && (
          <div className="summary-modal-error">
            <p>{errorMessage}</p>
          </div>
        )}

        {!isLoading && !errorMessage && summaryData && (
          <div className="summary-modal-body">
            <div className="summary-stats-grid">
              <span>Pages scanned: {stats.pagesScanned ?? 0}</span>
              <span>Relevant found: {stats.relevantFound ?? 0}</span>
              <span>Tokens used: {Number(stats.tokensUsed ?? 0).toLocaleString()}</span>
              <span>URLs attempted: {stats.urlsAttempted ?? 0}</span>
              <span>URL errors: {stats.urlErrors ?? 0}</span>
              <span>Error rate: {formatPercent(stats.errorRate ?? 0)}</span>
              <span>Sample size: {summaryData.sampleSize ?? 0}</span>
              <span>Status: {summaryData.status ?? 'unknown'}</span>
            </div>

            <div className="summary-copy">
              <h3>Narrative Summary</h3>
              <p>{summaryData.summaryText || 'No summary text returned by the API.'}</p>
            </div>

            {queryHistory.length > 0 && (
              <div className="summary-query-history">
                <h3>Query History</h3>
                <ul>
                  {queryHistory.slice(0, 40).map((query, index) => (
                    <li key={`${query}-${index}`}>{query}</li>
                  ))}
                </ul>
              </div>
            )}

            <div className="summary-modal-actions">
              <button
                type="button"
                className="summary-modal-export"
                onClick={onExportResults}
                disabled={!canExport || isLoading}
              >
                Export Results
              </button>
              <button type="button" className="summary-modal-secondary" onClick={onClose}>
                Close
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
