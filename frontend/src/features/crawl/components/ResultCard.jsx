import {
  ExternalLink,
  MessageSquare,
  ThumbsDown,
  ThumbsUp,
} from 'lucide-react';
import React from 'react';
import { FEEDBACK_TYPES } from '../clients/crawlerClientContract';

export function ResultCard({ data, onFeedback, onNotes, onSubmitFeedback }) {
  const statusClass = data.status === 'analyzing' ? 'is-analyzing' : 'is-ready';
  const canSubmit = Boolean(data.feedback) || data.notes.trim().length > 0;

  return (
    <article className="result-card">
      <div className="result-head">
        <div className="result-meta-row">
          <span className="domain-pill">{data.domain}</span>
          <span className="score-pill">score {data.relevanceScore.toFixed(2)}</span>
          <span className={`status-pill ${statusClass}`}>{data.status}</span>
          <span className="time-pill">{data.timestamp}</span>
        </div>

        <h3>{data.title}</h3>
        <a href={data.url} target="_blank" rel="noreferrer">
          {data.url}
          <ExternalLink size={12} />
        </a>
      </div>

      <p className="snippet">{data.snippet}</p>
      <p className="model-reason">
        <strong>Model reasoning:</strong> {data.reason}
      </p>

      <div className="feedback-row">
        <div className="binary-feedback" role="group" aria-label="binary relevance feedback">
          <button
            type="button"
            className={data.feedback === FEEDBACK_TYPES.YES ? 'active yes' : ''}
            onClick={() => onFeedback(data.id, FEEDBACK_TYPES.YES)}
          >
            <ThumbsUp size={15} />
            Yes
          </button>
          <button
            type="button"
            className={data.feedback === FEEDBACK_TYPES.NO ? 'active no' : ''}
            onClick={() => onFeedback(data.id, FEEDBACK_TYPES.NO)}
          >
            <ThumbsDown size={15} />
            No
          </button>
        </div>

        <label className="notes-label" htmlFor={`notes-${data.id}`}>
          <MessageSquare size={14} /> Notes for model guidance
        </label>
        <textarea
          id={`notes-${data.id}`}
          value={data.notes}
          onChange={(event) => onNotes(data.id, event.target.value)}
          placeholder="Explain why this page should or should not guide future crawl decisions..."
          rows={3}
        />
        <div className="feedback-actions">
          {data.feedbackSubmittedAt && (
            <span className="feedback-submitted-meta">Submitted {data.feedbackSubmittedAt}</span>
          )}
          <button
            type="button"
            className="submit-feedback-button"
            onClick={() => onSubmitFeedback(data.id)}
            disabled={!canSubmit}
          >
            Submit Feedback to Model
          </button>
        </div>
      </div>
    </article>
  );
}
