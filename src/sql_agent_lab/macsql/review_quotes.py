"""Presentation-only quote normalization; never fuzzy-match facts or SQL edits."""
import re


def normalize_quote(text):
    # Preserve case, numbers, negation and punctuation with semantic meaning.
    text = text.translate(str.maketrans({'‘': "'", '’': "'", '“': '"', '”': '"'}))
    return re.sub(r'\s+', ' ', text).strip()


def quote_in_source(quote, source):
    needle = normalize_quote(quote)
    if not needle:
        return False
    pattern = (r'(?<!\w)' if needle[0].isalnum() else '') + re.escape(needle)
    pattern += r'(?!\w)' if needle[-1].isalnum() else ''
    return re.search(pattern, normalize_quote(source)) is not None
