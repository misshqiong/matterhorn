from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
import yaml

from matterhorn.contracts import Correction
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


def _write_gateway(
    provider: str | None,
    base_url: str | None,
    api_key: str | None,
    model: str | None,
):
    from matterhorn.gateway_config import configured_gateway

    try:
        return configured_gateway(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


def _cursor_map(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values or []:
        container, separator, cursor = value.partition("=")
        if not separator or not container:
            raise typer.BadParameter("--cursor must be CONTAINER_ID=OPAQUE_CURSOR")
        result[container] = cursor
    return result


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


@app.command()
def extract(
    input_file: Path = typer.Argument(..., exists=True, readable=True),
    scope_id: str | None = typer.Option(
        None, help="Memory scope; may instead be supplied as top-level scope_id."
    ),
    adapter: str = typer.Option(
        "records",
        help="Input shape: records, slack-history, reme, or openviking.",
    ),
    container_id: str | None = typer.Option(
        None, help="Slack channel ID for a conversations.history response."
    ),
    workspace_domain: str | None = typer.Option(
        None, help="Slack workspace domain, for example acme.slack.com."
    ),
    cursor: list[str] | None = typer.Option(
        None,
        "--cursor",
        help="Persist CONTAINER_ID=OPAQUE_CURSOR; repeat for multiple containers.",
    ),
    backfill: bool = typer.Option(
        False, help="Process unseen older records without advancing sync positions."
    ),
    db: str = typer.Option(
        "matterhorn.db", help="SQLite path or writable-primary PostgreSQL DSN."
    ),
    schema: str = typer.Option("org-matters/v1", help="Profile id or YAML path."),
    schema_dir: Path | None = typer.Option(None),
    provider: str | None = typer.Option(
        None,
        help=(
            "Write-side LLM provider. Defaults to MATTERHORN_PROVIDER; "
            "openai-compatible or anthropic are supported."
        ),
    ),
    base_url: str | None = typer.Option(
        None, help="Override MATTERHORN_BASE_URL."
    ),
    api_key: str | None = typer.Option(
        None,
        help=(
            "Override MATTERHORN_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY."
        ),
    ),
    model: str | None = typer.Option(None, help="Override MATTERHORN_MODEL."),
) -> None:
    """Extract communication Records into cards and ingest them atomically."""

    try:
        payload = yaml.safe_load(input_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise typer.BadParameter(f"could not load input: {error}") from error
    selected_scope = scope_id
    cursors = _cursor_map(cursor)
    adapter_dropped: dict[str, int] = {}
    if adapter == "records":
        if isinstance(payload, dict):
            selected_scope = selected_scope or payload.get("scope_id")
            records = payload.get("records", [])
            payload_cursors = payload.get("cursors")
            if isinstance(payload_cursors, dict):
                cursors = {**payload_cursors, **cursors}
        else:
            records = payload
    elif adapter == "slack-history":
        if not container_id or not workspace_domain:
            raise typer.BadParameter(
                "slack-history requires --container-id and --workspace-domain"
            )
        if not isinstance(payload, dict):
            raise typer.BadParameter("Slack history input MUST be an object")
        from matterhorn.adapters import map_slack_history

        mapped = map_slack_history(
            payload,
            channel_id=container_id,
            workspace_domain=workspace_domain,
        )
        records = mapped.records
        adapter_dropped = mapped.dropped
        if container_id not in cursors and mapped.next_cursor is not None:
            cursors[container_id] = mapped.next_cursor
    elif adapter in {"reme", "openviking"}:
        if not isinstance(payload, dict):
            raise typer.BadParameter(f"{adapter} input MUST be an object")
        from matterhorn.adapters import map_openviking_digest, map_reme_digest

        mapper = (
            map_reme_digest if adapter == "reme" else map_openviking_digest
        )
        try:
            card = mapper(payload, scope_id=selected_scope)
            engine = _engine(db, schema, schema_dir)
            emitted = engine.ingest([card], scope_id=card.scope_id)
        except Exception as error:
            raise typer.BadParameter(str(error)) from error
        _print(
            {
                "adapter": adapter,
                "scope_id": card.scope_id,
                "cards_accepted": 1,
                "cards_dropped": 0,
                "card_ids": [card.card_id],
                "assertions_emitted": len(emitted),
                "assertion_ids": [item.assertion_id for item in emitted],
            }
        )
        return
    else:
        raise typer.BadParameter(
            "adapter MUST be records, slack-history, reme, or openviking"
        )
    if not isinstance(selected_scope, str) or not selected_scope:
        raise typer.BadParameter("--scope-id or top-level scope_id is required")
    if not isinstance(records, list):
        raise typer.BadParameter("input records MUST be an array")
    engine = _engine(
        db,
        schema,
        schema_dir,
        gateway=_write_gateway(provider, base_url, api_key, model),
    )
    try:
        report = engine.add_records(
            records,
            scope_id=selected_scope,
            cursors=cursors,
            backfill=backfill,
        ).model_dump(mode="json")
    except Exception as error:
        raise typer.BadParameter(str(error)) from error
    if adapter_dropped:
        report["adapter_dropped"] = adapter_dropped
    _print(report)


@app.command("sync-status")
def sync_status(
    scope_id: str,
    db: str = typer.Option("matterhorn.db"),
    schema: str = typer.Option("org-matters/v1"),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Print per-container watermarks and opaque host cursors."""

    positions = _engine(db, schema, schema_dir).sync_positions(scope_id)
    _print([item.model_dump(mode="json") for item in positions])


@app.command()
def correct(
    correction_file: Path | None = typer.Argument(
        None,
        exists=True,
        readable=True,
        dir_okay=False,
        help="YAML/JSON Correction mapping, optionally wrapped as {correction: ...}.",
    ),
    scope_id: str | None = typer.Option(None, help="Correction scope."),
    subject_key: str | None = typer.Option(None, help="Existing subject key."),
    subject_type: str | None = typer.Option(None, help="Declared subject type."),
    predicate: str | None = typer.Option(None, help="Registered predicate."),
    operation: str | None = typer.Option(
        None, help="ASSERT (default) or RETRACT."
    ),
    object_value: str | None = typer.Option(
        None, help="Correction value as a YAML/JSON scalar or object."
    ),
    object_key: str | None = typer.Option(
        None, help="Optional canonical object key, primarily for RETRACT."
    ),
    valid_from: str | None = typer.Option(
        None, help="Business effective time as an RFC 3339 timestamp."
    ),
    source_ref: list[str] | None = typer.Option(
        None,
        "--source-ref",
        help=(
            "Traceable SourceRef as an inline YAML/JSON mapping. "
            "Repeat for multiple sources."
        ),
    ),
    db: str = typer.Option(
        "matterhorn.db", help="SQLite path or writable-primary PostgreSQL DSN."
    ),
    schema: str = typer.Option("org-matters/v1", help="Profile id or YAML path."),
    schema_dir: Path | None = typer.Option(
        None, help="Optional directory containing additional schema profiles."
    ),
) -> None:
    """Append an origin-human correction and rebuild the ordinary projection."""
    direct_values = {
        "scope_id": scope_id,
        "subject_key": subject_key,
        "subject_type": subject_type,
        "predicate": predicate,
        "operation": operation,
        "object_value": object_value,
        "object_key": object_key,
        "valid_from": valid_from,
        "source_ref": source_ref,
    }
    if correction_file is not None:
        if any(value is not None for value in direct_values.values()):
            raise typer.BadParameter(
                "use either CORRECTION_FILE or direct correction flags, not both"
            )
        try:
            loaded = yaml.safe_load(correction_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise typer.BadParameter(
                f"could not load correction file: {error}"
            ) from error
        payload = (
            loaded["correction"]
            if isinstance(loaded, dict) and set(loaded) == {"correction"}
            else loaded
        )
        if not isinstance(payload, dict):
            raise typer.BadParameter(
                "correction file must contain a mapping or {correction: {...}}"
            )
    else:
        required = {
            "--scope-id": scope_id,
            "--subject-key": subject_key,
            "--subject-type": subject_type,
            "--predicate": predicate,
            "--valid-from": valid_from,
        }
        missing = [name for name, value in required.items() if value is None]
        if not source_ref:
            missing.append("--source-ref")
        if missing:
            raise typer.BadParameter(
                f"direct correction requires {', '.join(missing)}"
            )
        parsed_sources = []
        for raw_source in source_ref:
            try:
                parsed_source = yaml.safe_load(raw_source)
            except yaml.YAMLError as error:
                raise typer.BadParameter(
                    f"invalid --source-ref YAML/JSON: {error}"
                ) from error
            if not isinstance(parsed_source, dict):
                raise typer.BadParameter(
                    "--source-ref must be a YAML/JSON mapping"
                )
            parsed_sources.append(parsed_source)
        try:
            parsed_value = (
                yaml.safe_load(object_value) if object_value is not None else None
            )
        except yaml.YAMLError as error:
            raise typer.BadParameter(
                f"invalid --object-value YAML/JSON: {error}"
            ) from error
        payload = {
            "scope_id": scope_id,
            "subject_key": subject_key,
            "subject_type": subject_type,
            "predicate": predicate,
            "operation": operation or "ASSERT",
            "object_value": parsed_value,
            "object_key": object_key,
            "valid_from": valid_from,
            "source_refs": parsed_sources,
        }
    try:
        correction = Correction.model_validate(payload)
        assertion = _engine(db, schema, schema_dir).correct(correction)
    except (ValueError, TypeError) as error:
        raise typer.BadParameter(str(error)) from error
    _print(assertion.model_dump(mode="json"))


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
    instant: str,
    db: str = typer.Option("matterhorn.db"),
    schema: str = typer.Option("org-matters/v1"),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Reconstruct a predicate at an effective-time instant."""
    try:
        parsed_instant = datetime.fromisoformat(instant)
    except ValueError as error:
        raise typer.BadParameter(
            "instant must be an RFC 3339 timestamp"
        ) from error
    result = _engine(db, schema, schema_dir).query.at(
        scope_id, subject_key, predicate, parsed_instant
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
    gateway = _write_gateway(provider, base_url, api_key, model)
    report = _engine(db, schema, schema_dir, gateway=gateway).dream(
        scope_id, limit=limit
    )
    _print(report.model_dump(mode="json"))


@app.command("mcp")
def mcp_command(
    db: str = typer.Option("matterhorn.db"),
    schema: str = typer.Option("org-matters/v1"),
    provider: str | None = typer.Option(
        None, help="Defaults to MATTERHORN_PROVIDER."
    ),
    base_url: str | None = typer.Option(None),
    api_key: str | None = typer.Option(None),
    model: str | None = typer.Option(None, help="Defaults to MATTERHORN_MODEL."),
) -> None:
    """Run the eight-tool Matterhorn MCP server over stdio."""
    from matterhorn.mcp.runtime import run_stdio

    run_stdio(
        db=str(db),
        schema=schema,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
    )


@app.command()
def serve(
    db: str = typer.Option("matterhorn.db"),
    schema: str = typer.Option("org-matters/v1"),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
    provider: str | None = typer.Option(
        None, help="Defaults to MATTERHORN_PROVIDER."
    ),
    base_url: str | None = typer.Option(None),
    api_key: str | None = typer.Option(None),
    model: str | None = typer.Option(None, help="Defaults to MATTERHORN_MODEL."),
) -> None:
    """Serve the Matterhorn REST API and OpenAPI document."""
    import uvicorn

    from matterhorn.api import create_app

    uvicorn.run(
        create_app(
            engine=Engine(
                db,
                schema,
                gateway=_write_gateway(provider, base_url, api_key, model),
            )
        ),
        host=host,
        port=port,
    )


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
    """Run all cases.

    Exit status: 0 when all cases pass, 1 when any valid case fails, and 2 when
    the suite is missing, unreadable, empty, or malformed.
    """
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
        raise typer.Exit(code=2) from error
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        typer.echo(f"{status} {result.case_id} - {result.title}")
        if result.detail:
            typer.echo(f"     {result.detail}")
    passed = sum(item.passed for item in results)
    failed = len(results) - passed
    typer.echo(f"SUMMARY passed={passed} failed={failed} total={len(results)}")
    if failed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
