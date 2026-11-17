"""Bounded identity checks using an optional SymPy child process.

Untrusted text is read by our own small grammar into tuples. Only integers,
whitelisted operators and explicitly constructed symbols reach SymPy; none of
its string expression parsers are used. Equalities are checked as identities on
their common real domain, not as equations to solve or proofs of a whole problem.
Unsupported notation and inconclusive numerical evidence remain unverified.
"""

import importlib.util
import json
import math
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time


MAX_LENGTH = 1000
MAX_DEPTH = 24
MAX_TOKENS = 256
MAX_LINES = 32
LINE_TIMEOUT = 3.0
_FUNCTIONS = {"sin", "cos", "tan", "ln", "exp", "sqrt", "det", "norm",
              "dot", "cross", "diff", "integrate", "limit"}
_GREEK = {"alpha", "beta", "gamma", "theta", "phi", "lambda", "mu", "sigma"}
_TOKEN = re.compile(r"\\[A-Za-z]+|\\\\|\\[!,;| ]|\d+(?:\.\d+)?|[A-Za-z]+|\|\||[^\s]")


class Unreadable(ValueError):
    """Input is outside the deliberately restricted grammar or resource budget."""


def available():
    """Check availability without making SymPy a runtime import dependency."""
    try:
        return importlib.util.find_spec("sympy") is not None
    except (ImportError, ValueError):
        return False


def _bounded(text):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_LENGTH:
        raise Unreadable("Expression length limit exceeded or empty input")
    depth = 0
    for char in text:
        if char in "({[":
            depth += 1
            if depth > MAX_DEPTH:
                raise Unreadable("Expression nesting limit exceeded")
        elif char in ")}]":
            depth -= 1
            if depth < 0:
                raise Unreadable("Unbalanced delimiters")
    if depth:
        raise Unreadable("Unbalanced delimiters")


class _Reader:
    def __init__(self, text):
        _bounded(text)
        text = text.strip().strip("$")
        for command in (r"\left", r"\right", r"\displaystyle", r"\,", r"\!", r"\;", r"\ "):
            text = text.replace(command, "")
        text = text.replace("−", "-").replace(r"\lVert", "||").replace(r"\rVert", "||")
        text = text.replace(r"\|", "||").replace(r"\langle", "(").replace(r"\rangle", ")")
        self.tokens = _TOKEN.findall(text)
        if len(self.tokens) > MAX_TOKENS:
            raise Unreadable("Expression token limit exceeded")
        self.i = 0

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else ""

    def take(self, wanted=None):
        token = self.peek()
        if not token or wanted is not None and token != wanted:
            raise Unreadable("Unexpected or missing token")
        self.i += 1
        return token

    def read(self):
        result = self.expression()
        if self.peek():
            raise Unreadable("Unsupported trailing notation")
        stack = [(result, 0)]
        while stack:
            node, depth = stack.pop()
            if depth > MAX_DEPTH:
                raise Unreadable("Expression tree depth limit exceeded")
            stack.extend((child, depth + 1) for child in node[1:] if isinstance(child, tuple))
        return result

    def expression(self):
        node = self.product()
        while self.peek() in ("+", "-"):
            node = (self.take(), node, self.product())
        return node

    def product(self):
        node = self.unary()
        while True:
            token = self.peek()
            if token in ("*", "/", r"\cdot", r"\times"):
                self.take()
                node = (token, node, self.unary())
            elif (token in ("(", "{", r"\frac", r"\dfrac", r"\sqrt", r"\begin")
                  or token and (token.lstrip("\\") in _FUNCTIONS | _GREEK
                                or re.fullmatch(r"[A-Za-z]|\d+(?:\.\d+)?", token))):
                # Juxtaposition is multiplication; unknown words are never names.
                node = ("*", node, self.unary())
            else:
                return node

    def unary(self):
        if self.peek() in ("+", "-"):
            sign = self.take()
            return ("neg", self.unary()) if sign == "-" else self.unary()
        node = self.atom()
        if self.peek() == "^":
            self.take()
            node = ("^", node, self.unary())
        return node

    def group(self):
        opening = self.take()
        if opening not in ("(", "{", "["):
            raise Unreadable("A grouped expression is required")
        closing = {"(": ")", "{": "}", "[": "]"}[opening]
        values = [self.expression()]
        while self.peek() == ",":
            self.take()
            values.append(self.expression())
        self.take(closing)
        if len(values) == 1:
            return values[0]
        if len(values) not in (2, 3):
            raise Unreadable("Vectors need two or three entries")
        return ("vector", *values)

    def atom(self):
        token = self.peek()
        if token in ("(", "{", "["):
            return self.group()
        self.take()
        if re.fullmatch(r"\d+(?:\.\d+)?", token):
            if len(token) > 32:
                raise Unreadable("Number digit limit exceeded")
            top, dot, bottom = token.partition(".")
            return ("number", int(top + bottom), 10 ** len(bottom) if dot else 1)
        name = token.lstrip("\\")
        if token in (r"\frac", r"\dfrac"):
            # A first-order differential operator, never a string expression parser.
            if self.tokens[self.i:self.i + 3] == ["{", "d", "}"]:
                self.i += 3
                self.take("{")
                denominator = self.take()
                if re.fullmatch(r"d[A-Za-z]", denominator):
                    variable = ("symbol", denominator[1])
                elif denominator == "d":
                    variable = self.variable()
                else:
                    raise Unreadable("Unsupported derivative")
                self.take("}")
                return ("diff", self.expression(), variable)
            return ("/", self.group(), self.group())
        if name in _FUNCTIONS:
            if name in ("dot", "cross", "diff", "integrate", "limit"):
                self.take("(")
                args = [self.expression()]
                while self.peek() == ",":
                    self.take()
                    args.append(self.expression())
                self.take(")")
                arity = {"dot": (2,), "cross": (2,), "diff": (2,),
                         "integrate": (2, 4), "limit": (3,)}[name]
                if len(args) not in arity:
                    raise Unreadable("Unsupported function arguments")
                return (name, *args)
            power = None
            if name in ("sin", "cos", "tan") and self.peek() == "^":
                self.take()
                power = self.bound()
                if power[0] != "number" or power[1] < 0 or power[2] != 1:
                    raise Unreadable("Ambiguous trigonometric power")
            # Unbracketed trig arguments consume one power, as in sin x^2.
            result = (name, self.group() if self.peek() in ("(", "{") else self.unary())
            return ("^", result, power) if power is not None else result
        if token == r"\int":
            bounds = []
            if self.peek() == "_":
                self.take()
                bounds.append(self.bound())
                self.take("^")
                bounds.append(self.bound())
            # The differential terminates the integrand; a separate reader avoids
            # interpreting dx as a product or silently swallowing trailing text.
            start = self.i
            depth = 0
            while self.i < len(self.tokens):
                current = self.peek()
                if depth == 0 and re.fullmatch(r"d[A-Za-z]", current):
                    body = _Reader(" ".join(self.tokens[start:self.i])).read()
                    self.i += 1
                    return ("integrate", body, ("symbol", current[1]), *bounds)
                depth += (current in ("(", "{")) - (current in (")", "}"))
                self.i += 1
            raise Unreadable("An explicit differential such as dx is required")
        if token == r"\lim":
            self.take("_")
            self.take("{")
            variable = self.variable()
            self.take(r"\to")
            target = self.expression()
            self.take("}")
            return ("limit", self.expression(), variable, target)
        if token == r"\begin":
            self.take("{")
            kind = self.take()
            if kind not in ("pmatrix", "bmatrix", "vmatrix"):
                raise Unreadable("Unsupported matrix environment")
            self.take("}")
            rows = [[self.expression()]]
            while self.peek() in ("&", "\\\\"):
                separator = self.take()
                if separator == "\\\\":
                    rows.append([])
                rows[-1].append(self.expression())
                if len(rows) > 3 or len(rows[-1]) > 3:
                    raise Unreadable("Matrix dimension limit exceeded")
            self.take(r"\end")
            self.take("{")
            self.take(kind)
            self.take("}")
            if len({len(row) for row in rows}) != 1:
                raise Unreadable("Ragged matrix")
            node = ("matrix", *(tuple(["row", *row]) for row in rows))
            return ("det", node) if kind == "vmatrix" else node
        if token in ("|", "||"):
            body = self.expression()
            self.take(token)
            return ("bars" if token == "|" else "norm", body)
        if name in ("pi", "infty"):
            return (name,)
        if name in _GREEK or re.fullmatch("[A-Za-z]", token):
            return ("symbol", name)
        raise Unreadable("Unsupported notation")

    def variable(self):
        name = self.take()
        if not re.fullmatch("[A-Za-z]", name):
            raise Unreadable("A single variable is required")
        return ("symbol", name)

    def bound(self):
        return self.group() if self.peek() == "{" else self.atom()


def _build(node, sp):
    op = node[0]
    if op == "number":
        return sp.Rational(node[1], node[2])
    if op == "symbol":
        return sp.Symbol(node[1], real=True)
    if op in ("pi", "infty"):
        return sp.pi if op == "pi" else sp.oo
    args = [_build(child, sp) for child in node[1:]]
    if op == "matrix":
        return sp.ImmutableMatrix(args)
    if op == "row":
        return args
    if op == "vector":
        return sp.ImmutableMatrix(args)
    if op == "neg":
        return -args[0]
    if op == "+":
        return args[0] + args[1]
    if op == "-":
        return args[0] - args[1]
    if op in ("*", r"\cdot", r"\times", "dot", "cross"):
        a, b = args
        if isinstance(a, sp.MatrixBase) and isinstance(b, sp.MatrixBase):
            if op in (r"\cdot", "dot"):
                return a.dot(b)
            if op in (r"\times", "cross"):
                return a.cross(b)
        elif op in ("dot", "cross"):
            raise Unreadable("Vector operation requires vectors")
        return a * b
    if op == "/":
        return args[0] / args[1]
    if op == "^":
        exponent = args[1]
        if exponent.is_number and (exponent.is_real is not True or abs(exponent) > 100):
            raise Unreadable("Exponent limit exceeded")
        if args[0].is_Rational and exponent.is_Integer:
            bits = max(abs(int(args[0].p)).bit_length(), int(args[0].q).bit_length())
            if bits * abs(int(exponent)) > 4096:
                raise Unreadable("Integer power size limit exceeded")
        return args[0] ** exponent
    if op == "det":
        matrix = args[0]
        if not isinstance(matrix, sp.MatrixBase) or matrix.shape not in ((2, 2), (3, 3)):
            raise Unreadable("A two or three dimensional determinant is required")
        return matrix.det()
    if op in ("norm", "bars"):
        value = args[0]
        if op == "bars" and isinstance(value, sp.MatrixBase) and value.shape in ((2, 2), (3, 3)):
            return value.det()
        return sp.sqrt(value.dot(value)) if isinstance(value, sp.MatrixBase) else sp.Abs(value)
    if op == "diff":
        return sp.Derivative(*args)
    if op == "integrate":
        return sp.Integral(args[0], args[1] if len(args) == 2 else tuple(args[1:]))
    if op == "limit":
        # Two-sided real limits must agree; do not assume the right-hand limit.
        right = sp.Limit(*args, dir="+").doit()
        left = sp.Limit(*args, dir="-").doit()
        if sp.simplify(left - right) != 0:
            raise Unreadable("The two-sided limit does not exist or is unresolved")
        return right
    functions = {"sin": sp.sin, "cos": sp.cos, "tan": sp.tan,
                 "ln": sp.log, "exp": sp.exp, "sqrt": sp.sqrt}
    return functions[op](*args)


def _compare(a, b, sp):
    # Indefinite integration denotes a family of antiderivatives. Comparing a
    # selected primitive directly would incorrectly reject the usual "+ C".
    for integral, candidate in ((a, b), (b, a)):
        if (isinstance(integral, sp.Integral) and len(integral.limits) == 1
                and len(integral.limits[0]) == 1):
            variable = integral.limits[0][0]
            a, b = integral.function, sp.diff(candidate, variable)
            break
    a, b = a.doit(), b.doit()
    if isinstance(a, sp.MatrixBase) != isinstance(b, sp.MatrixBase):
        raise Unreadable("Cannot compare a scalar and a matrix")
    if isinstance(a, sp.MatrixBase):
        if a.shape != b.shape:
            return "false", 0
        pairs = list(zip(a, b))
    else:
        pairs = [(a, b)]
    unresolved, samples = False, 0
    for left, right in pairs:
        if left.has(sp.nan, sp.zoo, sp.oo, -sp.oo) or right.has(sp.nan, sp.zoo, sp.oo, -sp.oo):
            raise Unreadable("Undefined or non-finite expression")
        difference = sp.simplify(left - right)
        proved = difference == 0 or difference.is_zero is True
        disproved = difference.is_zero is False
        # Reproducible random rational substitutions, including negative values.
        # Numerical agreement alone never proves an identity, and a numerical
        # conflict with a symbolic result is always inconclusive.
        rng = random.Random(73819)
        symbols = sorted(left.free_symbols | right.free_symbols, key=lambda value: value.name)
        counterexample, exact_counterexample, valid_samples = False, False, 0
        for _ in range(7):
            substitutions = {symbol: sp.Rational(rng.choice((-7, -5, -2, 1, 3, 6, 9)),
                                                 rng.choice((1, 2, 3))) for symbol in symbols}
            exact_left, exact_right = left.subs(substitutions), right.subs(substitutions)
            lv, rv = exact_left.evalf(25), exact_right.evalf(25)
            if (lv.is_finite is not True or rv.is_finite is not True
                    or lv.is_real is not True or rv.is_real is not True):
                continue
            numeric_left, numeric_right = float(lv), float(rv)
            if not math.isfinite(numeric_left) or not math.isfinite(numeric_right):
                continue
            gap = abs(numeric_left - numeric_right)
            scale = max(1.0, abs(numeric_left), abs(numeric_right))
            samples += 1
            valid_samples += 1
            if gap > 1e-10 * scale:
                counterexample = True
                exact_counterexample |= sp.simplify(exact_left - exact_right).is_zero is False
        if proved:
            if counterexample or not valid_samples:
                unresolved = True
            continue
        if (bool(left.free_symbols) != bool(right.free_symbols)
                and difference.free_symbols and not disproved):
            # A statement such as x=2 or x^2=4 may be a valid solution equation,
            # rather than an asserted identity. Its problem-specific conditions
            # are unavailable here, so substitutions cannot certify a mistake.
            unresolved = True
            continue
        if counterexample and (disproved or exact_counterexample):
            return "false", samples
        # A nonzero polynomial/rational difference is an exact counterexample
        # certificate for an identity, independently of floating point evidence.
        if (not proved and counterexample and difference.is_rational_function(*symbols)
                and sp.cancel(difference) != 0):
            return "false", samples
        unresolved = True
    return ("unknown" if unresolved else "true"), samples


def _trees(text):
    _bounded(text)
    parts = text.split("=")
    if not 2 <= len(parts) <= 16:
        raise Unreadable("A bounded chain of equalities is required")
    return [_Reader(part).read() for part in parts]


def _check_line(text, sp):
    try:
        expressions = [_build(tree, sp) for tree in _trees(text)]
        statuses, samples = [], 0
        for a, b in zip(expressions, expressions[1:]):
            status, count = _compare(a, b, sp)
            statuses.append(status)
            samples += count
            if status == "false":
                break
        return {"status": "false" if "false" in statuses else
                  "unknown" if "unknown" in statuses else "true",
                  "equality": statuses.index("false") + 1 if "false" in statuses else None,
                  "checked": len(statuses), "samples": samples}
    except Exception:
        return {"status": "unreadable", "reason": "Unsupported or undefined expression"}


def _child_entry(output_fd):
    """Read raw text again using our grammar; import the optional engine once."""
    import sympy as sp

    lines = json.loads(sys.stdin.buffer.read(MAX_LINES * (MAX_LENGTH + 1) * 12))
    if not isinstance(lines, list) or len(lines) > MAX_LINES:
        raise Unreadable("Solution line limit exceeded")
    results = [_check_line(text, sp) for text in lines]
    with os.fdopen(output_fd, "w", encoding="utf-8") as output:
        json.dump(results, output, ensure_ascii=True)


def _run_lines(lines, timeout):
    # Redirect the actual stdout descriptor before importing the module, so even
    # optional libraries' native diagnostics cannot corrupt the JSON protocol.
    bootstrap = ("import os,sys; output=os.dup(1); os.dup2(2,1); "
                 "sys.path.insert(0,sys.argv[1]); "
                 "from igris.symcheck import _child_entry; _child_entry(output)")
    command = [sys.executable, "-I", "-c", bootstrap, str(Path(__file__).resolve().parent.parent)]
    process = None
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        stdout, _ = process.communicate(json.dumps(lines).encode("utf-8"),
                                        timeout=max(0, deadline - time.monotonic()))
        if process.returncode:
            raise ValueError("Child failed")
        results = json.loads(stdout)
        if (not isinstance(results, list) or len(results) != len(lines)
                or any(not isinstance(result, dict) or result.get("status") not in
                       {"true", "false", "unknown", "unreadable"} for result in results)):
            raise ValueError("Invalid child result")
        return results
    except subprocess.TimeoutExpired:
        reason = "Calculation time limit exceeded"
    except (OSError, ValueError, RuntimeError, TypeError):
        reason = "Calculation worker unavailable"
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate()
    return [{"status": "unreadable", "reason": reason} for _ in lines]


def verify_line(text, *, timeout=LINE_TIMEOUT):
    """Check every adjacent equality, with a hard wall-clock process deadline.

    ``true`` and ``false`` have symbolic support plus numeric spot checks;
    ``unknown`` is readable but inconclusive. A timeout is ``unreadable``.
    The deadline includes process startup and importing the optional library.
    """
    result = _verify_lines([text], timeout=timeout)[0]
    return {key: value for key, value in result.items() if key != "line"}


def verify_lines(lines):
    """Return per-line evidence, retaining unsupported lines as unverified.

    A leading equals sign continues the previous line's rightmost expression.
    No bindings or assumptions are inferred from preceding equalities.
    All readable lines share one child and a budget of three seconds per line,
    capped at twenty seconds including startup and the optional engine import.
    """
    return _verify_lines(lines)


def _verify_lines(lines, *, timeout=None):
    results, pending, positions = [], [], []
    previous = ""
    for number, line in enumerate(lines, 1):
        text = line
        if isinstance(line, str) and line.lstrip().startswith("=") and previous:
            text = previous + line.lstrip()
        try:
            if number > MAX_LINES:
                raise Unreadable("Solution line limit exceeded")
            _trees(text)
        except (Unreadable, RecursionError, ValueError):
            result = {"status": "unreadable", "reason": "Solution line limit exceeded" if
                      number > MAX_LINES else "Unsupported notation or input limit exceeded"}
        else:
            pending.append(text)
            positions.append(number - 1)
            result = {}
        results.append(dict(result, line=number))
        previous = (line.rsplit("=", 1)[-1].strip()
                    if isinstance(line, str) and len(line) <= MAX_LENGTH else "")
    if pending:
        budget = min(20.0, LINE_TIMEOUT * len(pending)) if timeout is None else timeout
        if not available():
            checked = [{"status": "unavailable", "reason": "SymPy is not installed"}] * len(pending)
        elif not isinstance(budget, (int, float)) or not 0 < budget <= 30:
            checked = [{"status": "unreadable", "reason": "Invalid time budget"}] * len(pending)
        else:
            checked = _run_lines(pending, budget)
        for index, result in zip(positions, checked):
            results[index] = dict(result, line=index + 1)
    return results
