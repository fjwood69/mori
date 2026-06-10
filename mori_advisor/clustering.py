"""Deterministic, embedding-free clustering for the Trusted-Dreamer review roll-up.

Groups near-duplicate review candidates so the TD can dispose of a *convention*
once instead of N times. This is a REVIEW-SIDE presentation affordance only:
it never changes what the dreamer emits (recall stays intact) and never
auto-merges (the TD still disposes) — it only changes how candidates are grouped
when shown.

Signal: the convention key. The dreamer is told "same convention -> same path",
but it imperfectly prefixes conventions with a module/instance segment
(`lineup4-game-state-contract`, `greedy-pig-game-state-contract`). Those share a
distinctive trailing suffix (`game-state-contract`), so candidates are grouped by
the longest trailing hyphen-suffix they share with at least one other candidate.

Exact-duplicate handling lives upstream (intake `content_hash` UNIQUE +
reinforcement_count); this module is the *near*-dup layer. Embeddings are
deliberately deferred until this proves too coarse (Fable's "path-prefix first").
"""

from __future__ import annotations

# A shared suffix must be at least this many hyphen-segments and characters to
# count as a convention — guards against over-merging on a single generic tail
# segment (e.g. every "*-contract" collapsing together).
_MIN_SUFFIX_SEGMENTS = 2
_MIN_SUFFIX_CHARS = 10


def _basis(candidate: dict, name_field: str = "name", path_field: str = "path") -> str:
    """The kebab string a candidate is clustered on.

    Prefer the explicit name; fall back to the last segment of a hierarchical
    `path` (the dreamer schema), else empty.
    """
    name = (candidate.get(name_field) or "").strip()
    if name:
        return name
    path = (candidate.get(path_field) or "").strip().strip("/")
    return path.rsplit("/", 1)[-1] if path else ""


def _trailing_suffixes(key: str) -> list[str]:
    """All trailing hyphen-suffixes of `key` meeting the segment/char floor,
    longest first (so the most specific shared convention wins)."""
    segs = [s for s in key.split("-") if s]
    out = []
    for start in range(len(segs) - _MIN_SUFFIX_SEGMENTS + 1):
        suffix = "-".join(segs[start:])
        if len(suffix) >= _MIN_SUFFIX_CHARS:
            out.append(suffix)
    return out  # already longest-first (smallest start index = longest suffix)


def cluster_keys(keys: list[str]) -> dict[str, str]:
    """Map each input key to its cluster key.

    A key's cluster is the LONGEST trailing suffix it shares with at least one
    other key; if it shares none, it is its own singleton cluster (the key
    itself). Pure and order-independent.
    """
    # Count how many distinct keys carry each candidate suffix.
    suffix_carriers: dict[str, set[str]] = {}
    for k in keys:
        for suf in set(_trailing_suffixes(k)):
            suffix_carriers.setdefault(suf, set()).add(k)

    out: dict[str, str] = {}
    for k in keys:
        chosen = k  # default: singleton
        for suf in _trailing_suffixes(k):  # longest-first
            if len(suffix_carriers.get(suf, ())) >= 2:
                chosen = suf
                break
        out[k] = chosen
    return out


def roll_up(
    candidates: list[dict], name_field: str = "name", path_field: str = "path"
) -> list[dict]:
    """Group candidates into review clusters.

    Returns a list of clusters, each: ``{"cluster_key", "size", "members"}``,
    sorted by size desc then key. Singletons (size 1) are included so callers
    can render them as ordinary rows. Members preserve input order.
    """
    bases = [_basis(c, name_field, path_field) for c in candidates]
    keymap = cluster_keys([b for b in bases if b])

    clusters: dict[str, list[dict]] = {}
    for cand, base in zip(candidates, bases):
        ck = keymap.get(base, base) if base else (base or "")
        clusters.setdefault(ck, []).append(cand)

    result = [
        {"cluster_key": ck, "size": len(members), "members": members}
        for ck, members in clusters.items()
    ]
    result.sort(key=lambda c: (-c["size"], c["cluster_key"]))
    return result


def cluster_index(
    candidates: list[dict],
    id_field: str = "name",
    name_field: str = "name",
    path_field: str = "path",
    min_size: int = 2,
) -> list[dict]:
    """Lightweight roll-up for API responses: ``[{cluster_key, size, members}]``
    where ``members`` are member *identifiers* (``id_field``), not full payloads.

    Returns only clusters of at least ``min_size`` — i.e. the actual roll-ups;
    singletons are omitted so the caller renders them as ordinary rows. The full
    candidate data stays in the endpoint's flat list; this is just the grouping.
    """
    return [
        {
            "cluster_key": g["cluster_key"],
            "size": g["size"],
            "members": [m.get(id_field) for m in g["members"]],
        }
        for g in roll_up(candidates, name_field, path_field)
        if g["size"] >= min_size
    ]
