"""docs/spec.md is executable: its grammar is checked against the lexer and parser, its examples are run.

The ```ebnf blocks of the spec are read here into a recogniser. Each grammar rule then has to agree with the
implementation in both directions:

* sentences generated from the grammar are accepted by the parser, and every rule and alternative is exercised;
* each rule has hand-written negative samples which both the parser and the grammar reject;
* thousands of mutated sentences, and every program in the repository, get the same verdict from both;
* deleting any one alternative from the grammar makes some sentence stop being accepted.

So a rule that changes in only the spec, or only the parser, fails here.
"""

from __future__ import annotations

import io
import json
import pathlib
import random
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from functools import cache

import pytest

from vex import lexer as lexer_module
from vex import parser as parser_module
from vex import runtime
from vex import vm as vm_module
from vex.api import compile_text
from vex.diagnostics import DiagSink, Source, VexError, VexRuntimeError
from vex.host import DEFAULT_BUDGET, MAX_HOST_DEPTH
from vex.lexer import BASE_DIGITS, KEYWORDS, MAX_INT_DIGITS, PUNCT, Lexer, Token, tokenize
from vex.parser import ASSIGN_OPS, BINARY_PREC, BLOCK_LIKE, ITEM_KEYWORDS, parse_source
from vex.runtime import VexExit
from vex.vm import VM

ROOT = pathlib.Path(__file__).parent.parent
SPEC = ROOT / "docs" / "spec.md"
SEED = 20261108


# ------------------------------------------------------------------ reading the EBNF
@dataclass(frozen=True)
class Lit:
    text: str


@dataclass(frozen=True)
class Ref:
    name: str


@dataclass(frozen=True)
class Seq:
    items: tuple[Node, ...]


@dataclass(frozen=True)
class Alt:
    uid: int
    items: tuple[Node, ...]


@dataclass(frozen=True)
class Opt:
    uid: int
    item: Node


@dataclass(frozen=True)
class Rep:
    uid: int
    item: Node


@dataclass(frozen=True)
class Peek:
    """``&x`` (``want`` is True) or ``!x``: test what follows without consuming it."""

    item: Node
    want: bool


Node = Lit | Ref | Seq | Alt | Opt | Rep | Peek
Rules = dict[str, Node]

_EBNF_TOKEN = re.compile(
    r"""\s+|\(\*.*?\*\)|(?P<str>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(?P<name>[A-Za-z_][A-Za-z0-9_]*)|(?P<sym>[=;|\[\]{}()&!])""",
    re.S,
)
_EBNF_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'", "\\": "\\"}


def _unescape(body: str) -> str:
    def one(m: re.Match[str]) -> str:
        c = m.group(1)
        if c.startswith("u"):
            return chr(int(c[1:], 16))
        return _EBNF_ESCAPES[c]

    return re.sub(r"\\(u[0-9A-Fa-f]{4}|.)", one, body)


class EbnfReader:
    """Parses ``name = expression ;`` rules. A counter gives every choice point a stable identity."""

    def __init__(self, text: str, first_uid: int) -> None:
        self.toks: list[tuple[str, str]] = []
        pos = 0
        while pos < len(text):
            m = _EBNF_TOKEN.match(text, pos)
            assert m is not None, f"bad EBNF near {text[pos:pos + 40]!r}"
            pos = m.end()
            if m.lastgroup is not None:
                self.toks.append((m.lastgroup, m.group(m.lastgroup)))
        self.i = 0
        self.uid = first_uid

    def next_uid(self) -> int:
        self.uid += 1
        return self.uid

    def peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, sym: str) -> None:
        got = self.peek()
        assert got == ("sym", sym), f"expected {sym!r} in EBNF, found {got}"
        self.i += 1

    def at(self, sym: str) -> bool:
        return self.peek() == ("sym", sym)

    def rules(self) -> Rules:
        out: Rules = {}
        while self.peek() is not None:
            kind, name = self.toks[self.i]
            assert kind == "name", f"expected a rule name, found {name!r}"
            self.i += 1
            self.take("=")
            assert name not in out, f"rule {name} is defined twice"
            out[name] = self.alt()
            self.take(";")
        return out

    def alt(self) -> Node:
        items = [self.seq()]
        while self.at("|"):
            self.i += 1
            items.append(self.seq())
        return items[0] if len(items) == 1 else Alt(self.next_uid(), tuple(items))

    def seq(self) -> Node:
        items: list[Node] = []
        while True:
            got = self.peek()
            if got is None or got in (("sym", "|"), ("sym", ";"), ("sym", ")"), ("sym", "]"), ("sym", "}")):
                break
            items.append(self.factor())
        assert items, "an EBNF sequence must not be empty"
        return items[0] if len(items) == 1 else Seq(tuple(items))

    def factor(self) -> Node:
        got = self.peek()
        assert got is not None
        kind, text = got
        self.i += 1
        if kind == "str":
            return Lit(_unescape(text[1:-1]))
        if kind == "name":
            return Ref(text)
        if text in ("&", "!"):
            return Peek(self.factor(), text == "&")
        close = {"(": ")", "[": "]", "{": "}"}[text]
        inner = self.alt()
        self.take(close)
        if text == "[":
            return Opt(self.next_uid(), inner)
        if text == "{":
            return Rep(self.next_uid(), inner)
        return inner


@cache
def spec_text() -> str:
    return SPEC.read_text(encoding="utf-8")


def ebnf_blocks(kind: str) -> list[str]:
    return re.findall(rf"```ebnf {kind}\n(.*?)```", spec_text(), re.S)


@cache
def lexical_rules() -> Rules:
    return EbnfReader("\n".join(ebnf_blocks("lexical")), 0).rules()


@cache
def syntax_rules() -> Rules:
    return EbnfReader("\n".join(ebnf_blocks("syntax")), 100_000).rules()


def walk_node(node: Node) -> Iterator[Node]:
    yield node
    if isinstance(node, (Seq, Alt)):
        for item in node.items:
            yield from walk_node(item)
    elif isinstance(node, (Opt, Rep, Peek)):
        yield from walk_node(node.item)


def references(node: Node) -> set[str]:
    return {n.name for n in walk_node(node) if isinstance(n, Ref)}


def literals(node: Node) -> set[str]:
    return {n.text for n in walk_node(node) if isinstance(n, Lit)}


# ------------------------------------------------------------------ the recogniser
Event = tuple[int, int]  # (choice point uid, branch taken), or (-1, index into Matcher.names) for a rule


class Matcher:
    """Interprets rules as a parsing expression grammar: ordered choice, greedy repetition, memoised."""

    def __init__(self, rules: Rules, disabled: Event | None = None) -> None:
        self.rules = rules
        self.memo: dict[tuple[str, int], tuple[int, tuple[Event, ...]]] = {}
        self.events: list[Event] = []
        self.disabled = disabled

    def lit(self, text: str, pos: int) -> int:
        raise NotImplementedError

    def terminal(self, name: str, pos: int) -> int:
        raise NotImplementedError

    def accept_rule(self, name: str, start: int, end: int) -> bool:
        return True

    def match(self, node: Node, pos: int) -> int:
        if isinstance(node, Lit):
            return self.lit(node.text, pos)
        if isinstance(node, Ref):
            return self.match_ref(node.name, pos)
        if isinstance(node, Seq):
            for item in node.items:
                pos = self.match(item, pos)
                if pos < 0:
                    return -1
            return pos
        if isinstance(node, Alt):
            for branch, item in enumerate(node.items):
                if self.disabled == (node.uid, branch):
                    continue
                mark = len(self.events)
                end = self.match(item, pos)
                if end >= 0:
                    self.events.append((node.uid, branch))
                    return end
                del self.events[mark:]
            return -1
        if isinstance(node, Opt):
            mark = len(self.events)
            end = self.match(node.item, pos)
            if end >= 0:
                self.events.append((node.uid, 1))
                return end
            del self.events[mark:]
            self.events.append((node.uid, 0))
            return pos
        if isinstance(node, Rep):
            taken = 0
            while True:
                mark = len(self.events)
                end = self.match(node.item, pos)
                if end < 0 or end == pos:
                    del self.events[mark:]
                    break
                pos = end
                taken = 1
            self.events.append((node.uid, taken))
            return pos
        mark = len(self.events)
        end = self.match(node.item, pos)
        if not (node.want and end >= 0):  # rules seen through `&x` count as used; a failed `!x` used nothing
            del self.events[mark:]
        return pos if (end >= 0) == node.want else -1

    def match_ref(self, name: str, pos: int) -> int:
        rule = self.rules.get(name)
        if rule is None:
            return self.terminal(name, pos)
        key = (name, pos)
        hit = self.memo.get(key)
        if hit is not None:
            self.events.extend(hit[1])
            return hit[0]
        mark = len(self.events)
        end = self.match(rule, pos)
        if end >= 0 and not self.accept_rule(name, pos, end):
            end = -1
        if end < 0:
            del self.events[mark:]
            self.memo[key] = (-1, ())
            return -1
        self.events.append((-1, _rule_id(name)))
        self.memo[key] = (end, tuple(self.events[mark:]))
        return end


_rule_ids: dict[str, int] = {}
_rule_names: dict[int, str] = {}


def _rule_id(name: str) -> int:
    rid = _rule_ids.get(name)
    if rid is None:
        rid = _rule_ids[name] = len(_rule_ids)
        _rule_names[rid] = name
    return rid


TOKEN_TERMINALS = ("IDENT", "INT", "FLOAT", "STRING", "EOF")
TOKEN_SUBCLASSES = ("UPPER_IDENT", "SELF", "WILD", "PLAIN_STRING")


class TokenMatcher(Matcher):
    def __init__(
        self, tokens: list[Token], source: Source, disabled: Event | None = None, rules: Rules | None = None
    ) -> None:
        super().__init__(syntax_rules() if rules is None else rules, disabled)
        self.tokens = tokens
        self.source = source

    def lit(self, text: str, pos: int) -> int:
        if pos < len(self.tokens) and self.tokens[pos].kind == text and text not in TOKEN_TERMINALS:
            return pos + 1
        return -1

    def terminal(self, name: str, pos: int) -> int:
        if pos >= len(self.tokens):
            return -1
        t = self.tokens[pos]
        ok: bool
        if name == "UPPER_IDENT":
            ok = t.kind == "IDENT" and t.text[:1].isupper()
        elif name == "SELF":
            ok = t.kind == "IDENT" and t.text == "self"
        elif name == "WILD":
            ok = t.kind == "IDENT" and t.text == "_"
        elif name == "PLAIN_STRING":
            ok = t.kind == "STRING" and _string_parts_are_plain(t)
        elif name == "STRING":
            ok = t.kind == "STRING" and self.interpolations_parse(t)
        else:
            assert name in TOKEN_TERMINALS, f"unknown terminal {name}"
            ok = t.kind == name
        return pos + 1 if ok else -1

    def interpolations_parse(self, t: Token) -> bool:
        assert isinstance(t.value, list)
        for part in t.value:
            if isinstance(part, str):
                continue
            sink = DiagSink()
            toks = Lexer(self.source, sink, part[0], part[1]).lex()
            if sink.has_errors:
                return False
            sub = TokenMatcher(toks, self.source, self.disabled, self.rules)
            if sub.match_ref("interp", 0) != len(toks):
                return False
            self.events.extend(sub.events)
        return True


def _string_parts_are_plain(t: Token) -> bool:
    assert isinstance(t.value, list)
    return all(isinstance(p, str) for p in t.value)


class CharMatcher(Matcher):
    CLASSES: dict[str, Callable[[str], bool]] = {
        "any": lambda ch: True,
        "alpha": str.isalpha,
        "alnum": str.isalnum,
        "surrogate": lambda ch: "\ud800" <= ch <= "\udfff",
    }

    def __init__(self, text: str, disabled: Event | None = None, rules: Rules | None = None) -> None:
        super().__init__(lexical_rules() if rules is None else rules, disabled)
        self.text = text

    def lit(self, text: str, pos: int) -> int:
        return pos + len(text) if self.text.startswith(text, pos) else -1

    def terminal(self, name: str, pos: int) -> int:
        test = self.CLASSES.get(name)
        assert test is not None, f"unknown character class {name}"
        return pos + 1 if pos < len(self.text) and test(self.text[pos]) else -1

    def accept_rule(self, name: str, start: int, end: int) -> bool:
        check = _LEXICAL_RANGE_CHECKS.get(name)
        return check is None or check(self.text[start:end])


def _digits_fit(body: str, base: int) -> bool:
    return len(body.replace("_", "").lstrip("0")) <= MAX_INT_DIGITS[base]


def _unicode_escape_valid(text: str) -> bool:
    digits = text[3:-1]  # the grammar, not this check, requires at least one digit
    return not digits or lexer_module._code_point(digits) is not None


def _float_finite(text: str) -> bool:
    try:
        return float(text.replace("_", "")) != float("inf")
    except ValueError:  # only a deliberately narrowed grammar can match something that is not a float
        return True


_LEXICAL_RANGE_CHECKS: dict[str, Callable[[str], bool]] = {
    "dec_int": lambda t: _digits_fit(t, 10),
    "hex_int": lambda t: _digits_fit(t[2:], 16),
    "oct_int": lambda t: _digits_fit(t[2:], 8),
    "bin_int": lambda t: _digits_fit(t[2:], 2),
    "FLOAT": _float_finite,
    "unicode_escape": _unicode_escape_valid,
}


def lex_clean(text: str) -> tuple[list[Token], bool]:
    sink = DiagSink()
    toks = tokenize(Source("t.vx", text), sink)
    return toks, not sink.has_errors


def diagnostics_of(text: str, repl: bool = False) -> list[str]:
    sink = DiagSink()
    parse_source(Source("t.vx", text), sink, repl=repl)
    return [d.code for d in sink.items if d.severity == "error"]


def parser_accepts(text: str, repl: bool = False) -> bool:
    return not diagnostics_of(text, repl)


def grammar_events(
    text: str, start: str = "module", disabled: Event | None = None, rules: Rules | None = None
) -> list[Event] | None:
    """The derivation of ``text`` from ``start`` as a list of choices, or None if the grammar rejects it."""
    source = Source("t.vx", text)
    toks, clean = lex_clean(text)
    if not clean:
        return None
    m = TokenMatcher(toks, source, disabled, rules)
    if m.match_ref(start, 0) != len(toks):
        return None
    return m.events


def grammar_accepts(text: str, start: str = "module") -> bool:
    return grammar_events(text, start) is not None


# ------------------------------------------------------------------ generating sentences
TOKEN_SAMPLES: dict[str, list[str]] = {
    "IDENT": ["a", "b", "xs", "foo", "Foo", "_", "self"],
    "UPPER_IDENT": ["A", "Point", "Émile"],
    "SELF": ["self"],
    "WILD": ["_"],
    "INT": ["0", "1", "42", "1_000", "0xFF", "0b101", "0o7"],
    "FLOAT": ["1.5", "2e3", "1_0.5e-2"],
    "STRING": ['""', '"s"', '"{a}"', '"x{ (1) }y"', '"{"nested"}"', '"\\{"', '"é"', '"{a.b(1)}{c}"'],
    "PLAIN_STRING": ['""', '"s"', '"\\n"'],
    "EOF": [""],
}
CHAR_SAMPLES: dict[str, list[str]] = {
    "any": ["a", "0", " ", "é", "}", "{", '"', "*", "/", "\n", "\\"],
    "alpha": ["a", "Z", "é", "ß", "日"],
    "alnum": ["a", "0", "é", "٣", "²"],
    "surrogate": ["\ud800"],
}


class Generator:
    """Random derivations, biased towards the choices it has visited least."""

    def __init__(self, rules: Rules, samples: dict[str, list[str]], seed: int) -> None:
        self.rules = rules
        self.samples = samples
        self.rng = random.Random(seed)
        self.visits: Counter[tuple[int, int]] = Counter()
        self.cost = self.minimum_costs()
        self.max_tokens = 120

    def minimum_costs(self) -> dict[str, float]:
        cost: dict[str, float] = {name: float("inf") for name in self.rules}
        changed = True
        while changed:
            changed = False
            for name, rule in self.rules.items():
                c = self.node_cost(rule, cost)
                if c + 1 < cost[name]:
                    cost[name] = c + 1
                    changed = True
        return cost

    def node_cost(self, node: Node, cost: dict[str, float]) -> float:
        if isinstance(node, Lit):
            return 0
        if isinstance(node, Ref):
            return cost.get(node.name, 0)
        if isinstance(node, Seq):
            return max(self.node_cost(i, cost) for i in node.items)
        if isinstance(node, Alt):
            return min(self.node_cost(i, cost) for i in node.items)
        if isinstance(node, Peek):  # an empty branch that only some followers allow: use it sparingly
            return 3
        return 0

    def expand(self, node: Node, budget: int, out: list[str]) -> None:
        if len(out) > self.max_tokens:
            budget = 0
        if isinstance(node, Lit):
            out.append(node.text)
        elif isinstance(node, Ref):
            rule = self.rules.get(node.name)
            if rule is None:
                out.append(self.rng.choice(self.samples[node.name]))
            else:
                self.expand(rule, budget - 1, out)
        elif isinstance(node, Seq):
            for i, item in enumerate(node.items):
                before = node.items[i - 1] if i else None
                if isinstance(before, Peek) and before.want:
                    self.expand_starting_with(item, literals(before.item), budget, out)
                else:
                    self.expand(item, budget, out)
        elif isinstance(node, Alt):
            self.expand_alt(node, budget, out)
        elif isinstance(node, Opt):
            if budget > 0 and self.want(node.uid):
                self.expand(node.item, budget, out)
        elif isinstance(node, Rep):
            n = 0
            while budget > 0 and n < 3 and self.want(node.uid):
                self.expand(node.item, budget - 1, out)
                n += 1
        # A lookahead consumes nothing; sentences that break one are filtered out by the recogniser.

    def expand_starting_with(self, node: Node, first: set[str], budget: int, out: list[str]) -> None:
        """Honour a leading ``&"x"``: keep trying until ``node`` happens to start with an allowed token."""
        mark = len(out)
        for _ in range(40):
            del out[mark:]
            self.expand(node, budget, out)
            if len(out) > mark and out[mark] in first:
                return

    def want(self, uid: int) -> bool:
        take = self.visits[(uid, 1)] <= self.visits[(uid, 0)]
        return take if self.rng.random() > 0.3 else not take

    def learn(self, events: Iterable[Event]) -> None:
        """Count the choices in a sentence that was accepted, so the rare ones get tried more."""
        self.visits.update(e for e in events if e[0] != -1)

    def expand_alt(self, node: Alt, budget: int, out: list[str]) -> None:
        options = [i for i, item in enumerate(node.items) if self.node_cost(item, self.cost) <= budget]
        if not options:
            best = min(self.node_cost(item, self.cost) for item in node.items)
            options = [i for i, item in enumerate(node.items) if self.node_cost(item, self.cost) == best]
        fussy = [i for i in options if isinstance(node.items[i], Peek)]
        if len(fussy) < len(options) and self.rng.random() > 0.2:
            options = [i for i in options if i not in fussy]
        if self.rng.random() < 0.7:
            least = min(self.visits[(node.uid, i)] for i in options)
            options = [i for i in options if self.visits[(node.uid, i)] == least]
        self.expand(node.items[self.rng.choice(options)], budget, out)


@dataclass
class Sentence:
    text: str
    tokens: list[str]
    start: str
    events: list[Event]


STARTS = ("module", "repl_input")

# Rarely generated shapes, spaced so that splitting on " " yields the tokens.
SEED_SENTENCES: tuple[tuple[str, str], ...] = (
    ("module", "fn m ( ) { ( return ) ; [ return , 1 ] ; return }"),
    ("module", "fn m ( ) { f ( return ) ; { return } ; return ; }"),
    ("repl_input", "return"),
    ("module", "fn m ( ) { if return { } { } }"),
    ("module", "fn m ( ) { while - return 1 { } ; }"),
    ("module", "fn m ( ) { match a { _ if b => { 1 } , c if d => 2 , e => { 3 } f => 4 } }"),
    ("module", "fn m ( ) { match a { S { b , } => 1 , T { c , .. } => 2 , U { .. } => 3 , V { } => 4 } }"),
    ("module", "fn m ( ) { let S { a , .. } = b ; let S { a , } = b ; let T { .. } = c ; let U { } = d ; }"),
    ("module", "fn m ( ) { | a : Int , b , | c ; || 1 ; || -> Int { 1 } ; #{ 1 : 2 , 3 : 4 , } ; }"),
    ("module", "fn m ( ) { { } . a = 1 ; if a { } ? ; { } . b . c ( ) [ 0 ] = 2 ; { } . d ? + 1 ; }"),
    ("module", "fn m ( ) { ( a . b ) = 1 ; ( ( x ) ) += 2 ; f ( ) . g [ 1 ] = 3 ; 1 . a = 2 ; }"),
    ("module", "fn f ( mut self , a : Int , mut self : Int , ) { }"),
)


@cache
def sentence_pool() -> tuple[Sentence, ...]:
    """Sentences of the grammar, deduplicated. Sentences that break a lookahead never get in."""
    gen = Generator(syntax_rules(), TOKEN_SAMPLES, SEED)
    seen: set[str] = set()
    pool: list[Sentence] = []
    for i in range(16000):
        start = STARTS[i % 7 == 0]
        tokens: list[str] = []
        gen.expand(Ref(start), 12 + i % 12, tokens)
        text = " ".join(tokens)
        if text in seen or len(tokens) > 400:  # a lookahead the generator ignored can also leave a stray token
            continue
        seen.add(text)
        events = grammar_events(text, start)
        if events is not None:
            gen.learn(events)
            pool.append(Sentence(text, tokens, start, events))
    for start, text in SEED_SENTENCES:
        events = grammar_events(text, start)
        assert events is not None, f"seed sentence rejected by the grammar: {text}"
        pool.append(Sentence(text, text.split(" "), start, events))
    return tuple(pool)


def choice_points(rules: Rules) -> Iterator[tuple[str, Alt | Opt | Rep]]:
    """Every choice that a derivation records; a lookahead consumes nothing and records nothing."""

    def inside(node: Node) -> Iterator[Alt | Opt | Rep]:
        if isinstance(node, Peek):
            return
        if isinstance(node, (Alt, Opt, Rep)):
            yield node
        if isinstance(node, (Seq, Alt)):
            for item in node.items:
                yield from inside(item)
        elif isinstance(node, (Opt, Rep)):
            yield from inside(node.item)

    for name, rule in rules.items():
        for choice in inside(rule):
            yield name, choice


def rules_used(events: Iterable[Event]) -> set[str]:
    return {_rule_names[b] for a, b in events if a == -1}


@cache
def sentences_by_rule() -> dict[str, list[Sentence]]:
    out: dict[str, list[Sentence]] = {name: [] for name in syntax_rules()}
    for s in sentence_pool():
        for name in rules_used(s.events):
            out[name].append(s)
    return out


# ------------------------------------------------------------------ grammar shape
def test_spec_has_both_grammars() -> None:
    assert ebnf_blocks("lexical") and ebnf_blocks("syntax")


@pytest.mark.parametrize("rules", [lexical_rules, syntax_rules], ids=["lexical", "syntax"])
def test_every_reference_is_defined_or_a_terminal(rules: Callable[[], Rules]) -> None:
    terminals = {*TOKEN_TERMINALS, *TOKEN_SUBCLASSES, *CharMatcher.CLASSES}
    table = rules()
    for name, rule in table.items():
        for ref in references(rule):
            assert ref in table or ref in terminals, f"{name} refers to undefined {ref}"


def test_every_syntax_rule_is_reachable_from_a_start_symbol() -> None:
    table = syntax_rules()
    seen: set[str] = set()
    todo = [*STARTS, "interp"]
    while todo:
        name = todo.pop()
        if name in table and name not in seen:
            seen.add(name)
            todo.extend(references(table[name]))
    assert seen == set(table)


def test_every_keyword_and_punctuator_is_in_the_grammar_and_vice_versa() -> None:
    used = set().union(*(literals(rule) for rule in syntax_rules().values()))
    assert used == KEYWORDS | set(PUNCT), used ^ (KEYWORDS | set(PUNCT))


def test_lexical_keyword_rule_is_exactly_the_lexer_keywords() -> None:
    assert literals(lexical_rules()["keyword_word"]) == set(KEYWORDS)


def test_lexical_punctuator_rule_lists_the_lexer_table_in_order() -> None:
    rule = lexical_rules()["PUNCT"]
    assert isinstance(rule, Alt)
    assert [item.text for item in rule.items if isinstance(item, Lit)] == PUNCT
    assert len(rule.items) == len(PUNCT)


def test_terminal_classes_are_documented_in_the_spec() -> None:
    for name in [*TOKEN_TERMINALS, *TOKEN_SUBCLASSES]:
        assert f"`{name}`" in spec_text(), f"terminal {name} is not described in the spec"
    for cls in CharMatcher.CLASSES:
        assert f"`{cls}`" in spec_text(), f"character class {cls} is not described in the spec"


def test_binary_operator_table_matches_the_parser() -> None:
    rows = re.findall(r"^\| ([1-6]) \| (.+?) \|$", _section("Binary operators"), re.M)
    table: dict[str, int] = {}
    for level, ops in rows:
        for op in re.findall(r"`([^`]+)`", ops.replace("\\|", "|")):
            table[op] = int(level)
    assert table == BINARY_PREC
    binop = literals(syntax_rules()["binop"])
    assert binop == set(BINARY_PREC)


def test_assignment_operators_match_the_parser() -> None:
    assert literals(syntax_rules()["assign_op"]) == ASSIGN_OPS


def test_block_like_and_item_starters_match_the_parser() -> None:
    table = syntax_rules()
    assert references(table["blocklike"]) == {"if_expr", "match_expr", "while_expr", "for_expr", "block"}
    assert {t[1] for t in _first_literals(["if_expr", "match_expr", "while_expr", "for_expr", "block"])} == BLOCK_LIKE
    assert {t[1] for t in _first_literals(references(table["item"]))} == ITEM_KEYWORDS


def _first_literals(names: Iterable[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name in names:
        rule = syntax_rules()[name]
        assert isinstance(rule, Seq) and isinstance(rule.items[0], Lit), name
        out.append((name, rule.items[0].text))
    return out


def _section(title: str) -> str:
    m = re.search(rf"^#+ [^\n]*{re.escape(title)}[^\n]*\n(.*?)(?=^#)", spec_text(), re.M | re.S)
    assert m is not None, f"the spec has no section {title!r}"
    return m.group(1)


# ------------------------------------------------------------------ positive: sentences from each rule
def test_generated_sentences_cover_every_rule_and_alternative() -> None:
    taken: set[Event] = set()
    for s in sentence_pool():
        taken.update(s.events)
    missing: list[str] = []
    for name, node in choice_points(syntax_rules()):
        branches = range(len(node.items)) if isinstance(node, Alt) else [1]
        for b in branches:
            if (node.uid, b) not in taken:
                missing.append(f"{name}: branch {b} of {type(node).__name__}")
    unused = [n for n, sentences in sentences_by_rule().items() if not sentences]
    assert not missing and not unused, (missing, unused)


@pytest.mark.parametrize("rule", sorted(syntax_rules()))
def test_parser_accepts_sentences_from_rule(rule: str) -> None:
    sentences = sentences_by_rule()[rule]
    assert sentences, f"no sentence uses {rule}"
    for s in sentences[:12]:
        assert parser_accepts(s.text, repl=s.start == "repl_input"), (rule, s.text, diagnostics_of(s.text))


def test_the_whole_pool_is_accepted_by_the_parser() -> None:
    pool = sentence_pool()
    assert len(pool) > 1500
    wrong = [s.text for s in pool if not parser_accepts(s.text, repl=s.start == "repl_input")]
    assert not wrong, wrong[:5]


def fn_body(stmts: str) -> str:
    return "fn m() { " + stmts + " }"


# One or more texts per rule that the rule's construct must reject: (source, parsed as REPL input?).
NEGATIVE_SAMPLES: dict[str, list[str]] = {
    "module": ["}", "let x = 1;", "1;"],
    "repl_input": ["\x00repl:1 2", "\x00repl:;", "\x00repl:1 }"],
    "item": ["trait T {}", "const X = 1;"],
    "import_decl": ["import a", "import;", "import 1;"],
    "fn_decl": ["fn () {}", "fn f {}", "fn f()", "fn f() -> {}"],
    "params": ["fn f(,) {}", "fn f(a: Int b: Int) {}", "fn f(a: Int,,) {}"],
    "self_param": ["fn f(self: Int) {}", "fn f(mut self: Int) {}", "fn f(a: Int, mut self) {}"],
    "param": ["fn f(a) {}", "fn f(a:) {}", "fn f(mut) {}", "fn f(a: Int, self) {}"],
    "tparams": ["fn f[,]() {}", "fn f[T U]() {}", "fn f[T() {}"],
    "tparam": ["fn f[T:]() {}", "fn f[T: Eq +]() {}", "fn f[1]() {}"],
    "struct_decl": ["struct {}", "struct S", "struct S { , }", "struct S { a: Int b: Int }"],
    "field": ["struct S { a }", "struct S { a: }", "struct S { 1: Int }"],
    "enum_decl": ["enum {}", "enum E", "enum E { , }", "enum E { A B }"],
    "variant": ["enum E { A(, ) }", "enum E { 1 }", "enum E { A(Int }"],
    "impl_decl": ["impl {}", "impl S", "impl S { let x = 1; }", "impl S { struct T {} }"],
    "type": ["fn f(a: 1) {}", "fn f(a: fn) {}", "fn f(a: fn(Int) ->) {}", "fn f(a: List[Int) {}", "fn f(a: (Int) {}"],
    "type_list": ["fn f(a: (,)) {}", "fn f(a: (Int Int)) {}", "fn f(a: (Int,,)) {}"],
    "block": ["fn m() {", "fn m() }", "fn m() { fn g() {} }", "fn m() { struct S {} }"],
    "block_entry": [fn_body("import a;"), fn_body("=;"), fn_body("impl S {}")],
    "let_stmt": [fn_body("let;"), fn_body("let x;"), fn_body("let x = 1"), fn_body("let x: = 1;"), fn_body("1 + let x = 2;")],
    "block_led": [fn_body("{} [1] = 2;"), fn_body("{} (1) = 2;"), fn_body("if a {} + 1;"), fn_body("{} .. 1;"), fn_body("while a {} * 2;")],
    "led_tail": [fn_body("{}.a .. 1;"), fn_body("{}.f() = 1;"), fn_body("{}? = 1;"), fn_body("{}.a + b = 1;")],
    "expr_stmt": [fn_body("1 2;"), fn_body("x = 1 = 2;"), fn_body("a b")],
    "stmt_end": [fn_body("a b;"), fn_body("x = 1 y;"), fn_body("{}.a b;")],
    "place": [fn_body("1 = 2;"), fn_body("f() = 1;"), fn_body("x? = 1;"), fn_body("a + b = 1;"), fn_body("(a, b) = 1;")],
    "place_suffix": [fn_body("a. = 1;"), fn_body("a[] = 1;"), fn_body("a.fn = 1;")],
    "assign_op": [fn_body("x == = 1;"), fn_body("x =+ 1;"), fn_body("x ** 1;"), fn_body("x <<= 1;")],
    "blocklike": [fn_body("do {}"), fn_body("loop {}"), fn_body("unless a {}")],
    "if_expr": [fn_body("if {}"), fn_body("if a"), fn_body("if a {} else"), fn_body("if a {} else 1;")],
    "match_expr": [fn_body("match {}"), fn_body("match a"), fn_body("match a { "), fn_body("match a b")],
    "arm": [
        fn_body("match a { _ }"),
        fn_body("match a { _ => }"),
        fn_body("match a { 1 => 2 3 => 4 }"),
        fn_body("match a { _ if => 1 }"),
        fn_body("match a { _ => if b {1} else {2} _ => 3 }"),
    ],
    "while_expr": [fn_body("while {}"), fn_body("while a"), fn_body("while a; {}")],
    "for_expr": [fn_body("for {}"), fn_body("for a b {}"), fn_body("for a in"), fn_body("for a in b"), fn_body("for mut a in b {}")],
    "expr": [fn_body("let x = ;"), fn_body("a..b..c;"), fn_body("..a;"), fn_body("a..;")],
    "range_op": [fn_body("a...b;"), fn_body("a..==b;")],
    "binary": [fn_body("a +;"), fn_body("* a;"), fn_body("a + * b;")],
    "binop": [fn_body("a ** b;"), fn_body("a & b;"), fn_body("a | b;"), fn_body("a << b;"), fn_body("a ^ b;")],
    "unary": [fn_body("-;"), fn_body("!;"), fn_body("~a;"), fn_body("+a;")],
    "postfix": [fn_body("a(;"), fn_body("a[;"), fn_body("a.;")],
    "suffix": [fn_body("a[];"), fn_body("a.1.5e;"), fn_body("a.if;"), fn_body("a(,);"), fn_body("a[1,2];")],
    "primary": [fn_body("S { 1 };"), fn_body("s { a: 1 };"), fn_body("let x = fn;")],
    "return_expr": [fn_body("[return];"), fn_body("a[return];"), fn_body("#{return: 1};"), fn_body("return.x;")],
    "return_end": [fn_body("[return ];"), fn_body("return ];")],
    "atom": [fn_body("@;"), fn_body("let x = let;"), fn_body("let x = in;")],
    "paren": [fn_body("(;"), fn_body("(,);"), fn_body("(1 2);")],
    "list_lit": [fn_body("[;"), fn_body("[,];"), fn_body("[1 2];")],
    "expr_list": [fn_body("f(1,,);"), fn_body("f(,1);"), fn_body("[1,,2];")],
    "map_lit": [fn_body("#{;"), fn_body("#{,};"), fn_body("#{1:2 3:4};"), fn_body("#{1};")],
    "map_entry": [fn_body("#{1:};"), fn_body("#{:1};"), fn_body("#{1 2};")],
    "struct_lit": [fn_body("S { a: };"), fn_body("S { a: 1 b: 2 };"), fn_body("S { a: 1,, };"), fn_body("if S { a: 1 } {}")],
    "field_init": [fn_body("S { a: 1, 2 };"), fn_body("S { a: };"), fn_body("S { a, fn };")],
    "lambda": [fn_body("|;"), fn_body("|a;"), fn_body("|a|;"), fn_body("|| -> Int 1;"), fn_body("|a| -> {};")],
    "lambda_param": [fn_body("|1| 2;"), fn_body("|a:| 2;"), fn_body("|,| 2;")],
    "cond": [fn_body("if a..b..c {}"), fn_body("while ..a {}"), fn_body("match a.. {}")],
    "cond_binary": [fn_body("if a + {}"), fn_body("while * a {}")],
    "cond_unary": [fn_body("if ! {}"), fn_body("while - {}")],
    "cond_postfix": [fn_body("if a. {}"), fn_body("if a[ {}")],
    "cond_primary": [fn_body("if S { a: 1 } {}"), fn_body("while S { a: 1 } {}"), fn_body("match S { a: 1 } { _ => 1 }")],
    "pattern": [fn_body("match a { | b => 1 }"), fn_body("match a { b | => 1 }"), fn_body("match a { b || c => 1 }")],
    "pat_atom": [fn_body("match a { + => 1 }"), fn_body("match a { if => 1 }"), fn_body("match a { mut b => 1 }"), fn_body("match a { _ . b => 1 }")],
    "pat_ctor": [fn_body("match a { A( => 1 }"), fn_body("match a { A(,) => 1 }"), fn_body("match a { b(c) => 1 }")],
    "pat_path": [fn_body("match a { A. => 1 }"), fn_body("match a { A.if => 1 }"), fn_body("match a { A.1 => 1 }")],
    "pat_struct": [
        fn_body("match a { A.b { c } => 1 }"),
        fn_body("match a { S { => 1 }"),
        fn_body("match a { S { b .. } => 1 }"),
        fn_body("match a { S { b, .., } => 1 }"),
        fn_body("match a { S { .., b } => 1 }"),
        fn_body("match a { s { b } => 1 }"),
    ],
    "pat_field": [fn_body("match a { S { b c } => 1 }"), fn_body("match a { S { 1 } => 1 }"), fn_body("match a { S { b: } => 1 }"), fn_body("match a { S { mut b } => 1 }")],
    "pat_tuple": [fn_body("match a { ( => 1 }"), fn_body("match a { (,) => 1 }"), fn_body("match a { (b c) => 1 }")],
    "pat_list": [fn_body("match a { A(b,,) => 1 }"), fn_body("match a { (b,,c) => 1 }")],
    "pat_literal": [fn_body("match a { - => 1 }"), fn_body("match a { -true => 1 }"), fn_body('match a { "{b}" => 1 }'), fn_body("match a { -b => 1 }")],
    "let_pattern": [fn_body("let | a = 1;"), fn_body("let a | = 1;")],
    "let_pat_atom": [fn_body("let + = 1;"), fn_body("let mut = 1;"), fn_body("let mut 1 = 1;"), fn_body("let _ . b = 1;")],
    "let_pat_ctor": [fn_body("let A( = 1;"), fn_body("let A(,) = 1;"), fn_body("let a(b) = 1;")],
    "let_pat_struct": [fn_body("let A.b { c } = 1;"), fn_body("let S { = 1;"), fn_body("let S { a .. } = 1;"), fn_body("let S { a, .., } = 1;")],
    "let_pat_field": [fn_body("let S { a b } = 1;"), fn_body("let S { mut a } = 1;"), fn_body("let S { a: } = 1;"), fn_body("let S { 1 } = 1;")],
    "let_pat_tuple": [fn_body("let ( = 1;"), fn_body("let (,) = 1;"), fn_body("let (a b) = 1;")],
    "let_pat_list": [fn_body("let (a,,b) = 1;"), fn_body("let A(a,,) = 1;")],
    "interp": ['fn m() { "{}"; }', 'fn m() { "{1 2}"; }', 'fn m() { "{let}"; }', 'fn m() { "{ }"; }'],
}


def test_every_syntax_rule_has_negative_samples() -> None:
    assert set(NEGATIVE_SAMPLES) == set(syntax_rules())


def negatives() -> Iterator[tuple[str, str]]:
    for rule, samples in sorted(NEGATIVE_SAMPLES.items()):
        for sample in samples:
            yield rule, sample


@pytest.mark.parametrize(("rule", "sample"), list(negatives()))
def test_negative_sample_rejected_by_parser_and_grammar(rule: str, sample: str) -> None:
    repl = sample.startswith("\x00repl:")
    text = sample.removeprefix("\x00repl:")
    assert not parser_accepts(text, repl), f"the parser accepts {text!r}, a negative sample for {rule}"
    start = "repl_input" if repl else "module"
    assert not grammar_accepts(text, start), f"the grammar accepts {text!r}, a negative sample for {rule}"


# ------------------------------------------------------------------ differential: parser verdict == grammar verdict
VOCABULARY = sorted(KEYWORDS | set(PUNCT)) + ["a", "A", "self", "_", "1", "1.5", '"s"', '"{a}"']


def mutate(tokens: list[str], rng: random.Random) -> list[str]:
    out = list(tokens)
    for _ in range(rng.choice((1, 1, 1, 2, 3))):
        if not out:
            break
        i = rng.randrange(len(out))
        op = rng.randrange(4)
        if op == 0:
            del out[i]
        elif op == 1:
            out.insert(i, rng.choice(VOCABULARY))
        elif op == 2:
            out[i] = rng.choice(VOCABULARY)
        else:
            j = rng.randrange(len(out))
            out[i], out[j] = out[j], out[i]
    return out


def verdicts_agree(text: str, start: str) -> tuple[bool, bool] | None:
    """(parser verdict, grammar verdict) if they differ, otherwise None."""
    repl = start == "repl_input"
    codes = diagnostics_of(text, repl)
    if "too-deep" in codes or "int-literal-range" in codes:  # value limits, not syntax: see the limit tests
        return None
    p = not codes
    g = grammar_accepts(text, start)
    return None if p == g else (p, g)


def test_mutated_sentences_get_the_same_verdict() -> None:
    rng = random.Random(SEED + 1)
    pool = list(sentence_pool())
    rng.shuffle(pool)
    disagreements: list[str] = []
    accepted = rejected = 0
    for s in pool[:2500]:
        for _ in range(2):
            mutated = mutate(s.tokens, rng)
            text = " ".join(mutated)
            diff = verdicts_agree(text, s.start)
            if diff is not None:
                disagreements.append(f"{text!r} ({s.start}): parser={diff[0]} grammar={diff[1]}")
            elif parser_accepts(text, repl=s.start == "repl_input"):
                accepted += 1
            else:
                rejected += 1
    assert not disagreements, disagreements[:5]
    assert accepted > 50 and rejected > 500  # the mutants must not be trivially one-sided


def repository_programs() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for pattern in ("examples/**/*.vx", "tests/errors/*.vx", "tests/modules/*.vx", "benchmarks/*.vx"):
        for path in sorted(ROOT.glob(pattern)):
            out.append((path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8", errors="replace")))
    return out


def test_repository_programs_get_the_same_verdict() -> None:
    programs = repository_programs()
    assert len(programs) > 100
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 6000))  # the recogniser spends several Python frames per grammar level
    try:
        wrong = [(n, d) for n, t in programs if (d := verdicts_agree(t, "module")) is not None]
    finally:
        sys.setrecursionlimit(limit)
    assert not wrong, wrong[:5]


REPL_MARK = "\x00repl:"


def all_negative_texts() -> list[str]:
    return [sample for _, sample in negatives()]


def grammar_accepts_without(sample: str, event: Event) -> bool:
    """Does the grammar accept a negative sample once one alternative is taken away?"""
    start = "repl_input" if sample.startswith("\x00repl:") else "module"
    return grammar_events(sample.removeprefix("\x00repl:"), start, disabled=event) is not None


def test_every_alternative_is_load_bearing() -> None:
    """Removing any one alternative from the spec's grammar must make the grammar reject a known sentence."""
    by_branch: dict[Event, list[Sentence]] = {}
    for s in sentence_pool():
        for event in set(s.events):
            if event[0] != -1:
                by_branch.setdefault(event, []).append(s)
    redundant: list[str] = []
    for name, node in choice_points(syntax_rules()):
        if not isinstance(node, Alt):
            continue
        for branch in range(len(node.items)):
            event = (node.uid, branch)
            users = by_branch.get(event, [])[:150]
            if not users or any(grammar_events(s.text, s.start, disabled=event) is None for s in users):
                continue  # a sentence depends on this alternative
            if not any(grammar_accepts_without(sample, event) for sample in all_negative_texts()):
                redundant.append(f"{name} branch {branch}: {users[0].text}")
    assert not redundant, redundant[:5]


# ------------------------------------------------------------------ lexical grammar
def lexes_to(text: str, kind: str) -> bool:
    toks, clean = lex_clean(text)
    return clean and len(toks) == 2 and toks[0].kind == kind and (toks[0].start, toks[0].end) == (0, len(text))


def lexes_to_keyword(text: str) -> bool:
    toks, clean = lex_clean(text)
    return clean and len(toks) == 2 and toks[0].kind in KEYWORDS and (toks[0].start, toks[0].end) == (0, len(text))


def lexes_to_punct(text: str) -> bool:
    toks, clean = lex_clean(text)
    return clean and len(toks) == 2 and toks[0].kind in PUNCT and (toks[0].start, toks[0].end) == (0, len(text))


def lexes_to_nothing(text: str) -> bool:
    toks, clean = lex_clean(text)
    return clean and len(toks) == 1


LEXER_ORACLES: dict[str, Callable[[str], bool]] = {
    "IDENT": lambda t: lexes_to(t, "IDENT"),
    "INT": lambda t: lexes_to(t, "INT"),
    "FLOAT": lambda t: lexes_to(t, "FLOAT"),
    "STRING": lambda t: lexes_to(t, "STRING"),
    "keyword": lexes_to_keyword,
    "PUNCT": lexes_to_punct,
    "trivia": lexes_to_nothing,
}

LEXICAL_NEGATIVES: dict[str, list[str]] = {
    "trivia": ["/*", "/* /* */", "\x0c", "/ /", "// c\nx", "/*/*/"],
    "whitespace": ["\x0b", "\xa0"],
    "bom": ["\ufffe"],
    "line_comment": ["/x"],
    "block_comment": ["/* unterminated", "/*/"],
    "IDENT": ["1a", "let", "fn", "é-", "a b"],
    "keyword": ["lets", "Let"],
    "keyword_word": ["trait"],
    "ident_start": ["1", "²", "-"],
    "ident_part": ["-", " "],
    "INT": ["", "1a", "0x", "1_a", "0x_", "0o_", "0b_"],
    "radix_int": ["0x", "0xg", "0b2", "0o8", "0x_"],
    "hex_int": ["0xg", "0x_"],
    "oct_int": ["0o8", "0o_"],
    "bin_int": ["0b2", "0b_"],
    "dec_int": ["_1", "a1"],
    "digit": ["a", "٣"],
    "hex_digit": ["g"],
    "oct_digit": ["8"],
    "bin_digit": ["2"],
    "FLOAT": ["1.", ".5", "1e", "1.5e", "1e400", "1e5_", "1.5x", "1"],
    "fraction": ["1.a", "1.-5"],
    "exponent": ["1e+", "1e_5", "1ex"],
    "STRING": ['"', '"a', '"\\q"', '"{"', '"{a', '"\\', '"\\u{}"', '"\\u{41"', '"{{}"', '"{"a}', '"\ud800"'],
    "escape": ['"\\a"', '"\\x41"', '"\\'],
    "simple_escape": ['"\\e"'],
    "unicode_escape": ['"\\u{}"', '"\\u{D800}"', '"\\u{110000}"', '"\\u41"', '"\\u{g}"', '"\\u{41"'],
    "interpolation": ['"{"', '"{{}"'],
    "interp_text": ['"{"}'],
    "nested_string": ['"{"a}'],
    "string_char": ['"\ud800"'],
    "PUNCT": ["#", "&", "^", "~", "@", "$", "`", "\\", "'"],
}


def _lexical_positive_rules() -> list[str]:
    return sorted(LEXER_ORACLES)


def lexical_pool(rule: str) -> list[str]:
    gen = Generator(lexical_rules(), CHAR_SAMPLES, SEED + hash_rule(rule))
    out: list[str] = []
    seen: set[str] = set()
    for i in range(600):
        pieces: list[str] = []
        gen.expand(Ref(rule), 6 + i % 8, pieces)
        text = "".join(pieces)
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def hash_rule(rule: str) -> int:
    return sum(ord(c) for c in rule)


def char_full_match(rule: str, text: str, disabled: Event | None = None, rules: Rules | None = None) -> bool:
    m = CharMatcher(text, disabled, rules)
    return m.match_ref(rule, 0) == len(text)


@pytest.mark.parametrize("rule", _lexical_positive_rules())
def test_generated_lexical_sentences_agree_with_the_lexer(rule: str) -> None:
    oracle = LEXER_ORACLES[rule]
    accepted = 0
    for text in lexical_pool(rule):
        grammar = char_full_match(rule, text)
        assert grammar == oracle(text), (rule, text)
        accepted += grammar
    assert accepted > 10, rule


@pytest.mark.parametrize("rule", _lexical_positive_rules())
def test_mutated_lexical_sentences_agree_with_the_lexer(rule: str) -> None:
    rng = random.Random(SEED + hash_rule(rule))
    pool = lexical_pool(rule)
    alphabet = list('0192_aeEfgxXbBoO.+-"{}\\/*é² \n')
    oracle = LEXER_ORACLES[rule]
    for text in pool[:400]:
        chars = list(text)
        for _ in range(rng.choice((1, 1, 2))):
            i = rng.randrange(len(chars) + 1)
            if chars and i < len(chars) and rng.random() < 0.5:
                del chars[i]
            else:
                chars.insert(i, rng.choice(alphabet))
        mutated = "".join(chars)
        assert char_full_match(rule, mutated) == oracle(mutated), (rule, mutated)


def test_every_lexical_rule_has_negative_samples() -> None:
    assert set(LEXICAL_NEGATIVES) == set(lexical_rules())


def lexical_negatives() -> Iterator[tuple[str, str]]:
    for rule, samples in sorted(LEXICAL_NEGATIVES.items()):
        for sample in samples:
            yield rule, sample


@pytest.mark.parametrize(("rule", "sample"), list(lexical_negatives()))
def test_lexical_negative_sample(rule: str, sample: str) -> None:
    assert not char_full_match(rule, sample), f"{rule} accepts {sample!r}"
    oracle = LEXER_ORACLES.get(rule)
    if oracle is not None:
        assert not oracle(sample), f"the lexer accepts {sample!r} as {rule}"


def test_every_lexical_alternative_is_exercised() -> None:
    taken: set[Event] = set()
    gen_rules = lexical_rules()
    for rule in _lexical_positive_rules():
        for text in lexical_pool(rule):
            m = CharMatcher(text)
            if m.match_ref(rule, 0) == len(text):
                taken.update(m.events)
    missing: list[str] = []
    for name, node in choice_points(gen_rules):
        branches = range(len(node.items)) if isinstance(node, Alt) else [1]
        for b in branches:
            if (node.uid, b) not in taken:
                missing.append(f"{name}: branch {b} of {type(node).__name__}")
    assert not missing, missing


# ------------------------------------------------------------------ the spec must not be narrower than the parser
# Everything generated above comes from the spec, so a spec that loses a form simply generates fewer sentences. The
# forms below are written independently of the spec: each is accepted by the parser, and the grammar must accept it.
FORMS_FILE = ROOT / "tests" / "spec_accepted_forms.txt"
REPL_PREFIX = "repl: "


def accepted_forms() -> list[tuple[str, str]]:
    """(start symbol, source) for each line of the corpus; blank lines and ``//`` lines are skipped."""
    out: list[tuple[str, str]] = []
    for line in FORMS_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("//"):
            continue
        out.append(("repl_input", line[len(REPL_PREFIX) :]) if line.startswith(REPL_PREFIX) else ("module", line))
    return out


@pytest.mark.parametrize(("start", "form"), accepted_forms())
def test_accepted_form_is_accepted_by_parser_and_grammar(start: str, form: str) -> None:
    assert parser_accepts(form, repl=start == "repl_input"), f"the parser rejects {form!r}: {diagnostics_of(form)}"
    assert grammar_accepts(form, start), f"the spec's grammar rejects {form!r}, which the parser accepts"


def spec_code_blocks() -> list[str]:
    """Every ```vex block and spec example in the documents, as complete programs."""
    out: list[str] = []
    for path in [*sorted((ROOT / "docs").glob("*.md")), ROOT / "README.md"]:
        out.extend(re.findall(r"```vex(?: example [\w-]+)?\n(.*?)```", path.read_text(encoding="utf-8"), re.S))
    return out


def test_every_parser_accepted_program_is_in_the_grammar() -> None:
    programs = [t for _, t in repository_programs()] + spec_code_blocks()
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 6000))
    try:
        missing = [t for t in programs if parser_accepts(t) and not grammar_accepts(t)]
    finally:
        sys.setrecursionlimit(limit)
    assert not missing, missing[:3]


def narrowings(node: Node) -> Iterator[tuple[str, Node]]:
    """Every way of changing ``node`` in one place that makes it match less, or drops a required part."""
    nothing = Seq(())
    if isinstance(node, Ref):
        if node.name == "IDENT":
            yield "IDENT becomes UPPER_IDENT", Ref("UPPER_IDENT")
        elif node.name == "STRING":
            yield "STRING becomes PLAIN_STRING", Ref("PLAIN_STRING")
    elif isinstance(node, Seq):
        for i, item in enumerate(node.items):
            yield f"drop item {i}", Seq(node.items[:i] + node.items[i + 1 :])
            for what, changed in narrowings(item):
                yield f"item {i}: {what}", Seq(node.items[:i] + (changed,) + node.items[i + 1 :])
    elif isinstance(node, Alt):
        for i, item in enumerate(node.items):
            yield f"drop branch {i}", Alt(node.uid, node.items[:i] + node.items[i + 1 :])
            for what, changed in narrowings(item):
                yield f"branch {i}: {what}", Alt(node.uid, node.items[:i] + (changed,) + node.items[i + 1 :])
    elif isinstance(node, Opt):
        yield "optional part removed", nothing
        yield "optional part required", node.item
        for what, changed in narrowings(node.item):
            yield f"inside optional: {what}", Opt(node.uid, changed)
    elif isinstance(node, Rep):
        yield "repetition removed", nothing
        yield "repetition at most once", Opt(node.uid, node.item)
        yield "repetition at least once", Seq((node.item, node))
        for what, changed in narrowings(node.item):
            yield f"inside repetition: {what}", Rep(node.uid, changed)
    elif isinstance(node, Peek):
        for what, changed in narrowings(node.item):
            yield f"inside lookahead: {what}", Peek(changed, node.want)


def narrowed_grammars(rules: Rules) -> Iterator[tuple[str, Rules]]:
    for name, rule in rules.items():
        for what, changed in narrowings(rule):
            yield f"{name}: {what}", {**rules, name: changed}


def undetected_narrowings() -> list[str]:
    forms = accepted_forms()
    for start, text in forms:  # a form the grammar already rejects would make every narrowing look detected
        assert grammar_accepts(text, start), f"the spec's grammar rejects {text!r}"
    negative = [
        ("repl_input", s.removeprefix(REPL_MARK)) if s.startswith(REPL_MARK) else ("module", s)
        for s in all_negative_texts()
    ]
    programs = [t for t in [*(s for _, s in repository_programs()), *spec_code_blocks()] if parser_accepts(t)]
    missed: list[str] = []
    for what, rules in narrowed_grammars(syntax_rules()):
        try:
            if any(grammar_events(text, start, rules=rules) is None for start, text in forms):
                continue
            if any(grammar_events(text, start, rules=rules) is not None for start, text in negative):
                continue
            if any(grammar_events(text, "module", rules=rules) is None for text in programs):
                continue
        except RecursionError:  # dropping a prefix can leave a left-recursive rule, which no longer parses anything
            continue
        missed.append(what)
    return missed


# Changes that cannot alter what the grammar accepts. A stale entry fails the tests below.
EQUIVALENT_SYNTAX = {
    "led_tail: branch 1: drop item 0": "`&( . | ? )` before led_tail guarantees a suffix, so `{ suffix }` matches the same",
}
EQUIVALENT_LEXICAL = {
    "interpolation: drop item 2": "without the `}`, interp_text stops at it and it is then read as an ordinary string character",
}


def test_every_narrowing_of_the_syntax_is_detected() -> None:
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 6000))
    try:
        missed = undetected_narrowings()
    finally:
        sys.setrecursionlimit(limit)
    assert set(missed) == set(EQUIVALENT_SYNTAX), f"{len(missed)} changes to the grammar go unnoticed: {missed}"


LEXICAL_FORMS: dict[str, list[str]] = {
    "trivia": ["", " \t\r\n", "//", "// c", "// c\n", "/* c */", "/* /* n */ */", "\ufeff", "/* * / */", " /**/ // x\n"],
    "IDENT": ["a", "_", "a1", "é", "letx", "_1", "a_b", "日本"],
    "keyword": sorted(KEYWORDS),
    "PUNCT": list(PUNCT),
    "INT": [
        "0", "7", "1_000", "1_", "0x1F", "0XaF", "0x_1", "0x1_F", "0o7", "0O7", "0o_7", "0o1_7", "0b1", "0B0", "0b_1", "0b1_0", "0x__1", "0o__7", "0b__1",
        "0123456789", "0x0123456789abcdefABCDEF"[:18], "0o01234567", "0xabcdef", "0xABCDEF",
    ],
    "FLOAT": ["1.5", "1.5e3", "1e3", "1E3", "1e+3", "1e-3", "1e10", "1e100", "1_0.5_0", "1_0e3", "1.5E-2", "0.0123456789"],
    "STRING": [
        '""', '"a"', '"a\nb"', '"}"', '"é"', '"\\n"', '"\\t"', '"\\r"', '"\\0"', '"\\\\"', '"\\""', '"\\{"', '"\\}"', '"\\u{41}"',
        '"\\u{A}"', '"\\u{10FFFF}"', '"{a}"', '"{ {} }"', '"{ {a} }"', '"{ {a} \\q }"', '"{"a}"}"', '"{"}"}"', '"{"s"}"', '"{""}"', '"{"ab"}"', '"{"\\""}"', '"a{b}c{d}"',
    ],
}


# A number directly followed by a letter or `_` is `bad-number`: the rule must not match even a prefix of these.
NO_PREFIX_MATCH: list[tuple[str, str]] = [("INT", "1a"), ("INT", "1_a"), ("INT", "0x1g"), ("FLOAT", "1.5x"), ("FLOAT", "1e5_")]


def matches_a_prefix(rule: str, text: str, rules: Rules | None = None) -> bool:
    return CharMatcher(text, None, rules).match_ref(rule, 0) >= 0


def undetected_lexical_narrowings() -> list[str]:
    forms = [(rule, text) for rule, texts in LEXICAL_FORMS.items() for text in texts]
    for rule, text in forms:
        assert LEXER_ORACLES[rule](text), f"the lexer rejects {text!r} as {rule}"
        assert char_full_match(rule, text), f"the spec's lexical grammar rejects {text!r} as {rule}"
    negative = [(r, s) for r, s in lexical_negatives() if r in LEXER_ORACLES]
    for rule, text in NO_PREFIX_MATCH:
        assert not lex_clean(text)[1], f"the lexer accepts {text!r}"
        assert not matches_a_prefix(rule, text), f"{rule} matches a prefix of {text!r}"
    missed: list[str] = []
    for what, rules in narrowed_grammars(lexical_rules()):
        try:
            if any(not char_full_match(r, s, rules=rules) for r, s in forms):
                continue
            if any(char_full_match(r, s, rules=rules) for r, s in negative):
                continue
            if any(matches_a_prefix(r, s, rules) for r, s in NO_PREFIX_MATCH):
                continue
        except RecursionError:
            continue
        missed.append(what)
    return missed


def test_every_narrowing_of_the_lexical_grammar_is_detected() -> None:
    missed = undetected_lexical_narrowings()
    assert set(missed) == set(EQUIVALENT_LEXICAL), f"{len(missed)} changes to the lexical grammar go unnoticed: {missed}"


# ------------------------------------------------------------------ narrowing one call site
# The narrowings above change a rule's own shape. These restrict what one reference may expand to: the rule that was
# referenced is untouched, so only a sentence using that alternative at that place can notice. The sentences come from
# CALL_SITE_CORPUS, which is frozen in the repository: anything generated from the spec during the test would narrow too.
CALL_SITE_CORPUS = ROOT / "tests" / "spec_call_site_corpus.jsonl"


@cache
def call_site_corpus() -> tuple[tuple[str, str, str], ...]:
    """(grammar, start rule, sentence) for each line of the corpus."""
    out: list[tuple[str, str, str]] = []
    for line in CALL_SITE_CORPUS.read_text(encoding="utf-8").splitlines():
        grammar, start, text = json.loads(line)
        out.append((grammar, start, text))
    return tuple(out)


def _syntax_evidence() -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[str]]:
    """What a narrowed syntax is held to: the accepted forms, the negative samples, and the parseable programs."""
    negative = [
        ("repl_input", s.removeprefix(REPL_MARK)) if s.startswith(REPL_MARK) else ("module", s)
        for s in all_negative_texts()
    ]
    programs = [t for t in [*(s for _, s in repository_programs()), *spec_code_blocks()] if parser_accepts(t)]
    return accepted_forms(), negative, programs


def syntax_narrowing_detected(rules: Rules) -> bool:
    """Does the grammar ``rules`` differ from the parser on anything the tests hold it to?"""
    forms, negative, programs = _syntax_evidence()
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 6000))
    try:
        return (
            any(grammar_events(text, start, rules=rules) is None for start, text in forms)
            or any(grammar_events(text, start, rules=rules) is not None for start, text in negative)
            or any(grammar_events(text, "module", rules=rules) is None for text in programs)
            or any(grammar_events(text, start, rules=rules) is None for g, start, text in call_site_corpus() if g == "syntax")
        )
    except RecursionError:  # as in undetected_narrowings: a left-recursive rule no longer parses anything
        return True
    finally:
        sys.setrecursionlimit(limit)


def lexical_narrowing_detected(rules: Rules) -> bool:
    forms = [(rule, text) for rule, texts in LEXICAL_FORMS.items() for text in texts]
    negative = [(r, s) for r, s in lexical_negatives() if r in LEXER_ORACLES]
    return (
        any(not char_full_match(r, s, rules=rules) for r, s in forms)
        or any(char_full_match(r, s, rules=rules) for r, s in negative)
        or any(matches_a_prefix(r, s, rules) for r, s in NO_PREFIX_MATCH)
        or any(not char_full_match(start, text, rules=rules) for g, start, text in call_site_corpus() if g == "lexical")
    )


@dataclass(frozen=True)
class SiteNarrowing:
    name: str
    grammar: str  # "syntax" | "lexical"
    old: str
    new: str
    start: str
    witness: str  # accepted by the parser, lost by the narrowed grammar


COND_ATOM_WITHOUT_LAMBDA = (
    'cond_primary = "return" cond | cond_atom ;\n'
    'cond_atom    = INT | FLOAT | STRING | "true" | "false" | IDENT | paren | list_lit | map_lit | blocklike | "break" | "continue" ;'
)
BLOCK_COMMENT_NESTING_ONCE = (
    'block_comment  = "/*" { flat_comment | !( "*/" | "/*" ) any } "*/" ;\n'
    'flat_comment   = "/*" { !( "*/" | "/*" ) any } "*/" ;'
)

SITE_NARROWINGS: list[SiteNarrowing] = [
    # Found by review: each lost the range form, or the or-pattern, at one place, and nothing noticed.
    SiteNarrowing("map-key", "syntax", 'map_entry    = expr ":" expr ;', 'map_entry    = binary ":" expr ;', "module", "fn m() { #{a..b: c}; }"),
    SiteNarrowing("interpolation", "syntax", "interp       = expr EOF ;", "interp       = binary EOF ;", "module", 'fn m() { "{a..b}"; }'),
    SiteNarrowing("field-value", "syntax", 'field_init   = IDENT [ ":" expr ] ;', 'field_init   = IDENT [ ":" binary ] ;', "module", "fn m() { S { a: b..c }; }"),
    SiteNarrowing("lambda-body", "syntax", '"->" type block | expr ) ;', '"->" type block | binary ) ;', "module", "fn m() { a..|| b..c; }"),
    SiteNarrowing("guard", "syntax", 'arm          = pattern [ "if" cond ]', 'arm          = pattern [ "if" cond_binary ]', "module", "fn m() { match a { _ if b..c => 1 } }"),
    SiteNarrowing("return", "syntax", 'return_expr  = "return" ( &return_end | expr ) ;', 'return_expr  = "return" ( &return_end | binary ) ;', "module", "fn m() { a..return b..c; }"),
    SiteNarrowing("place-index", "syntax", 'place_suffix = "." ( IDENT | INT ) | "[" expr "]" ;', 'place_suffix = "." ( IDENT | INT ) | "[" binary "]" ;', "module", "fn m() { a[b..c] = 1; }"),
    SiteNarrowing("pat-field", "syntax", 'pat_field    = IDENT [ ":" pattern ] ;', 'pat_field    = IDENT [ ":" pat_atom ] ;', "module", "fn m() { match a { S { b: c | d } => 1 } }"),
    # Chosen to probe the mechanism: one occurrence among several, an alternative of a referenced rule, recursion, lexing.
    SiteNarrowing("place-head-struct", "syntax", "place        = primary { suffix &suffix } place_suffix", "place        = atom { suffix &suffix } place_suffix", "module", "fn m() { S { a: 1 }.b = 2; }"),
    SiteNarrowing("cond-lambda", "syntax", 'cond_primary = "return" cond | atom ;', COND_ATOM_WITHOUT_LAMBDA, "module", "fn m() { if || a {} }"),
    SiteNarrowing("expr-list-later", "syntax", 'expr_list    = expr { "," expr } [ "," ] ;', 'expr_list    = expr { "," binary } [ "," ] ;', "module", "fn m() { [a, b..c]; }"),
    SiteNarrowing("map-value", "syntax", 'map_entry    = expr ":" expr ;', 'map_entry    = expr ":" binary ;', "module", "fn m() { #{a: b..c}; }"),
    SiteNarrowing("arm-body", "syntax", '| expr ( "," | &"}" ) ) ;', '| binary ( "," | &"}" ) ) ;', "module", "fn m() { match a { _ => b..c, } }"),
    SiteNarrowing("pat-list-later", "syntax", 'pat_list     = pattern { "," pattern } [ "," ] ;', 'pat_list     = pattern { "," pat_atom } [ "," ] ;', "module", "fn m() { match a { (b, c | d) => 1 } }"),
    SiteNarrowing("let-pat-field", "syntax", 'let_pat_field  = IDENT [ ":" let_pattern ] ;', 'let_pat_field  = IDENT [ ":" let_pat_atom ] ;', "module", "fn m() { let S { a: b | c } = 1; }"),
    SiteNarrowing("match-scrutinee", "syntax", 'match_expr   = "match" cond "{"', 'match_expr   = "match" cond_binary "{"', "module", "fn m() { match a..b { _ => 1 } }"),
    SiteNarrowing("bin-digit-later", "lexical", '{ "_" } bin_digit { bin_digit | "_" } ;', '{ "_" } bin_digit { "0" | "_" } ;', "INT", "0b11"),
    SiteNarrowing("block-comment-depth", "lexical", 'block_comment  = "/*" { block_comment | !( "*/" | "/*" ) any } "*/" ;', BLOCK_COMMENT_NESTING_ONCE, "trivia", "/* /* /* x */ */ */"),
]


def narrowed_rules(case: SiteNarrowing) -> Rules:
    text = "\n".join(ebnf_blocks(case.grammar))
    assert text.count(case.old) == 1, f"{case.name}: the spec does not contain {case.old!r} exactly once"
    return EbnfReader(text.replace(case.old, case.new), 0).rules()


@pytest.mark.parametrize("case", SITE_NARROWINGS, ids=lambda c: c.name)
def test_narrowing_one_call_site_is_detected(case: SiteNarrowing) -> None:
    narrowed = narrowed_rules(case)
    if case.grammar == "syntax":
        assert parser_accepts(case.witness), f"the parser rejects {case.witness!r}"
        assert grammar_accepts(case.witness, case.start), f"the spec rejects {case.witness!r}"
        assert grammar_events(case.witness, case.start, rules=narrowed) is None, "the edit does not narrow the grammar"
        detected = syntax_narrowing_detected(narrowed)
    else:
        assert LEXER_ORACLES[case.start](case.witness), f"the lexer rejects {case.witness!r}"
        assert char_full_match(case.start, case.witness), f"the spec rejects {case.witness!r}"
        assert not char_full_match(case.start, case.witness, rules=narrowed), "the edit does not narrow the grammar"
        detected = lexical_narrowing_detected(narrowed)
    assert detected, f"{case.name}: the spec lost {case.witness!r} and no test noticed"


# ------------------------------------------------------------------ rules that depend on token values or context
@pytest.mark.parametrize(
    ("source", "ok"),
    [
        ("9223372036854775807", True),
        ("9223372036854775808", False),
        ("-9223372036854775808", True),
        ("- 9223372036854775808", True),
        ("-0x8000000000000000", True),
        ("0x8000000000000000", False),
        ("1 - 9223372036854775808", False),
        ("99999999999999999999", False),
    ],
)
def test_integer_literal_range(source: str, ok: bool) -> None:
    assert parser_accepts(fn_body(f"let x = {source};")) is ok


@pytest.mark.parametrize(
    ("source", "kinds"),
    [
        ("t.0.1", ["IDENT", ".", "INT", ".", "INT", "EOF"]),
        ("t . 1e", None),
        ("1.5", ["FLOAT", "EOF"]),
        ("1..2", ["INT", "..", "INT", "EOF"]),
        ("1.foo", ["INT", ".", "IDENT", "EOF"]),
        ("a<=b", ["IDENT", "<=", "IDENT", "EOF"]),
        ("a..=b", ["IDENT", "..=", "IDENT", "EOF"]),
        ("||", ["||", "EOF"]),
        ("| |", ["|", "|", "EOF"]),
    ],
)
def test_token_boundaries(source: str, kinds: list[str] | None) -> None:
    toks, clean = lex_clean(source)
    assert clean is (kinds is not None)
    if kinds is not None:
        assert [t.kind for t in toks] == kinds


def test_digits_after_a_dot_are_a_decimal_tuple_index() -> None:
    toks, clean = lex_clean("t.1_0")
    assert clean and toks[2].text == "10"


def test_radix_digit_tables_match_the_grammar() -> None:
    table = lexical_rules()
    assert "".join(sorted(literals(table["hex_digit"]) | literals(table["digit"]))) == "".join(sorted(BASE_DIGITS[16]))
    assert "".join(sorted(literals(table["oct_digit"]))) == BASE_DIGITS[8]
    assert "".join(sorted(literals(table["bin_digit"]))) == BASE_DIGITS[2]
    assert literals(table["digit"]) == set(lexer_module.DIGITS)


def test_escapes_table_matches_the_lexer() -> None:
    table = lexical_rules()
    assert literals(table["simple_escape"]) == set(lexer_module.ESCAPES)


# ------------------------------------------------------------------ limits
@pytest.mark.parametrize(
    ("build", "name"),
    [
        (lambda n: fn_body("let x = " + "(" * n + "1" + ")" * n + ";"), "parentheses"),
        (lambda n: fn_body("let x = " + "-" * n + "1;"), "prefix operators"),
        (lambda n: fn_body("let x: " + "(" * n + "Int" + ")" * n + " = 1;"), "types"),
        (lambda n: fn_body("let " + "(" * n + "x" + ")" * n + " = 1;"), "patterns"),
    ],
)
def test_nesting_limit_boundary(build: Callable[[int], str], name: str) -> None:
    limit = parser_module.MAX_DEPTH - 1
    assert parser_accepts(build(limit)), name
    assert "too-deep" in diagnostics_of(build(limit + 1)), name


LIMITS: dict[str, int] = {
    "parser.MAX_DEPTH": parser_module.MAX_DEPTH,
    "runtime.MAX_STR_LEN": runtime.MAX_STR_LEN,
    "runtime.MAX_LIST_LEN": runtime.MAX_LIST_LEN,
    "vm.MAX_DEPTH": vm_module.MAX_DEPTH,
    "vm.MAX_CALLBACK_DEPTH": vm_module.MAX_CALLBACK_DEPTH,
    "host.DEFAULT_BUDGET": DEFAULT_BUDGET,
    "host.MAX_HOST_DEPTH": MAX_HOST_DEPTH,
    "DiagSink.MAX_ERRORS": DiagSink.MAX_ERRORS,
}


def test_limit_table_matches_the_implementation() -> None:
    rows = dict(re.findall(r"^\| [^|]+ \| ([\d,]+) \| `([\w.]+)` \|", _section("Limits"), re.M))
    table = {name: int(value.replace(",", "")) for value, name in rows.items()}
    assert table == LIMITS


# ------------------------------------------------------------------ spec examples
@dataclass(frozen=True)
class Example:
    name: str
    source: str
    kind: str  # "output" | "panic" | "exit" | "error"
    expected: str


EXAMPLE = re.compile(r"```vex example (?P<name>[\w-]+)\n(?P<src>.*?)```\s*```(?P<kind>output|panic|error)(?: exit=(?P<code>-?\d+))?\n(?P<want>.*?)```", re.S)


def examples() -> list[Example]:
    out: list[Example] = []
    for m in EXAMPLE.finditer(spec_text()):
        kind = m.group("kind")
        want = m.group("want")
        if m.group("code") is not None:
            kind, want = "exit", f"{m.group('code')}\n{want}"
        out.append(Example(m.group("name"), m.group("src"), kind, want))
    return out


def flat_spec() -> str:
    return " ".join(spec_text().split())


def test_display_cap_is_documented_in_errors_and_limits() -> None:
    message = "value is too large to display"
    assert message in _section("9. Errors"), "missing from the panic table"
    assert message in _section("10. Limits"), "missing from the limits"


@pytest.mark.parametrize(
    "claim",
    [
        "that makes `a == a` true on any value",  # a scalar NaN is not equal to itself
        "scalar values and is indexed by them",  # indexing a Str is `not-indexable`
    ],
)
def test_retracted_claims_stay_out_of_the_spec(claim: str) -> None:
    assert claim not in flat_spec()


def test_examples_exist_and_are_uniquely_named() -> None:
    names = [e.name for e in examples()]
    assert len(names) >= 40
    assert len(names) == len(set(names))
    assert spec_text().count("```vex example") == len(names), "an example block has no expected-result block"


def run_example(source: str) -> tuple[str, str, int | None]:
    """Compile and run: (outcome kind, text, exit code)."""
    try:
        program = compile_text(source, "spec.vx")
    except VexError as e:
        return "error", "".join(_diagnostic_key(d) + "\n" for d in e.diagnostics), None
    assert program.main is not None
    out = io.StringIO()
    vm = VM(out)
    try:
        result = vm.run_function(program.main)
    except VexRuntimeError as e:
        return "panic", out.getvalue() + e.message + "\n", None
    except VexExit as e:
        return "exit", out.getvalue(), e.code
    return "output", out.getvalue(), result if type(result) is int else None


def _diagnostic_key(d: object) -> str:
    code = getattr(d, "code")
    span = getattr(d, "span")
    return f"{code} {span.line}:{span.col}" if span is not None else str(code)


@pytest.mark.parametrize("example", examples(), ids=lambda e: e.name)
def test_spec_example(example: Example) -> None:
    kind, text, code = run_example(example.source)
    if example.kind == "exit":
        expected_code, _, want = example.expected.partition("\n")
        assert (kind, text, code) in (("exit", want, int(expected_code)), ("output", want, int(expected_code)))
        return
    assert kind == example.kind, text
    assert text == example.expected
    assert code is None
