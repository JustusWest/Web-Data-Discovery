import { ArrowUp, Cpu, Search } from 'lucide-react';
import React, { useMemo } from 'react';
import { RUN_STATES } from '../clients/crawlerClientContract';
import { ResultCard } from './ResultCard';

function getLoadingText(runState, resumingAfterReview) {
  if (runState === RUN_STATES.STARTING) return 'Initializing crawler session...';
  if (runState === RUN_STATES.PAUSED) return 'Crawl paused. Resume when ready.';
  if (runState === RUN_STATES.AWAITING_REVIEW) {
    return 'Awaiting review feedback before crawl continues...';
  }
  if (resumingAfterReview) {
    return 'Initial feedback received. Starting crawl...';
  }
  if (runState === RUN_STATES.STOPPING) return 'Stopping crawl session...';
  return 'Exploring links and ranking pages...';
}

function getStatusHeadline(runState) {
  if (runState === RUN_STATES.RUNNING) {
    return 'Crawl Running';
  }
  if (runState === RUN_STATES.STARTING) return 'Starting crawl session';
  if (runState === RUN_STATES.PAUSED) return 'Crawler paused';
  if (runState === RUN_STATES.AWAITING_REVIEW) return 'Awaiting review feedback';
  if (runState === RUN_STATES.STOPPING) return 'Stopping crawl';
  if (runState === RUN_STATES.COMPLETED) return 'Crawl completed';
  if (runState === RUN_STATES.FAILED) return 'Crawl failed';
  return 'Crawler idle';
}

function getStatusDetail(runState) {
  if (runState === RUN_STATES.RUNNING) return 'Live crawl is actively processing pages';
  if (runState === RUN_STATES.STARTING) return 'Preparing crawler session and first query';
  if (runState === RUN_STATES.PAUSED) return 'Result stream and crawl actions are paused';
  if (runState === RUN_STATES.AWAITING_REVIEW) return 'Waiting for required user feedback to proceed';
  if (runState === RUN_STATES.STOPPING) return 'Gracefully ending crawl tasks';
  if (runState === RUN_STATES.COMPLETED) return 'Session finished';
  if (runState === RUN_STATES.FAILED) return 'Session failed, check error banner';
  return 'Ready to start a new crawl';
}

function getWaitingFirstBatchCopy(runState) {
  if (runState === RUN_STATES.STARTING) {
    return {
      title: 'Crawl started',
      body: 'Initializing the crawler and preparing the first discovery batch...',
    };
  }

  if (runState === RUN_STATES.RUNNING) {
    return {
      title: 'Crawl in progress',
      body: 'Crawler is active and waiting for the first pages to be discovered...',
    };
  }

  if (runState === RUN_STATES.PAUSED) {
    return {
      title: 'Crawl paused',
      body: 'Resume the crawl to continue discovering pages.',
    };
  }

  if (runState === RUN_STATES.AWAITING_REVIEW) {
    return {
      title: 'Awaiting review feedback',
      body: 'Submit required feedback on initial pages to continue the crawl.',
    };
  }

  if (runState === RUN_STATES.STOPPING) {
    return {
      title: 'Stopping crawl',
      body: 'Finalizing active tasks. New pages will stop appearing shortly.',
    };
  }

  return {
    title: 'Results will appear here',
    body: 'Start a crawl from the topic box on the left. Each discovered page can be marked Yes/No and reviewed with written feedback.',
  };
}

export function ResultsPanel({
  panelRef,
  onScroll,
  results,
  visibleResults,
  feedFilter,
  onFeedFilterChange,
  runState,
  resumingAfterReview,
  isAtTop,
  unseenNewCount,
  onJumpToTop,
  onFeedback,
  onNotes,
  onSubmitFeedback,
}) {
  const isRunActive =
    runState === RUN_STATES.STARTING ||
    runState === RUN_STATES.RUNNING ||
    runState === RUN_STATES.PAUSED ||
    runState === RUN_STATES.AWAITING_REVIEW ||
    runState === RUN_STATES.STOPPING;

  const waitingCopy = useMemo(() => getWaitingFirstBatchCopy(runState), [runState]);
  const statusHeadline = useMemo(() => getStatusHeadline(runState), [runState]);
  const statusDetail = useMemo(() => getStatusDetail(runState), [runState]);

  return (
    <section className="results-panel" aria-live="polite" ref={panelRef} onScroll={onScroll}>
      <div className="feed-toolbar">
        <div className={`crawler-status-card state-${runState}`}>
          <span className="crawler-status-headline">{statusHeadline}</span>
          <span className="crawler-status-detail">{statusDetail}</span>
        </div>
        <div className="feed-filter-group" role="group" aria-label="feedback filter">
          <button
            type="button"
            className={feedFilter === 'positive' ? 'active' : ''}
            onClick={() => onFeedFilterChange('positive')}
          >
            Positive
          </button>
          <button
            type="button"
            className={feedFilter === 'negative' ? 'active' : ''}
            onClick={() => onFeedFilterChange('negative')}
          >
            Negative
          </button>
          <button
            type="button"
            className={feedFilter === 'both' ? 'active' : ''}
            onClick={() => onFeedFilterChange('both')}
          >
            Both
          </button>
        </div>
      </div>

      {results.length === 0 && (
        <div className="empty-state">
          <Search size={36} />
          <h2>{waitingCopy.title}</h2>
          <p>{waitingCopy.body}</p>
        </div>
      )}

      {results.length > 0 && visibleResults.length === 0 && (
        <div className="filter-empty-state">
          <p>No {feedFilter} examples yet. Add feedback on pages to populate this view.</p>
        </div>
      )}

      {visibleResults.map((result) => (
        <ResultCard
          key={result.id}
          data={result}
          onFeedback={onFeedback}
          onNotes={onNotes}
          onSubmitFeedback={onSubmitFeedback}
        />
      ))}

      {isRunActive && (
        <div className="loading-row">
          <Cpu size={16} />
          <span>{getLoadingText(runState, resumingAfterReview)}</span>
        </div>
      )}

      {!isAtTop && unseenNewCount >= 15 && (
        <button type="button" className="go-top-button" onClick={onJumpToTop}>
          <ArrowUp size={14} />
          Go to top ({unseenNewCount} new)
        </button>
      )}
    </section>
  );
}
