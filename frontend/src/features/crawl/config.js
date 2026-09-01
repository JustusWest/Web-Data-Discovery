const DEFAULTS = {
  clientMode: 'mock',
  transport: 'sse',
  apiBaseUrl: '',
  apiPrefix: '/api/crawl',
};

const SUPPORTED_CLIENT_MODES = new Set(['mock', 'live']);
const SUPPORTED_TRANSPORTS = new Set(['sse', 'poll']);

function trimTrailingSlash(value) {
  return value.endsWith('/') ? value.slice(0, -1) : value;
}

function normalizeClientMode(rawMode) {
  if (!rawMode) return DEFAULTS.clientMode;
  const normalized = rawMode.toLowerCase();
  return SUPPORTED_CLIENT_MODES.has(normalized) ? normalized : DEFAULTS.clientMode;
}

function normalizeTransport(rawTransport) {
  if (!rawTransport) return DEFAULTS.transport;
  const normalized = rawTransport.toLowerCase();
  return SUPPORTED_TRANSPORTS.has(normalized) ? normalized : DEFAULTS.transport;
}

export function getCrawlRuntimeConfig() {
  const env = import.meta.env;
  const apiBaseUrl = trimTrailingSlash(env.VITE_CRAWL_API_BASE_URL || DEFAULTS.apiBaseUrl);
  const rawPrefix = env.VITE_CRAWL_API_PREFIX || DEFAULTS.apiPrefix;
  const apiPrefix = rawPrefix.startsWith('/') ? rawPrefix : `/${rawPrefix}`;

  return {
    clientMode: normalizeClientMode(env.VITE_CRAWL_CLIENT_MODE),
    transport: normalizeTransport(env.VITE_CRAWL_TRANSPORT),
    apiBaseUrl,
    apiPrefix,
  };
}
