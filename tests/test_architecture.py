from __future__ import annotations

import ast
from pathlib import Path

ARCHITECTURE_RULES = (
    {
        "sources": {"contracts"},
        "allow_only": {"contracts", "canonical", "errors"},
        "forbid": set(),
        "scope": "all",
    },
    {
        "sources": {"canonical"},
        "allow_only": {"contracts", "errors"},
        "forbid": set(),
        "scope": "all",
    },
    {
        "sources": {"store", "distill"},
        "allow_only": None,
        "forbid": {"engine", "adapters"},
        "scope": "all",
    },
    {
        "sources": {"query", "render"},
        "allow_only": None,
        "forbid": {"engine", "adapters", "distill"},
        "scope": "all",
    },
    {
        "sources": {"engine"},
        "allow_only": None,
        "forbid": {"adapters"},
        "scope": "module",
    },
    # REST and MCP are sibling transports composed on one ASGI app through the
    # shared service boundary. They may mount each other, but never import the
    # write-side distillation implementation.
    {
        "sources": {"api", "mcp"},
        "allow_only": None,
        "forbid": {"distill"},
        "scope": "all",
    },
    # Network connectors normalize through adapters and invoke only the
    # injected engine facade; they never import store implementations or core.
    {
        "sources": {"connectors"},
        "allow_only": {
            "adapters",
            "canonical",
            "connectors",
            "contracts",
            "errors",
        },
        "forbid": set(),
        "scope": "all",
    },
)

# A future SDK-compatibility fallback that lazily imports an adapter must be
# named here as ("engine/file.py", "matterhorn.adapters.module") and justified.
LAZY_ENGINE_ADAPTER_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "matterhorn"


def test_matterhorn_import_graph_respects_layering() -> None:
    violations: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        source = _source_layer(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            module_level = _is_module_level(node, parents)
            for target, module in _matterhorn_targets(path, node):
                for rule in ARCHITECTURE_RULES:
                    if source not in rule["sources"]:
                        continue
                    if rule["scope"] == "module" and not module_level:
                        continue
                    allowed = rule["allow_only"]
                    forbidden = rule["forbid"]
                    if allowed is not None and target not in allowed:
                        violations.append(
                            _violation(path, node, module, "is not allowlisted")
                        )
                    if target in forbidden:
                        violations.append(
                            _violation(path, node, module, "is forbidden")
                        )

                if source == "engine" and target == "adapters" and not module_level:
                    key = (path.relative_to(SOURCE_ROOT).as_posix(), module)
                    if key not in LAZY_ENGINE_ADAPTER_ALLOWLIST:
                        violations.append(
                            _violation(
                                path,
                                node,
                                module,
                                "is a non-allowlisted lazy adapter fallback",
                            )
                        )

    assert not violations, "\n".join(violations)


def _source_layer(path: Path) -> str:
    relative = path.relative_to(SOURCE_ROOT)
    return relative.parts[0].removesuffix(".py")


def _matterhorn_targets(
    path: Path,
    node: ast.Import | ast.ImportFrom,
) -> list[tuple[str, str]]:
    modules: list[str] = []
    if isinstance(node, ast.Import):
        modules.extend(alias.name for alias in node.names)
    else:
        resolved = _resolve_from_module(path, node)
        if resolved == "matterhorn":
            modules.extend(f"matterhorn.{alias.name}" for alias in node.names)
        elif resolved:
            modules.append(resolved)

    result: list[tuple[str, str]] = []
    for module in modules:
        parts = module.split(".")
        if parts[0] == "matterhorn" and len(parts) > 1:
            result.append((parts[1], module))
    return result


def _resolve_from_module(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = ("matterhorn", *path.relative_to(SOURCE_ROOT).parent.parts)
    retained = len(package) - (node.level - 1)
    suffix = tuple((node.module or "").split(".")) if node.module else ()
    return ".".join((*package[:retained], *suffix))


def _is_module_level(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.AsyncFunctionDef, ast.FunctionDef, ast.Lambda)):
            return False
    return True


def _violation(
    path: Path,
    node: ast.Import | ast.ImportFrom,
    module: str,
    reason: str,
) -> str:
    relative = path.relative_to(SOURCE_ROOT)
    return f"{relative}:{node.lineno}: {module} {reason}"
