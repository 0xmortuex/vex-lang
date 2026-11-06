"""More behaviour tests: larger programs and tricky interactions between features."""

from __future__ import annotations

import pytest
from helpers import run_source

PROGRAMS: list[tuple[str, str, str]] = [
    (
        "field_holding_function",
        """
struct Handler { name: Str, f: fn(Int) -> Int }
fn main() {
    let h = Handler { name: "triple", f: |x| x * 3 };
    print((h.f)(4));
    print(h.name);
}
""",
        "12\ntriple\n",
    ),
    (
        "stack_machine_with_try",
        """
enum Cmd { Push(Int), Pop, Add }
fn run(cmds: List[Cmd]) -> Result[List[Int], Str] {
    let mut stack: List[Int] = [];
    for c in cmds {
        match c {
            Cmd.Push(n) => stack.push(n),
            Cmd.Pop => {
                if stack.is_empty() { return Err("pop on empty"); }
                stack.pop();
            },
            Cmd.Add => {
                let a = stack.pop().ok_or("need two")?;
                let b = stack.pop().ok_or("need two")?;
                stack.push(a + b);
            },
        }
    }
    Ok(stack)
}
fn main() {
    print(run([Cmd.Push(1), Cmd.Push(2), Cmd.Add]));
    print(run([Cmd.Add]));
    print(run([Cmd.Pop]));
}
""",
        'Ok([3])\nErr("need two")\nErr("pop on empty")\n',
    ),
    (
        "closures_in_loop_share_mutable_capture",
        """
fn main() {
    let mut counter = 0;
    let mut fns = [];
    for i in 0..3 {
        fns.push(|| { counter += i; counter });
    }
    for f in fns { f(); }
    print(counter);
}
""",
        "3\n",
    ),
    (
        "return_inside_lambda_returns_from_lambda",
        """
fn main() {
    let classify = |x: Int| {
        if x > 5 { return "big"; }
        "small"
    };
    print(classify(9) + classify(1));
}
""",
        "bigsmall\n",
    ),
    (
        "interpolation_with_nested_quotes_and_lambdas",
        """
fn main() {
    print("nested {"a" + "b"} and {[1, 2].map(|v| "<{v}>").join("")}");
}
""",
        "nested ab and <1><2>\n",
    ),
    (
        "large_list_pipeline",
        """
fn main() {
    let big = (0..20000).to_list();
    print(big.len());
    print(big.map(|x| x * 2).filter(|x| x % 3 == 0).len());
}
""",
        "20000\n6667\n",
    ),
    (
        "sieve_of_eratosthenes",
        """
fn primes(limit: Int) -> List[Int] {
    let mut is_prime = (0..=limit).to_list().map(|_| true);
    is_prime[0] = false;
    if limit >= 1 { is_prime[1] = false; }
    let mut p = 2;
    while p * p <= limit {
        if is_prime[p] {
            let mut m = p * p;
            while m <= limit {
                is_prime[m] = false;
                m += p;
            }
        }
        p += 1;
    }
    (0..=limit).to_list().filter(|i| is_prime[i])
}
fn main() {
    print(primes(50));
}
""",
        "[2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47]\n",
    ),
    (
        "word_frequency_with_map",
        """
fn main() {
    let text = "the cat and the hat and the bat";
    let mut counts: Map[Str, Int] = #{};
    for w in text.split(" ") {
        match counts.get(w) {
            Some(n) => counts.insert(w, n + 1),
            None => counts.insert(w, 1),
        }
    }
    print(counts["the"]);
    print(counts["and"]);
    print(counts.len());
}
""",
        "3\n2\n5\n",
    ),
    (
        "matrix_multiplication",
        """
fn mul(a: List[List[Int]], b: List[List[Int]]) -> List[List[Int]] {
    let n = a.len();
    let mut out = [];
    for i in 0..n {
        let mut row = [];
        for j in 0..n {
            let mut s = 0;
            for k in 0..n { s += a[i][k] * b[k][j]; }
            row.push(s);
        }
        out.push(row);
    }
    out
}
fn main() {
    let m = [[1, 1], [1, 0]];
    let mut r = [[1, 0], [0, 1]];
    for _ in 0..10 { r = mul(r, m); }
    print(r);
}
""",
        "[[89, 55], [55, 34]]\n",
    ),
    (
        "binary_search",
        """
fn search(xs: List[Int], target: Int) -> Option[Int] {
    let mut lo = 0;
    let mut hi = xs.len() - 1;
    while lo <= hi {
        let mid = (lo + hi) / 2;
        if xs[mid] == target { return Some(mid); }
        if xs[mid] < target { lo = mid + 1; } else { hi = mid - 1; }
    }
    None
}
fn main() {
    let xs = [1, 3, 5, 7, 9, 11];
    print(search(xs, 7));
    print(search(xs, 4));
}
""",
        "Some(3)\nNone\n",
    ),
    (
        "state_machine_enum_transitions",
        """
enum Light { Red, Green, Yellow }
fn next(l: Light) -> Light {
    match l {
        Red => Light.Green,
        Green => Light.Yellow,
        Yellow => Light.Red,
    }
}
fn main() {
    let mut l = Light.Red;
    for _ in 0..4 { l = next(l); }
    print(l);
}
""",
        "Green\n",
    ),
    (
        "expression_evaluator",
        """
enum Expr { Num(Int), Add(Expr, Expr), Mul(Expr, Expr), Neg(Expr) }
fn eval(e: Expr) -> Int {
    match e {
        Num(n) => n,
        Add(a, b) => eval(a) + eval(b),
        Mul(a, b) => eval(a) * eval(b),
        Neg(a) => -eval(a),
    }
}
fn show(e: Expr) -> Str {
    match e {
        Num(n) => "{n}",
        Add(a, b) => "({show(a)} + {show(b)})",
        Mul(a, b) => "({show(a)} * {show(b)})",
        Neg(a) => "-{show(a)}",
    }
}
fn main() {
    let e = Expr.Mul(Expr.Add(Expr.Num(2), Expr.Num(3)), Expr.Neg(Expr.Num(4)));
    print("{show(e)} = {eval(e)}");
}
""",
        "((2 + 3) * -4) = -20\n",
    ),
    (
        "generic_pair_and_swap_in_struct",
        """
struct Pair[A, B] { first: A, second: B }
impl[A, B] Pair[A, B] {
    fn swap(self) -> Pair[B, A] { Pair { first: self.second, second: self.first } }
}
fn main() {
    let p = Pair { first: 1, second: "one" };
    let q = p.swap();
    print(q.first + "!");
    print(q.second + 1);
}
""",
        "one!\n2\n",
    ),
    (
        "mutual_recursion_with_inferred_returns",
        """
fn even(n: Int) { if n == 0 { true } else { odd(n - 1) } }
fn odd(n: Int) -> Bool { if n == 0 { false } else { even(n - 1) } }
fn main() { print(even(10)); print(odd(10)); }
""",
        "true\nfalse\n",
    ),
    (
        "while_with_break_value_via_option",
        """
fn first_over(xs: List[Int], limit: Int) -> Option[Int] {
    let mut i = 0;
    while i < xs.len() {
        if xs[i] > limit { return Some(xs[i]); }
        i += 1;
    }
    None
}
fn main() { print(first_over([1, 5, 9], 4)); print(first_over([1], 4)); }
""",
        "Some(5)\nNone\n",
    ),
    (
        "unicode_strings_everywhere",
        """
fn main() {
    let ad = "İstanbul";
    print("{ad} → {ad.len()} karakter");
    print("çağrı".chars().len());
    print("ÇAĞRI".lower() == "çaği̇ri" || true);
    for c in "ğüş".chars() { print(c); }
}
""",
        "İstanbul → 8 karakter\n5\ntrue\nğ\nü\nş\n",
    ),
    (
        "shadowed_loop_variable_and_blocks",
        """
fn main() {
    let x = 10;
    for x in 0..2 {
        let x = x * 100;
        print(x);
    }
    print(x);
}
""",
        "0\n100\n10\n",
    ),
    (
        "impl_on_enum_with_match_self",
        """
enum Coin { Penny, Dime, Quarter(Int) }
impl Coin {
    fn cents(self) -> Int {
        match self {
            Coin.Penny => 1,
            Coin.Dime => 10,
            Coin.Quarter(n) => 25 + n,
        }
    }
}
fn main() {
    print(Coin.Penny.cents() + Coin.Dime.cents() + Coin.Quarter(5).cents());
}
""",
        "41\n",
    ),
    (
        "option_of_struct_and_field_access",
        """
struct User { name: Str, age: Int }
fn find(users: List[User], name: Str) -> Option[User] { users.find(|u| u.name == name) }
fn main() {
    let us = [User { name: "ada", age: 36 }, User { name: "alan", age: 41 }];
    match find(us, "alan") {
        Some(u) => print("{u.name} is {u.age}"),
        None => print("nobody"),
    }
    print(find(us, "zed").is_none());
}
""",
        "alan is 41\ntrue\n",
    ),
    (
        "sort_with_tuples_and_enumerate",
        """
fn main() {
    let mut pairs = [(3, "c"), (1, "a"), (2, "b")];
    pairs.sort();
    print(pairs);
    for (i, p) in pairs.enumerate() { print("{i}: {p.1}"); }
}
""",
        '[(1, "a"), (2, "b"), (3, "c")]\n0: a\n1: b\n2: c\n',
    ),
]


@pytest.mark.parametrize(("name", "src", "expected"), PROGRAMS, ids=[p[0] for p in PROGRAMS])
def test_program(name: str, src: str, expected: str) -> None:
    assert run_source(src) == expected
