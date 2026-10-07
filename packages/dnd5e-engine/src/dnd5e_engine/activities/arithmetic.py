"""Closed integer/dice grammar. Parsing and validation never receive an RNG.

Only +, -, *, parentheses, dice and min/max are executable. Dice are visited
left to right, including function arguments; neither validation nor arithmetic
constants draw. This is the grammar used by the activity dice evaluator.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Expression:
    op: str
    value: int = 0
    args: tuple[Expression, ...] = ()

    @property
    def has_dice(self) -> bool:
        return self.op == "dice" or any(arg.has_dice for arg in self.args)


_TOKEN = re.compile(r"\s*(\d+|[A-Za-z_]+|[()+*,\-])")


class _Parser:
    def __init__(self, text: str) -> None:
        self.tokens: list[str] = []
        position = 0
        text = text.strip()
        if len(text) > 4096:
            raise ValueError("formula is too long")
        while position < len(text):
            match = _TOKEN.match(text, position)
            if match is None:
                raise ValueError(f"Unsupported formula token at {text[position:]!r}")
            self.tokens.append(match[1])
            position = match.end()
        if len(self.tokens) > 256:
            raise ValueError("formula has too many terms")
        self.index = 0

    def peek(self) -> str:
        return self.tokens[self.index] if self.index < len(self.tokens) else ""

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if not token or (expected is not None and token != expected):
            raise ValueError(f"Expected {expected or 'expression'}, got {token!r}")
        self.index += 1
        return token

    def expression(self, depth: int = 0) -> Expression:
        if depth > 64:
            raise ValueError("formula nesting is too deep")
        node = self.product(depth + 1)
        while self.peek() in ("+", "-"):
            op = self.take()
            node = Expression(op, args=(node, self.product(depth + 1)))
        return node

    def product(self, depth: int) -> Expression:
        node = self.factor(depth + 1)
        while self.peek() == "*":
            self.take()
            node = Expression("*", args=(node, self.factor(depth + 1)))
        return node

    def factor(self, depth: int) -> Expression:
        if depth > 64:
            raise ValueError("formula nesting is too deep")
        symbol = self.peek()
        if symbol in ("+", "-"):
            return Expression("unary" + self.take(), args=(self.factor(depth + 1),))
        if symbol == "d":
            node = Expression("int", 1)
        elif symbol == "(":
            self.take()
            node = self.expression(depth + 1)
            self.take(")")
        elif symbol in ("max", "min"):
            name = self.take()
            self.take("(")
            args = [self.expression(depth + 1)]
            while self.peek() == ",":
                self.take()
                args.append(self.expression(depth + 1))
            self.take(")")
            if len(args) < 2:
                raise ValueError("min/max require at least two arguments")
            node = Expression(name, args=tuple(args))
        elif symbol.isdecimal():
            node = Expression("int", int(self.take()))
        else:
            raise ValueError(f"Unsupported expression: {symbol!r}")
        if self.peek() == "d":
            self.take()
            size = self.take()
            count = scalar(node)
            if not size.isdecimal() or not 1 <= int(size) <= 10000 or not 0 <= count <= 10000:
                raise ValueError("invalid dice count or size")
            node = Expression("dice", int(size), (Expression("int", count),))
        return node


def parse_expression(text: str, *, allow_dice: bool = True) -> Expression:
    parser = _Parser(text)
    node = parser.expression()
    if parser.peek():
        raise ValueError(f"Unexpected formula suffix: {parser.peek()!r}")
    if not allow_dice and node.has_dice:
        raise ValueError("formula must be scalar")
    return node


def scalar(node: Expression) -> int:
    if node.has_dice:
        raise ValueError("formula must be scalar")
    return evaluate(node, None)


def evaluate(
    node: Expression, rng: random.Random | None, *, crit: bool = False, die_floor: int | None = None
) -> int:
    if node.op == "int":
        return node.value
    if node.op == "dice":
        if rng is None:
            raise ValueError("dice require a resolution RNG")
        count = node.args[0].value * (2 if crit else 1)
        return sum(max(rng.randint(1, node.value), die_floor or 1) for _ in range(count))
    values = [evaluate(arg, rng, crit=crit, die_floor=die_floor) for arg in node.args]
    match node.op:
        case "+":
            return values[0] + values[1]
        case "-":
            return values[0] - values[1]
        case "*":
            return values[0] * values[1]
        case "unary+":
            return values[0]
        case "unary-":
            return -values[0]
        case "min":
            return min(values)
        case "max":
            return max(values)
    raise ValueError(f"Unsupported expression operation: {node.op}")
