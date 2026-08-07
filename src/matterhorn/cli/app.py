from __future__ import annotations

import json
import os
import sys
import threading
import tomllib
import uuid
import webbrowser
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import typer
import yaml

from matterhorn.canonical import canonical_json
from matterhorn.capacity import CapacitySettings, resolve_capacity
from matterhorn.connectors.mail import (
    MAIL_INTERVALS,
    MAIL_PROVIDERS,
    MailAuthError,
    MailboxResetError,
    MailConfig,
    MailConnector,
    MailSyncError,
    load_mail_configs,
    reset_mail_sync_position,
    save_mail_config,
)
from matterhorn.console.groups import console_group_patterns
from matterhorn.contracts import Correction, ExportEnvelope, SourceRef
from matterhorn.contracts.schema import discover_schemas, resolve_schema
from matterhorn.defaults import Engine
from matterhorn.errors import MatterhornError, ResourceNotFoundError

app = typer.Typer(help="Matterhorn deterministic temporal memory engine.")
query_app = typer.Typer(help="Read projected memory without an LLM.")
schema_app = typer.Typer(help="Inspect schema profiles.")
conformance_app = typer.Typer(help="Run the language-neutral golden contract.")
eval_app = typer.Typer(help="Measure message-to-matter extraction quality.")
mail_app = typer.Typer(help="Configure and pull an IMAP mailbox.")
setup_app = typer.Typer(help="Configure agent clients for Matterhorn.")
hook_app = typer.Typer(help="Fail-open agent lifecycle hooks.")
handles_app = typer.Typer(help="Maintain the subject handle registry.")
staging_app = typer.Typer(help="Maintain raw extraction context staging.")
themes_app = typer.Typer(help="Converge flat matters under parent themes.")
app.add_typer(query_app, name="query")
app.add_typer(schema_app, name="schema")
app.add_typer(conformance_app, name="conformance")
app.add_typer(eval_app, name="eval")
app.add_typer(mail_app, name="mail")
app.add_typer(setup_app, name="setup")
app.add_typer(hook_app, name="hook")
app.add_typer(handles_app, name="handles")
app.add_typer(staging_app, name="staging")
app.add_typer(themes_app, name="themes")

CONFIG_NAME = "matterhorn.toml"
DEFAULT_DB = "matterhorn.db"
DEFAULT_SCHEMA = "org-matters/v1"


class ExportFormat(str, Enum):
    json = "json"
    markdown = "markdown"
    html = "html"


def _load_config() -> dict[str, Any]:
    path = Path.cwd() / CONFIG_NAME
    if not path.is_file():
        return {}
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise typer.BadParameter(f"could not load {CONFIG_NAME}: {error}") from error
    if not isinstance(payload, dict):
        raise typer.BadParameter(f"{CONFIG_NAME} MUST contain a TOML table")
    return payload


def _setting(value: Any, default: Any, key: str) -> Any:
    if value != default:
        return value
    return _load_config().get(key, default)


def _console_groups(config: dict[str, Any]) -> dict[str, list[str]]:
    try:
        return console_group_patterns(config)
    except (TypeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error


def _capacity_settings(config: dict[str, Any]) -> CapacitySettings:
    try:
        return resolve_capacity(config=config)
    except (TypeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error


def _signal_settings(
    config: dict[str, Any], capacity: CapacitySettings | None = None
) -> dict[str, Any]:
    identity = config.get("identity", {})
    signals = config.get("signals", {})
    if not isinstance(identity, dict):
        raise typer.BadParameter("[identity] MUST be a TOML table")
    if not isinstance(signals, dict):
        raise typer.BadParameter("[signals] MUST be a TOML table")

    def string_list(table: dict[str, Any], key: str, label: str) -> list[str]:
        value = table.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise typer.BadParameter(f"{label} MUST be an array of non-empty strings")
        return value

    selected_capacity = capacity or _capacity_settings(config)

    return {
        "identity_handles": string_list(identity, "handles", "[identity] handles"),
        "machine_senders": string_list(
            signals, "machine_senders", "[signals] machine_senders"
        ),
        "alert_keywords": string_list(
            signals, "alert_keywords", "[signals] alert_keywords"
        ),
        "hot_min_authors": selected_capacity.hot_min_authors,
        "hot_min_messages": selected_capacity.hot_min_messages,
    }


def _theme_settings(
    config: dict[str, Any], capacity: CapacitySettings | None = None
) -> dict[str, Any]:
    from matterhorn.engine.theme_converge import configured_theme_settings

    themes = config.get("themes", {})
    distill = config.get("distill", {})
    if not isinstance(themes, dict):
        raise typer.BadParameter("[themes] MUST be a TOML table")
    if not isinstance(distill, dict):
        raise typer.BadParameter("[distill] MUST be a TOML table")
    mode = themes.get("theme_converge", distill.get("theme_converge"))
    selected_capacity = capacity or _capacity_settings(config)
    try:
        settings = configured_theme_settings(
            mode=mode,
            min_cluster=selected_capacity.theme_min_cluster,
            min_backlog=selected_capacity.theme_min_backlog,
            interval_hours=themes.get(
                "theme_interval_hours", distill.get("theme_interval_hours")
            ),
            conversation_fanout=selected_capacity.conversation_fanout,
            human_edge_weight=selected_capacity.human_edge_weight,
        )
    except (TypeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    return {
        "theme_converge": settings.mode,
        "theme_min_cluster": settings.min_cluster,
        "theme_min_backlog": settings.min_backlog,
        "theme_interval_hours": settings.interval_hours,
        "theme_conversation_fanout": settings.conversation_fanout,
        "human_edge_weight": settings.human_edge_weight,
    }


def _engine(
    db: str,
    schema: str,
    schema_dir: Path | None,
    *,
    gateway: Any = None,
    max_batch_delay_minutes: float | None = None,
    min_batch_messages: int | None = None,
) -> Engine:
    config = _load_config()
    capacity = _capacity_settings(config)
    db = _setting(db, DEFAULT_DB, "db")
    schema = _setting(schema, DEFAULT_SCHEMA, "schema")
    try:
        profile = resolve_schema(schema, schema_dir=schema_dir)
    except FileNotFoundError as error:
        raise typer.BadParameter(str(error)) from error
    return Engine(
        db,
        profile,
        gateway=gateway,
        staging_retention_days=_staging_retention_days(),
        max_batch_delay_minutes=(
            _max_batch_delay_minutes()
            if max_batch_delay_minutes is None
            else max_batch_delay_minutes
        ),
        min_batch_messages=(
            _min_batch_messages()
            if min_batch_messages is None
            else min_batch_messages
        ),
        **_signal_settings(config, capacity),
        **_theme_settings(config, capacity),
        capacity=capacity,
        unified_loop=_unified_loop_setting(config),
    )


def _unified_loop_setting(config: dict[str, Any]) -> bool:
    distill = config.get("distill", {})
    if not isinstance(distill, dict):
        raise typer.BadParameter("[distill] MUST be a TOML table")
    raw: Any = os.environ.get(
        "MATTERHORN_UNIFIED_LOOP",
        distill.get("unified_loop", False),
    )
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        normalized = raw.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise typer.BadParameter(
        "[distill] unified_loop / MATTERHORN_UNIFIED_LOOP MUST be a boolean"
    )


def _min_batch_messages(value: object | None = None) -> int:
    raw = value if value is not None else os.environ.get("MATTERHORN_MIN_BATCH", "1")
    try:
        parsed = int(str(raw))
    except (TypeError, ValueError) as error:
        raise typer.BadParameter(
            "MATTERHORN_MIN_BATCH MUST be a positive integer"
        ) from error
    if parsed < 1:
        raise typer.BadParameter("MATTERHORN_MIN_BATCH MUST be a positive integer")
    return parsed


def _staging_retention_days() -> float:
    from matterhorn.engine.engine import (
        DEFAULT_STAGING_RETENTION_DAYS,
        validate_staging_retention_days,
    )

    raw = os.environ.get(
        "MATTERHORN_STAGING_RETENTION_DAYS",
        str(DEFAULT_STAGING_RETENTION_DAYS),
    )
    try:
        return validate_staging_retention_days(raw)
    except (TypeError, ValueError) as error:
        raise typer.BadParameter(
            "MATTERHORN_STAGING_RETENTION_DAYS MUST be a positive finite number"
        ) from error


def _max_batch_delay_minutes(
    value: object | None = None,
    config: dict[str, Any] | None = None,
) -> float:
    from matterhorn.engine.engine import (
        DEFAULT_MAX_BATCH_DELAY_MINUTES,
        validate_max_batch_delay_minutes,
    )

    if value is not None:
        raw = value
    elif "MATTERHORN_MAX_BATCH_DELAY" in os.environ:
        raw = os.environ["MATTERHORN_MAX_BATCH_DELAY"]
    else:
        settings = _load_config() if config is None else config
        raw = settings.get(
            "max_batch_delay_minutes",
            DEFAULT_MAX_BATCH_DELAY_MINUTES,
        )
    try:
        return validate_max_batch_delay_minutes(raw)
    except (TypeError, ValueError) as error:
        raise typer.BadParameter(
            "MATTERHORN_MAX_BATCH_DELAY MUST be a positive finite number"
        ) from error


def _print(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _write_gateway(
    provider: str | None,
    base_url: str | None,
    api_key: str | None,
    model: str | None,
):
    from matterhorn.gateway_config import configured_gateway

    config = _load_config()
    ai_config = config.get("ai")
    if not isinstance(ai_config, dict):
        ai_config = {}
    try:
        return configured_gateway(
            provider=provider or ai_config.get("provider") or config.get("provider"),
            base_url=base_url or ai_config.get("base_url"),
            api_key=api_key,
            model=model or ai_config.get("model"),
            fixture_path=(
                os.environ.get("MATTERHORN_FIXTURE_PATH")
                or config.get("fixture_path")
            ),
            timeout=(
                float(ai_config["timeout"])
                if "timeout" in ai_config
                else None
            ),
        )
    except (TypeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error


def _cursor_map(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values or []:
        container, separator, cursor = value.partition("=")
        if not separator or not container:
            raise typer.BadParameter("--cursor must be CONTAINER_ID=OPAQUE_CURSOR")
        result[container] = cursor
    return result


def _scope(value: str | None) -> str:
    selected = value or _load_config().get("scope")
    if not isinstance(selected, str) or not selected:
        raise typer.BadParameter("scope is required (argument, --scope, or config)")
    return selected


def _mail_account(
    configs: list[MailConfig],
    account_id: str | None,
) -> MailConfig:
    if account_id is not None:
        for config in configs:
            if config.account_id == account_id:
                return config
        choices = ", ".join(config.account_id for config in configs) or "none"
        raise typer.BadParameter(
            f"unknown --account {account_id!r}; configured accounts: {choices}"
        )
    if len(configs) == 1:
        return configs[0]
    if not configs:
        raise typer.BadParameter("run `mh mail setup` first")
    choices = ", ".join(config.account_id for config in configs)
    raise typer.BadParameter(
        f"--account is required; configured accounts: {choices}"
    )


def _read_yaml_or_json(path: str) -> Any:
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        return yaml.safe_load(text)
    except (OSError, yaml.YAMLError) as error:
        raise typer.BadParameter(f"could not load input: {error}") from error


def _mail_connector(
    engine: Engine,
    config: MailConfig,
    password: str,
) -> MailConnector:
    return MailConnector(engine, config, password)


@mail_app.command("setup")
def mail_setup(
    name: str | None = typer.Option(
        None,
        "--name",
        "--account-id",
        help="Stable account id; defaults to user@host/folder.",
    ),
    provider: str | None = typer.Option(
        None,
        help="gmail, outlook, icloud, qq, 163, or manual.",
    ),
    host: str | None = typer.Option(
        None,
        help="Override the preset IMAP host; required for manual.",
    ),
    port: int | None = typer.Option(
        None,
        min=1,
        max=65535,
        help="Override the preset IMAP port.",
    ),
    ssl: bool | None = typer.Option(
        None,
        "--ssl/--no-ssl",
        help="Use implicit TLS; presets default to SSL.",
    ),
    user: str | None = typer.Option(
        None,
        "--user",
        "--account",
        help="Mailbox login/account.",
    ),
    folder: str = typer.Option("INBOX", help="IMAP folder."),
    interval: str = typer.Option(
        "off",
        help="Auto-sync interval: off, 15min, 1h, or 6h.",
    ),
    initial_window: int = typer.Option(
        50,
        min=1,
        help="Most recent messages to pull on the first non-backfill sync.",
    ),
    scope: str | None = typer.Option(
        None,
        help="Default Matterhorn scope for mail sync.",
    ),
) -> None:
    """Persist non-secret IMAP settings in matterhorn.toml."""

    selected_provider = (
        provider
        or typer.prompt(
            "Provider",
            default="gmail",
        )
    ).casefold()
    if selected_provider not in {*MAIL_PROVIDERS, "manual"}:
        raise typer.BadParameter(
            "--provider MUST be gmail, outlook, icloud, qq, 163, or manual"
        )
    if interval not in MAIL_INTERVALS:
        raise typer.BadParameter("--interval MUST be off, 15min, 1h, or 6h")
    preset = MAIL_PROVIDERS.get(selected_provider)
    selected_ssl = ssl if ssl is not None else (preset.ssl if preset else True)
    selected_host = host or (preset.host if preset else None)
    if not selected_host:
        selected_host = typer.prompt("IMAP host")
    selected_port = port or (
        preset.port if preset else (993 if selected_ssl else 143)
    )
    selected_user = user or typer.prompt("Mail account")
    root_config = _load_config()
    selected_scope = scope or root_config.get("scope")
    config = MailConfig(
        provider=selected_provider,
        host=selected_host,
        port=selected_port,
        ssl=selected_ssl,
        user=selected_user,
        folder=folder,
        interval=interval,
        initial_window=initial_window,
        scope=selected_scope,
        name=name,
    )
    save_mail_config(Path.cwd() / CONFIG_NAME, config)
    _print(
        {
            "saved": CONFIG_NAME,
            **config.public_dict(),
            "credential": "not stored; use MATTERHORN_MAIL_PASSWORD or prompt",
        }
    )


@mail_app.command("sync")
def mail_sync(
    backfill: bool = typer.Option(
        False,
        "--backfill",
        help="Permit a full mailbox re-pull, including after UIDVALIDITY reset.",
    ),
    scope: str | None = typer.Option(
        None,
        help="Matterhorn scope; defaults to mail.scope or root scope.",
    ),
    account: str | None = typer.Option(
        None,
        "--account",
        help="Account id; optional only when exactly one mailbox exists.",
    ),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Pull new IMAP UIDs, add messages, and flush synchronously."""

    try:
        configs = load_mail_configs(Path.cwd() / CONFIG_NAME)
    except (TypeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    config = _mail_account(configs, account)
    selected_scope = scope or config.scope
    if selected_scope is None:
        selected_scope = _scope(None)
    password = os.environ.get("MATTERHORN_MAIL_PASSWORD")
    if not password:
        password = typer.prompt(
            "Mail app password / authorization code",
            hide_input=True,
        )
    engine = _engine(
        db,
        schema,
        schema_dir,
        gateway=_write_gateway(None, None, None, None),
    )
    try:
        report = _mail_connector(engine, config, password).sync(
            scope_id=selected_scope,
            backfill=backfill,
        )
    except MailboxResetError as error:
        _print(error.report.to_dict())
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error
    except (MailAuthError, MailSyncError) as error:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=1) from error
    _print(report.to_dict())


@mail_app.command("reset")
def mail_reset(
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Confirm deletion of the configured connector sync position.",
    ),
    scope: str | None = typer.Option(
        None,
        help="Matterhorn scope; defaults to mail.scope or root scope.",
    ),
    account: str | None = typer.Option(
        None,
        "--account",
        help="Account id; optional only when exactly one mailbox exists.",
    ),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Forget the mail UID position so the next sync re-pulls recent mail."""

    if not yes:
        raise typer.BadParameter(
            "--yes is required because reset deletes the mail sync position"
        )
    try:
        configs = load_mail_configs(Path.cwd() / CONFIG_NAME)
    except (TypeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    config = _mail_account(configs, account)
    selected_scope = scope or config.scope
    if selected_scope is None:
        selected_scope = _scope(None)
    engine = _engine(db, schema, schema_dir)
    result = reset_mail_sync_position(
        engine,
        config,
        scope_id=selected_scope,
        confirm=yes,
    )
    _print(result)


@app.command("init")
def init_project(
    schema: str = typer.Option(DEFAULT_SCHEMA, help="Built-in schema profile id."),
    db: Path = typer.Option(Path(DEFAULT_DB), help="SQLite database path."),
) -> None:
    """Scaffold an idempotent local setup and an offline five-minute demo."""

    config_path = Path.cwd() / CONFIG_NAME
    demo_path = Path.cwd() / "demo-messages.yaml"
    fixture_path = Path.cwd() / "matterhorn-demo-gateway.json"
    if not config_path.exists():
        config_path.write_text(
            "\n".join(
                [
                    f"db = {json.dumps(str(db), ensure_ascii=False)}",
                    f"schema = {json.dumps(schema, ensure_ascii=False)}",
                    'scope = "demo"',
                    'provider = "fixture"',
                    'fixture_path = "matterhorn-demo-gateway.json"',
                    "quiet_period_minutes = 10",
                    "max_batch_delay_minutes = 5",
                    "",
                    "[distill]",
                    "unified_loop = false",
                    "",
                    "[themes]",
                    'theme_converge = "review"',
                    "theme_min_cluster = 3",
                    "theme_min_backlog = 6",
                    "theme_interval_hours = 6",
                    "theme_conversation_fanout = 8",
                    "human_edge_weight = 10",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    if not demo_path.exists():
        demo_path.write_text(
            """messages:
  - id: m1
    conversation_id: payments
    sender: {id: u1, name: Dana Reyes}
    text: I'm taking over the payment refactor; the API split is done and integration testing is next.
    sent_at: 2026-07-28T14:00:00+08:00
""",
            encoding="utf-8",
        )
    if not fixture_path.exists():
        fixture_path.write_text(
            json.dumps(
                {
                    "record_extraction": {
                        "cards": [
                            {
                                "date": "2026-07-28",
                                "title": "Payment refactor",
                                "status": "in_progress",
                                "participants": [
                                    {
                                        "id": "u1",
                                        "display_name": "Dana Reyes",
                                        "role": "owner",
                                    }
                                ],
                                "progress": "API split completed",
                                "next_step": "Integration testing",
                                "source_ids": ["demo:payments:m1"],
                            }
                        ]
                    },
                    "distillation": {"candidates": []},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    selected_config = _load_config()
    selected_db = Path(str(selected_config.get("db", db)))
    from matterhorn.store import SQLiteStore

    store = SQLiteStore(selected_db)
    store.close()
    typer.echo(f"Initialized {config_path.name} and {selected_db}")
    typer.echo("Next:")
    typer.echo("  mh add demo-messages.yaml")
    typer.echo("  mh flush demo")
    typer.echo("  mh matters demo")


@app.command("add")
def add_messages(
    input_file: str = typer.Argument(
        "-",
        help="YAML/JSON messages, an email file with --adapter email, or -.",
    ),
    adapter: str = typer.Option(
        "messages",
        help="Input adapter: messages or email (.mbox/.eml).",
    ),
    scope_id: str | None = typer.Option(None, "--scope", help="Memory scope."),
    wait: bool = typer.Option(False, help="Run the pipeline synchronously."),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
    provider: str | None = typer.Option(None),
    base_url: str | None = typer.Option(None),
    api_key: str | None = typer.Option(None),
    model: str | None = typer.Option(None),
) -> None:
    """Add minimal messages or normalize a host-supplied email file."""

    if adapter == "email":
        from matterhorn.adapters import map_email_file

        selected_scope = _scope(scope_id)
        try:
            mapped = map_email_file(input_file)
            report = _engine(
                db,
                schema,
                schema_dir,
                gateway=_write_gateway(provider, base_url, api_key, model),
            ).add_records(mapped.records, scope_id=selected_scope)
        except Exception as error:
            raise typer.BadParameter(str(error)) from error
        payload = report.model_dump(mode="json")
        payload["adapter"] = "email"
        payload["adapter_dropped"] = mapped.dropped
        _print(payload)
        return
    if adapter != "messages":
        raise typer.BadParameter("adapter MUST be messages or email")
    payload = _read_yaml_or_json(input_file)
    messages = payload.get("messages") if isinstance(payload, dict) else payload
    if not isinstance(messages, list):
        raise typer.BadParameter("input MUST be a message list or {messages: [...]}")
    selected_scope = _scope(
        scope_id
        or (payload.get("scope_id") if isinstance(payload, dict) else None)
    )
    engine = _engine(
        db,
        schema,
        schema_dir,
        gateway=_write_gateway(provider, base_url, api_key, model),
    )
    try:
        result = engine.add(selected_scope, messages, wait=wait)
    except (MatterhornError, ValueError, TypeError) as error:
        raise typer.BadParameter(str(error)) from error
    _print(result.model_dump(mode="json"))


@app.command("matters")
def matters(
    scope_id: str | None = typer.Argument(None),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """List ergonomic projected matters with owners and blockers."""

    result = _engine(db, schema, schema_dir).matters(_scope(scope_id))
    _print([item.to_dict() for item in result])


@app.command("graph")
def graph_command(
    scope_id: str = typer.Argument(..., help="Matter scope."),
    subject_key: str = typer.Argument(..., help="Matter subject key."),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Print the canonical goal tree containing one matter."""

    graph = _engine(db, schema, schema_dir).matter_graph(
        scope_id, subject_key
    )

    def print_node(node: dict[str, Any], prefix: str = "") -> None:
        status = node.get("status") or "—"
        typer.echo(
            f"{prefix}- {node['title']} [{status}] "
            f"({node['subject_key']})"
        )
        for child in node.get("children", []):
            print_node(child, prefix + "  ")

    rollup = graph.rollup
    typer.echo(
        f"root={graph.root_subject_key} "
        f"completed={rollup.descendants_completed}/"
        f"{rollup.descendants_total} blocked={rollup.descendants_blocked}"
    )
    print_node(graph.tree)


@app.command("brief")
def brief_command(
    window_start: datetime | None = typer.Option(None),
    window_end: datetime | None = typer.Option(None),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Print today's zero-model signals, matter activity, and quiet threads."""

    engine = _engine(db, schema, schema_dir)
    now = engine.now().astimezone(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    result = engine.brief(
        window_start or today,
        window_end or (today + timedelta(days=1)),
        console_groups=_console_groups(_load_config()),
    )
    typer.echo("需要我")
    if not result["needs_me"]:
        typer.echo("  none")
    for item in result["needs_me"]:
        label = item["title"] or item["matched_text"] or item["record_id"]
        typer.echo(f"  - [{item['reason']}] {item['scope_id']}: {label}")
    typer.echo("Groups")
    for group in result["groups"]:
        counts = group["counts"]
        titles = ", ".join(
            f"{item['title']} "
            f"({item['descendants_completed']}/{item['descendants_total']})"
            for item in group["matters"]
        ) or "none"
        typer.echo(
            f"  {group['name']}: touched={counts['touched']} "
            f"completed={counts['completed']} blocked={counts['blocked']} | {titles}"
        )
    typer.echo("Quiet")
    if not result["quiet"]:
        typer.echo("  none")
    for item in result["quiet"]:
        hot = " hot" if item["hot"] else ""
        typer.echo(
            f"  {item['scope_id']}/{item['container_id']}: "
            f"messages={item['message_count']} authors={item['distinct_authors']}{hot}"
        )


@handles_app.command("backfill")
def handles_backfill(
    scope_id: str = typer.Argument(..., help="Scope whose retained evidence is scanned."),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Offline, zero-model handle backfill over retained subject evidence."""

    try:
        report = _engine(db, schema, schema_dir).backfill_handles(scope_id)
    except (MatterhornError, ValueError, TypeError) as error:
        raise typer.BadParameter(str(error)) from error
    rows = (
        ("bound", report.bound),
        ("skipped-conflict", report.skipped_conflict),
        ("already-bound", report.already_bound),
    )
    typer.echo("metric             count")
    typer.echo("----------------- -----")
    for label, count in rows:
        typer.echo(f"{label:<17} {count:>5}")


@staging_app.command("purge")
def staging_purge(
    scope_id: str | None = typer.Argument(None),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Delete expired raw context without touching retained evidence."""

    selected_scope = _scope(scope_id)
    deleted = _engine(db, schema, schema_dir).purge_staging(selected_scope)
    _print({"scope_id": selected_scope, "deleted": deleted})


@themes_app.command("run")
def themes_run(
    scope_id: str = typer.Argument(..., help="Scope whose flat matters are clustered."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print clusters and naming proposals without writing subjects or edges.",
    ),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
    provider: str | None = typer.Option(None),
    base_url: str | None = typer.Option(None),
    api_key: str | None = typer.Option(None),
    model: str | None = typer.Option(None),
) -> None:
    """Run one governed theme pass; the unified-loop rollout flag is ignored."""

    engine = _engine(
        db,
        schema,
        schema_dir,
        gateway=_write_gateway(provider, base_url, api_key, model),
    )
    try:
        report = engine.themes(_scope(scope_id), dry_run=dry_run)
    except (MatterhornError, TypeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    _print(report.to_dict())


@app.command("flush")
def flush(
    scope_id: str | None = typer.Argument(None),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
    provider: str | None = typer.Option(None),
    base_url: str | None = typer.Option(None),
    api_key: str | None = typer.Option(None),
    model: str | None = typer.Option(None),
) -> None:
    """Synchronously run pending extraction, distillation, and projection."""

    engine = _engine(
        db,
        schema,
        schema_dir,
        gateway=_write_gateway(provider, base_url, api_key, model),
    )
    _print(engine.flush(_scope(scope_id)).model_dump(mode="json"))


@app.command("task")
def task(
    task_id: str,
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Inspect a persistent task receipt and gate breakdown."""

    try:
        result = _engine(db, schema, schema_dir).task(task_id)
    except ResourceNotFoundError as error:
        raise typer.BadParameter(str(error)) from error
    _print(result.model_dump(mode="json"))


@app.command("events")
def events(
    scope_id: str | None = typer.Argument(None),
    since: str | None = typer.Option(
        None, help="Inclusive RFC 3339 recorded_at lower bound."
    ),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """List deterministic projection-derived change events."""

    engine = _engine(db, schema, schema_dir)
    selected_scope = _scope(scope_id)
    if not engine.scope_exists(selected_scope):
        raise typer.BadParameter(f"unknown scope_id: {selected_scope}")
    try:
        result = engine.events(selected_scope, since=since)
    except ValueError as error:
        raise typer.BadParameter("since MUST be an RFC 3339 timestamp") from error
    _print([item.model_dump(mode="json") for item in result])


@app.command("export")
def export_scope(
    scope_id: str | None = typer.Argument(None),
    output_format: ExportFormat = typer.Option(
        ExportFormat.json,
        "--format",
        help="Output format: json, markdown, or html.",
    ),
    as_of: str | None = typer.Option(
        None,
        "--as-of",
        help=(
            "HTML overdue reference instant. Defaults to the maximum "
            "recorded_at in the scope."
        ),
    ),
    out: Path | None = typer.Option(
        None, help="Write output to this file instead of stdout."
    ),
    related: list[str] = typer.Option(
        [],
        "--related",
        help=(
            "HTML footer cross-link as LABEL=HREF; repeatable. "
            "Valid only with --format html."
        ),
    ),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Export JSON ownership, Markdown, or a self-contained HTML ledger."""

    engine = _engine(db, schema, schema_dir)
    try:
        selected_scope = _scope(scope_id)
        if as_of is not None and output_format != ExportFormat.html:
            raise ValueError("--as-of is valid only with --format html")
        if related and output_format != ExportFormat.html:
            raise ValueError("--related is valid only with --format html")
        related_pairs: list[tuple[str, str]] = []
        for item in related:
            label, separator, href = item.partition("=")
            if not separator or not label or not href:
                raise ValueError("--related expects LABEL=HREF")
            related_pairs.append((label, href))
        if output_format == ExportFormat.html:
            from matterhorn.render import render_scope_html

            payload = render_scope_html(
                engine.export(selected_scope),
                engine.profile,
                as_of=as_of,
                related=related_pairs,
            )
        elif output_format == ExportFormat.markdown:
            from matterhorn.render import render_scope_markdown

            payload = render_scope_markdown(engine, selected_scope)
        else:
            snapshot = engine.export(selected_scope)
            payload = canonical_json(snapshot.model_dump(mode="json")) + "\n"
    except Exception as error:
        raise typer.BadParameter(str(error)) from error
    if out is None:
        typer.echo(payload, nl=False)
    else:
        out.write_text(payload, encoding="utf-8")
        typer.echo(
            f"Exported {selected_scope} as {output_format.value} to {out}"
        )


@app.command("import")
def import_scope(
    input_file: Path = typer.Argument(..., exists=True, readable=True),
    db: str = typer.Option(DEFAULT_DB),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Import a versioned scope export into an empty store."""

    try:
        snapshot = ExportEnvelope.model_validate_json(
            input_file.read_text(encoding="utf-8")
        )
    except Exception as error:
        raise typer.BadParameter(f"invalid Matterhorn export: {error}") from error
    try:
        profile = resolve_schema(snapshot.schema_profile.id, schema_dir=schema_dir)
    except FileNotFoundError as error:
        raise typer.BadParameter(
            "export schema profile is not available locally: "
            f"{snapshot.schema_profile.id}"
        ) from error
    selected_db = str(_setting(db, DEFAULT_DB, "db"))
    try:
        report = Engine(selected_db, profile).import_snapshot(snapshot)
    except Exception as error:
        raise typer.BadParameter(str(error)) from error
    _print(report.model_dump(mode="json"))


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
    emitted = engine._ingest_cards_sync(cards)
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
    model: str | None = typer.Option(
        None,
        help=(
            "Override MATTERHORN_MODEL. MATTERHORN_TIMEOUT controls request "
            "seconds (default 60)."
        ),
    ),
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
            emitted = engine._ingest_cards_sync(
                [card], scope_id=card.scope_id
            )
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
    except (MatterhornError, ValueError, TypeError) as error:
        raise typer.BadParameter(str(error)) from error
    _print(assertion.model_dump(mode="json"))


@app.command("merge")
def merge_subjects(
    scope_id: str = typer.Argument(..., help="Scope containing both subjects."),
    source_subject_key: str = typer.Argument(
        ..., help="Subject to merge away."
    ),
    target_subject_key: str = typer.Argument(
        ..., help="Canonical subject that remains."
    ),
    reason: str = typer.Option(
        ..., help="Human reason retained in merge provenance."
    ),
    sender: str = typer.Option(
        ..., help="Name of the human authorizing the merge."
    ),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Merge SOURCE into TARGET as a reversible human correction."""

    engine = _engine(db, schema, schema_dir)
    instant = engine.now()
    try:
        event = engine.merge_subjects(
            scope_id,
            source_subject_key,
            target_subject_key,
            source_refs=[
                SourceRef(
                    source_id=f"console:{uuid.uuid4()}",
                    sent_at=instant,
                    sender=sender,
                    excerpt=f"CLI subject merge. Reason: {reason}",
                )
            ],
            valid_from=instant,
        )
    except (MatterhornError, ValueError, TypeError) as error:
        raise typer.BadParameter(str(error)) from error
    _print(event.model_dump(mode="json"))


@app.command("unmerge")
def unmerge_subject(
    scope_id: str = typer.Argument(..., help="Scope containing the merge."),
    source_subject_key: str = typer.Argument(
        ..., help="Merged-away subject to restore."
    ),
    reason: str = typer.Option(
        ..., help="Human reason retained in unmerge provenance."
    ),
    sender: str = typer.Option(
        ..., help="Name of the human authorizing the unmerge."
    ),
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    schema_dir: Path | None = typer.Option(None),
) -> None:
    """Reverse the active merge for SOURCE."""

    engine = _engine(db, schema, schema_dir)
    instant = engine.now()
    try:
        event = engine.unmerge_subjects(
            scope_id,
            source_subject_key,
            source_refs=[
                SourceRef(
                    source_id=f"console:{uuid.uuid4()}",
                    sent_at=instant,
                    sender=sender,
                    excerpt=f"CLI subject unmerge. Reason: {reason}",
                )
            ],
            valid_from=instant,
        )
    except (MatterhornError, ValueError, TypeError) as error:
        raise typer.BadParameter(str(error)) from error
    _print(event.model_dump(mode="json"))


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
    report = engine.replay(scope_id)
    _print(
        {
            "scope_id": scope_id,
            "intervals": report.intervals,
            "memory_cards": report.memory_cards,
            "events_emitted": report.events_emitted,
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
    model: str | None = typer.Option(
        None,
        help=(
            "Override MATTERHORN_MODEL. MATTERHORN_TIMEOUT controls request "
            "seconds (default 60)."
        ),
    ),
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
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    provider: str | None = typer.Option(
        None, help="Defaults to MATTERHORN_PROVIDER."
    ),
    base_url: str | None = typer.Option(None),
    api_key: str | None = typer.Option(None),
    model: str | None = typer.Option(None, help="Defaults to MATTERHORN_MODEL."),
) -> None:
    """Run the nine-tool Matterhorn MCP server over stdio."""
    from matterhorn.mcp.runtime import run_stdio

    config = _load_config()
    ai_config = config.get("ai")
    if not isinstance(ai_config, dict):
        ai_config = {}
    run_stdio(
        db=str(_setting(db, DEFAULT_DB, "db")),
        schema=_setting(schema, DEFAULT_SCHEMA, "schema"),
        provider=provider or ai_config.get("provider") or config.get("provider"),
        base_url=base_url or ai_config.get("base_url"),
        api_key=api_key,
        model=model or ai_config.get("model"),
        fixture_path=config.get("fixture_path"),
        timeout=(
            float(ai_config["timeout"]) if "timeout" in ai_config else None
        ),
    )


@setup_app.command("claude-code")
def setup_claude_code(
    url: str | None = typer.Option(
        None,
        help=(
            "Matterhorn hub base URL. Omit for an embedded stdio MCP server."
        ),
    ),
    scope: str | None = typer.Option(
        None,
        help="Shared memory scope; defaults to the current directory name.",
    ),
) -> None:
    """Merge project MCP and lifecycle-hook configuration for Claude Code."""

    from matterhorn.claude_code import (
        DEFAULT_HUB_URL,
        configure_project,
        probe_health,
    )

    config = _load_config()
    db = str(config.get("db", DEFAULT_DB))
    schema = str(config.get("schema", DEFAULT_SCHEMA))
    try:
        mcp_path, settings_path, selected_scope = configure_project(
            Path.cwd(),
            url=url,
            scope=scope,
            db=db,
            schema=schema,
        )
    except (TypeError, ValueError) as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"Wrote {mcp_path}")
    typer.echo(f"Wrote {settings_path}")
    typer.echo(f"Matterhorn scope: {selected_scope}")
    if url is None and probe_health():
        typer.echo(
            "Hint: a Matterhorn hub answered at "
            f"{DEFAULT_HUB_URL}; rerun with --url {DEFAULT_HUB_URL} "
            "to mount it instead. Embedded mode was kept."
        )


@hook_app.command("turn-end")
def hook_turn_end(
    url: str | None = typer.Option(None, help="Hub base URL."),
    scope: str = typer.Option(..., help="Target scope."),
) -> None:
    """Best-effort per-turn transcript tail delivery to a hub."""

    from matterhorn.claude_code import resolve_hook_scope, session_end

    session_end(sys.stdin, url=url, scope=resolve_hook_scope(scope), tail=40)


@hook_app.command("session-end")
def hook_session_end(
    url: str | None = typer.Option(
        None,
        help="Matterhorn hub base URL; omitted embedded hooks are no-ops.",
    ),
    scope: str = typer.Option(..., help="Matterhorn scope to receive messages."),
) -> None:
    """Best-effort delivery of a Claude Code transcript to a hub."""

    from matterhorn.claude_code import resolve_hook_scope, session_end

    session_end(sys.stdin, url=url, scope=resolve_hook_scope(scope))


@hook_app.command("session-start")
def hook_session_start(
    url: str | None = typer.Option(
        None,
        help="Matterhorn hub base URL; omitted embedded hooks are no-ops.",
    ),
    scope: str = typer.Option(..., help="Matterhorn scope to query."),
) -> None:
    """Best-effort open-matter context for a Claude Code session."""

    from matterhorn.claude_code import resolve_hook_scope, session_start

    session_start(sys.stdin, sys.stdout, url=url, scope=resolve_hook_scope(scope))


@app.command()
def serve(
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
    provider: str | None = typer.Option(
        None, help="Defaults to MATTERHORN_PROVIDER."
    ),
    base_url: str | None = typer.Option(None),
    api_key: str | None = typer.Option(None),
    model: str | None = typer.Option(None, help="Defaults to MATTERHORN_MODEL."),
    quiet_period_minutes: float | None = typer.Option(
        None,
        help="Auto-flush quiet message scopes; config default is 10 minutes.",
    ),
    max_batch_delay_minutes: float | None = typer.Option(
        None,
        help=(
            "Maximum pending-message batch age; defaults to "
            "MATTERHORN_MAX_BATCH_DELAY, config, or 5 minutes."
        ),
    ),
    daily_flush_at: str | None = typer.Option(
        None,
        help="Daily UTC auto-flush time as HH:MM; may come from config.",
    ),
    webhook_url: str | None = typer.Option(
        None,
        help="POST new event batches; may come from config webhook_url.",
    ),
    console: bool = typer.Option(
        False,
        "--console",
        help="Mount the Console static client at /console.",
    ),
) -> None:
    """Serve REST and MCP-HTTP with scheduling, webhooks, and optional Console."""
    config = _load_config()
    quiet = (
        quiet_period_minutes
        if quiet_period_minutes is not None
        else float(config.get("quiet_period_minutes", 10))
    )
    daily = daily_flush_at or config.get("daily_flush_at")
    maximum_delay = _max_batch_delay_minutes(
        max_batch_delay_minutes,
        config,
    )
    webhook = webhook_url or config.get("webhook_url")
    _run_service(
        db=db,
        schema=schema,
        host=host,
        port=port,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        quiet_period_minutes=quiet,
        max_batch_delay_minutes=maximum_delay,
        daily_flush_at=daily,
        webhook_url=webhook,
        console_enabled=console,
        open_browser=False,
        console_groups=_console_groups(config) if console else {},
    )


@app.command("console")
def console_command(
    db: str = typer.Option(DEFAULT_DB),
    schema: str = typer.Option(DEFAULT_SCHEMA),
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
    provider: str | None = typer.Option(
        None, help="Defaults to MATTERHORN_PROVIDER."
    ),
    base_url: str | None = typer.Option(None),
    api_key: str | None = typer.Option(None),
    model: str | None = typer.Option(None, help="Defaults to MATTERHORN_MODEL."),
    quiet_period_minutes: float | None = typer.Option(
        None,
        help="Auto-flush quiet message scopes; config default is 10 minutes.",
    ),
    max_batch_delay_minutes: float | None = typer.Option(
        None,
        help=(
            "Maximum pending-message batch age; defaults to "
            "MATTERHORN_MAX_BATCH_DELAY, config, or 5 minutes."
        ),
    ),
    daily_flush_at: str | None = typer.Option(
        None,
        help="Daily UTC auto-flush time as HH:MM; may come from config.",
    ),
    webhook_url: str | None = typer.Option(
        None,
        help="POST new event batches; may come from config webhook_url.",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="Open the Console URL in the default browser.",
    ),
) -> None:
    """Start REST and the browser-based operating Console on one port."""
    config = _load_config()
    quiet = (
        quiet_period_minutes
        if quiet_period_minutes is not None
        else float(config.get("quiet_period_minutes", 10))
    )
    maximum_delay = _max_batch_delay_minutes(
        max_batch_delay_minutes,
        config,
    )
    _run_service(
        db=db,
        schema=schema,
        host=host,
        port=port,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        quiet_period_minutes=quiet,
        max_batch_delay_minutes=maximum_delay,
        daily_flush_at=daily_flush_at or config.get("daily_flush_at"),
        webhook_url=webhook_url or config.get("webhook_url"),
        console_enabled=True,
        open_browser=open_browser,
        console_groups=_console_groups(config),
    )


def _run_service(
    *,
    db: str,
    schema: str,
    host: str,
    port: int,
    provider: str | None,
    base_url: str | None,
    api_key: str | None,
    model: str | None,
    quiet_period_minutes: float,
    max_batch_delay_minutes: float,
    daily_flush_at: str | None,
    webhook_url: str | None,
    console_enabled: bool,
    open_browser: bool,
    console_groups: dict[str, list[str]],
) -> None:
    import uvicorn

    from matterhorn.api import create_app
    from matterhorn.runtime_ai import AIRuntime

    gateway = _write_gateway(provider, base_url, api_key, model)
    if console_enabled:
        from matterhorn.console import ConsoleSampleGateway

        gateway = ConsoleSampleGateway(gateway)
    engine = _engine(
        db,
        schema,
        None,
        gateway=gateway,
        max_batch_delay_minutes=max_batch_delay_minutes,
    )
    ai_environment = dict(os.environ)
    if provider is not None:
        ai_environment["MATTERHORN_PROVIDER"] = provider
    if base_url is not None:
        ai_environment["MATTERHORN_BASE_URL"] = base_url
    if api_key is not None:
        ai_environment["MATTERHORN_API_KEY"] = api_key
    if model is not None:
        ai_environment["MATTERHORN_MODEL"] = model
    ai_runtime = AIRuntime(
        engine,
        config_path=Path.cwd() / CONFIG_NAME,
        environment=ai_environment,
    )
    application = create_app(
        engine=engine,
        quiet_period_minutes=quiet_period_minutes,
        max_batch_delay_minutes=max_batch_delay_minutes,
        daily_flush_at=daily_flush_at,
        webhook_url=webhook_url,
        console_enabled=console_enabled,
        ai_runtime=ai_runtime,
        console_groups=console_groups,
    )
    if console_enabled:
        console_url = f"http://{host}:{port}/console"
        typer.echo(f"Matterhorn Console: {console_url}")
        if open_browser:
            timer = threading.Timer(0.75, webbrowser.open, args=(console_url,))
            timer.daemon = True
            timer.start()
    uvicorn.run(
        application,
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


@eval_app.command("run")
def eval_run(
    dataset: Path | None = typer.Option(
        None,
        help="Eval dataset directory; defaults to the packaged spec/eval dataset.",
    ),
    case_id: str | None = typer.Option(
        None,
        "--case",
        help="Run one stable case_id.",
    ),
    provider: str | None = typer.Option(
        None,
        help=(
            "Write-side provider. Defaults to MATTERHORN_PROVIDER, or "
            "fixture-file when that variable is unset."
        ),
    ),
    responses: Path | None = typer.Option(
        None,
        help="Extractor response YAML override; requires one selected case.",
    ),
    base_url: str | None = typer.Option(
        None,
        help="Override MATTERHORN_BASE_URL for a live provider.",
    ),
    api_key: str | None = typer.Option(
        None,
        help=(
            "Override MATTERHORN_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY."
        ),
    ),
    model: str | None = typer.Option(
        None,
        help="Override MATTERHORN_MODEL for a live provider.",
    ),
    json_path: Path | None = typer.Option(
        None,
        "--json",
        help="Write the full versioned JSON report.",
    ),
    seed_note: bool = typer.Option(
        False,
        "--seed-note",
        help="Record that provider-side seed control is outside this harness.",
    ),
    assertion_results: Path | None = typer.Option(
        None,
        "--assertion-results",
        help="YAML assertion sets to diff against spec/eval/samples.",
    ),
    live_samples: bool = typer.Option(
        False,
        "--live-samples",
        help=(
            "Run alignment samples through legacy and unified paths with the "
            "configured real gateway. Off by default for CI safety."
        ),
    ),
    themes: bool = typer.Option(
        False,
        "--themes",
        help="Run the fixture-driven flat-matter theme rediscovery score.",
    ),
) -> None:
    """Run quality measurement; metric values never determine exit status."""
    from matterhorn.evalrunner import (
        format_live_sample_table,
        format_report_table,
        format_theme_rediscovery_table,
        run_eval_dataset,
        run_live_sample_comparison,
        run_theme_rediscovery,
    )

    try:
        capacity = _capacity_settings(_load_config())
        if live_samples and themes:
            raise ValueError("--live-samples and --themes are mutually exclusive")
        if themes:
            if any(value is not None for value in (case_id, responses, assertion_results)):
                raise ValueError(
                    "--themes cannot be combined with --case, --responses, or "
                    "--assertion-results"
                )
            report = run_theme_rediscovery(
                dataset / "themes" / "rediscovery.yaml"
                if dataset is not None
                else None
            )
        elif live_samples:
            if any(
                value is not None
                for value in (case_id, responses, assertion_results)
            ):
                raise ValueError(
                    "--live-samples cannot be combined with --case, "
                    "--responses, or --assertion-results"
                )
            report = run_live_sample_comparison(
                dataset=dataset,
                provider=provider,
                base_url=base_url,
                api_key=api_key,
                model=model,
                loss_weights=capacity.loss_weights,
            )
        else:
            report = run_eval_dataset(
                dataset,
                case_id=case_id,
                provider=provider,
                base_url=base_url,
                api_key=api_key,
                model=model,
                responses=responses,
                seed_note=seed_note,
                assertion_results=assertion_results,
                loss_weights=capacity.loss_weights,
            )
        if json_path is not None:
            json_path.write_text(
                json.dumps(
                    report,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
    except Exception as error:
        typer.echo(f"ERROR {error}", err=True)
        raise typer.Exit(code=2) from error
    typer.echo(
        format_theme_rediscovery_table(report)
        if themes
        else format_live_sample_table(report)
        if live_samples
        else format_report_table(report)
    )


if __name__ == "__main__":
    app()
