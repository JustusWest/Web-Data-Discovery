import { CONNECTION_STATES } from './crawlerClientContract';

function buildApiUrl(apiBaseUrl, path) {
  if (!apiBaseUrl) return path;
  return `${apiBaseUrl}${path}`;
}

function parseFilenameFromContentDisposition(value) {
  if (!value) return null;
  const match = value.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i);
  if (!match?.[1]) return null;
  const cleaned = match[1].replace(/"/g, '').trim();
  try {
    return decodeURIComponent(cleaned);
  } catch {
    return cleaned;
  }
}

async function requestJson(url, options) {
  const response = await fetch(url, options);

  if (!response.ok) {
    const errorBody = await response.text();
    throw new Error(
      `Crawler API request failed (${response.status} ${response.statusText}): ${errorBody}`,
    );
  }

  if (response.status === 204) {
    return {};
  }

  return response.json();
}

export function createLiveCrawlerClient(runtimeConfig) {
  const { apiBaseUrl, apiPrefix, transport } = runtimeConfig;

  return {
    async startCrawl(requestPayload) {
      const url = buildApiUrl(apiBaseUrl, `${apiPrefix}/sessions`);
      return requestJson(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestPayload),
      });
    },

    async stopCrawl(sessionId) {
      const url = buildApiUrl(apiBaseUrl, `${apiPrefix}/sessions/${sessionId}/stop`);
      return requestJson(url, {
        method: 'POST',
      });
    },

    async pauseCrawl(sessionId) {
      const url = buildApiUrl(apiBaseUrl, `${apiPrefix}/sessions/${sessionId}/pause`);
      return requestJson(url, {
        method: 'POST',
      });
    },

    async resumeCrawl(sessionId) {
      const url = buildApiUrl(apiBaseUrl, `${apiPrefix}/sessions/${sessionId}/resume`);
      return requestJson(url, {
        method: 'POST',
      });
    },

    async getSessionInfo(sessionId) {
      const url = buildApiUrl(apiBaseUrl, `${apiPrefix}/sessions/${sessionId}`);
      return requestJson(url, { method: 'GET' });
    },

    async getSessionResults(sessionId) {
      const url = buildApiUrl(apiBaseUrl, `${apiPrefix}/sessions/${sessionId}/results`);
      return requestJson(url, { method: 'GET' });
    },

    async submitFeedback(sessionId, feedbackPayload) {
      const url = buildApiUrl(apiBaseUrl, `${apiPrefix}/sessions/${sessionId}/feedback`);
      return requestJson(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(feedbackPayload),
      });
    },

    async exportSessionArtifacts(sessionId) {
      const url = buildApiUrl(apiBaseUrl, `${apiPrefix}/sessions/${sessionId}/export`);
      const response = await fetch(url, { method: 'GET' });

      if (!response.ok) {
        const errorBody = await response.text();
        throw new Error(
          `Crawler export request failed (${response.status} ${response.statusText}): ${errorBody}`,
        );
      }

      const blob = await response.blob();
      const contentDisposition = response.headers.get('content-disposition');
      const filename =
        parseFilenameFromContentDisposition(contentDisposition) || `${sessionId}-artifacts.zip`;

      return { blob, filename };
    },

    async summarizeResults(sessionId, requestPayload = {}) {
      const url = buildApiUrl(apiBaseUrl, `${apiPrefix}/sessions/${sessionId}/summary`);
      return requestJson(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(requestPayload),
      });
    },

    subscribe(sessionId, handlers) {
      if (transport !== 'sse') {
        throw new Error(
          `Unsupported live transport "${transport}". Configure VITE_CRAWL_TRANSPORT=sse.`,
        );
      }

      const streamUrl = buildApiUrl(apiBaseUrl, `${apiPrefix}/sessions/${sessionId}/events`);

      handlers.onConnectionState?.(CONNECTION_STATES.CONNECTING);

      const eventSource = new EventSource(streamUrl);

      eventSource.onopen = () => {
        handlers.onConnectionState?.(CONNECTION_STATES.CONNECTED);
      };

      eventSource.onmessage = (message) => {
        try {
          const parsed = JSON.parse(message.data);
          if (!parsed?.type) {
            return;
          }

          handlers.onEvent?.(parsed);
        } catch (error) {
          handlers.onError?.(
            error instanceof Error ? error : new Error('Invalid crawler stream payload'),
          );
        }
      };

      eventSource.onerror = () => {
        handlers.onConnectionState?.(CONNECTION_STATES.RECONNECTING);
        handlers.onError?.(new Error('Crawler stream connection issue'));
      };

      return () => {
        eventSource.close();
        handlers.onConnectionState?.(CONNECTION_STATES.DISCONNECTED);
      };
    },
  };
}
