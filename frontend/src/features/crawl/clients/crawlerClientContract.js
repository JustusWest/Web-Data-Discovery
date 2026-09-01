export const RUN_STATES = {
  IDLE: 'idle',
  STARTING: 'starting',
  RUNNING: 'running',
  PAUSED: 'paused',
  AWAITING_REVIEW: 'awaiting_review',
  STOPPING: 'stopping',
  COMPLETED: 'completed',
  FAILED: 'failed',
};

export const CONNECTION_STATES = {
  CONNECTING: 'connecting',
  CONNECTED: 'connected',
  RECONNECTING: 'reconnecting',
  DISCONNECTED: 'disconnected',
  ERROR: 'error',
};

export const CRAWL_EVENT_TYPES = {
  SESSION_STARTED: 'session.started',
  CRAWL_PROGRESS: 'crawl.progress',
  RESULT_DISCOVERED: 'result.discovered',
  RESULT_UPDATED: 'result.updated',
  CRAWL_PAUSED: 'crawl.paused',
  CRAWL_AWAITING_REVIEW: 'crawl.awaiting_review',
  CRAWL_RESUMED: 'crawl.resumed',
  SESSION_COMPLETED: 'session.completed',
  SESSION_FAILED: 'session.failed',
};

export const FEEDBACK_TYPES = {
  YES: 'yes',
  NO: 'no',
};
