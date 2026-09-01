import React, { useEffect, useMemo, useRef, useState } from 'react';
import { createCrawlerClient } from './clients/createCrawlerClient';
import { RUN_STATES } from './clients/crawlerClientContract';
import { CrawlHeader } from './components/CrawlHeader';
import { QueryPanel } from './components/QueryPanel';
import { ResultsPanel } from './components/ResultsPanel';
import { SessionErrorBanner } from './components/SessionErrorBanner';
import { SummaryModal } from './components/SummaryModal';
import { getCrawlRuntimeConfig } from './config';
import { useCrawlSession } from './hooks/useCrawlSession';
import './crawl.css';

function buildExamplePayload(examples) {
  return examples.map((example) => {
    if (example.type === 'file') {
      return {
        type: 'file',
        label: example.label,
      };
    }

    return {
      type: 'url',
      url: example.label,
    };
  });
}

export function CrawlPage() {
  const runtimeConfig = useMemo(() => getCrawlRuntimeConfig(), []);
  const crawlerClient = useMemo(() => createCrawlerClient(runtimeConfig), [runtimeConfig]);

  const {
    sessionId,
    runState,
    connectionState,
    stats,
    results,
    resumingAfterReview,
    errorMessage,
    startSession,
    stopSession,
    pauseSession,
    resumeSession,
    setResultFeedback,
    setResultNotes,
    submitResultFeedback,
    exportSessionArtifacts,
    summarizeSession,
    clearError,
  } = useCrawlSession({ client: crawlerClient });

  const [topic, setTopic] = useState('');
  const [maxDepth, setMaxDepth] = useState(3);
  const [minRelevance, setMinRelevance] = useState(0.75);
  const [domainFilter, setDomainFilter] = useState('');
  const [examples, setExamples] = useState([]);
  const [reviewBeforeCrawl, setReviewBeforeCrawl] = useState(true);
  const [reviewPageCount, setReviewPageCount] = useState(3);
  const [feedFilter, setFeedFilter] = useState('both');
  const [isSummaryOpen, setIsSummaryOpen] = useState(false);
  const [isSummarizing, setIsSummarizing] = useState(false);
  const [summaryData, setSummaryData] = useState(null);
  const [summaryError, setSummaryError] = useState(null);

  const [isAtTop, setIsAtTop] = useState(true);
  const [unseenNewCount, setUnseenNewCount] = useState(0);
  const resultsPanelRef = useRef(null);
  const previousResultCountRef = useRef(0);

  const isRunActive =
    runState === RUN_STATES.STARTING ||
    runState === RUN_STATES.RUNNING ||
    runState === RUN_STATES.PAUSED ||
    runState === RUN_STATES.AWAITING_REVIEW ||
    runState === RUN_STATES.STOPPING;
  const canExport = Boolean(sessionId && results.length > 0);
  const canSummarize = Boolean(sessionId && results.length > 0);

  const canStart = topic.trim().length > 0 && !isRunActive;

  const sortedResults = useMemo(() => [...results].reverse(), [results]);

  const visibleResults = useMemo(() => {
    if (feedFilter === 'positive') {
      return sortedResults.filter((result) => result.feedback === 'yes');
    }

    if (feedFilter === 'negative') {
      return sortedResults.filter((result) => result.feedback === 'no');
    }

    return sortedResults;
  }, [sortedResults, feedFilter]);

  useEffect(() => {
    const addedCount = results.length - previousResultCountRef.current;

    if (addedCount > 0) {
      if (isAtTop) {
        resultsPanelRef.current?.scrollTo({ top: 0, behavior: 'smooth' });
      } else {
        setUnseenNewCount((current) => current + addedCount);
      }
    }

    previousResultCountRef.current = results.length;
  }, [results.length, isAtTop]);

  const handleResultsScroll = () => {
    const scrollTop = resultsPanelRef.current?.scrollTop ?? 0;
    const nearTop = scrollTop <= 24;

    setIsAtTop(nearTop);
    if (nearTop) {
      setUnseenNewCount(0);
    }
  };

  const jumpToTop = () => {
    resultsPanelRef.current?.scrollTo({ top: 0, behavior: 'smooth' });
    setIsAtTop(true);
    setUnseenNewCount(0);
  };

  const handleStartCrawl = async () => {
    if (!canStart) return;
    await startSession({
      topic,
      maxDepth,
      minRelevance,
      domainFilter,
      examples: buildExamplePayload(examples),
      reviewBeforeCrawl,
      reviewPageCount,
    });
  };

  const handleStopCrawl = async () => {
    await stopSession();
  };

  const handlePauseResumeCrawl = async () => {
    if (runState === RUN_STATES.PAUSED) {
      await resumeSession();
      return;
    }
    await pauseSession();
  };

  const handleAddExamplesFromFiles = (files) => {
    const nextExamples = files.map((file, index) => ({
      id: `file-${Date.now()}-${index}`,
      type: 'file',
      label: file.name,
      file,
    }));

    setExamples((current) => [...nextExamples, ...current]);
  };

  const handleAddExampleUrl = (url) => {
    setExamples((current) => [
      {
        id: `url-${Date.now()}`,
        type: 'url',
        label: url,
      },
      ...current,
    ]);
  };

  const handleRemoveExample = (exampleId) => {
    setExamples((current) => current.filter((example) => example.id !== exampleId));
  };

  const handleExportResults = async () => {
    await exportSessionArtifacts();
  };

  const handleSummarizeResults = async () => {
    if (!canSummarize || isSummarizing) return;

    setIsSummaryOpen(true);
    setIsSummarizing(true);
    setSummaryError(null);

    try {
      const payload = await summarizeSession({
        sampleSize: 40,
        includeQueryHistory: true,
      });
      setSummaryData(payload);
    } catch (error) {
      setSummaryData(null);
      setSummaryError(error instanceof Error ? error.message : 'Unable to summarize results');
    } finally {
      setIsSummarizing(false);
    }
  };

  const handleCloseSummary = () => {
    if (isSummarizing) return;
    setIsSummaryOpen(false);
  };

  return (
    <div className="app-shell">
      <CrawlHeader stats={stats} connectionState={connectionState} />
      <SessionErrorBanner message={errorMessage} onDismiss={clearError} />

      <main className="workspace">
        <QueryPanel
          topic={topic}
          onTopicChange={setTopic}
          maxDepth={maxDepth}
          onMaxDepthChange={setMaxDepth}
          minRelevance={minRelevance}
          onMinRelevanceChange={setMinRelevance}
          domainFilter={domainFilter}
          onDomainFilterChange={setDomainFilter}
          examples={examples}
          onAddExamplesFromFiles={handleAddExamplesFromFiles}
          onAddExampleUrl={handleAddExampleUrl}
          onRemoveExample={handleRemoveExample}
          reviewBeforeCrawl={reviewBeforeCrawl}
          onReviewBeforeCrawlChange={setReviewBeforeCrawl}
          reviewPageCount={reviewPageCount}
          onReviewPageCountChange={setReviewPageCount}
          runState={runState}
          canStart={canStart}
          onStartCrawl={handleStartCrawl}
          onStopCrawl={handleStopCrawl}
          onPauseResumeCrawl={handlePauseResumeCrawl}
          canExport={canExport}
          onExportResults={handleExportResults}
          onSummarizeResults={handleSummarizeResults}
          canSummarize={canSummarize}
          isSummarizing={isSummarizing}
        />

        <ResultsPanel
          panelRef={resultsPanelRef}
          onScroll={handleResultsScroll}
          results={results}
          visibleResults={visibleResults}
          feedFilter={feedFilter}
          onFeedFilterChange={setFeedFilter}
          runState={runState}
          resumingAfterReview={resumingAfterReview}
          isAtTop={isAtTop}
          unseenNewCount={unseenNewCount}
          onJumpToTop={jumpToTop}
          onFeedback={setResultFeedback}
          onNotes={setResultNotes}
          onSubmitFeedback={submitResultFeedback}
        />
      </main>

      <SummaryModal
        isOpen={isSummaryOpen}
        isLoading={isSummarizing}
        errorMessage={summaryError}
        summaryData={summaryData}
        onExportResults={handleExportResults}
        canExport={canExport}
        onClose={handleCloseSummary}
      />
    </div>
  );
}
