from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ExpressionError(ValueError):
    """An expression is outside the frozen safe AST or cannot be evaluated."""


ALLOWED_REFERENCE_NAMESPACES = frozenset({"input", "outputs", "metrics", "runtime", "loop"})
COMPARISON_OPERATIONS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})
MAX_EXPRESSION_DEPTH = 16
MAX_EXPRESSION_NODES = 128


def validate_expression(expression: object) -> dict[str, object]:
    node_count = 0

    def visit(node: object, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > MAX_EXPRESSION_NODES:
            raise ExpressionError(f"expression node count exceeds {MAX_EXPRESSION_NODES}")
        if depth > MAX_EXPRESSION_DEPTH:
            raise ExpressionError(f"expression depth exceeds {MAX_EXPRESSION_DEPTH}")
        if not isinstance(node, dict):
            raise ExpressionError("expression node must be an object")
        operation = node.get("op")
        if operation == "const":
            if set(node) != {"op", "value"}:
                raise ExpressionError("const accepts only op and value")
            if not isinstance(node["value"], (str, int, float, bool, type(None))):
                raise ExpressionError("const value must be scalar")
            return
        if operation == "ref":
            if set(node) != {"op", "path"} or not isinstance(node.get("path"), str):
                raise ExpressionError("ref requires only a string path")
            path = str(node["path"])
            parts = path.split(".")
            if len(parts) < 2 or parts[0] not in ALLOWED_REFERENCE_NAMESPACES or any(not part for part in parts):
                raise ExpressionError(f"illegal reference path: {path}")
            return
        if operation in {"and", "or"}:
            if set(node) != {"op", "args"} or not isinstance(node.get("args"), list):
                raise ExpressionError(f"{operation} requires only an args array")
            arguments = node["args"]
            if len(arguments) < 2:
                raise ExpressionError(f"{operation} requires at least two arguments")
            for argument in arguments:
                visit(argument, depth + 1)
            return
        if operation in {"not", "exists"}:
            if set(node) != {"op", "arg"}:
                raise ExpressionError(f"{operation} requires only arg")
            if operation == "exists" and (
                not isinstance(node["arg"], dict) or node["arg"].get("op") != "ref"
            ):
                raise ExpressionError("exists argument must be a ref")
            visit(node["arg"], depth + 1)
            return
        if operation in COMPARISON_OPERATIONS:
            if set(node) != {"op", "left", "right"}:
                raise ExpressionError(f"{operation} requires left and right")
            visit(node["left"], depth + 1)
            visit(node["right"], depth + 1)
            return
        raise ExpressionError(f"unknown expression operation: {operation!r}")

    visit(expression, 1)
    return expression  # type: ignore[return-value]


def _resolve_reference(path: str, context: Mapping[str, object]) -> tuple[bool, object | None]:
    current: object = context
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def evaluate_expression(expression: object, context: Mapping[str, object]) -> object:
    validated = validate_expression(expression)

    def evaluate(node: dict[str, object]) -> object:
        operation = node["op"]
        if operation == "const":
            return node["value"]
        if operation == "ref":
            found, value = _resolve_reference(str(node["path"]), context)
            if not found:
                raise ExpressionError(f"missing reference: {node['path']}")
            return value
        if operation == "exists":
            argument = node["arg"]
            assert isinstance(argument, dict)
            found, _value = _resolve_reference(str(argument["path"]), context)
            return found
        if operation == "and":
            return all(bool(evaluate(argument)) for argument in node["args"])  # type: ignore[union-attr]
        if operation == "or":
            return any(bool(evaluate(argument)) for argument in node["args"])  # type: ignore[union-attr]
        if operation == "not":
            return not bool(evaluate(node["arg"]))  # type: ignore[arg-type]
        left = evaluate(node["left"])  # type: ignore[arg-type]
        right = evaluate(node["right"])  # type: ignore[arg-type]
        try:
            if operation == "eq":
                return left == right
            if operation == "ne":
                return left != right
            if operation == "gt":
                return left > right  # type: ignore[operator]
            if operation == "gte":
                return left >= right  # type: ignore[operator]
            if operation == "lt":
                return left < right  # type: ignore[operator]
            if operation == "lte":
                return left <= right  # type: ignore[operator]
        except TypeError as error:
            raise ExpressionError(f"incomparable expression values for {operation}") from error
        raise ExpressionError(f"unknown expression operation: {operation!r}")

    return evaluate(validated)
