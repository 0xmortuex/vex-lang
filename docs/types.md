# Vex type rules

This is the specification of what the checker accepts. The implementation is
`src/vex/checker.py` (inference), `src/vex/vtypes.py` (types and unification) and
`src/vex/exhaust.py` (pattern analysis). Notation: `Γ` is the environment, `e : τ` means
"expression `e` has type `τ`", `τ₁ ~ τ₂` means "`τ₁` unifies with `τ₂`".

## Types

```text
τ ::= Int | Float | Bool | Str | Unit | Range
    | List[τ] | Map[τ, τ]
    | Option[τ] | Result[τ, τ]            (built-in enums)
    | N[τ, …]                              (user struct / enum N applied to its type arguments)
    | (τ, …, τ)                            (tuple, at least 2 elements; () is Unit)
    | fn(τ, …) -> τ
    | T                                    (rigid type parameter, declared by the programmer)
```

Internally there are three more: unification variables `?n` (solved during inference),
`Never` (the type of `return`, `break`, `continue`, `panic(..)`) and an error type that
silences cascades after a reported error.

## Inference algorithm

Vex uses **unification-based inference with explicit generics**:

1. Parameter types, struct fields and variant payloads are always annotated.
2. Return types are optional. An unannotated return type starts as a fresh variable `?r`.
   Function bodies are checked **on demand**: when `f` is called and its return type is still
   unknown, `f`'s body is checked first. Recursion inside `f` just sees `?r` (a monomorphic
   use), which is sound for non-generic functions. A *generic* function that is called
   recursively before its return type is known is rejected with `recursive-inference`;
   annotate the return type.
3. Inside a body, `let` bindings, lambda parameters and `[]`/`None` literals get fresh variables
   which unification solves. Types flow in both directions: an expected type is passed down
   into lambdas and list literals (`xs.map(|x| …)` knows `x`), and call arguments are checked
   with lambda arguments last.
4. At the end of each function, any variable still unsolved in the type of some
   expression is an error (`cannot-infer`) unless another error was already reported in that
   function.

**Polymorphism.** Only top-level functions, methods, structs and enums declared with type
parameters are polymorphic. At each use their parameters are replaced by fresh variables
(instantiation). `let`-bound values and lambdas are *monomorphic*: `let id = |x| x;` can be used
at one type only. This is deliberate (it keeps inference simple and sound) and means there is
no let-generalisation to reason about.

**Soundness.** Within a generic function body the parameters `T` are rigid: they unify only with
themselves. Hence a generic function type-checks once, for all instantiations. Operations on
`T` require a bound:

| Bound | Grants | Satisfied by |
|-------|--------|--------------|
| `Eq` | `==`, `!=` | every type that contains no function |
| `Ord` | `<` `<=` `>` `>=` (and `Eq`) | `Int Float Str Bool Unit`, tuples and lists of `Ord` types |
| `Hash` | use as `Map` key (and `Eq`) | `Int Str Bool Unit` and tuples of those |

A bound is checked at every instantiation (`unsatisfied-bound`). Built-ins use them too:
`List.contains` needs `Eq`, `List.sort` needs `Ord`, `assert_eq` needs `Eq`.

## Expression rules

Literals: `1 : Int`, `1.0 : Float`, `"s" : Str`, `true : Bool`, `() : Unit`; a string with
interpolation is `Str` and its parts may have any type (they are displayed).

**Operators** (`a`, `b` of the same type — there are no implicit conversions):

```text
a + b        a, b : Int | Float | Str          result: that type
a - b  a * b  a / b  a % b      a, b : Int | Float
-a           a : Int | Float
a == b  a != b       a, b : τ with Eq τ        result: Bool
a < b  a <= b  a > b  a >= b      a, b : τ with Ord τ      result: Bool
a && b  a || b  !a   Bool operands (&& and || short-circuit)
a..b  a..=b          a, b : Int                result: Range
```

When the operand type is still a variable at the time the operator is checked, the
requirement is *deferred* and verified when the type is known (end of the function).

**Conditionals.** `if c { a } else { b }`: `c : Bool`; `a` and `b` unify, except that a branch
of type `Never` conforms to anything. `if` without `else` has type `Unit` and its block must be `Unit`.
`match e { p => b, … }`: every pattern is checked against `e`'s type, guards are `Bool`, arm bodies
unify (again with `Never` conforming), and the match must be exhaustive.

**Blocks.** The type of a block is its tail expression, or `Unit` if there is none, or `Never` if
some statement diverges and there is no tail. A function body must unify with the return type.

**Loops.** `while c { … }` and `for p in it { … }` have type `Unit`. `it` must be a `Range`
(elements `Int`) or a `List[T]` (elements `T`). `p` must be irrefutable.

**Calls.** `f(a₁…aₙ)`: `f : fn(τ₁…τₙ) -> τ`, each `aᵢ : τᵢ`; arity is checked. `x.m(a…)` resolves
`m` on the (already inferred) type of `x`: user `impl` methods first, then built-in methods of
that type constructor; the receiver is unified with the first parameter. If `x`'s type is
not known yet at that point the call is rejected (`cannot-infer`): annotate it. A struct field
holding a function is called as `(x.f)(…)`.

**`?`.** `e? ` where `e : Option[T]` has type `T` and requires the enclosing function (or lambda)
to return `Option[U]`. Where `e : Result[T, E]` it has type `T` and requires the enclosing function
to return `Result[U, E]` with the *same* `E`.

**Fields and indexing.** `s.f` for a struct looks up the field and substitutes the struct's type
arguments; `t.0` projects a tuple. `l[i]` needs `l : List[T]`, `i : Int` and has type `T`;
`m[k]` needs `m : Map[K, V]`, `k : K`, type `V`. `Str` is not indexable (use `char_at`/`chars`).

**Constructors.** A struct literal `S { f: e, … }` must give each field exactly once; type
arguments are fresh variables solved from the field values. A variant `E.V(a…)` (or `V(a…)` when
unique) is a function from the payload types to `E[…]`; `E.V` of a nullary variant is a value. Variants can
be passed as functions: `xs.map(Some)`.

## Mutability

A *root variable* of an assignment target is the variable at the left end: in `a.b[i].c = v` it is `a`.

- `x = v`, `x += v` need `x` declared `let mut` (parameters are immutable unless `mut`).
- `p.f = v` and `xs[i] = v` need the root variable to be mutable. Tuples are immutable.
- Calling a method declared `mut self`, or one of the mutating built-ins (`push`, `pop`, `insert`,
  `remove`, `remove_at`, `clear`, `extend`, `reverse`, `sort`), needs a mutable root.
- Passing a list, map or struct to a `mut` parameter needs a mutable root.
- Values are references: `let b = a;` aliases, it does not copy (see `copy()`).
- A captured `let mut` variable is shared by reference between the function and its closures.

## Patterns and exhaustiveness

Pattern typing: a literal pattern has the literal's type; `(p₁, …, pₙ)` needs an n-tuple;
`V(p…)` needs the variant's enum (arity checked); `S { f: p, … }` needs the struct, naming only
existing fields (and all of them unless `..` is present). Variables bind with the scrutinee
sub-type. Alternatives of an or-pattern must bind the same names at the same types.

Exhaustiveness uses Maranget's matrix algorithm over constructors: a value is covered when
some arm's pattern matches it. Arms with guards do not count towards coverage. If the arms
do not cover all values, the error shows one **witness** pattern that is missing
(e.g. `(Some(_), false)`). `let` and `for` patterns must be irrefutable. An arm that is covered
by earlier unguarded arms produces an `unreachable-pattern` **warning**.

## Integers and floats

`Int` is a signed 64-bit integer. **Every** `+ - * /`, unary `-`, `abs`, `pow`, `sum`, `math.gcd`, `Range.len` whose true result
is outside `-9223372036854775808 … 9223372036854775807` stops the program with
`integer overflow in <op>` (no wraparound, no saturation, no silent bignum). `/` truncates toward zero,
`%` takes the sign of the dividend (`-7 / 2 == -3`, `-7 % 3 == -1`), and division or remainder by zero is
a runtime error. `Float` follows IEEE-754 for `+ - *` (overflow gives `inf`, `NaN` propagates), but
`/` and `%` by `0.0` are runtime errors; `Float.to_int()` truncates and rejects `NaN`/`inf`.

## Known limits of the type system

No traits or interfaces beyond the three built-in bounds; no higher-kinded types; no let-polymorphism;
no explicit type arguments at call sites (`f[Int](x)`) — they are always inferred; `impl` blocks only
for your own structs and enums; no associated types or constants; closures cannot be recursive
(use a named function).
