"""Deterministic lexical retrieval: tokenizer + BM25 (V2.0 T3.4).

Stdlib only, held in RAM, rebuilt from decrypted ``active`` rows. No embeddings, no vector
store, no on-disk index. At V.O.I.D's scale (hundreds to low thousands of short items) this
is milliseconds; add anything heavier only if a measured recall failure justifies it.

Determinism: identical inputs always give identical output - ties are broken by recency
then id, and nothing depends on hash ordering, wall-clock time or randomness.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

_STOP = frozenset("""a an the is are am was were be been being to of in on for and or but that this these those
it its as at by from with about into over under my me i we you your our us he she they them his her their
what who whom which when where why how do does did done have has had will would can could should shall may
might must not no yes so if then than there here just also very""".split())

_ACRONYM = re.compile(r"\b(?:[A-Za-z]\.){2,}[A-Za-z]?")     # "V.O.I.D" -> "VOID"
_WORD = re.compile(r"[a-z0-9]+")
_SUFFIXES = ("ingly", "edly", "ing", "ed", "es", "s", "ly")


def _stem(word: str) -> str:
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 3 and not word.endswith("ss"):
            return word[: -len(suf)]
    return word


def tokenize(text: str) -> list[str]:
    text = _ACRONYM.sub(lambda m: m.group().replace(".", ""), text or "")
    out = []
    for w in _WORD.findall(text.lower()):
        if w in _STOP or (len(w) < 2 and not w.isdigit()):
            continue
        out.append(_stem(w))
    return out


class BM25Index:
    """Okapi BM25 over a fixed set of documents (``id -> tokens``)."""

    def __init__(self, docs: dict[str, list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self._tf = {i: Counter(t) for i, t in docs.items()}
        self._len = {i: len(t) for i, t in docs.items()}
        self.n = len(docs)
        self._avg = (sum(self._len.values()) / self.n) if self.n else 0.0
        df: Counter = Counter()
        for tf in self._tf.values():
            df.update(tf.keys())
        self._idf = {t: math.log(1.0 + (self.n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    def score(self, query_tokens: Iterable[str]) -> dict[str, float]:
        """BM25 score per document with at least one matching term (non-matches omitted)."""
        terms = sorted(set(query_tokens))
        scores: dict[str, float] = {}
        for doc_id, tf in self._tf.items():
            s = 0.0
            dl = self._len[doc_id]
            for t in terms:
                f = tf.get(t)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * (dl / self._avg if self._avg else 1.0))
                s += self._idf.get(t, 0.0) * (f * (self.k1 + 1)) / denom
            if s > 0.0:
                scores[doc_id] = s
        return scores
