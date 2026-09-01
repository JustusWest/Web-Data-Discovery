import {
  CONNECTION_STATES,
  CRAWL_EVENT_TYPES,
  RUN_STATES,
} from './crawlerClientContract';
import { generateMockResult } from '../domain/generateMockResult';

function clampReviewPageCount(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 3;
  return Math.min(10, Math.max(1, Math.round(numeric)));
}

function buildEvent(type, sessionId, payload) {
  return {
    type,
    sessionId,
    timestamp: new Date().toISOString(),
    payload,
  };
}

function createSession(sessionId, request) {
  return {
    sessionId,
    request,
    reviewBeforeCrawl: Boolean(request.reviewBeforeCrawl),
    reviewPageCount: clampReviewPageCount(request.reviewPageCount),
    reviewGateOpen: false,
    reviewGateRequiredIds: new Set(),
    runState: RUN_STATES.IDLE,
    connectionState: CONNECTION_STATES.DISCONNECTED,
    listeners: new Set(),
    results: new Map(),
    stats: {
      pagesScanned: 0,
      relevantFound: 0,
      tokensUsed: 0,
      urlsAttempted: 0,
      urlErrors: 0,
      errorRate: 0,
    },
    resultCounter: 0,
    streamIntervalId: null,
    pendingTimeouts: new Set(),
    reconnectSimulated: false,
    startedAt: null,
  };
}

function removeTimer(session, timerId) {
  session.pendingTimeouts.delete(timerId);
}

function clearAllTimers(session) {
  if (session.streamIntervalId) {
    clearInterval(session.streamIntervalId);
    session.streamIntervalId = null;
  }

  session.pendingTimeouts.forEach((timerId) => {
    clearTimeout(timerId);
  });
  session.pendingTimeouts.clear();
}

function buildMockExportBlob(session) {
  const payload = {
    sessionId: session.sessionId,
    request: session.request,
    stats: session.stats,
    exportedAt: new Date().toISOString(),
    results: Array.from(session.results.values()),
  };

  const json = JSON.stringify(payload, null, 2);
  return new Blob([json], { type: 'application/json' });
}

function clampSummarySampleSize(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 40;
  return Math.min(100, Math.max(5, Math.round(numeric)));
}

function sampleResultsForSummary(results, sampleSize) {
  if (results.length <= sampleSize) {
    return [...results];
  }

  const headCount = Math.floor(sampleSize / 2);
  const tailCount = sampleSize - headCount;
  return [...results.slice(0, headCount), ...results.slice(-tailCount)];
}

function uniqueQueryHistoryFromResults(results) {
  const seen = new Set();
  const queries = [];

  results.forEach((result) => {
    const raw = typeof result.query === 'string' ? result.query.trim() : '';
    if (!raw || seen.has(raw)) return;
    seen.add(raw);
    queries.push(raw);
  });

  return queries;
}

export function createMockCrawlerClient() {
  const sessions = new Map();
  let nextSessionNumber = 1;

  const emitConnectionState = (session, connectionState) => {
    if (session.connectionState === connectionState) return;
    session.connectionState = connectionState;
    session.listeners.forEach((listener) => {
      listener.onConnectionState?.(connectionState);
    });
  };

  const emitEvent = (session, type, payload) => {
    const event = buildEvent(type, session.sessionId, payload);
    session.listeners.forEach((listener) => {
      listener.onEvent?.(event);
    });
  };

  const emitError = (session, error) => {
    session.listeners.forEach((listener) => {
      listener.onError?.(error);
    });
  };

  const setReviewGate = (session, requiredResultIds) => {
    const deduped = requiredResultIds.filter(Boolean);
    session.reviewGateRequiredIds = new Set(deduped);
    session.reviewGateOpen = deduped.length > 0;

    if (!session.reviewGateOpen) {
      return;
    }

    session.runState = RUN_STATES.AWAITING_REVIEW;
    emitEvent(session, CRAWL_EVENT_TYPES.CRAWL_AWAITING_REVIEW, {
      requiredCount: deduped.length,
      remainingReviews: deduped.length,
      requiredResultIds: deduped,
      stats: session.stats,
    });
  };

  const maybeOpenInitialReviewGate = (session) => {
    if (!session.reviewBeforeCrawl || session.reviewGateOpen) {
      return;
    }

    const firstPageResultIds = Array.from(session.results.keys()).slice(0, session.reviewPageCount);
    if (firstPageResultIds.length < session.reviewPageCount) {
      return;
    }

    const pendingIds = firstPageResultIds.filter((resultId) => {
      const result = session.results.get(resultId);
      return result && !result.feedbackSubmitted;
    });

    setReviewGate(session, pendingIds);
  };

  const maybeSimulateReconnect = (session) => {
    if (session.reconnectSimulated || session.resultCounter < 4) return;

    session.reconnectSimulated = true;
    emitConnectionState(session, CONNECTION_STATES.RECONNECTING);

    const reconnectTimer = setTimeout(() => {
      if (session.runState === RUN_STATES.COMPLETED) return;
      emitConnectionState(session, CONNECTION_STATES.CONNECTED);
      removeTimer(session, reconnectTimer);
    }, 750);

    session.pendingTimeouts.add(reconnectTimer);
  };

  const startStreaming = (session) => {
    if (session.streamIntervalId) return;

    session.runState = RUN_STATES.RUNNING;
    session.startedAt = new Date().toISOString();

    emitEvent(session, CRAWL_EVENT_TYPES.SESSION_STARTED, {
      sessionId: session.sessionId,
      startedAt: session.startedAt,
      request: session.request,
    });

    session.streamIntervalId = setInterval(() => {
      if (session.runState !== RUN_STATES.RUNNING) return;

      session.resultCounter += 1;
      const id = `${session.sessionId}-result-${session.resultCounter}`;
      const nextResult = generateMockResult({
        id,
        topic: session.request.topic,
        minRelevance: session.request.minRelevance,
        domainFilter: session.request.domainFilter,
        maxDepth: session.request.maxDepth,
      });

      session.results.set(nextResult.id, nextResult);
      session.stats = {
        pagesScanned: session.stats.pagesScanned + Math.floor(Math.random() * 5) + 1,
        relevantFound:
          session.stats.relevantFound +
          (nextResult.relevanceScore >= session.request.minRelevance ? 1 : 0),
        tokensUsed: session.stats.tokensUsed + Math.floor(Math.random() * 700) + 200,
        urlsAttempted: session.stats.urlsAttempted + 1,
        urlErrors: session.stats.urlErrors,
        errorRate:
          session.stats.urlsAttempted + 1 > 0
            ? session.stats.urlErrors / (session.stats.urlsAttempted + 1)
            : 0,
      };

      emitEvent(session, CRAWL_EVENT_TYPES.RESULT_DISCOVERED, {
        result: nextResult,
      });

      emitEvent(session, CRAWL_EVENT_TYPES.CRAWL_PROGRESS, {
        stats: session.stats,
        lastResultId: nextResult.id,
      });

      const readyTimer = setTimeout(() => {
        const existing = session.results.get(nextResult.id);
        if (!existing || session.runState === RUN_STATES.COMPLETED) {
          removeTimer(session, readyTimer);
          return;
        }

        const patchedResult = {
          ...existing,
          status: 'ready',
        };
        session.results.set(nextResult.id, patchedResult);

        emitEvent(session, CRAWL_EVENT_TYPES.RESULT_UPDATED, {
          result: patchedResult,
        });

        removeTimer(session, readyTimer);
      }, 900);

      session.pendingTimeouts.add(readyTimer);
      maybeSimulateReconnect(session);
      maybeOpenInitialReviewGate(session);
    }, 2600);
  };

  const completeSession = (session, payload = {}) => {
    if (session.runState === RUN_STATES.COMPLETED) return;

    session.runState = RUN_STATES.COMPLETED;
    clearAllTimers(session);

    emitEvent(session, CRAWL_EVENT_TYPES.SESSION_COMPLETED, {
      stats: session.stats,
      completedAt: new Date().toISOString(),
      ...payload,
    });

    emitConnectionState(session, CONNECTION_STATES.DISCONNECTED);
  };

  return {
    async startCrawl(request) {
      const sessionId = `mock-${Date.now()}-${nextSessionNumber}`;
      nextSessionNumber += 1;

      const session = createSession(sessionId, request);
      sessions.set(sessionId, session);

      return { sessionId };
    },

    async stopCrawl(sessionId) {
      const session = sessions.get(sessionId);
      if (!session) {
        throw new Error(`Unknown mock session: ${sessionId}`);
      }

      if (session.runState === RUN_STATES.COMPLETED) {
        return { sessionId, status: RUN_STATES.COMPLETED };
      }

      session.runState = RUN_STATES.STOPPING;

      const stopTimer = setTimeout(() => {
        completeSession(session, { reason: 'stopped_by_user' });
        removeTimer(session, stopTimer);
      }, 300);

      session.pendingTimeouts.add(stopTimer);

      return { sessionId, status: RUN_STATES.STOPPING };
    },

    async pauseCrawl(sessionId) {
      const session = sessions.get(sessionId);
      if (!session) {
        throw new Error(`Unknown mock session: ${sessionId}`);
      }

      if (session.runState !== RUN_STATES.RUNNING) {
        throw new Error(`Cannot pause mock session in state '${session.runState}'`);
      }

      session.runState = RUN_STATES.PAUSED;
      emitEvent(session, CRAWL_EVENT_TYPES.CRAWL_PAUSED, {
        reason: 'manual',
        stats: session.stats,
      });
      return { sessionId, status: RUN_STATES.PAUSED };
    },

    async resumeCrawl(sessionId) {
      const session = sessions.get(sessionId);
      if (!session) {
        throw new Error(`Unknown mock session: ${sessionId}`);
      }

      if (session.runState !== RUN_STATES.PAUSED) {
        throw new Error(`Cannot resume mock session in state '${session.runState}'`);
      }

      session.runState = RUN_STATES.RUNNING;
      emitEvent(session, CRAWL_EVENT_TYPES.CRAWL_RESUMED, {
        reason: 'manual',
        stats: session.stats,
      });
      return { sessionId, status: RUN_STATES.RUNNING };
    },

    async getSessionInfo(sessionId) {
      const session = sessions.get(sessionId);
      if (!session) {
        throw new Error(`Unknown mock session: ${sessionId}`);
      }

      return {
        sessionId,
        status: session.runState,
        stats: session.stats,
        startedAt: session.startedAt,
        completedAt: session.runState === RUN_STATES.COMPLETED ? new Date().toISOString() : null,
        errorMessage: null,
      };
    },

    async getSessionResults(sessionId) {
      const session = sessions.get(sessionId);
      if (!session) {
        throw new Error(`Unknown mock session: ${sessionId}`);
      }

      return {
        results: Array.from(session.results.values()),
      };
    },

    async submitFeedback(sessionId, feedbackPayload) {
      const session = sessions.get(sessionId);
      if (!session) {
        throw new Error(`Unknown mock session: ${sessionId}`);
      }

      const { resultId, feedback, notes } = feedbackPayload;
      const existing = session.results.get(resultId);
      if (!existing) {
        return { updated: false };
      }

      const patchedResult = {
        ...existing,
        feedback,
        notes,
        feedbackSubmitted: true,
        feedbackSubmittedAt: new Date().toLocaleTimeString(),
      };

      session.results.set(resultId, patchedResult);
      emitEvent(session, CRAWL_EVENT_TYPES.RESULT_UPDATED, {
        result: patchedResult,
      });

      if (session.reviewGateOpen && session.reviewGateRequiredIds.has(resultId)) {
        session.reviewGateRequiredIds.delete(resultId);
        const remainingIds = Array.from(session.reviewGateRequiredIds);

        if (remainingIds.length === 0) {
          session.reviewGateOpen = false;
          session.runState = RUN_STATES.RUNNING;
          emitEvent(session, CRAWL_EVENT_TYPES.CRAWL_RESUMED, {
            reviewedCount: session.reviewPageCount,
            stats: session.stats,
          });
        } else {
          emitEvent(session, CRAWL_EVENT_TYPES.CRAWL_AWAITING_REVIEW, {
            requiredCount: remainingIds.length,
            remainingReviews: remainingIds.length,
            requiredResultIds: remainingIds,
            stats: session.stats,
          });
        }
      }

      return { updated: true };
    },

    async exportSessionArtifacts(sessionId) {
      const session = sessions.get(sessionId);
      if (!session) {
        throw new Error(`Unknown mock session: ${sessionId}`);
      }

      return {
        blob: buildMockExportBlob(session),
        filename: `${sessionId}-artifacts.json`,
      };
    },

    async summarizeResults(sessionId, requestPayload = {}) {
      const session = sessions.get(sessionId);
      if (!session) {
        throw new Error(`Unknown mock session: ${sessionId}`);
      }

      const sampleSize = clampSummarySampleSize(requestPayload.sampleSize ?? 40);
      const includeQueryHistory = requestPayload.includeQueryHistory !== false;
      const allResults = Array.from(session.results.values());
      const sampledResults = sampleResultsForSummary(allResults, sampleSize);
      const queryHistory = includeQueryHistory ? uniqueQueryHistoryFromResults(allResults) : [];
      const summaryText = [
        `This mock crawl explored "${session.request.topic}" and is currently ${session.runState}.`,
        `It scanned ${session.stats.pagesScanned} pages, found ${session.stats.relevantFound} likely-relevant pages, and recorded ${session.stats.urlErrors} URL errors.`,
        `This summary was generated from ${sampledResults.length} sampled results out of ${allResults.length} total discovered pages.`,
        queryHistory.length > 0
          ? `Query history included ${queryHistory.length} distinct query patterns.`
          : 'No query history was included in this summary request.',
      ].join(' ');

      return {
        sessionId,
        status: session.runState,
        generatedAt: new Date().toISOString(),
        sampleSize: sampledResults.length,
        queryHistory,
        stats: session.stats,
        summaryText,
      };
    },

    subscribe(sessionId, handlers) {
      const session = sessions.get(sessionId);
      if (!session) {
        throw new Error(`Unknown mock session: ${sessionId}`);
      }

      const listener = {
        onEvent: handlers.onEvent,
        onConnectionState: handlers.onConnectionState,
        onError: handlers.onError,
      };

      session.listeners.add(listener);

      emitConnectionState(session, CONNECTION_STATES.CONNECTING);

      const connectTimer = setTimeout(() => {
        try {
          if (session.runState === RUN_STATES.COMPLETED) {
            removeTimer(session, connectTimer);
            return;
          }

          emitConnectionState(session, CONNECTION_STATES.CONNECTED);
          if (session.runState === RUN_STATES.IDLE) {
            startStreaming(session);
          }
        } catch (error) {
          emitConnectionState(session, CONNECTION_STATES.ERROR);
          emitError(session, error instanceof Error ? error : new Error('Mock stream failed'));
        } finally {
          removeTimer(session, connectTimer);
        }
      }, 220);

      session.pendingTimeouts.add(connectTimer);

      return () => {
        session.listeners.delete(listener);

        if (session.listeners.size === 0 && session.runState === RUN_STATES.COMPLETED) {
          sessions.delete(sessionId);
        }
      };
    },
  };
}
