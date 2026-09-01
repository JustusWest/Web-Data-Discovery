const MOCK_DOMAINS = [
  'techcrunch.com',
  'arxiv.org',
  'github.com',
  'medium.com',
  'stackoverflow.com',
  'nytimes.com',
  'reddit.com',
  'ycombinator.com',
];

const MOCK_TITLES = [
  'Understanding Transformer Architectures in 2024',
  'Optimizing React Performance with Concurrent Mode',
  'The Future of Generative AI in Healthcare',
  'A Deep Dive into Distributed Systems',
  'Rust vs Go: A Systems Programming Showdown',
  'Ethical Considerations in Large Language Model Deployment',
  'Quantum Computing: Practical Applications for Developers',
  'Kubernetes Networking Explained Simply',
];

function getDomainPool(domainFilter) {
  const allowedDomains = domainFilter
    .split(',')
    .map((domain) => domain.trim().toLowerCase())
    .filter(Boolean);

  if (allowedDomains.length === 0) {
    return MOCK_DOMAINS;
  }

  const matchingDomains = MOCK_DOMAINS.filter((mockDomain) =>
    allowedDomains.some((allowed) => mockDomain.includes(allowed)),
  );

  return matchingDomains.length ? matchingDomains : MOCK_DOMAINS;
}

export function generateMockResult({ id, topic, minRelevance, domainFilter, maxDepth }) {
  const domainPool = getDomainPool(domainFilter);
  const domain = domainPool[Math.floor(Math.random() * domainPool.length)];
  const title = MOCK_TITLES[Math.floor(Math.random() * MOCK_TITLES.length)];

  const threshold = Math.min(0.95, Math.max(0.5, minRelevance));
  const lowerBandMin = Math.max(0.3, threshold - 0.25);
  const upperBandMax = Math.min(0.99, threshold + 0.25);
  const shouldBePositive = Math.random() >= 0.45;
  const relevanceScore = Number(
    (
      shouldBePositive
        ? Math.random() * (upperBandMax - threshold) + threshold
        : Math.random() * (threshold - lowerBandMin) + lowerBandMin
    ).toFixed(2),
  );

  const seededFeedback = relevanceScore >= threshold ? 'yes' : 'no';

  return {
    id,
    url: `https://${domain}/article/${Math.floor(Math.random() * 10000)}`,
    domain,
    title,
    snippet: `This page appears relevant to "${topic || 'your topic'}". It discusses practical methods, tradeoffs, and examples that could guide the next crawl steps at depth ${maxDepth}.`,
    relevanceScore,
    timestamp: new Date().toLocaleTimeString(),
    feedback: seededFeedback,
    notes: '',
    feedbackSubmitted: false,
    feedbackSubmittedAt: null,
    status: 'analyzing',
  };
}
