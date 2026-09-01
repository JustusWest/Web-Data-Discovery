import { createLiveCrawlerClient } from './createLiveCrawlerClient';
import { createMockCrawlerClient } from './createMockCrawlerClient';

export function createCrawlerClient(runtimeConfig) {
  if (runtimeConfig.clientMode === 'live') {
    return createLiveCrawlerClient(runtimeConfig);
  }

  return createMockCrawlerClient(runtimeConfig);
}
