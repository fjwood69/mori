"""Unit tests for the flat scope-filter primitive (mori_advisor.scope)."""

from mori_advisor.scope import ScopeMap, in_scope

# ── ScopeMap.parse ───────────────────────────────────────────────────────────


def test_parse_none_and_empty_are_global():
    assert ScopeMap.parse(None).is_global
    assert ScopeMap.parse("").is_global


def test_parse_json_string():
    s = ScopeMap.parse('{"tags": ["repo:bifrost", "lang:go"], "match": "all"}')
    assert s.tags == frozenset({"repo:bifrost", "lang:go"})
    assert s.match == "all"


def test_parse_dict():
    s = ScopeMap.parse({"tags": ["project:mori"], "match": "any"})
    assert s.tags == frozenset({"project:mori"})
    assert s.match == "any"


def test_parse_malformed_json_is_global():
    assert ScopeMap.parse("{not json").is_global
    assert ScopeMap.parse("[1,2,3]").is_global  # not a dict
    assert ScopeMap.parse(12345).is_global  # not str/dict


def test_parse_bad_match_defaults_to_any():
    s = ScopeMap.parse({"tags": ["x"], "match": "xor"})
    assert s.match == "any"


def test_parse_empty_tags_is_global():
    assert ScopeMap.parse({"tags": [], "match": "any"}).is_global
    assert ScopeMap.parse({"match": "any"}).is_global  # tags absent


# ── in_scope: the flat decision ──────────────────────────────────────────────


def test_global_always_in_scope():
    assert in_scope(ScopeMap(), set())
    assert in_scope(ScopeMap(), {"anything"})


def test_match_any():
    s = ScopeMap(tags=frozenset({"repo:a", "repo:b"}), match="any")
    assert in_scope(s, {"repo:a"})  # shares one
    assert in_scope(s, {"repo:b", "lang:go"})  # shares one
    assert not in_scope(s, {"repo:c"})  # shares none
    assert not in_scope(s, set())  # empty context, non-global scope -> out


def test_match_all():
    s = ScopeMap(tags=frozenset({"repo:a", "lang:go"}), match="all")
    assert in_scope(s, {"repo:a", "lang:go", "extra"})  # superset -> in
    assert not in_scope(s, {"repo:a"})  # missing lang:go -> out
    assert not in_scope(s, set())


def test_accepts_list_context():
    s = ScopeMap(tags=frozenset({"x"}), match="any")
    assert in_scope(s, ["x", "y"])  # list is fine, coerced to set
    assert not in_scope(s, ["y", "z"])


def test_scopemap_is_hashable():
    a = ScopeMap(tags=frozenset({"x"}))
    b = ScopeMap(tags=frozenset({"x"}))
    assert a == b
    assert len({a, b}) == 1  # usable as dict key / in a set
