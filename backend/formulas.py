"""Versioned, diagnostic VAK evaluation using a restricted arithmetic AST."""

from __future__ import annotations

import ast
import json
import math
import re
from pathlib import Path

from .analytics import snapshot
from .config import REGISTRY

LIMS_ALIASES = {
    "LIMS:24-2000.Pipeline.95%.T": ("LIMS_95_T", "lims.ht.2.95%.T"),
}


def evaluate_expression(expression: str, inputs: dict[str, float]) -> float:
    tree = ast.parse(expression, mode="eval")

    def calculate(node):
        if isinstance(node, ast.Expression):
            return calculate(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (float, int):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in inputs or inputs[node.id] is None:
                raise ValueError(f"Нет значения {node.id}")
            return inputs[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = calculate(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp):
            left, right = calculate(node.left), calculate(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                if abs(right) < 1e-9:
                    raise ValueError("Деление на ноль или почти нулевой знаменатель")
                return left / right
        raise ValueError("Разрешены только числа, теги, скобки и операции + − × /")

    result = calculate(tree)
    if not math.isfinite(result):
        raise ValueError("Результат не является конечным числом")
    return result


def formula_results(directory: Path, at: str) -> dict:
    registry = json.loads(REGISTRY.read_text())
    manifest = json.loads((directory / "manifest.json").read_text())
    stored = {item["id"]: item for item in manifest.get("formulas", [])}
    definitions = []
    for canonical in registry.get("formulas", []):
        item = stored.get(canonical["id"], {})
        definitions.append({**item, **canonical})
    values = {v["metric_id"]: v for v in snapshot(directory, at)["values"]}
    results = []
    for definition in definitions:
        f = dict(definition)
        expression = f["expression"].replace(",", ".").replace("x", "*").replace("×", "*")
        dependencies = []
        for alias, (variable, metric_id) in LIMS_ALIASES.items():
            if alias in expression:
                expression = expression.replace(alias, variable)
                dependencies.append((variable, metric_id))
        plant = f.get("plant", "ht" if f["id"].startswith("24") else "avt")
        tags = list(dict.fromkeys(re.findall(r"\b[A-Z]\d+\b", expression)))
        data = [
            {
                "tag": tag,
                **{
                    k: values.get(f"{plant}.{tag}", {}).get(k)
                    for k in ("value", "timestamp", "flags", "freshness")
                },
            }
            for tag in tags
        ]
        data.extend(
            {
                "tag": variable,
                **{
                    k: values.get(metric_id, {}).get(k)
                    for k in ("value", "timestamp", "flags", "freshness")
                },
            }
            for variable, metric_id in dependencies
        )
        for item in data:
            item["flags"] = item["flags"] or []
        inputs = {item["tag"]: item["value"] for item in data}
        substitution = re.sub(
            r"\b[A-Z]\d+\b",
            lambda m: "нет данных" if inputs.get(m[0]) is None else f"({inputs[m[0]]:.6g})",
            expression,
        )
        f.update(inputs=data, substitution=substitution, result=None)
        issues = []
        if "LIMS:" in expression:
            f["status"] = "unresolved"
            issues.append(
                "Не установлено соответствие зависимости LIMS:24-2000.Pipeline лабораторной точке"
            )
        elif any(item["value"] is None for item in data):
            issues.append("Нет необходимых входных измерений")
        elif any(item["freshness"] != "fresh" for item in data):
            issues.append("Входные КИП устарели относительно выбранного момента")
        elif any(set(item["flags"] or []) & {"invalid", "conflict"} for item in data):
            issues.append("Входные измерения недостоверны")
        else:
            try:
                f["result"] = evaluate_expression(expression, inputs)
            except (ValueError, SyntaxError, ZeroDivisionError) as exc:
                issues.append("Ошибка записи формулы" if isinstance(exc, SyntaxError) else str(exc))
                f["status"] = "invalid"
        if any(set(item["flags"] or []) & {"flatline", "suspect"} for item in data):
            issues.append("Есть подозрительные входные измерения; результат только для диагностики")
        if f.get("status") == "verified" and issues:
            f["status"] = "experimental"
        f["reason"] = ". ".join(filter(None, [definition.get("reason"), *issues]))
        results.append(f)
    return {"formulas": results}
