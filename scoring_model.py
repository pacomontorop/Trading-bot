import os
import pandas as pd
import numpy as np
from types import SimpleNamespace

# Fallback implementation if joblib is unavailable
try:
    import joblib  # type: ignore
except Exception:  # pragma: no cover - only if joblib missing
    import pickle

    class _SimpleJoblib(SimpleNamespace):
        @staticmethod
        def dump(obj, file):
            with open(file, "wb") as f:
                pickle.dump(obj, f)

        @staticmethod
        def load(file):
            with open(file, "rb") as f:
                return pickle.load(f)

    joblib = _SimpleJoblib()


class LogisticRegressionGD:
    """Simple logistic regression using gradient descent."""

    def __init__(self, lr=0.01, n_iter=1000):
        self.lr = lr
        self.n_iter = n_iter
        self.coef_ = None
        self.intercept_ = None
        self.theta_ = None

    @staticmethod
    def _sigmoid(z):
        return 1.0 / (1.0 + np.exp(-z))

    def fit(self, X, y):
        X = np.c_[np.ones(X.shape[0]), X]  # add intercept
        self.theta_ = np.zeros(X.shape[1])
        for _ in range(self.n_iter):
            scores = np.dot(X, self.theta_)
            predictions = self._sigmoid(scores)
            gradient = np.dot(X.T, predictions - y) / y.size
            self.theta_ -= self.lr * gradient
        self.intercept_ = self.theta_[0]
        self.coef_ = self.theta_[1:]
        return self

    def predict_proba(self, X):
        X = np.c_[np.ones(X.shape[0]), X]
        scores = np.dot(X, self.theta_)
        probs = self._sigmoid(scores)
        return np.vstack([1 - probs, probs]).T

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


def train_and_save_model(csv_path="data/trades.csv", model_path="scoring_model.pkl"):
    """Train logistic regression on trades and persist model."""
    df = pd.read_csv(csv_path)
    feature_cols = [
        col
        for col in ["score_quiver", "active_signals", "market_cap", "volume"]
        if col in df.columns
    ]
    if not feature_cols:
        raise ValueError("No se encontraron columnas de características")
    X = df[feature_cols].fillna(0).values
    if "win" in df.columns:
        y = df["win"].values
    else:
        y = (df["pnl_usd"] > 0).astype(int).values

    model = LogisticRegressionGD(lr=0.01, n_iter=5000)
    model.fit(X, y)
    joblib.dump((model, feature_cols), model_path)
    print("Coeficientes:", dict(zip(feature_cols, model.coef_)))
    return model, feature_cols


def predict_win_proba(features_dict, model_path="scoring_model.pkl"):
    """Return win probability for a dict of features."""
    model, feature_cols = joblib.load(model_path)
    X = np.array([[features_dict.get(col, 0) for col in feature_cols]])
    proba = model.predict_proba(X)[0, 1]
    return float(proba)


if __name__ == "__main__":
    if os.path.exists("data/trades.csv"):
        train_and_save_model()
    else:
        print("data/trades.csv no encontrado")
