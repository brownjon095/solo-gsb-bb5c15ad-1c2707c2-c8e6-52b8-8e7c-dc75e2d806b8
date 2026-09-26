"""RFC 6901-style path-pattern rules for bulk conflict resolution.

Each rule carries up to two glob-like patterns over JSON Pointer paths:

  * ``left``  — conflicts matched by this pattern take the left candidate
  * ``right`` — conflicts matched by this pattern take the right candidate

Pattern syntax (paths look like RFC 6901 pointers, ``~`` -> ``~0`` etc.):

  * ``/db/host``   literal path (``""`` names the document root)
  * ``/svc/*``     a single ``*`` segment matches exactly one path segment
  * ``/svc/**``    a trailing ``**`` matches zero or more remaining segments

``*`` / ``**`` are only recognised as whole segments; ``**`` is only valid
as the final segment. Every open conflict is assigned the unique best rule:
the match with more literal segments wins, then fewer ``*`` characters
(a single ``*`` beats ``**``), then the earlier declared rule, then the
``left`` pattern before ``right``.

Everything here is pure and in-memory; the caller decides whether the
outcome is persisted.
"""

from __future__ import annotations

from dataclasses import dataclass

from .merge import delete_at, parse_pointer, set_at, unescape

SINGLE_WILDCARD = "*"
MULTI_WILDCARD = "**"


class InvalidPattern(ValueError):
    """A path pattern is syntactically invalid."""


class InvalidRuleSet(ValueError):
    """The ordered rule list itself is malformed."""


class RuleApplicationFailed(Exception):
    """A selected rule cannot be applied to the working document."""

    def __init__(self, pointer: str, reason: str):
        self.pointer = pointer
        self.reason = reason
        super().__init__(f"cannot apply rule at {pointer!r}: {reason}")


@dataclass(frozen=True)
class CompiledPattern:
    source: str
    # Literal segment strings, or SINGLE_WILDCARD / MULTI_WILDCARD markers.
    segments: tuple[str, ...]

    @property
    def literal_count(self) -> int:
        return sum(
            1
            for seg in self.segments
            if seg != SINGLE_WILDCARD and seg != MULTI_WILDCARD
        )

    @property
    def star_count(self) -> int:
        """Number of ``*`` characters: one for a '*' segment, two for '**'."""
        return sum(
            1 if seg == SINGLE_WILDCARD else 2 if seg == MULTI_WILDCARD else 0
            for seg in self.segments
        )


@dataclass(frozen=True)
class CompiledRule:
    index: int
    left: CompiledPattern | None
    right: CompiledPattern | None

    def pattern(self, side: str) -> CompiledPattern | None:
        return self.left if side == "left" else self.right


@dataclass(frozen=True)
class Winner:
    """The unique rule/side selected for one open conflict."""

    pointer: str
    tokens: tuple[str, ...]
    rule_index: int
    side: str
    pattern: str


def compile_pattern(pattern: str) -> CompiledPattern:
    """Parse and validate one RFC 6901-style path pattern."""
    if not isinstance(pattern, str):
        raise InvalidPattern(f"path pattern must be a string, got {type(pattern).__name__}")
    if pattern == "":
        return CompiledPattern("", ())
    if not pattern.startswith("/"):
        raise InvalidPattern(f"path pattern {pattern!r} must start with '/'")
    raw_tokens = pattern.split("/")[1:]
    segments: list[str] = []
    for position, raw in enumerate(raw_tokens):
        token = unescape(raw)
        last = position == len(raw_tokens) - 1
        if token == MULTI_WILDCARD:
            if not last:
                raise InvalidPattern(
                    f"path pattern {pattern!r}: '**' is only allowed as the final segment"
                )
            segments.append(MULTI_WILDCARD)
        elif token == SINGLE_WILDCARD:
            segments.append(SINGLE_WILDCARD)
        elif SINGLE_WILDCARD in token or MULTI_WILDCARD in token:
            raise InvalidPattern(
                f"path pattern {pattern!r}: a wildcard must be a whole segment "
                f"('*' or a trailing '**'), got {token!r}"
            )
        else:
            segments.append(token)
    return CompiledPattern(pattern, tuple(segments))


def compile_rules(rules: list[dict]) -> list[CompiledRule]:
    """Compile the ordered request rules, rejecting any invalid one."""
    if not rules:
        raise InvalidRuleSet("rules must not be empty")
    compiled: list[CompiledRule] = []
    for index, rule in enumerate(rules):
        left = compile_pattern(rule["left"]) if rule.get("left") is not None else None
        right = compile_pattern(rule["right"]) if rule.get("right") is not None else None
        if left is None and right is None:
            raise InvalidRuleSet(
                f"rule at index {index} needs at least one of 'left' / 'right'"
            )
        compiled.append(CompiledRule(index, left, right))
    return compiled


def pattern_matches(pattern: CompiledPattern, tokens: tuple[str, ...]) -> bool:
    """Whether a compiled pattern matches a concrete conflict pointer."""
    segments = pattern.segments
    i = 0
    for seg in segments:
        if seg == MULTI_WILDCARD:
            # Always the final segment: consumes zero or more remainder.
            return True
        if i >= len(tokens):
            return False
        if seg != SINGLE_WILDCARD and seg != tokens[i]:
            return False
        i += 1
    return i == len(tokens)


def plan_resolutions(
    conflicts: list[dict], rules: list[CompiledRule]
) -> list[Winner]:
    """Select the unique winning rule/side for every conflict it matches.

    Conflicts without any matching rule are simply absent from the result.
    """
    winners: list[Winner] = []
    for conflict in conflicts:
        pointer = conflict["pointer"]
        tokens = tuple(parse_pointer(pointer))
        best: Winner | None = None
        best_rank: tuple | None = None
        for rule in rules:
            for side_order, side in enumerate(("left", "right")):
                pattern = rule.pattern(side)
                if pattern is None or not pattern_matches(pattern, tokens):
                    continue
                # Larger rank wins: more literals, fewer '*' characters,
                # earlier rule, then 'left' (side_order 0) before 'right'.
                rank = (
                    pattern.literal_count,
                    -pattern.star_count,
                    -rule.index,
                    -side_order,
                )
                if best_rank is None or rank > best_rank:
                    best_rank = rank
                    best = Winner(pointer, tokens, rule.index, side, pattern.source)
        if best is not None:
            winners.append(best)
    return winners


def group_matches(winners: list[Winner]) -> list[dict]:
    """Group hit pointers by (rule, side), ordered by rule then left/right."""
    buckets: dict[tuple[int, str], list[str]] = {}
    for winner in winners:
        key = (winner.rule_index, winner.side)
        buckets.setdefault(key, []).append(winner.pointer)
    groups: list[dict] = []
    for rule_index in sorted({key[0] for key in buckets}):
        for side in ("left", "right"):
            pointers = buckets.get((rule_index, side))
            if pointers is not None:
                groups.append(
                    {
                        "rule_index": rule_index,
                        "side": side,
                        "pointers": sorted(pointers),
                    }
                )
    return groups


def apply_plan(
    document, conflicts: list[dict], winners: list[Winner]
) -> tuple[object, list[dict]]:
    """Apply all selected winners in memory and return (document, remaining).

    Raises RuleApplicationFailed if any single selection cannot be applied;
    the caller performs no persistence when that happens, so no partial
    modification is visible.
    """
    open_conflicts = {c["pointer"]: c for c in conflicts}
    chosen: set[str] = set()
    for winner in winners:
        if winner.pointer in chosen:
            # Defensive: plan_resolutions yields one winner per conflict.
            raise RuleApplicationFailed(
                winner.pointer, "conflict selected by more than one rule"
            )
        if winner.pointer not in open_conflicts:
            raise RuleApplicationFailed(winner.pointer, "no open conflict at pointer")
        chosen.add(winner.pointer)

    # Pointer order only matters when arrays are involved; conflict pointers
    # are otherwise disjoint. A deterministic order keeps results stable.
    for winner in sorted(winners, key=lambda w: w.pointer):
        conflict = open_conflicts[winner.pointer]
        try:
            if winner.side in conflict["missing"]:
                # The chosen side deleted the key: honour the deletion.
                if not winner.tokens:
                    raise ValueError(
                        "the chosen side is missing: the document root cannot be deleted"
                    )
                document = delete_at(document, list(winner.tokens))
            else:
                document = set_at(document, list(winner.tokens), conflict[winner.side])
        except RuleApplicationFailed:
            raise
        except Exception as exc:  # structural failure applying the selection
            raise RuleApplicationFailed(winner.pointer, str(exc)) from exc

    remaining = [c for c in conflicts if c["pointer"] not in chosen]
    return document, remaining
