from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import click
import typer
import yaml

from matterhorn.contracts.schema import discover_schemas, resolve_schema
from matterhorn.engine.engine import Engine

app = typer.Typer(help="Matterhorn deterministic temporal memory engine.")
query_app = typer.Typer(help="Read projected memory without an LLM.")
schema_app = typer.Typer(help="Inspect schema profiles.")
conformance_app = typer.Typer(help="Run the language-neutral golden contract.")
app.add_typer(query_app, name="query")
app.add_typer(schema_app, name="schema")
app.add_typer(conformance_app, name="conformance")


def _engine(
    db: str,
    schema: str,
    schema_dir: Path | None,
    *,
    gateway: Any = None,
) -> Engine:
    try:
        profile = resolve_schema(schema, schema_dir=schema_dir)
    except FileNotFoundError as error:
        raise typer.BadParameter(str(error)) from error
    return Engine(db, profile, gateway=gateway)


def _print(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@app.command()
def ingest(
    input_file: Path = typer.Argument(..., exists=True, readable=True),
    db: str = typer.Option(
        "matterhorn.db", help="SQLite path or writable-primary PostgreSQL DSN."
    ),
    schema: str = typer.Option("org-matters/v1", help="Profile id or YAML path."),
    schema_dir: Path | None = typer.Option(
        None, help="Optional directory containing additional schema profiles."
    ),
) -> None:
    """Ingest an EpisodeCard or a YAML/JSON list of EpisodeCards."""
    with input_file.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    cards = payload.get("cards", []) if isinstance(payload, dict) and "cards" in payload else payload
    if isinstance(cards, dict):
        cards = [cards]
    if not isinstance(cards, list):
        raise typer.BadParameter("input must be a card, a list, or {cards: [...]}")
    engine = _engine(db, schema, schema_dir)
    emitted = engine.ingest(cards)
    _print(
        {
            "cards": len(cards),
            "assertions_emitted": len(emitted),
            "assertion_ids": [item.assertion_id for item in emitted],
        }
    )


@query_app.command("current")
def query_current(
    scope_id: str,
    subject_key: str,
    predicate: str,
    db: str = typer.Option("matterhorn.db"),
    schema: str = typer.Option("org-matters/v1"),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Return the current value(s) for one subject and predicate."""
    result = _engine(db, schema, schema_dir).query.current(
        scope_id, subject_key, predicate
    )
    _print([item.to_dict() for item in result])


@query_app.command("timeline")
def query_timeline(
    scope_id: str,
    subject_key: str,
    predicate: str,
    db: str = typer.Option("matterhorn.db"),
    schema: str = typer.Option("org-matters/v1"),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Return all projected intervals."""
    result = _engine(db, schema, schema_dir).query.timeline(
        scope_id, subject_key, predicate
    )
    _print([item.to_dict() for item in result])


@query_app.command("at")
def query_at(
    scope_id: str,
    subject_key: str,
    predicate: str,
    instant: datetime,
    db: str = typer.Option("matterhorn.db"),
    schema: str = typer.Option("org-matters/v1"),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Reconstruct a predicate at an effective-time instant."""
    result = _engine(db, schema, schema_dir).query.at(
        scope_id, subject_key, predicate, instant
    )
    _print([item.to_dict() for item in result])


@query_app.command("by-person")
def query_by_person(
    scope_id: str,
    person_id: str,
    db: str = typer.Option("matterhorn.db"),
    schema: str = typer.Option("org-matters/v1"),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """List subjects currently related to a person-valued predicate."""
    result = _engine(db, schema, schema_dir).query.by_person(scope_id, person_id)
    _print([item.to_dict() for item in result])


@query_app.command("list")
def query_list(
    scope_id: str,
    db: str = typer.Option("matterhorn.db"),
    schema: str = typer.Option("org-matters/v1"),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """List materialized primary subjects in a scope."""
    result = _engine(db, schema, schema_dir).query.list_matters(scope_id)
    _print([item.to_dict() for item in result])


@app.command()
def replay(
    scope_id: str,
    db: str = typer.Option("matterhorn.db"),
    schema: str = typer.Option("org-matters/v1"),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Delete and deterministically rebuild intervals and memory cards."""
    engine = _engine(db, schema, schema_dir)
    engine.replay(scope_id)
    _print(
        {
            "scope_id": scope_id,
            "intervals": len(engine.store.intervals(scope_id)),
            "memory_cards": len(engine.store.memory_cards(scope_id)),
            "status": "rebuilt",
        }
    )


@app.command()
def dream(
    scope_id: str,
    limit: int | None = typer.Option(None),
    db: str = typer.Option("matterhorn.db"),
    schema: str = typer.Option("org-matters/v1"),
    schema_dir: Path | None = typer.Option(None),
    provider: str = typer.Option(
        "null", help="LLM provider: null, openai-compatible, or anthropic."
    ),
    base_url: str | None = typer.Option(
        None,
        help=(
            "Gateway base URL override. Defaults to MATTERHORN_BASE_URL."
        ),
    ),
    api_key: str | None = typer.Option(
        None,
        help=(
            "Explicit credential override. Defaults to MATTERHORN_API_KEY, "
            "then OPENAI_API_KEY or ANTHROPIC_API_KEY for the selected provider."
        ),
    ),
    model: str | None = typer.Option(None),
) -> None:
    """Drain queued cards through the configured write-side LLM gateway."""
    if limit is not None and limit < 0:
        raise typer.BadParameter("limit MUST be non-negative")
    from matterhorn.distill import (
        AnthropicGateway,
        NullGateway,
        OpenAICompatibleGateway,
    )

    resolved_base_url = (
        base_url
        if base_url is not None
        else os.environ.get("MATTERHORN_BASE_URL")
    )
    provider_fallback_key = {
        "openai-compatible": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }.get(provider)
    resolved_api_key = (
        api_key
        if api_key is not None
        else os.environ.get("MATTERHORN_API_KEY")
    )
    if resolved_api_key is None and provider_fallback_key is not None:
        resolved_api_key = os.environ.get(provider_fallback_key)

    if provider == "null":
        gateway = NullGateway()
    elif provider == "openai-compatible":
        if not all((resolved_base_url, resolved_api_key, model)):
            raise typer.BadParameter(
                "openai-compatible requires a base URL, API key, and --model; "
                "use MATTERHORN_BASE_URL and MATTERHORN_API_KEY/OPENAI_API_KEY "
                "or explicit --base-url/--api-key overrides"
            )
        gateway = OpenAICompatibleGateway(
            base_url=resolved_base_url,
            api_key=resolved_api_key,
            model=model,
        )
    elif provider == "anthropic":
        if not all((resolved_api_key, model)):
            raise typer.BadParameter(
                "anthropic requires an API key and --model; use "
                "MATTERHORN_API_KEY/ANTHROPIC_API_KEY or explicit --api-key"
            )
        kwargs = {"api_key": resolved_api_key, "model": model}
        if resolved_base_url is not None:
            kwargs["base_url"] = resolved_base_url
        gateway = AnthropicGateway(**kwargs)
    else:
        raise typer.BadParameter(f"unknown provider: {provider}")
    report = _engine(db, schema, schema_dir, gateway=gateway).dream(
        scope_id, limit=limit
    )
    _print(report.model_dump(mode="json"))


@app.command("mcp")
def mcp_command(
    db: str = typer.Option("matterhorn.db"),
    schema: str = typer.Option("org-matters/v1"),
) -> None:
    """Run the seven-tool Matterhorn MCP server over stdio."""
    from matterhorn.mcp.runtime import run_stdio

    run_stdio(db=str(db), schema=schema)


@app.command()
def serve(
    db: str = typer.Option("matterhorn.db"),
    schema: str = typer.Option("org-matters/v1"),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
) -> None:
    """Serve the Matterhorn REST API and OpenAPI document."""
    import uvicorn

    from matterhorn.api import create_app

    uvicorn.run(create_app(engine=Engine(db, schema)), host=host, port=port)


@schema_app.command("list")
def schema_list(
    schema_dir: Path | None = typer.Option(
        None, help="Optional directory containing additional profiles."
    ),
) -> None:
    """List available schema profile ids."""
    _print(sorted(discover_schemas(schema_dir)))


@schema_app.command("show")
def schema_show(
    schema: str,
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Show a validated schema profile."""
    profile = resolve_schema(schema, schema_dir=schema_dir)
    _print(profile.model_dump(mode="json", by_alias=True))


@conformance_app.command("run")
def conformance_run(
    suite: Path | None = typer.Option(
        None,
        help="Golden YAML suite directory; defaults to the packaged suite.",
    ),
    backend: str = typer.Option(
        "sqlite",
        help="Store backend: sqlite or postgres.",
    ),
    dsn: str | None = typer.Option(
        None,
        help=(
            "Writable-primary PostgreSQL DSN. For test automation only, "
            "defaults to MATTERHORN_TEST_POSTGRES_DSN."
        ),
    ),
) -> None:
    """Execute every golden case and report a stable per-case result."""
    from matterhorn.conformance import default_suite, run_suite

    if backend not in {"sqlite", "postgres"}:
        raise typer.BadParameter("backend MUST be sqlite or postgres")
    if backend == "sqlite" and dsn is not None:
        raise typer.BadParameter("--dsn requires --backend postgres")
    store_factory = None
    if backend == "postgres":
        selected_dsn = dsn or os.environ.get("MATTERHORN_TEST_POSTGRES_DSN")
        if not selected_dsn:
            raise typer.BadParameter(
                "--backend postgres requires --dsn or "
                "MATTERHORN_TEST_POSTGRES_DSN"
            )
        from matterhorn.store.postgres import PostgresStore

        def postgres_store_factory(_case_path: Path) -> PostgresStore:
            return PostgresStore(selected_dsn)

        store_factory = postgres_store_factory

    try:
        selected = suite or default_suite()
        results = run_suite(selected, store_factory=store_factory)
    except Exception as error:
        typer.echo(f"ERROR {error}", err=True)
        raise click.exceptions.Exit(2) from error
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        typer.echo(f"{status} {result.case_id} - {result.title}")
        if result.detail:
            typer.echo(f"     {result.detail}")
    passed = sum(item.passed for item in results)
    failed = len(results) - passed
    typer.echo(f"SUMMARY passed={passed} failed={failed} total={len(results)}")
    if failed:
        raise click.exceptions.Exit(1)


if __name__ == "__main__":
    app()
