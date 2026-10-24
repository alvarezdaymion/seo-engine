"""Text helpers shared by matching, drafting and the claim check. Stdlib only."""

from __future__ import annotations

import hashlib
import re

# Words that say nothing about the topic. Used to match search queries to recorded questions.
STOPWORDS = frozenset("""
a about after again am an and any are as at be because been before being but by can could did do
does doing down during each for from further had has have having he her here him his how i i'd i'll
i'm i've if in into is it it's its itself just me my of off on once or our out over own she should so
some such than that that's the their them then there these they this those through to too under until
up very was we were what when where which while who whom why will with would you you'll you're your
""".split())

# The only words a model draft may add that are not in the sentences it cites. Everything else
# (verbs, nouns, numbers, negations, modals, prepositions like "on" or "before") must come from them.
GLUE = frozenset("""
a an the and but or so it its it's this that that's these those they them their you your we our
i me my he she him her his is are was were be been being do does did have has had of to
""".split())

NEGATIONS = frozenset("""
no not never none nor nothing cannot can't don't doesn't didn't isn't aren't wasn't weren't won't
wouldn't shouldn't couldn't haven't hasn't hadn't without
""".split())

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")
_FILLER = re.compile(r"\b(?:um+|uh+|uhm|erm|hmm+)\b[,.]?\s*", re.I)
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def clean(text: str) -> str:
    """Straight quotes and single spaces."""
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(text.split())


def paragraphs(text: str) -> list[str]:
    return [clean(p) for p in re.split(r"\n\s*\n", text) if p.strip()]


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(clean(text)) if s]


def tidy(sentence: str) -> str:
    """Drop filler sounds ("um", "uh") and re-capitalize. Never changes any other word."""
    s = clean(_FILLER.sub("", sentence))
    return s[:1].upper() + s[1:] if s else s


def words(text: str) -> list[str]:
    return _WORD.findall(clean(text).lower())


def stem(word: str) -> str:
    if word.endswith("'s"):
        word = word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    return word


def content_words(text: str) -> set[str]:
    """Topic words, for matching a search query to a recorded question."""
    return {stem(w) for w in words(text) if w not in STOPWORDS}


def claim_words(text: str) -> set[str]:
    """Every word except grammatical glue, for checking a draft against its source sentences."""
    return {stem(w) for w in words(text) if w not in GLUE}


def has_negation(text: str) -> bool:
    return any(w in NEGATIONS for w in words(text))


def dice(a: set[str], b: set[str]) -> float:
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def phrase_in(phrase: str, text: str) -> bool:
    """Whole-word, case-insensitive containment that ignores punctuation."""
    needle = " ".join(words(phrase))
    return bool(needle) and (" %s " % needle) in (" %s " % " ".join(words(text)))


def slugify(text: str, max_len: int = 80) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", clean(text).lower().replace("'", "")).strip("-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit("-", 1)[0]
    return slug


def is_slug(value: str) -> bool:
    return bool(_SLUG.match(value or ""))


def sha256(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()
