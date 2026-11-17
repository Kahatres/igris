"""Tool registry, safe calculator, clock and TOOL: line parser."""

from __future__ import annotations

import ast
import json
import math
import operator
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone as _dt_timezone
from typing import Callable, Optional

MAX_EXPRESSION_LENGTH = 200
MAX_EXPONENT = 100
MAX_INTEGER_BITS = 10_000


@dataclass(frozen=True)
class Tool:
    """A named callable the assistant can invoke."""

    name: str
    description: str
    func: Callable[..., str]
    # most runs per assistant turn (None: no limit beyond the turn's tool steps)
    max_per_turn: Optional[int] = None


class ToolError(RuntimeError):
    """Raised when a tool fails or is given invalid input."""


class ToolRegistry:
    """Holds tools by name."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._turn = threading.local()

    def set_turn_text(self, text: str | None) -> None:
        """Record the human message of the turn running on this thread."""
        self._turn.text = text

    def turn_text(self) -> str | None:
        """The human message of this thread's current turn, or None outside a turn.

        Tools use it to check that a value came from the owner's own words rather
        than from model output or from data a tool returned.
        """
        return getattr(self._turn, "text", None)

    def register(self, name: str, description: str) -> Callable:
        """Decorator registering a function as a tool."""

        def decorator(func: Callable[..., str]) -> Callable[..., str]:
            self.add(Tool(name, description, func))
            return func

        return decorator

    def add(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name}") from None

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> str:
        return "\n".join(
            f"- {n}: {self._tools[n].description}" for n in self.names()
        )

    def run(self, name: str, args: dict) -> str:
        """Run a tool; any exception raised by it becomes ToolError."""
        tool = self.get(name)
        try:
            return str(tool.func(**args))
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"tool {name} failed: {exc}") from exc


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval(node: ast.AST):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if type(node.value) in (int, float):
            return node.value
        raise ToolError("only numbers are allowed")
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        left = _eval(node.left)
        right = _eval(node.right)
        if isinstance(node.op, ast.Pow) and right > MAX_EXPONENT:
            raise ToolError("exponent too large")
        if isinstance(left, int) and isinstance(right, int):
            bits = 0
            if isinstance(node.op, ast.Pow) and right >= 0:
                bits = left.bit_length() * right
            elif isinstance(node.op, ast.Mult) and left and right:
                bits = left.bit_length() + right.bit_length()
            if bits > MAX_INTEGER_BITS:
                raise ToolError("integer result too large")
        return _BIN_OPS[type(node.op)](left, right)
    raise ToolError("unsupported expression")


def calculate(expression: str) -> str:
    """Safely evaluate an arithmetic expression."""
    if not isinstance(expression, str):
        raise ToolError("expression must be a string")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        raise ToolError("expression too long")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        raise ToolError(f"invalid expression: {exc}") from exc
    try:
        result = _eval(tree)
    except ZeroDivisionError:
        raise ToolError("division by zero") from None
    except (OverflowError, RecursionError, MemoryError) as exc:
        raise ToolError(f"evaluation failed: {exc}") from exc
    if isinstance(result, float):
        if not math.isfinite(result):
            raise ToolError("result is not a finite number")
        if result.is_integer():
            result = int(result)
    return str(result)


def now(timezone: str = "UTC", clock: Optional[Callable[[], float]] = None) -> str:
    """Return the current time in ISO 8601 for an IANA zone."""
    if timezone == "UTC":
        tz = _dt_timezone.utc
    else:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(timezone)
        except Exception as exc:
            raise ToolError(f"unknown timezone: {timezone}") from exc
    ts = (clock or time.time)()
    return datetime.fromtimestamp(ts, tz).isoformat()


_CALL_RE = re.compile(r"^TOOL:\s*([A-Za-z_][\w\-]*)\s*(.*)$")


CONTENT_OPEN, CONTENT_CLOSE = "<<<CONTENT", ">>>CONTENT"


def parse_tool_call(text: str) -> Optional[tuple[str, dict]]:
    """Parse a reply that is entirely one tool call; None otherwise.

    The whole (stripped) reply must be a ``TOOL: name {json}`` line. Long text (a document)
    may follow between a ``<<<CONTENT`` line and a final ``>>>CONTENT`` line that ends the
    reply; it becomes the ``content`` argument. A ``TOOL:`` line inside prose, quotes, code
    fences or a quoted document is never a call, so quoted untrusted text cannot run tools.
    """
    lines = text.strip().splitlines()
    if not lines:
        return None
    match = _CALL_RE.match(lines[0].strip())
    if match is None:
        return None
    name, rest = match.group(1), match.group(2).strip()
    args: object = {}
    if rest:
        try:
            args = json.loads(rest)
        except ValueError:
            return None
    if not isinstance(args, dict):
        return None
    if len(lines) > 1:
        if (len(lines) < 3 or lines[1].strip() != CONTENT_OPEN
                or lines[-1].strip() != CONTENT_CLOSE):
            return None
        args["content"] = "\n".join(lines[2:-1])
    return name, args


def default_registry() -> ToolRegistry:
    """Registry with the built-in tools."""
    reg = ToolRegistry()
    reg.add(Tool("calculate", "Evaluate an arithmetic expression.", calculate))
    reg.add(Tool("now", "Current ISO 8601 time in an IANA timezone.", now))
    return reg
