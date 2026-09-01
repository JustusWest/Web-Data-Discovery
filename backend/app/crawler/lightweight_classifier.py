from __future__ import annotations

from dataclasses import dataclass

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline


@dataclass
class LightweightRelevanceClassifier:
    """Simple TF-IDF + Logistic Regression classifier for low-latency relevance checks."""

    model_name: str = "tfidf_logreg_v1"

    def __post_init__(self) -> None:
        self._pipeline = Pipeline(
            steps=[
                (
                    "tfidf",
                    TfidfVectorizer(
                        ngram_range=(1, 2),
                        max_features=6000,
                        min_df=2,
                        strip_accents="unicode",
                    ),
                ),
                (
                    "clf",
                    LogisticRegression(
                        max_iter=300,
                        solver="liblinear",
                        class_weight="balanced",
                    ),
                ),
            ]
        )
        self._is_fit = False

    @property
    def is_fit(self) -> bool:
        return self._is_fit

    def fit(self, texts: list[str], labels: list[int]) -> None:
        if not texts:
            raise ValueError("No training texts provided")
        if len(texts) != len(labels):
            raise ValueError("texts and labels must have the same length")
        if len(set(labels)) < 2:
            raise ValueError("Need at least two classes to train")
        self._pipeline.fit(texts, labels)
        self._is_fit = True

    def predict(self, text: str) -> tuple[bool, float]:
        if not self._is_fit:
            raise RuntimeError("Lightweight classifier is not trained yet")
        probs = self._pipeline.predict_proba([text])[0]
        positive = float(probs[1]) if len(probs) > 1 else float(probs[0])
        return positive >= 0.5, positive
