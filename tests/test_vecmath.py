"""Exact vector arithmetic and safe comparison of written answers."""

from fractions import Fraction as F
import math

import pytest

from igris import vecmath as v


@pytest.mark.parametrize("text, expected", [
    ("(1, -2, 3/2)", (F(1), F(-2), F(3, 2))),
    ("<1,2>", (F(1), F(2))),
    ("2i - j + 3k", (F(2), F(-1), F(3))),
    ("i+j", (F(1), F(1))),
])
def test_parse(text, expected):
    assert v.parse(text) == expected
    assert all(isinstance(x, F) for x in v.parse(text))


def test_exact_operations_and_dispatch():
    a, b = v.parse("(1,2,3)"), v.parse("(4,5,6)")
    assert v.dot(a, b) == 32
    assert v.cross(v.parse("(1,0,0)"), v.parse("(0,1,0)")) == (0, 0, 1)
    assert v.projection(v.parse("(1,0)"), v.parse("(1,2)")) == (F(1, 5), F(2, 5))
    assert v.add(a, b) == (5, 7, 9)
    assert v.sub(b, a) == (3, 3, 3)
    assert v.scale(a, F(1, 2)) == (F(1, 2), 1, F(3, 2))
    assert v.norm_sq(a) == 14
    assert v.is_parallel(a, v.scale(a, 2))
    assert v.is_perpendicular(v.parse("(1,0)"), v.parse("(0,1)"))
    assert v.angle_deg(v.parse("(1,0)"), v.parse("(0,1)")) == 90
    assert v.compute("dot", ["(1,2,3)", "(4,5,6)"]) == 32
    assert v.compute("scale", ["(1,2)", "1/2"]) == (F(1, 2), 1)


@pytest.mark.parametrize("claim, exact", [
    ("sqrt(14)", math.sqrt(14)), ("3.7417", math.sqrt(14)),
    ("7/2", F(7, 2)), ("(1/3, sqrt(4))", (F(1, 3), F(2))),
    (r"\frac{7}{2}", F(7, 2)), (r"\sqrt{14}", math.sqrt(14)),
    ("2i-j+3k", (F(2), F(-1), F(3))),
    (r"\boxed{\frac{7}{2}}", F(7, 2)),
    (r"\begin{pmatrix}1/3\\2\end{pmatrix}", (F(1, 3), F(2))),
])
def test_same_value_accepts_exact_and_rounded_answers(claim, exact):
    assert v.same_value(claim, exact)


@pytest.mark.parametrize("claim", ["3.74", "sqrt(15)", "7/3", "nan", "__import__('os')", "1/0"])
def test_same_value_rejects_wrong_or_unsafe_answers(claim):
    assert not v.same_value(claim, math.sqrt(14))


def test_invalid_vectors_and_undefined_operations():
    with pytest.raises(v.VecError):
        v.angle_deg(v.parse("(0,0)"), v.parse("(1,0)"))
    with pytest.raises(v.VecError):
        v.dot(v.parse("(1,2)"), v.parse("(1,2,3)"))
    with pytest.raises(v.VecError):
        v.projection(v.parse("(1,2)"), v.parse("(0,0)"))
    for text in ("(1)", "(1,2,3,4)", "i + junk", "(1/0,2)", "ij"):
        with pytest.raises(v.VecError):
            v.parse(text)
    with pytest.raises(v.VecError):
        v.compute("__import__", [])


def test_norm_is_exact_for_rational_lengths_and_float_otherwise():
    from fractions import Fraction
    from igris import vecmath

    assert vecmath.compute("norm", ["(3,4,0)"]) == Fraction(5)
    assert vecmath.norm("(1/2,0)") == Fraction(1, 2)
    assert abs(vecmath.norm("(1,1)") - 2 ** 0.5) < 1e-12
    assert vecmath.same_value("5", vecmath.norm("(3,4,0)"))
