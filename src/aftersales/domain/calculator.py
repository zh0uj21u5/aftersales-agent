"""Bounded AST arithmetic using exact rational numbers, never executable code."""

import ast
import operator
import re
from decimal import Decimal, localcontext
from fractions import Fraction


def calculate(expression: str) -> str:
    if not expression.strip() or len(expression) > 512:
        raise ValueError("Expression must contain 1 to 512 characters")
    tree = ast.parse(expression.strip(), mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ValueError("Expression is too complex")
    operations = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
    }

    def visit(node: ast.AST) -> Fraction:
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            literal = ast.get_source_segment(expression.strip(), node)
            if literal is None or not re.fullmatch(
                r"(?:\d{1,100}(?:\.\d{0,100})?|\.\d{1,100})", literal
            ):
                raise ValueError("Only bounded decimal literals are allowed")
            value = Fraction(literal)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in operations:
            value = operations[type(node.op)](visit(node.left), visit(node.right))
        else:
            raise ValueError("Only numbers, parentheses and + - * / are allowed")
        if value.numerator.bit_length() > 2048 or value.denominator.bit_length() > 2048:
            raise ValueError("Arithmetic result is too large")
        return value

    value = visit(tree.body)
    with localcontext() as context:
        context.prec = 50
        return format(Decimal(value.numerator) / Decimal(value.denominator), "f")
