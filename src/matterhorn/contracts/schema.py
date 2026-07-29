from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import TypeAlias

import yaml

from matterhorn.contracts.models import SchemaProfile

SchemaSource: TypeAlias = str | Path | Traversable


def load_schema(source: SchemaSource) -> SchemaProfile:
    path = Path(source) if isinstance(source, (str, Path)) else source
    with path.open(encoding="utf-8") as handle:
        return SchemaProfile.model_validate(yaml.safe_load(handle))


def builtin_schema_root() -> Traversable:
    return resources.files("matterhorn.schemas")


def _yaml_resources(root: Traversable) -> list[Traversable]:
    result: list[Traversable] = []
    for child in root.iterdir():
        if child.is_dir():
            result.extend(_yaml_resources(child))
        elif child.name.endswith(".yaml"):
            result.append(child)
    return sorted(result, key=str)


def discover_schemas(directory: str | Path | None = None) -> dict[str, SchemaSource]:
    result: dict[str, SchemaSource] = {}
    paths: list[SchemaSource] = list(_yaml_resources(builtin_schema_root()))
    if directory is not None:
        paths.extend(sorted(Path(directory).glob("**/*.yaml")))
    for path in paths:
        profile = load_schema(path)
        result[profile.schema_id] = path
    return result


def resolve_schema(
    schema: str | Path | SchemaProfile,
    *,
    schema_dir: str | Path | None = None,
) -> SchemaProfile:
    """Resolve an instance, filesystem path, or built-in profile id."""
    if isinstance(schema, SchemaProfile):
        return schema
    candidate = Path(schema)
    if candidate.is_file():
        return load_schema(candidate)
    if isinstance(schema, Path):
        raise FileNotFoundError(schema)
    profiles = discover_schemas(schema_dir)
    try:
        return load_schema(profiles[schema])
    except KeyError as error:
        raise FileNotFoundError(
            f"unknown schema profile {schema!r}; available: {sorted(profiles)}"
        ) from error
