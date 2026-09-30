"""数据契约映射器（A6 / ADR-005）：JSONPath 简化实现的提取与拍平。

支持的路径子集：`$`（根）、`.key`（对象键）、`[*]`（数组全元素）。
上游换模型/改 schema = 改一行映射配置，下游零改动。
"""
from __future__ import annotations

from ..container.policy import ContractViolation
from ..container.signature import unified_method


def resolve_path(doc: dict, path: str):
    """在文档中求值路径；返回 (found, value)。支持 $.a.b 与 $.a[*]。"""
    if not path.startswith("$"):
        raise ContractViolation(f"非法映射路径: {path}")
    current: object = doc
    token = ""
    for raw in path[1:].split("."):
        token = raw
        if not token:
            continue
        if token.endswith("[*]"):
            key = token[:-3]
            items = _dig(current, key) if key else current
            if not isinstance(items, list):
                return False, None
            return True, [v for item in items for v in ([item] if key else item)]
        current = _dig(current, token)
        if current is _MISSING:
            return False, None
    return True, current


class _Missing:
    pass


_MISSING = _Missing()

TYPE_TABLE: dict[str, tuple] = {
    "str": (str,), "int": (int,), "float": (float,), "number": (int, float),
    "bool": (bool,), "list": (list,), "dict": (dict,),
}


def _check_type(value, expected: str) -> bool:
    types = TYPE_TABLE.get(expected)
    return isinstance(value, types) if types else True


def _dig(node: object, key: str):
    if isinstance(node, dict):
        return node.get(key, _MISSING)
    return _MISSING


@unified_method("contract.extract_by_mappings", 1, "data")
def extract_by_mappings(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-001：按输入映射从 source map 抽取字段（required/default/类型断言）。"""
    source = input["source"]
    values: dict = {}
    for field, spec in (input.get("mappings") or {}).items():
        found, value = resolve_path(source, spec["path"])
        if not found:
            if spec.get("required"):
                raise ContractViolation(f"必填字段缺失: {field} ({spec['path']})")
            value = spec.get("default")
        if found and value is not None and spec.get("type") and not _check_type(value, spec["type"]):
            raise ContractViolation(f"字段类型断言失败: {field}")
        values[field] = value
    output["values"] = values
    return True


@unified_method("contract.flatten_by_mappings", 1, "data")
def flatten_by_mappings(input: dict, output: dict, context, tenant, metrics, evolution) -> bool:
    """I-002：把操作段返回对象按输出映射拍平，写入 output map。"""
    obj = input["obj"]
    for field, spec in (input.get("mappings") or {}).items():
        value = obj.get(spec.get("from", field)) if isinstance(obj, dict) else getattr(obj, spec.get("from", field))
        if value is None:
            value = spec.get("default")
        output[field] = value
    return True
