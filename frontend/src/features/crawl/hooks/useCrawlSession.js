import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react';
import {
  CONNECTION_STATES,
  CRAWL_EVENT_TYPES,
  FEEDBACK_TYPES,
  RUN_STATES,
} from '../clients/crawlerClientContract';

const RESULT_REVEAL_INTERVAL_MS = 1000;

const EMPTY_STATS = {
  pagesScanned: 0,
  relevantFound: 0,
  tokensUsed: 0,
  urlsAttempted: 0,
  urlErrors: 0,
  errorRate: 0,
};

const INITIAL_STATE = {
  sessionId: null,
  runState: RUN_STATES.IDLE,
  connectionState: CONNECTION_STATES.DISCONNECTED,
  stats: EMPTY_STATS,
  resultsById: {},
  resultOrder: [],
  resumingAfterReview: false,
  errorMessage: null,
};

const ACTIONS = {
  SESSION_STARTING: 'SESSION_STARTING',
  SESSION_ASSIGNED: 'SESSION_ASSIGNED',
  SESSION_RUNNING: 'SESSION_RUNNING',
  SESSION_PAUSED: 'SESSION_PAUSED',
  SESSION_AWAITING_REVIEW: 'SESSION_AWAITING_REVIEW',
  SESSION_STOPPING: 'SESSION_STOPPING',
  SESSION_COMPLETED: 'SESSION_COMPLETED',
  SESSION_FAILED: 'SESSION_FAILED',
  CONNECTION_UPDATED: 'CONNECTION_UPDATED',
  CRAWL_PROGRESS: 'CRAWL_PROGRESS',
  RESULT_DISCOVERED: 'RESULT_DISCOVERED',
  RESULT_UPDATED: 'RESULT_UPDATED',
  REVIEW_RESUME_PENDING: 'REVIEW_RESUME_PENDING',
  REVIEW_RESUME_CLEARED: 'REVIEW_RESUME_CLEARED',
  RESULT_FEEDBACK_SET: 'RESULT_FEEDBACK_SET',
  RESULT_NOTES_SET: 'RESULT_NOTES_SET',
  RESULT_FEEDBACK_SUBMITTED: 'RESULT_FEEDBACK_SUBMITTED',
  ERROR_SET: 'ERROR_SET',
  ERROR_CLEARED: 'ERROR_CLEARED',
};

function extractMessage(error, fallback) {
  if (!error) return fallback;
  if (error instanceof Error) return error.message;
  return String(error);
}

function downloadBlob(blob, filename) {
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(objectUrl);
}

function mergeIncomingResultWithLocalState(existing, incoming) {
  const merged = {
    ...existing,
    ...incoming,
  };

  const existingNotes = typeof existing.notes === 'string' ? existing.notes : '';
  const incomingNotes = typeof incoming.notes === 'string' ? incoming.notes : '';
  const hasLocalDraftNotes = existingNotes.trim().length > 0 && !existing.feedbackSubmitted;

  if (hasLocalDraftNotes && incomingNotes.trim().length === 0) {
    merged.notes = existingNotes;
  }

  const hasLocalFeedbackSelection =
    !existing.feedbackSubmitted &&
    (existing.feedback === FEEDBACK_TYPES.YES || existing.feedback === FEEDBACK_TYPES.NO);
  const incomingSubmitted = Boolean(incoming.feedbackSubmitted);
  if (hasLocalFeedbackSelection && !incomingSubmitted) {
    merged.feedback = existing.feedback;
  }

  if (existing.feedbackSubmitted && !incomingSubmitted) {
    merged.feedbackSubmitted = true;
    if (!incoming.feedbackSubmittedAt && existing.feedbackSubmittedAt) {
      merged.feedbackSubmittedAt = existing.feedbackSubmittedAt;
    }
  }

  return merged;
}

function upsertDiscoveredResult(state, result) {
  if (state.resultsById[result.id]) {
    return {
      ...state,
      resultsById: {
        ...state.resultsById,
        [result.id]: mergeIncomingResultWithLocalState(state.resultsById[result.id], result),
      },
    };
  }

  return {
    ...state,
    resultsById: {
      ...state.resultsById,
      [result.id]: result,
    },
    resultOrder: [...state.resultOrder, result.id],
  };
}

function patchResult(state, result) {
  const existing = state.resultsById[result.id];
  if (!existing) {
    return state;
  }

  return {
    ...state,
    resultsById: {
      ...state.resultsById,
      [result.id]: mergeIncomingResultWithLocalState(existing, result),
    },
  };
}

function crawlSessionReducer(state, action) {
  switch (action.type) {
    case ACTIONS.SESSION_STARTING:
      return {
        ...INITIAL_STATE,
        runState: RUN_STATES.STARTING,
      };

    case ACTIONS.SESSION_ASSIGNED:
      return {
        ...state,
        sessionId: action.payload.sessionId,
      };

    case ACTIONS.SESSION_RUNNING:
      return {
        ...state,
        runState: RUN_STATES.RUNNING,
        errorMessage: null,
      };

    case ACTIONS.SESSION_PAUSED:
      return {
        ...state,
        runState: RUN_STATES.PAUSED,
        errorMessage: null,
      };

    case ACTIONS.SESSION_AWAITING_REVIEW:
      return {
        ...state,
        runState: RUN_STATES.AWAITING_REVIEW,
        errorMessage: null,
      };

    case ACTIONS.SESSION_STOPPING:
      return {
        ...state,
        runState: RUN_STATES.STOPPING,
      };

    case ACTIONS.SESSION_COMPLETED:
      return {
        ...state,
        runState: RUN_STATES.COMPLETED,
        connectionState: CONNECTION_STATES.DISCONNECTED,
        errorMessage: null,
      };

    case ACTIONS.SESSION_FAILED:
      return {
        ...state,
        runState: RUN_STATES.FAILED,
        connectionState: CONNECTION_STATES.ERROR,
        errorMessage: action.payload.errorMessage,
      };

    case ACTIONS.CONNECTION_UPDATED:
      return {
        ...state,
        connectionState: action.payload.connectionState,
      };

    case ACTIONS.CRAWL_PROGRESS:
      return {
        ...state,
        stats: action.payload.stats || state.stats,
      };

    case ACTIONS.RESULT_DISCOVERED:
      return {
        ...upsertDiscoveredResult(state, action.payload.result),
        resumingAfterReview: false,
      };

    case ACTIONS.RESULT_UPDATED:
      return patchResult(state, action.payload.result);

    case ACTIONS.REVIEW_RESUME_PENDING:
      return {
        ...state,
        resumingAfterReview: true,
      };

    case ACTIONS.REVIEW_RESUME_CLEARED:
      return {
        ...state,
        resumingAfterReview: false,
      };

    case ACTIONS.RESULT_FEEDBACK_SET: {
      const existing = state.resultsById[action.payload.resultId];
      if (!existing) return state;

      const nextFeedback =
        existing.feedback === action.payload.feedback ? null : action.payload.feedback;

      return {
        ...state,
        resultsById: {
          ...state.resultsById,
          [action.payload.resultId]: {
            ...existing,
            feedback: nextFeedback,
          },
        },
      };
    }

    case ACTIONS.RESULT_NOTES_SET: {
      const existing = state.resultsById[action.payload.resultId];
      if (!existing) return state;

      return {
        ...state,
        resultsById: {
          ...state.resultsById,
          [action.payload.resultId]: {
            ...existing,
            notes: action.payload.notes,
          },
        },
      };
    }

    case ACTIONS.RESULT_FEEDBACK_SUBMITTED: {
      const existing = state.resultsById[action.payload.resultId];
      if (!existing) return state;

      return {
        ...state,
        resultsById: {
          ...state.resultsById,
          [action.payload.resultId]: {
            ...existing,
            feedbackSubmitted: true,
            feedbackSubmittedAt: action.payload.feedbackSubmittedAt,
          },
        },
      };
    }

    case ACTIONS.ERROR_SET:
      return {
        ...state,
        errorMessage: action.payload.errorMessage,
      };

    case ACTIONS.ERROR_CLEARED:
      return {
        ...state,
        errorMessage: null,
      };

    default:
      return state;
  }
}

export function useCrawlSession({ client }) {
  const [state, dispatch] = useReducer(crawlSessionReducer, INITIAL_STATE);

  const sessionIdRef = useRef(state.sessionId);
  const runStateRef = useRef(state.runState);
  const resultsByIdRef = useRef(state.resultsById);
  const unsubscribeRef = useRef(null);
  const cleanupSubscriptionRef = useRef(() => {});
  const stopFallbackTimerRef = useRef(null);
  const pollTimerRef = useRef(null);
  const pollInFlightRef = useRef(false);
  const resumeHintTimerRef = useRef(null);
  const discoveryQueueOrderRef = useRef([]);
  const discoveryQueueByIdRef = useRef(new Map());
  const discoveryDrainTimerRef = useRef(null);

  useEffect(() => {
    sessionIdRef.current = state.sessionId;
  }, [state.sessionId]);

  useEffect(() => {
    runStateRef.current = state.runState;
  }, [state.runState]);

  useEffect(() => {
    resultsByIdRef.current = state.resultsById;
  }, [state.resultsById]);

  const clearStopFallback = useCallback(() => {
    if (stopFallbackTimerRef.current) {
      clearTimeout(stopFallbackTimerRef.current);
      stopFallbackTimerRef.current = null;
    }
  }, []);

  const clearPollTimer = useCallback(() => {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const stopDiscoveryDrain = useCallback(() => {
    if (discoveryDrainTimerRef.current) {
      clearInterval(discoveryDrainTimerRef.current);
      discoveryDrainTimerRef.current = null;
    }
  }, []);

  const clearDiscoveryQueue = useCallback(() => {
    stopDiscoveryDrain();
    discoveryQueueOrderRef.current = [];
    discoveryQueueByIdRef.current.clear();
  }, [stopDiscoveryDrain]);

  const clearResumeHint = useCallback(() => {
    if (resumeHintTimerRef.current) {
      clearTimeout(resumeHintTimerRef.current);
      resumeHintTimerRef.current = null;
    }
    dispatch({ type: ACTIONS.REVIEW_RESUME_CLEARED });
  }, []);

  const showResumeHint = useCallback(() => {
    if (resumeHintTimerRef.current) {
      clearTimeout(resumeHintTimerRef.current);
    }
    dispatch({ type: ACTIONS.REVIEW_RESUME_PENDING });
    resumeHintTimerRef.current = setTimeout(() => {
      dispatch({ type: ACTIONS.REVIEW_RESUME_CLEARED });
      resumeHintTimerRef.current = null;
    }, 8000);
  }, []);

  const cleanupSubscription = useCallback(() => {
    if (unsubscribeRef.current) {
      unsubscribeRef.current();
      unsubscribeRef.current = null;
    }
  }, []);

  const drainOneQueuedDiscovery = useCallback(() => {
    if (runStateRef.current === RUN_STATES.PAUSED) {
      return;
    }

    const nextResultId = discoveryQueueOrderRef.current.shift();
    if (!nextResultId) {
      stopDiscoveryDrain();
      return;
    }

    const nextResult = discoveryQueueByIdRef.current.get(nextResultId);
    discoveryQueueByIdRef.current.delete(nextResultId);
    if (!nextResult) {
      if (discoveryQueueOrderRef.current.length === 0) {
        stopDiscoveryDrain();
      }
      return;
    }

    if (resultsByIdRef.current[nextResult.id]) {
      dispatch({
        type: ACTIONS.RESULT_UPDATED,
        payload: { result: nextResult },
      });
    } else {
      dispatch({
        type: ACTIONS.RESULT_DISCOVERED,
        payload: { result: nextResult },
      });
    }

    if (discoveryQueueOrderRef.current.length === 0) {
      stopDiscoveryDrain();
    }
  }, [stopDiscoveryDrain]);

  const ensureDiscoveryDrain = useCallback(() => {
    if (discoveryDrainTimerRef.current) {
      return;
    }

    discoveryDrainTimerRef.current = setInterval(drainOneQueuedDiscovery, RESULT_REVEAL_INTERVAL_MS);
  }, [drainOneQueuedDiscovery]);

  const enqueueResultDiscovery = useCallback(
    (result) => {
      if (!result?.id) {
        return;
      }

      if (resultsByIdRef.current[result.id]) {
        dispatch({
          type: ACTIONS.RESULT_UPDATED,
          payload: { result },
        });
        return;
      }

      const alreadyQueued = discoveryQueueByIdRef.current.has(result.id);
      discoveryQueueByIdRef.current.set(result.id, result);
      if (!alreadyQueued) {
        discoveryQueueOrderRef.current.push(result.id);
      }

      ensureDiscoveryDrain();
    },
    [ensureDiscoveryDrain],
  );

  const syncPolledSessionResults = useCallback(
    (polledResults) => {
      if (!Array.isArray(polledResults)) return;

      polledResults.forEach((result) => {
        enqueueResultDiscovery(result);
      });
    },
    [enqueueResultDiscovery],
  );

  const syncPolledSessionInfo = useCallback(
    (sessionInfo) => {
      if (!sessionInfo || typeof sessionInfo !== 'object') return;

      if (sessionInfo.stats) {
        dispatch({
          type: ACTIONS.CRAWL_PROGRESS,
          payload: { stats: sessionInfo.stats },
        });
      }

      if (sessionInfo.status === RUN_STATES.RUNNING) {
        if (runStateRef.current === RUN_STATES.AWAITING_REVIEW) {
          showResumeHint();
        }
        dispatch({ type: ACTIONS.SESSION_RUNNING });
      } else if (sessionInfo.status === RUN_STATES.PAUSED) {
        dispatch({ type: ACTIONS.SESSION_PAUSED });
      } else if (sessionInfo.status === RUN_STATES.AWAITING_REVIEW) {
        dispatch({ type: ACTIONS.SESSION_AWAITING_REVIEW });
      } else if (sessionInfo.status === RUN_STATES.STOPPING) {
        dispatch({ type: ACTIONS.SESSION_STOPPING });
      } else if (sessionInfo.status === RUN_STATES.COMPLETED) {
        clearStopFallback();
        clearPollTimer();
        cleanupSubscriptionRef.current();
        dispatch({ type: ACTIONS.SESSION_COMPLETED });
      } else if (sessionInfo.status === RUN_STATES.FAILED) {
        clearStopFallback();
        clearPollTimer();
        cleanupSubscriptionRef.current();
        dispatch({
          type: ACTIONS.SESSION_FAILED,
          payload: {
            errorMessage: sessionInfo.errorMessage || 'Crawler session failed',
          },
        });
      }
    },
    [clearPollTimer, clearStopFallback, dispatch, showResumeHint],
  );

  const startSessionPolling = useCallback(
    (sessionId) => {
      if (!client.getSessionInfo) return;

      clearPollTimer();
      pollInFlightRef.current = false;

      const poll = async () => {
        if (pollInFlightRef.current) return;
        pollInFlightRef.current = true;

        try {
          const sessionInfo = await client.getSessionInfo(sessionId);
          syncPolledSessionInfo(sessionInfo);

          if (client.getSessionResults) {
            const response = await client.getSessionResults(sessionId);
            const polledResults = Array.isArray(response?.results)
              ? response.results
              : Array.isArray(response)
                ? response
                : [];
            syncPolledSessionResults(polledResults);
          }
        } catch (error) {
          dispatch({
            type: ACTIONS.ERROR_SET,
            payload: {
              errorMessage: extractMessage(error, 'Crawler polling failed'),
            },
          });
        } finally {
          pollInFlightRef.current = false;
        }
      };

      poll();
      pollTimerRef.current = setInterval(poll, 2500);
    },
    [client, clearPollTimer, syncPolledSessionInfo, syncPolledSessionResults],
  );

  useEffect(() => {
    cleanupSubscriptionRef.current = cleanupSubscription;
  }, [cleanupSubscription]);

  const clearError = useCallback(() => {
    dispatch({ type: ACTIONS.ERROR_CLEARED });
  }, []);

  const handleEvent = useCallback(
    (event) => {
      switch (event.type) {
        case CRAWL_EVENT_TYPES.SESSION_STARTED:
          dispatch({ type: ACTIONS.SESSION_RUNNING });
          if (event.sessionId) {
            dispatch({
              type: ACTIONS.SESSION_ASSIGNED,
              payload: { sessionId: event.sessionId },
            });
          }
          return;

        case CRAWL_EVENT_TYPES.CRAWL_PROGRESS:
          dispatch({
            type: ACTIONS.CRAWL_PROGRESS,
            payload: {
              stats: event.payload?.stats,
            },
          });
          return;

        case CRAWL_EVENT_TYPES.CRAWL_PAUSED:
          dispatch({ type: ACTIONS.SESSION_PAUSED });
          if (event.payload?.stats) {
            dispatch({
              type: ACTIONS.CRAWL_PROGRESS,
              payload: {
                stats: event.payload.stats,
              },
            });
          }
          return;

        case CRAWL_EVENT_TYPES.CRAWL_AWAITING_REVIEW:
          dispatch({ type: ACTIONS.SESSION_AWAITING_REVIEW });
          if (event.payload?.stats) {
            dispatch({
              type: ACTIONS.CRAWL_PROGRESS,
              payload: {
                stats: event.payload.stats,
              },
            });
          }
          return;

        case CRAWL_EVENT_TYPES.CRAWL_RESUMED:
          dispatch({ type: ACTIONS.SESSION_RUNNING });
          if (event.payload?.reason === 'review_gate') {
            showResumeHint();
          } else {
            clearResumeHint();
          }
          if (event.payload?.stats) {
            dispatch({
              type: ACTIONS.CRAWL_PROGRESS,
              payload: {
                stats: event.payload.stats,
              },
            });
          }
          return;

        case CRAWL_EVENT_TYPES.RESULT_DISCOVERED:
          if (!event.payload?.result) return;
          enqueueResultDiscovery(event.payload.result);
          return;

        case CRAWL_EVENT_TYPES.RESULT_UPDATED:
          if (!event.payload?.result) return;

          if (discoveryQueueByIdRef.current.has(event.payload.result.id)) {
            const existingQueued = discoveryQueueByIdRef.current.get(event.payload.result.id) || {};
            discoveryQueueByIdRef.current.set(event.payload.result.id, {
              ...existingQueued,
              ...event.payload.result,
            });
            return;
          }

          dispatch({
            type: ACTIONS.RESULT_UPDATED,
            payload: {
              result: event.payload.result,
            },
          });
          return;

        case CRAWL_EVENT_TYPES.SESSION_COMPLETED:
          clearStopFallback();
          clearPollTimer();
          clearResumeHint();
          cleanupSubscriptionRef.current();
          dispatch({ type: ACTIONS.SESSION_COMPLETED });
          return;

        case CRAWL_EVENT_TYPES.SESSION_FAILED:
          clearStopFallback();
          clearPollTimer();
          clearResumeHint();
          cleanupSubscriptionRef.current();
          dispatch({
            type: ACTIONS.SESSION_FAILED,
            payload: {
              errorMessage: event.payload?.message || 'Crawler session failed',
            },
          });
          return;

        default:
          return;
      }
    },
    [clearPollTimer, clearResumeHint, clearStopFallback, enqueueResultDiscovery, showResumeHint],
  );

  const startSession = useCallback(
    async (requestPayload) => {
      if (
        runStateRef.current === RUN_STATES.STARTING ||
        runStateRef.current === RUN_STATES.RUNNING ||
        runStateRef.current === RUN_STATES.PAUSED ||
        runStateRef.current === RUN_STATES.AWAITING_REVIEW ||
        runStateRef.current === RUN_STATES.STOPPING
      ) {
        return;
      }

      clearStopFallback();
      clearResumeHint();
      clearDiscoveryQueue();
      cleanupSubscription();
      dispatch({ type: ACTIONS.SESSION_STARTING });

      try {
        const { sessionId } = await client.startCrawl(requestPayload);

        dispatch({
          type: ACTIONS.SESSION_ASSIGNED,
          payload: { sessionId },
        });

        const unsubscribe = client.subscribe(sessionId, {
          onEvent: handleEvent,
          onConnectionState: (connectionState) => {
            dispatch({
              type: ACTIONS.CONNECTION_UPDATED,
              payload: { connectionState },
            });
          },
          onError: (error) => {
            dispatch({
              type: ACTIONS.ERROR_SET,
              payload: {
                errorMessage: extractMessage(error, 'Crawler stream error'),
              },
            });
          },
        });

        unsubscribeRef.current = unsubscribe;
        startSessionPolling(sessionId);
      } catch (error) {
        clearPollTimer();
        dispatch({
          type: ACTIONS.SESSION_FAILED,
          payload: {
            errorMessage: extractMessage(error, 'Unable to start crawler session'),
          },
        });
      }
    },
    [
      clearPollTimer,
      clearDiscoveryQueue,
      clearResumeHint,
      clearStopFallback,
      cleanupSubscription,
      client,
      handleEvent,
      startSessionPolling,
    ],
  );

  const stopSession = useCallback(async () => {
    if (
      !sessionIdRef.current ||
      (runStateRef.current !== RUN_STATES.RUNNING &&
        runStateRef.current !== RUN_STATES.PAUSED &&
        runStateRef.current !== RUN_STATES.AWAITING_REVIEW &&
        runStateRef.current !== RUN_STATES.STARTING)
    ) {
      return;
    }

    dispatch({ type: ACTIONS.SESSION_STOPPING });
    clearResumeHint();

    try {
      await client.stopCrawl(sessionIdRef.current);

      clearStopFallback();
      stopFallbackTimerRef.current = setTimeout(() => {
        if (runStateRef.current === RUN_STATES.STOPPING) {
          cleanupSubscriptionRef.current();
          dispatch({ type: ACTIONS.SESSION_COMPLETED });
        }
      }, 15000);
    } catch (error) {
      dispatch({
        type: ACTIONS.SESSION_FAILED,
        payload: {
          errorMessage: extractMessage(error, 'Unable to stop crawler session'),
        },
      });
    }
  }, [clearResumeHint, clearStopFallback, client]);

  const pauseSession = useCallback(async () => {
    if (!sessionIdRef.current || runStateRef.current !== RUN_STATES.RUNNING) {
      return;
    }

    try {
      await client.pauseCrawl(sessionIdRef.current);
      dispatch({ type: ACTIONS.SESSION_PAUSED });
    } catch (error) {
      dispatch({
        type: ACTIONS.ERROR_SET,
        payload: {
          errorMessage: extractMessage(error, 'Unable to pause crawler session'),
        },
      });
    }
  }, [client]);

  const resumeSession = useCallback(async () => {
    if (!sessionIdRef.current || runStateRef.current !== RUN_STATES.PAUSED) {
      return;
    }

    try {
      await client.resumeCrawl(sessionIdRef.current);
      dispatch({ type: ACTIONS.SESSION_RUNNING });
    } catch (error) {
      dispatch({
        type: ACTIONS.ERROR_SET,
        payload: {
          errorMessage: extractMessage(error, 'Unable to resume crawler session'),
        },
      });
    }
  }, [client]);

  const setResultFeedback = useCallback((resultId, feedback) => {
    if (feedback !== FEEDBACK_TYPES.YES && feedback !== FEEDBACK_TYPES.NO) {
      return;
    }

    dispatch({
      type: ACTIONS.RESULT_FEEDBACK_SET,
      payload: {
        resultId,
        feedback,
      },
    });
  }, []);

  const setResultNotes = useCallback((resultId, notes) => {
    dispatch({
      type: ACTIONS.RESULT_NOTES_SET,
      payload: {
        resultId,
        notes,
      },
    });
  }, []);

  const submitResultFeedback = useCallback(
    async (resultId) => {
      const currentSessionId = sessionIdRef.current;
      if (!currentSessionId) {
        dispatch({
          type: ACTIONS.ERROR_SET,
          payload: {
            errorMessage: 'No active crawl session for feedback submission',
          },
        });
        return;
      }

      const existing = resultsByIdRef.current[resultId];
      if (!existing) {
        return;
      }

      try {
        await client.submitFeedback(currentSessionId, {
          resultId,
          feedback: existing.feedback,
          notes: existing.notes,
        });

        dispatch({
          type: ACTIONS.RESULT_FEEDBACK_SUBMITTED,
          payload: {
            resultId,
            feedbackSubmittedAt: new Date().toLocaleTimeString(),
          },
        });
      } catch (error) {
        dispatch({
          type: ACTIONS.ERROR_SET,
          payload: {
            errorMessage: extractMessage(error, 'Unable to submit feedback'),
          },
        });
      }
    },
    [client],
  );

  const exportSessionArtifacts = useCallback(async () => {
    const currentSessionId = sessionIdRef.current;
    if (!currentSessionId) {
      dispatch({
        type: ACTIONS.ERROR_SET,
        payload: {
          errorMessage: 'No crawl session is available to export',
        },
      });
      return;
    }

    try {
      const { blob, filename } = await client.exportSessionArtifacts(currentSessionId);
      if (!(blob instanceof Blob)) {
        throw new Error('Crawler client export did not return a Blob payload');
      }

      downloadBlob(blob, filename || `${currentSessionId}-artifacts.zip`);
    } catch (error) {
      dispatch({
        type: ACTIONS.ERROR_SET,
        payload: {
          errorMessage: extractMessage(error, 'Unable to export crawl artifacts'),
        },
      });
    }
  }, [client]);

  const summarizeSession = useCallback(
    async ({ sampleSize = 40, includeQueryHistory = true } = {}) => {
      const currentSessionId = sessionIdRef.current;
      if (!currentSessionId) {
        const errorMessage = 'No crawl session is available to summarize';
        dispatch({
          type: ACTIONS.ERROR_SET,
          payload: { errorMessage },
        });
        throw new Error(errorMessage);
      }

      if (typeof client.summarizeResults !== 'function') {
        const errorMessage = 'Crawler client does not support session summarization';
        dispatch({
          type: ACTIONS.ERROR_SET,
          payload: { errorMessage },
        });
        throw new Error(errorMessage);
      }

      try {
        return await client.summarizeResults(currentSessionId, {
          sampleSize,
          includeQueryHistory,
        });
      } catch (error) {
        dispatch({
          type: ACTIONS.ERROR_SET,
          payload: {
            errorMessage: extractMessage(error, 'Unable to summarize crawl results'),
          },
        });
        throw error;
      }
    },
    [client],
  );

  useEffect(() => {
    return () => {
      clearStopFallback();
      clearPollTimer();
      clearResumeHint();
      clearDiscoveryQueue();
      cleanupSubscription();
    };
  }, [clearDiscoveryQueue, clearPollTimer, clearResumeHint, clearStopFallback, cleanupSubscription]);

  const orderedResults = useMemo(
    () =>
      state.resultOrder
        .map((resultId) => state.resultsById[resultId])
        .filter(Boolean),
    [state.resultOrder, state.resultsById],
  );

  return {
    sessionId: state.sessionId,
    runState: state.runState,
    connectionState: state.connectionState,
    stats: state.stats,
    results: orderedResults,
    resumingAfterReview: state.resumingAfterReview,
    errorMessage: state.errorMessage,
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
  };
}
