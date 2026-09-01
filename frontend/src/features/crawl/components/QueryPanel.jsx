import {
  ChevronDown,
  Link2,
  PauseCircle,
  Paperclip,
  Play,
  PlayCircle,
  StopCircle,
  X,
} from 'lucide-react';
import React, { useEffect, useRef, useState } from 'react';
import { RUN_STATES } from '../clients/crawlerClientContract';

export function QueryPanel({
  topic,
  onTopicChange,
  maxDepth,
  onMaxDepthChange,
  minRelevance,
  onMinRelevanceChange,
  domainFilter,
  onDomainFilterChange,
  examples,
  onAddExamplesFromFiles,
  onAddExampleUrl,
  onRemoveExample,
  reviewBeforeCrawl,
  onReviewBeforeCrawlChange,
  reviewPageCount,
  onReviewPageCountChange,
  runState,
  canStart,
  onStartCrawl,
  onStopCrawl,
  onPauseResumeCrawl,
  canExport,
  onExportResults,
  onSummarizeResults,
  canSummarize,
  isSummarizing,
}) {
  const [isExampleMenuOpen, setIsExampleMenuOpen] = useState(false);
  const [isUrlInputOpen, setIsUrlInputOpen] = useState(false);
  const [pendingUrl, setPendingUrl] = useState('');
  const exampleMenuRef = useRef(null);
  const fileInputRef = useRef(null);

  useEffect(() => {
    const handleOutsideClick = (event) => {
      if (!exampleMenuRef.current?.contains(event.target)) {
        setIsExampleMenuOpen(false);
      }
    };

    document.addEventListener('mousedown', handleOutsideClick);
    return () => document.removeEventListener('mousedown', handleOutsideClick);
  }, []);

  const isSessionActive =
    runState === RUN_STATES.STARTING ||
    runState === RUN_STATES.RUNNING ||
    runState === RUN_STATES.PAUSED ||
    runState === RUN_STATES.AWAITING_REVIEW ||
    runState === RUN_STATES.STOPPING;

  const isBusy = runState === RUN_STATES.STARTING || runState === RUN_STATES.STOPPING;
  const isPauseAvailable = runState === RUN_STATES.RUNNING || runState === RUN_STATES.PAUSED;
  const pauseButtonLabel = runState === RUN_STATES.PAUSED ? 'Resume Crawl' : 'Pause Crawl';

  const handleFileAttachClick = () => {
    setIsExampleMenuOpen(false);
    fileInputRef.current?.click();
  };

  const handleFileSelection = (event) => {
    const files = Array.from(event.target.files || []);
    if (files.length === 0) return;

    onAddExamplesFromFiles(files);
    event.target.value = '';
  };

  const handleUrlAttachClick = () => {
    setIsUrlInputOpen(true);
    setIsExampleMenuOpen(false);
  };

  const handleAddUrl = () => {
    const rawUrl = pendingUrl.trim();
    if (!rawUrl) return;

    const normalizedUrl = /^https?:\/\//i.test(rawUrl) ? rawUrl : `https://${rawUrl}`;
    onAddExampleUrl(normalizedUrl);
    setPendingUrl('');
    setIsUrlInputOpen(false);
  };

  return (
    <section className="query-panel">
      <label htmlFor="topic-input">Describe your topic</label>
      <div className="query-controls">
        <textarea
          id="topic-input"
          value={topic}
          onChange={(event) => onTopicChange(event.target.value)}
          placeholder="Example: Recent benchmarks and practical guides for running small open LLMs on consumer GPUs"
          rows={3}
          disabled={isBusy}
        />

        <div className="examples-wrap" ref={exampleMenuRef}>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            hidden
            onChange={handleFileSelection}
          />

          <button
            type="button"
            className="example-trigger"
            onClick={() => setIsExampleMenuOpen((open) => !open)}
            disabled={isBusy}
          >
            <Paperclip size={14} />
            Add examples
            <ChevronDown size={14} />
          </button>

          {isExampleMenuOpen && (
            <div className="example-menu">
              <button type="button" onClick={handleFileAttachClick}>
                <Paperclip size={14} />
                File attachments
              </button>
              <button type="button" onClick={handleUrlAttachClick}>
                <Link2 size={14} />
                URLs
              </button>
            </div>
          )}

          {isUrlInputOpen && (
            <div className="url-input-row">
              <input
                type="text"
                value={pendingUrl}
                onChange={(event) => setPendingUrl(event.target.value)}
                placeholder="https://example.com/resource"
                disabled={isBusy}
              />
              <button type="button" onClick={handleAddUrl} disabled={isBusy}>
                Add
              </button>
            </div>
          )}

          {examples.length > 0 && (
            <div className="example-list">
              {examples.map((example) => (
                <div key={example.id} className="example-item">
                  {example.type === 'file' ? <Paperclip size={12} /> : <Link2 size={12} />}
                  <span>{example.label}</span>
                  <button type="button" onClick={() => onRemoveExample(example.id)}>
                    <X size={12} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {!isSessionActive && (
          <button
            type="button"
            className="crawl-button start"
            onClick={onStartCrawl}
            disabled={!canStart}
          >
            <Play size={18} />
            Start Crawl
          </button>
        )}

        {isSessionActive && (
          <div className="crawl-action-row">
            <button
              type="button"
              className="crawl-button stop"
              onClick={onStopCrawl}
              disabled={runState === RUN_STATES.STOPPING}
            >
              <StopCircle size={18} />
              {runState === RUN_STATES.STOPPING ? 'Stopping...' : 'Stop Crawl'}
            </button>

            <button
              type="button"
              className={`crawl-button ${runState === RUN_STATES.PAUSED ? 'resume' : 'pause'}`}
              onClick={onPauseResumeCrawl}
              disabled={!isPauseAvailable}
            >
              {runState === RUN_STATES.PAUSED ? <PlayCircle size={18} /> : <PauseCircle size={18} />}
              {pauseButtonLabel}
            </button>
          </div>
        )}
      </div>

      <div className="crawl-params">
        <div className="param-control review-toggle-control">
          <label className="review-toggle">
            <input
              type="checkbox"
              checked={reviewBeforeCrawl}
              onChange={(event) => onReviewBeforeCrawlChange(event.target.checked)}
              disabled={isSessionActive}
            />
            <span>Review Before Crawl</span>
          </label>
        </div>

        {reviewBeforeCrawl && (
          <div className="param-control">
            <div className="param-head">
              <label htmlFor="review-page-count-slider">Pages to Review First</label>
              <span>{reviewPageCount}</span>
            </div>
            <input
              id="review-page-count-slider"
              type="range"
              min="1"
              max="10"
              step="1"
              value={reviewPageCount}
              onChange={(event) => onReviewPageCountChange(Number(event.target.value))}
              disabled={isSessionActive}
            />
          </div>
        )}

        <div className="param-control">
          <div className="param-head">
            <label htmlFor="depth-slider">Max Depth</label>
            <span>{maxDepth}</span>
          </div>
          <input
            id="depth-slider"
            type="range"
            min="1"
            max="10"
            step="1"
            value={maxDepth}
            onChange={(event) => onMaxDepthChange(Number(event.target.value))}
            disabled={isBusy}
          />
        </div>

        <div className="param-control">
          <div className="param-head">
            <label htmlFor="relevance-slider">Min Relevance</label>
            <span>{minRelevance.toFixed(2)}</span>
          </div>
          <input
            id="relevance-slider"
            type="range"
            min="0.5"
            max="0.95"
            step="0.01"
            value={minRelevance}
            onChange={(event) => onMinRelevanceChange(Number(event.target.value))}
            disabled={isBusy}
          />
        </div>

        <div className="param-control">
          <div className="param-head">
            <label htmlFor="domain-filter">Allowed Domains (optional)</label>
          </div>
          <input
            id="domain-filter"
            type="text"
            value={domainFilter}
            onChange={(event) => onDomainFilterChange(event.target.value)}
            placeholder="arxiv.org, github.com, .edu"
            disabled={isBusy}
          />
        </div>
      </div>

      <div className="sidebar-actions">
        <button
          type="button"
          className="sidebar-action-button"
          onClick={onExportResults}
          disabled={!canExport}
        >
          Export Results
        </button>
        <button
          type="button"
          className="sidebar-action-button secondary"
          onClick={onSummarizeResults}
          disabled={!canSummarize || isSummarizing}
        >
          {isSummarizing ? 'Summarizing...' : 'Summarize Results'}
        </button>
      </div>
    </section>
  );
}
