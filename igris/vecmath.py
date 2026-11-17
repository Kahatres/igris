"""Exact rational vectors and bounded, safe arithmetic for written answers.

Comparisons accept rounding to four or more decimal places (half a last-place
unit), in addition to ``tol``. Coarser decimal approximations get only ``tol``.
No expression is executed as Python code.
"""

import ast
from fractions import Fraction
import math
import re


class VecError(ValueError):
    """Invalid vector input or an undefined vector operation."""


def _latex(text):
    text = text.strip().strip("$").replace(r"\left", "").replace(r"\right", "")
    text = text.replace(r"\langle", "<").replace(r"\rangle", ">")
    text = text.replace(r"\,", "").replace(r"\!", "").replace(r"\displaystyle", "")
    matrix = re.fullmatch(r"\\begin\{([pb]matrix)\}(.*?)\\end\{\1\}", text.strip(), re.DOTALL)
    if matrix:
        text = "(" + matrix.group(2).replace(r"\\", ",").replace("&", ",") + ")"
    for _ in range(12):
        changed = re.sub(r"\\(?:d?frac)\{([^{}]+)\}\{([^{}]+)\}", r"((\1)/(\2))", text)
        changed = re.sub(r"\\sqrt\{([^{}]+)\}", r"sqrt(\1)", changed)
        changed = re.sub(r"\\boxed\{([^{}]+)\}", r"(\1)", changed)
        if changed == text:
            break
        text = changed
    return text.replace(r"\cdot", "*").replace("−", "-")


def _number(value):
    if isinstance(value, bool):
        raise VecError("A number is required")
    if isinstance(value, (int, Fraction)):
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise VecError("A finite number is required")
        return Fraction(str(value))
    if not isinstance(value, str) or len(value) > 500:
        raise VecError("A short numeric expression is required")
    text = _latex(value)

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return Fraction(ast.get_source_segment(text, node))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            return (-1 if isinstance(node.op, ast.USub) else 1) * visit(node.operand)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            return a / b
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "sqrt" and len(node.args) == 1 and not node.keywords):
            n = visit(node.args[0])
            if n < 0:
                raise VecError("Square roots must be real")
            if isinstance(n, Fraction):
                top, bottom = math.isqrt(n.numerator), math.isqrt(n.denominator)
                if top * top == n.numerator and bottom * bottom == n.denominator:
                    return Fraction(top, bottom)
            return math.sqrt(n)
        raise VecError("Only numbers, arithmetic and sqrt(...) are allowed")

    try:
        result = visit(ast.parse(text, mode="eval").body)
        if not math.isfinite(result):
            raise VecError("A finite number is required")
        return result
    except (SyntaxError, TypeError, ValueError, ZeroDivisionError, OverflowError, RecursionError) as exc:
        raise VecError("Invalid numeric expression") from exc


def _parts(text):
    """Split a vector's components, ignoring commas inside parentheses."""
    if text[:1] in ("(", "<", "[") and text[-1:] == {"(": ")", "<": ">", "[": "]"}[text[0]]:
        text = text[1:-1]
    depth, start, parts = 0, 0, []
    for i, char in enumerate(text):
        depth += (char == "(") - (char == ")")
        if char == "," and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts


def parse(text):
    """Read a 2D/3D rational vector in component or i/j/k notation."""
    if not isinstance(text, str) or not text.strip() or len(text) > 500:
        raise VecError("A vector with two or three components is required")
    text = _latex(text)
    if "," in text:
        values = [_number(part) for part in _parts(text)]
    else:
        compact = re.sub(r"\s+", "", text)
        terms = list(re.finditer(r"([+-]?)(\d+(?:\.\d+)?(?:/\d+)?|\.\d+)?([ijk])", compact))
        if not terms or "".join(t.group(0) for t in terms) != compact:
            raise VecError("Invalid vector notation")
        values = [Fraction(0)] * (3 if "k" in compact else 2)
        for term in terms:
            sign, coefficient, axis = term.groups()
            if term.start() and not sign:
                raise VecError("Vector terms must be separated by a sign")
            values["ijk".index(axis)] += (-1 if sign == "-" else 1) * _number(coefficient or "1")
    if len(values) not in (2, 3) or not all(isinstance(n, Fraction) for n in values):
        raise VecError("Vectors need two or three rational components")
    return tuple(values)


def _vector(value):
    if isinstance(value, str):
        return parse(value)
    if not isinstance(value, (tuple, list)) or len(value) not in (2, 3):
        raise VecError("Vectors need two or three components")
    values = tuple(_number(n) for n in value)
    if not all(isinstance(n, Fraction) for n in values):
        raise VecError("Vector components must be rational")
    return values


def _pair(a, b):
    a, b = _vector(a), _vector(b)
    if len(a) != len(b):
        raise VecError("Vector dimensions must match")
    return a, b


def add(a, b):
    a, b = _pair(a, b)
    return tuple(x + y for x, y in zip(a, b))


def sub(a, b):
    a, b = _pair(a, b)
    return tuple(x - y for x, y in zip(a, b))


def scale(a, scalar):
    scalar = _number(scalar)
    if not isinstance(scalar, Fraction):
        raise VecError("The scale must be rational")
    return tuple(x * scalar for x in _vector(a))


def dot(a, b):
    a, b = _pair(a, b)
    return sum((x * y for x, y in zip(a, b)), Fraction(0))


def cross(a, b):
    a, b = _pair(a, b)
    if len(a) != 3:
        raise VecError("Cross product requires 3D vectors")
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def norm_sq(a):
    return dot(a, a)


def norm(a):
    """Length of a vector: exact when the square root is rational, otherwise a float."""
    squared = Fraction(norm_sq(a))
    top, bottom = math.isqrt(squared.numerator), math.isqrt(squared.denominator)
    if top * top == squared.numerator and bottom * bottom == squared.denominator:
        return Fraction(top, bottom)
    return math.sqrt(squared)


def angle_deg(a, b):
    a, b = _pair(a, b)
    squared = norm_sq(a) * norm_sq(b)
    if not squared:
        raise VecError("The angle of a zero vector is undefined")
    return math.degrees(math.acos(max(-1.0, min(1.0, float(dot(a, b)) / math.sqrt(squared)))))


def projection(a, onto):
    a, onto = _pair(a, onto)
    denominator = norm_sq(onto)
    if not denominator:
        raise VecError("Projection onto a zero vector is undefined")
    return scale(onto, dot(a, onto) / denominator)


def is_parallel(a, b):
    a, b = _pair(a, b)
    return all(a[i] * b[j] == a[j] * b[i] for i in range(len(a)) for j in range(i))


def is_perpendicular(a, b):
    return dot(a, b) == 0


def same_value(a, b, tol=1e-6):
    """Compare a written scalar/vector to a calculated value, safely.

    Invalid or unsupported claims return False. Decimal rounding is accepted only
    at four or more places, so a coarse decimal cannot conceal a wrong result.
    """
    try:
        if not isinstance(tol, (int, float)) or not math.isfinite(tol) or tol < 0:
            return False
        if isinstance(a, str):
            a = _latex(a)
        if isinstance(b, str):
            b = _latex(b)
        def is_vector(value):
            return (isinstance(value, (tuple, list)) or isinstance(value, str)
                    and ("," in value or re.search(r"[ijk]", value) is not None))

        def components(value):
            if isinstance(value, str):
                return _parts(value) if "," in value else parse(value)
            return value

        vector_a, vector_b = is_vector(a), is_vector(b)
        if vector_a or vector_b:
            aa, bb = components(a), components(b)
            return (vector_a and vector_b and len(aa) == len(bb) and len(aa) in (2, 3)
                    and all(same_value(x, y, tol) for x, y in zip(aa, bb)))
        left, right = _number(a), _number(b)
        rounding = 0
        if isinstance(a, str) and re.fullmatch(r"[+-]?\d*\.\d{4,}", a):
            rounding = 0.5 * 10 ** (-len(a.split(".")[1]))
        return abs(left - right) <= max(tol, rounding) + 1e-15
    except (VecError, TypeError, OverflowError, RecursionError):
        return False


def compute(op, args):
    """Dispatch a supported operation on positional JSON-compatible arguments."""
    operations = {name: globals()[name] for name in (
        "parse", "add", "sub", "scale", "dot", "cross", "norm", "norm_sq", "angle_deg",
        "projection", "is_parallel", "is_perpendicular", "same_value")}
    if not isinstance(op, str) or op not in operations or not isinstance(args, (list, tuple)):
        raise VecError("Unknown operation or invalid args list")
    try:
        return operations[op](*args)
    except (TypeError, OverflowError, ZeroDivisionError) as exc:
        raise VecError(f"Invalid arguments for {op}") from exc
