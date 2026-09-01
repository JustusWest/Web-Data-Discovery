import { AlertTriangle, X } from 'lucide-react';
import React from 'react';

export function SessionErrorBanner({ message, onDismiss }) {
  if (!message) return null;

  return (
    <div className="error-banner" role="alert">
      <span>
        <AlertTriangle size={16} />
        {message}
      </span>
      <button type="button" onClick={onDismiss} aria-label="Dismiss error">
        <X size={14} />
      </button>
    </div>
  );
}
