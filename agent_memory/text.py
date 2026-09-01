"""Text utilities: chunking, tag handling, FTS query building, token estimates."""

from __future__ import annotations

import re

# Terms that match nearly every document and only dilute ranking.
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "did", "do", "does",
    "for", "from", "had", "has", "have", "how", "i", "if", "in", "is", "it",
    "its", "of", "on", "or", "our", "so", "than", "that", "the", "their",
    "then", "there", "these", "they", "this", "to", "was", "we", "were", "what",
    "when", "where", "which", "who", "why", "will", "with", "you", "your",
}


def chunk_text(text: str, chunk_size: int = 900, overlap: int = 120) -> list[str]:
    """Split text on paragraph boundaries, falling back to a sliding window."""
    text = text.strip()
    if not text:
        return []

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(paragraph) <= chunk_size:
            current = paragraph
            continue

        start = 0
        while start < len(paragraph):
            stop = min(len(paragraph), start + chunk_size)
            chunks.append(paragraph[start:stop].strip())
            if stop >= len(paragraph):
                break
            start = max(stop - overlap, start + 1)
        current = ""

    if current:
        chunks.append(current)

    return chunks


def normalize_tags(tags: list[str]) -> list[str]:
    cleaned: list[str] = []
    for tag in tags:
        value = tag.strip().lower()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def build_fts_query(query: str, *, max_terms: int = 16) -> str | None:
    """Build an FTS5 MATCH expression that ORs terms instead of ANDing them.

    The original implementation joined quoted tokens with spaces, which FTS5
    reads as implicit AND: a natural-language question only matched documents
    containing *every* word, so realistic queries returned nothing. ORing lets
    bm25 rank by how many (and how rare) the matched terms are.

    Returns None when the query has no searchable terms.
    """
    tokens = re.findall(r"[A-Za-z0-9_./:@-]+", query)
    terms: list[str] = []
    for token in tokens:
        value = token.strip(".:/-").lower()
        if len(value) < 2 or value in STOPWORDS:
            continue
        if value not in terms:
            terms.append(value)
        if len(terms) >= max_terms:
            break

    if not terms:
        # Every token was a stopword or too short; fall back to the raw
        # alphanumerics so an all-stopword query still does something sane.
        terms = [t.lower() for t in tokens if t.strip()][:max_terms]
    if not terms:
        return None

    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token). Used for budgeting, not billing."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def truncate_tokens(text: str, max_tokens: int) -> str:
    limit = max_tokens * 4
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def query_terms(query: str, *, max_terms: int = 16) -> list[str]:
    """The significant terms of a query, used for absolute term-coverage scoring."""
    tokens = re.findall(r"[A-Za-z0-9_./:@-]+", query)
    terms: list[str] = []
    for token in tokens:
        value = token.strip(".:/-").lower()
        if len(value) < 2 or value in STOPWORDS:
            continue
        if value not in terms:
            terms.append(value)
        if len(terms) >= max_terms:
            break
    return terms


def term_coverage(terms: list[str], text: str) -> float:
    """Fraction of query terms present in the text.

    This is an *absolute* relevance signal, unlike BM25 or RRF which only rank
    results against each other. The relevance floor needs an absolute measure:
    otherwise the best of a set of irrelevant results always looks relevant.
    """
    if not terms:
        return 0.0
    haystack = text.lower()
    hits = sum(1 for term in terms if term in haystack)
    return hits / len(terms)
