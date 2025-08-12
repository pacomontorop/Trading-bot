import os
import re
from typing import Dict, Iterable, List

try:
    import praw
except Exception as exc:  # pragma: no cover - optional dependency
    praw = None

REDDIT_ID = os.getenv("REDDIT_ID")
REDDIT_SECRET = os.getenv("REDDIT_SECRET")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT")

TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")
STOP_WORDS = {"FDA", "CEO", "SEC", "USD", "GDP", "FBI"}


def _get_client() -> "praw.Reddit":
    if praw is None:
        raise RuntimeError("praw is required for reddit scraping")
    if not (REDDIT_ID and REDDIT_SECRET and REDDIT_USER_AGENT):
        raise RuntimeError("Reddit API credentials not set")
    return praw.Reddit(
        client_id=REDDIT_ID,
        client_secret=REDDIT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )


def extract_tickers(text: str) -> List[str]:
    """Return potential ticker symbols found in ``text``."""
    tickers = {m.group(0).upper() for m in TICKER_RE.finditer(text or "")}
    return [t for t in tickers if t not in STOP_WORDS]


def get_reddit_sentiment(ticker: str, subreddits: Iterable[str] = ("wallstreetbets", "stocks"), limit: int = 50) -> float:
    """Compute a simple sentiment score based on Reddit mentions.

    The score is based on upvote-to-comment ratio for posts mentioning
    ``ticker`` across the given ``subreddits``. Result is clamped to [-1, 1].
    """
    reddit = _get_client()
    ticker = ticker.upper()
    scores: List[float] = []
    for sub in subreddits:
        try:
            for submission in reddit.subreddit(sub).search(ticker, limit=limit, sort="new"):
                text = f"{submission.title} {submission.selftext}"
                if ticker in extract_tickers(text):
                    ups = submission.score
                    comments = submission.num_comments or 1
                    scores.append(ups / comments)
        except Exception:
            continue
    if not scores:
        return 0.0
    avg = sum(scores) / len(scores)
    normalized = max(min(avg / 100, 1), -1)  # heuristic normalization
    return normalized


def subreddit_ticker_counts(subreddit: str, limit: int = 100, time_filter: str = "day") -> Dict[str, int]:
    """Return a count of tickers mentioned in top posts of ``subreddit``."""
    reddit = _get_client()
    counts: Dict[str, int] = {}
    try:
        for submission in reddit.subreddit(subreddit).top(time_filter=time_filter, limit=limit):
            tickers = extract_tickers(f"{submission.title} {submission.selftext}")
            for t in tickers:
                counts[t] = counts.get(t, 0) + 1
    except Exception:
        return counts
    return counts
