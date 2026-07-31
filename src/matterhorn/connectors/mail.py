"""Stdlib-only IMAP pull connector with process-memory credentials."""

from __future__ import annotations

import imaplib
import json
import logging
import os
import re
import tomllib
from collections import Counter
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

from matterhorn.adapters.email_mbox import (
    coalesce_email_conversations,
    map_email_bytes,
)

MAIL_INTERVALS: dict[str, int | None] = {
    "off": None,
    "15min": 15 * 60,
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
}
MAIL_FETCH_BATCH_SIZE = 20


@dataclass(frozen=True)
class MailProvider:
    host: str
    port: int
    ssl: bool
    help_url: str
    auth_note: str | None = None


MAIL_PROVIDERS: dict[str, MailProvider] = {
    "gmail": MailProvider(
        "imap.gmail.com",
        993,
        True,
        "https://support.google.com/accounts/answer/185833",
    ),
    "outlook": MailProvider(
        "outlook.office365.com",
        993,
        True,
        "https://support.microsoft.com/en-us/outlook/"
        "pop-imap-and-smtp-settings-for-outlook-com",
        "Outlook.com currently documents OAuth2/Modern Auth as required.",
    ),
    "icloud": MailProvider(
        "imap.mail.me.com",
        993,
        True,
        "https://support.apple.com/en-us/102654",
    ),
    "qq": MailProvider(
        "imap.qq.com",
        993,
        True,
        "https://wx.mail.qq.com/list/readtemplate"
        "?name=app_intro.html#/agreement/authorizationCode",
    ),
    "163": MailProvider(
        "imap.163.com",
        993,
        True,
        "https://help.mail.163.com/faq.do?m=list&categoryID=197",
    ),
}

_UID = re.compile(rb"\d+")
_MAIL_SECTION = re.compile(r"^\s*\[\[?([^\]]+)\]\]?\s*(?:#.*)?$")
_EPOCH_WATERMARK = datetime(1970, 1, 1, tzinfo=UTC)
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MailConfig:
    provider: str
    host: str
    port: int
    ssl: bool
    user: str
    folder: str = "INBOX"
    interval: str = "off"
    initial_window: int = 50
    scope: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if self.provider not in {*MAIL_PROVIDERS, "manual"}:
            raise ValueError(
                "mail provider MUST be gmail, outlook, icloud, qq, 163, or manual"
            )
        if not self.host.strip():
            raise ValueError("mail host is required")
        if not 1 <= self.port <= 65535:
            raise ValueError("mail port MUST be between 1 and 65535")
        if not self.user.strip():
            raise ValueError("mail user is required")
        if not self.folder.strip():
            raise ValueError("mail folder is required")
        if self.interval not in MAIL_INTERVALS:
            raise ValueError("mail interval MUST be off, 15min, 1h, or 6h")
        if self.initial_window < 1:
            raise ValueError("mail initial_window MUST be at least 1")
        if self.scope is not None and not self.scope.strip():
            raise ValueError("mail scope MUST be non-empty when supplied")
        if self.name is not None and not self.name.strip():
            raise ValueError("mail name MUST be non-empty when supplied")

    @property
    def help_url(self) -> str | None:
        preset = MAIL_PROVIDERS.get(self.provider)
        return preset.help_url if preset is not None else None

    @property
    def container_id(self) -> str:
        return f"imap:{self.user}@{self.host}/{self.folder}"

    @property
    def account_id(self) -> str:
        return self.name or f"{self.user}@{self.host}/{self.folder}"

    def public_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "name": self.name,
            "provider": self.provider,
            "host": self.host,
            "port": self.port,
            "ssl": self.ssl,
            "account": self.user,
            "folder": self.folder,
            "interval": self.interval,
            "initial_window": self.initial_window,
            "scope": self.scope,
            "help_url": self.help_url,
            "auth_note": (
                MAIL_PROVIDERS[self.provider].auth_note
                if self.provider in MAIL_PROVIDERS
                else None
            ),
        }


@dataclass(frozen=True)
class MailSyncReport:
    scope_id: str
    account: str
    folder: str
    container_id: str
    pulled: int
    filtered: int
    filtered_by_reason: dict[str, int]
    parse_errors: int
    effective_window: int | None
    cards_produced: int
    new_assertions: int
    new_matters: int
    new_watermark: int
    uidvalidity: str
    previous_uidvalidity: str | None
    reset_detected: bool
    backfill: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MailSyncError(RuntimeError):
    """Safe-to-render mail connector failure."""


class MailAuthError(MailSyncError):
    """Authentication failed without retaining provider error text."""


class MailboxResetError(MailSyncError):
    def __init__(
        self,
        report: MailSyncReport,
        *,
        message: str | None = None,
    ):
        self.report = report
        super().__init__(
            message
            or (
                "IMAP UIDVALIDITY changed "
                f"from {report.previous_uidvalidity} to {report.uidvalidity}; "
                "refusing to re-pull the mailbox without --backfill."
            )
        )


def load_mail_configs(path: str | Path) -> list[MailConfig]:
    source = Path(path)
    if not source.is_file():
        return []
    try:
        payload = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"could not load {source.name}: {error}") from error
    raw = payload.get("mail")
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise TypeError("[mail] MUST be a TOML table")
    accounts = raw.get("accounts")
    if accounts is None:
        # Legacy [mail] is intentionally accepted as one account. Any later
        # save rewrites it as [[mail.accounts]] without dropping other tables.
        accounts = [raw]
    if not isinstance(accounts, list) or not all(
        isinstance(item, dict) for item in accounts
    ):
        raise TypeError("[[mail.accounts]] MUST be an array of TOML tables")
    return [_mail_config_from_mapping(item) for item in accounts]


def load_mail_config(path: str | Path) -> MailConfig | None:
    """Compatibility helper returning the first configured account."""

    configs = load_mail_configs(path)
    return configs[0] if configs else None


def _mail_config_from_mapping(raw: dict[str, Any]) -> MailConfig:
    return MailConfig(
        provider=str(raw.get("provider", "manual")),
        host=str(raw.get("host", "")),
        port=int(raw.get("port", 993)),
        ssl=bool(raw.get("ssl", True)),
        user=str(raw.get("user", "")),
        folder=str(raw.get("folder", "INBOX")),
        interval=str(raw.get("interval", "off")),
        initial_window=int(raw.get("initial_window", 50)),
        scope=(str(raw["scope"]) if raw.get("scope") is not None else None),
        name=(str(raw["name"]) if raw.get("name") is not None else None),
    )


def save_mail_config(path: str | Path, config: MailConfig) -> None:
    """Upsert one account and always write the collection-shaped TOML."""

    configs = load_mail_configs(path)
    replaced = False
    for index, existing in enumerate(configs):
        if existing.account_id == config.account_id:
            configs[index] = config
            replaced = True
            break
    if not replaced:
        configs.append(config)
    save_mail_configs(path, configs)


def save_mail_configs(
    path: str | Path,
    configs: list[MailConfig],
) -> None:
    """Replace only mail tables and never serialize a credential."""

    target = Path(path)
    original = target.read_text(encoding="utf-8") if target.is_file() else ""
    retained: list[str] = []
    skipping = False
    for line in original.splitlines():
        match = _MAIL_SECTION.match(line)
        if match is not None:
            skipping = match.group(1) == "mail" or match.group(1).startswith("mail.")
            if skipping:
                continue
        if not skipping:
            retained.append(line)
    while retained and not retained[-1].strip():
        retained.pop()
    rendered = [*retained]
    for config in configs:
        values: list[tuple[str, Any]] = [
            ("provider", config.provider),
            ("host", config.host),
            ("port", config.port),
            ("ssl", config.ssl),
            ("user", config.user),
            ("folder", config.folder),
            ("interval", config.interval),
            ("initial_window", config.initial_window),
        ]
        if config.scope is not None:
            values.append(("scope", config.scope))
        if config.name is not None:
            values.append(("name", config.name))
        rendered.extend(
            [
                *([""] if rendered else []),
                "[[mail.accounts]]",
                *[f"{key} = {_toml_scalar(value)}" for key, value in values],
            ]
        )
    rendered.append("")
    target.write_text("\n".join(rendered), encoding="utf-8")


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def redact_secret(text: str, secret: str | None) -> str:
    if secret:
        return text.replace(secret, "[REDACTED]")
    return text


def _contextual_sync_error(error: BaseException, secret: str | None) -> str:
    safe = redact_secret(str(error), secret)
    if safe.startswith(("IMAP ", "Mail ", "Could not connect ")):
        return safe
    return f"IMAP sync failed: {safe or type(error).__name__}"


def reset_mail_sync_position(
    engine: Any,
    config: MailConfig,
    *,
    scope_id: str,
    confirm: bool,
) -> dict[str, Any]:
    """Delete only the configured connector position after explicit consent."""

    if not confirm:
        raise MailSyncError(
            "Mail sync reset requires explicit confirmation "
            "(--yes or {\"confirm\": true})."
        )
    if not scope_id:
        raise MailSyncError("Mail sync reset requires a scope.")
    with engine.store.transaction():
        deleted = engine.store.delete_sync_position(
            scope_id,
            config.container_id,
        )
    return {
        "scope_id": scope_id,
        "container_id": config.container_id,
        "position_deleted": deleted,
        "next_sync": "initial_window",
    }


class MailConnector:
    def __init__(
        self,
        engine: Any,
        config: MailConfig,
        password: str,
        *,
        imap_ssl_factory: Any = imaplib.IMAP4_SSL,
        imap_factory: Any = imaplib.IMAP4,
    ):
        if not password:
            raise MailAuthError("Mail authentication requires an in-memory password.")
        self.engine = engine
        self.config = config
        self._password = password
        self._imap_ssl_factory = imap_ssl_factory
        self._imap_factory = imap_factory

    def sync(self, *, scope_id: str, backfill: bool = False) -> MailSyncReport:
        if not scope_id:
            raise MailSyncError("Mail sync requires a scope.")
        config = self.config
        position = next(
            (
                item
                for item in self.engine.sync_positions(scope_id)
                if item.container_id == config.container_id
            ),
            None,
        )
        previous_uid = position.uid_watermark if position is not None else None
        previous_uidvalidity = position.cursor if position is not None else None
        client = self._connect()
        try:
            self._login(client)
            status, _ = client.select(config.folder, readonly=True)
            _require_ok(status, "select mail folder")
            uidvalidity = _uidvalidity(client)
            reset = (
                previous_uidvalidity is not None
                and previous_uidvalidity != uidvalidity
            )
            if reset and not backfill:
                raise MailboxResetError(
                    MailSyncReport(
                        scope_id=scope_id,
                        account=config.user,
                        folder=config.folder,
                        container_id=config.container_id,
                        pulled=0,
                        filtered=0,
                        filtered_by_reason={},
                        parse_errors=0,
                        effective_window=None,
                        cards_produced=0,
                        new_assertions=0,
                        new_matters=0,
                        new_watermark=previous_uid or 0,
                        uidvalidity=uidvalidity,
                        previous_uidvalidity=previous_uidvalidity,
                        reset_detected=True,
                        backfill=False,
                    )
                )
            if previous_uid is None and not backfill:
                search_criteria = "ALL"
            else:
                start_uid = 1 if backfill else previous_uid + 1
                search_criteria = f"{start_uid}:*"
            status, data = client.uid("search", None, search_criteria)
            _require_ok(status, "search mail UIDs")
            searched_uids = _parse_uids(data)
            if previous_uid is None and not backfill:
                uids = searched_uids[-config.initial_window :]
                effective_window: int | None = len(uids)
            else:
                uids = [
                    uid
                    for uid in searched_uids
                    if backfill or previous_uid is None or uid > previous_uid
                ]
                effective_window = None
            records = []
            dropped: Counter[str] = Counter()
            for batch in _batches(uids, MAIL_FETCH_BATCH_SIZE):
                uid_set = ",".join(str(uid) for uid in batch)
                status, fetched = client.uid("fetch", uid_set, "(RFC822)")
                _require_ok(status, f"fetch mail UIDs {uid_set}")
                payloads = _rfc822_payloads(fetched)
                if len(payloads) != len(batch):
                    raise MailSyncError(
                        "IMAP fetch returned an unexpected RFC822 payload count."
                    )
                for uid, payload in zip(batch, payloads, strict=True):
                    try:
                        mapped = map_email_bytes(
                            payload,
                            container_id=config.container_id,
                        )
                    except Exception as error:  # noqa: BLE001
                        dropped["PARSE_ERROR"] += 1
                        _LOGGER.warning(
                            "Dropped IMAP UID %s after %s.",
                            uid,
                            type(error).__name__,
                        )
                        continue
                    dropped.update(mapped.dropped)
                    records.extend(mapped.records)

            before_matters = {
                item.subject_key for item in self.engine.matters(scope_id)
            }
            cards_produced = 0
            new_assertions = 0
            if records:
                records = coalesce_email_conversations(records)
                records = _reconcile_existing_email_conversations(
                    records,
                    self.engine.store.subjects(scope_id),
                )
                record_report = self.engine.add_records(
                    records,
                    scope_id=scope_id,
                )
                dream_report = self.engine.dream(scope_id)
                if dream_report.failed:
                    raise MailSyncError(
                        "Mail messages were accepted but extraction did not complete."
                    )
                cards_produced = record_report.cards_accepted
                new_assertions = (
                    record_report.assertions_emitted
                    + dream_report.new_assertions
                )
            after_matters = {
                item.subject_key for item in self.engine.matters(scope_id)
            }
            watermark = (
                max(searched_uids, default=0)
                if backfill
                else max([previous_uid or 0, *searched_uids])
            )
            with self.engine.store.transaction():
                self.engine.store.update_mail_sync_position(
                    scope_id,
                    config.container_id,
                    uid_watermark=watermark,
                    uidvalidity=uidvalidity,
                    fallback_watermark=_EPOCH_WATERMARK,
                )
            return MailSyncReport(
                scope_id=scope_id,
                account=config.user,
                folder=config.folder,
                container_id=config.container_id,
                pulled=len(records),
                filtered=sum(dropped.values()),
                filtered_by_reason=dict(
                    sorted(
                        (reason, count)
                        for reason, count in dropped.items()
                        if reason != "PARSE_ERROR"
                    )
                ),
                parse_errors=dropped["PARSE_ERROR"],
                effective_window=effective_window,
                cards_produced=cards_produced,
                new_assertions=new_assertions,
                new_matters=len(after_matters - before_matters),
                new_watermark=watermark,
                uidvalidity=uidvalidity,
                previous_uidvalidity=previous_uidvalidity,
                reset_detected=reset,
                backfill=backfill,
            )
        except MailboxResetError:
            raise
        except MailSyncError as error:
            safe = _contextual_sync_error(error, self._password)
            raise type(error)(safe) from None
        # Provider/socket implementations may raise heterogeneous exceptions.
        # This boundary catches all of them specifically to redact the secret.
        except Exception as error:  # noqa: BLE001
            safe = _contextual_sync_error(error, self._password)
            raise MailSyncError(safe) from None
        finally:
            with suppress(Exception):
                client.logout()

    def _connect(self) -> Any:
        factory = (
            self._imap_ssl_factory if self.config.ssl else self._imap_factory
        )
        try:
            return factory(self.config.host, self.config.port)
        # Socket and injected IMAP factories do not share an exception base.
        except Exception as error:  # noqa: BLE001
            safe = redact_secret(str(error), self._password)
            raise MailSyncError(
                f"Could not connect to {self.config.host}:{self.config.port}: {safe}"
            ) from None

    def _login(self, client: Any) -> None:
        try:
            status, _ = client.login(self.config.user, self._password)
            _require_ok(status, "authenticate")
        # Authentication backends may echo credentials from arbitrary errors;
        # replace every provider error with a fixed safe message.
        except Exception:  # noqa: BLE001
            help_text = (
                f" See {self.config.help_url}" if self.config.help_url else ""
            )
            message = (
                f"IMAP authentication failed for {self.config.user}."
                f"{help_text}"
            )
            _LOGGER.warning("%s", message)
            raise MailAuthError(message) from None


class MailRuntime:
    """Process-local password, status, and scheduled sync coordination."""

    def __init__(
        self,
        engine: Any,
        *,
        config_path: str | Path,
        environment: dict[str, str] | None = None,
        clock: Any = None,
        imap_ssl_factory: Any = imaplib.IMAP4_SSL,
        imap_factory: Any = imaplib.IMAP4,
        config: MailConfig | None = None,
        persist_config: bool = True,
    ):
        self.engine = engine
        self.config_path = Path(config_path)
        self.config = (
            config if config is not None else load_mail_config(self.config_path)
        )
        self._persist_config = persist_config
        selected_environment = environment if environment is not None else os.environ
        account_key = (
            re.sub(r"[^A-Z0-9]+", "_", self.config.account_id.upper()).strip("_")
            if self.config is not None
            else ""
        )
        self._password = (
            selected_environment.get(f"MATTERHORN_MAIL_PASSWORD_{account_key}")
            if account_key
            else None
        ) or selected_environment.get("MATTERHORN_MAIL_PASSWORD")
        self._password_source = "environment" if self._password else None
        self._clock = clock or engine.now
        self._imap_ssl_factory = imap_ssl_factory
        self._imap_factory = imap_factory
        self._lock = Lock()
        self._syncing = False
        self.last_sync_at: datetime | None = None
        self.last_run_at: datetime | None = None
        self.last_report: MailSyncReport | None = None
        self.last_error: str | None = None
        self.next_run_at: datetime | None = None
        self._schedule_from_now()

    def configure(
        self,
        config: MailConfig,
        *,
        password: str | None = None,
    ) -> dict[str, Any]:
        if self._persist_config:
            save_mail_config(self.config_path, config)
        self.config = config
        if password:
            self._password = password
            self._password_source = "memory"
        self.last_error = None
        self._schedule_from_now()
        return config.public_dict()

    def sync(
        self,
        *,
        scope_id: str | None = None,
        backfill: bool = False,
    ) -> MailSyncReport:
        with self._lock:
            config = self.config
            if config is None:
                raise MailSyncError("Mail connector is not configured.")
            selected_scope = scope_id or config.scope
            if not selected_scope:
                raise MailSyncError(
                    "Mail sync requires scope_id or mail.scope configuration."
                )
            if not self._password:
                self.last_error = "Re-enter password before syncing."
                raise MailAuthError(self.last_error)
            connector = MailConnector(
                self.engine,
                config,
                self._password,
                imap_ssl_factory=self._imap_ssl_factory,
                imap_factory=self._imap_factory,
            )
            self.last_run_at = _as_utc(self._clock())
            self._syncing = True
            try:
                report = connector.sync(
                    scope_id=selected_scope,
                    backfill=backfill,
                )
            except MailboxResetError as error:
                self.last_report = error.report
                self.last_error = _contextual_sync_error(
                    error,
                    self._password,
                )
                self._schedule_from_now()
                raise MailboxResetError(
                    error.report,
                    message=self.last_error,
                ) from None
            except MailSyncError as error:
                self.last_error = _contextual_sync_error(
                    error,
                    self._password,
                )
                self._schedule_from_now()
                raise type(error)(self.last_error) from None
            finally:
                self._syncing = False
            self.last_report = report
            self.last_error = None
            self.last_sync_at = self.last_run_at
            self._schedule_from_now()
            return report

    def tick(self) -> MailSyncReport | None:
        if self.next_run_at is None:
            return None
        now = _as_utc(self._clock())
        if now < self.next_run_at:
            return None
        try:
            return self.sync()
        except MailSyncError:
            return None

    def reset(
        self,
        *,
        scope_id: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            config = self.config
            if config is None:
                raise MailSyncError("Mail connector is not configured.")
            selected_scope = scope_id or config.scope
            if not selected_scope:
                raise MailSyncError(
                    "Mail sync reset requires scope_id or mail.scope configuration."
                )
            result = reset_mail_sync_position(
                self.engine,
                config,
                scope_id=selected_scope,
                confirm=confirm,
            )
            self.last_error = None
            self._schedule_from_now()
            return result

    def status(self, *, scope_id: str | None = None) -> dict[str, Any]:
        config = self.config
        selected_scope = scope_id or (config.scope if config is not None else None)
        position = None
        if config is not None and selected_scope:
            position = next(
                (
                    item
                    for item in self.engine.sync_positions(selected_scope)
                    if item.container_id == config.container_id
                ),
                None,
            )
        return {
            "configured": config is not None,
            "config": config.public_dict() if config is not None else None,
            "scope_id": selected_scope,
            "password_state": (
                "loaded from environment"
                if self._password_source == "environment"
                else (
                    "loaded in process memory"
                    if self._password_source == "memory"
                    else "re-enter password"
                )
            ),
            "last_sync_at": _iso(self.last_sync_at),
            "last_run_at": _iso(self.last_run_at),
            "next_run_at": _iso(self.next_run_at),
            "syncing": self._syncing,
            "uid_watermark": (
                position.uid_watermark if position is not None else None
            ),
            "uidvalidity": position.cursor if position is not None else None,
            "last_report": (
                self.last_report.to_dict() if self.last_report is not None else None
            ),
            "error": self.last_error,
        }

    def _schedule_from_now(self) -> None:
        seconds = (
            MAIL_INTERVALS.get(self.config.interval)
            if self.config is not None
            else None
        )
        self.next_run_at = (
            _as_utc(self._clock()) + timedelta(seconds=seconds)
            if seconds is not None
            else None
        )


class MailRuntimeRegistry:
    """Registry of isolated process-local mailbox runtimes."""

    def __init__(
        self,
        engine: Any,
        *,
        config_path: str | Path,
        environment: dict[str, str] | None = None,
        clock: Any = None,
        imap_ssl_factory: Any = imaplib.IMAP4_SSL,
        imap_factory: Any = imaplib.IMAP4,
    ):
        self.engine = engine
        self.config_path = Path(config_path)
        self._environment = environment if environment is not None else os.environ
        self._clock = clock
        self._imap_ssl_factory = imap_ssl_factory
        self._imap_factory = imap_factory
        self._runtimes: dict[str, MailRuntime] = {}
        for config in load_mail_configs(self.config_path):
            self._runtimes[config.account_id] = self._runtime(config)

    def _runtime(self, config: MailConfig) -> MailRuntime:
        kwargs: dict[str, Any] = {
            "config_path": self.config_path,
            "environment": self._environment,
            "imap_ssl_factory": self._imap_ssl_factory,
            "imap_factory": self._imap_factory,
            "config": config,
            "persist_config": False,
        }
        if self._clock is not None:
            kwargs["clock"] = self._clock
        return MailRuntime(self.engine, **kwargs)

    def configure(
        self,
        config: MailConfig,
        *,
        password: str | None = None,
    ) -> dict[str, Any]:
        runtime = self._runtimes.get(config.account_id)
        if runtime is None:
            runtime = self._runtime(config)
            self._runtimes[config.account_id] = runtime
        runtime.configure(config, password=password)
        self._save()
        return config.public_dict()

    def configure_first(
        self,
        config: MailConfig,
        *,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Compatibility update targeting the first account."""

        first_id = next(iter(self._runtimes), None)
        if first_id is None:
            return self.configure(config, password=password)
        if config.account_id != first_id:
            config = replace(config, name=first_id)
        return self.configure(config, password=password)

    def accounts(self) -> list[dict[str, Any]]:
        return [runtime.status() for runtime in self._runtimes.values()]

    def account(self, account_id: str) -> MailRuntime:
        try:
            return self._runtimes[account_id]
        except KeyError:
            choices = ", ".join(self._runtimes) or "none"
            raise MailSyncError(
                f"Unknown mail account {account_id!r}; configured accounts: {choices}."
            ) from None

    def delete(self, account_id: str) -> dict[str, Any]:
        self.account(account_id)
        del self._runtimes[account_id]
        self._save()
        return {
            "account_id": account_id,
            "removed": True,
            "watermark_retained": True,
            "message": (
                "Mailbox configuration and its in-memory credential were removed; "
                "stored sync watermark data was retained."
            ),
        }

    def sync_account(
        self,
        account_id: str,
        *,
        scope_id: str | None = None,
        backfill: bool = False,
    ) -> MailSyncReport:
        return self.account(account_id).sync(
            scope_id=scope_id,
            backfill=backfill,
        )

    def reset_account(
        self,
        account_id: str,
        *,
        scope_id: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return self.account(account_id).reset(
            scope_id=scope_id,
            confirm=confirm,
        )

    def tick(self) -> list[MailSyncReport]:
        reports = []
        for runtime in list(self._runtimes.values()):
            report = runtime.tick()
            if report is not None:
                reports.append(report)
        return reports

    # Compatibility aliases deliberately target the first configured account.
    def status(self, *, scope_id: str | None = None) -> dict[str, Any]:
        runtime = self._first(required=False)
        if runtime is None:
            return _empty_mail_status()
        return runtime.status(scope_id=scope_id)

    def sync(
        self,
        *,
        scope_id: str | None = None,
        backfill: bool = False,
    ) -> MailSyncReport:
        return self._first().sync(scope_id=scope_id, backfill=backfill)

    def reset(
        self,
        *,
        scope_id: str | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        return self._first().reset(scope_id=scope_id, confirm=confirm)

    def _first(self, *, required: bool = True) -> MailRuntime | None:
        runtime = next(iter(self._runtimes.values()), None)
        if runtime is None and required:
            raise MailSyncError("Mail connector is not configured.")
        return runtime

    def _save(self) -> None:
        save_mail_configs(
            self.config_path,
            [
                runtime.config
                for runtime in self._runtimes.values()
                if runtime.config is not None
            ],
        )


def _empty_mail_status() -> dict[str, Any]:
    return {
        "configured": False,
        "config": None,
        "scope_id": None,
        "password_state": "re-enter password",
        "last_sync_at": None,
        "last_run_at": None,
        "next_run_at": None,
        "syncing": False,
        "uid_watermark": None,
        "uidvalidity": None,
        "last_report": None,
        "error": None,
    }


def _require_ok(status: Any, action: str) -> None:
    normalized = (
        status.decode("ascii", errors="replace")
        if isinstance(status, bytes)
        else str(status)
    )
    if normalized.upper() != "OK":
        raise MailSyncError(f"IMAP could not {action}.")


def _uidvalidity(client: Any) -> str:
    response = client.response("UIDVALIDITY")
    values = response[1] if isinstance(response, tuple) and len(response) > 1 else []
    parsed = _parse_uids(values)
    if not parsed:
        raise MailSyncError("IMAP server did not report UIDVALIDITY.")
    return str(parsed[0])


def _parse_uids(values: Any) -> list[int]:
    if values is None:
        return []
    if isinstance(values, (bytes, bytearray)):
        chunks = [bytes(values)]
    else:
        chunks = [
            item
            for item in values
            if isinstance(item, (bytes, bytearray))
        ]
    return sorted({int(match) for chunk in chunks for match in _UID.findall(chunk)})


def _rfc822_payloads(values: Any) -> list[bytes]:
    if not isinstance(values, list):
        raise MailSyncError("IMAP fetch returned an invalid RFC822 response.")
    return [
        bytes(item[1])
        for item in values
        if (
            isinstance(item, tuple)
            and len(item) >= 2
            and isinstance(item[1], (bytes, bytearray))
        )
    ]


def _reconcile_existing_email_conversations(
    records: list[Any],
    subjects: list[Any],
) -> list[Any]:
    """Reuse a root email's earlier subject-fallback identity across syncs."""

    subjects_by_source: dict[str, list[Any]] = {}
    for subject in subjects:
        for source_id in subject.source_ids:
            subjects_by_source.setdefault(source_id, []).append(subject)
    result = []
    for record in records:
        thread_id = record.thread_id
        prefix = f"{record.container_id}:message-id:"
        if thread_id is None or not thread_id.startswith(prefix):
            result.append(record)
            continue
        root_id = thread_id.removeprefix(prefix)
        root_source_id = f"{record.container_id}:{root_id}"
        matches = subjects_by_source.get(root_source_id, [])
        existing_threads = sorted(
            thread
            for subject in matches
            for thread in subject.thread_ids
            if thread.startswith(f"{record.container_id}:")
        )
        if len(matches) == 1 and existing_threads:
            record = record.model_copy(
                update={"thread_id": existing_threads[0]}
            )
        result.append(record)
    return result


def _batches(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _as_utc(value: datetime) -> datetime:
    return (
        value.replace(tzinfo=UTC)
        if value.tzinfo is None
        else value.astimezone(UTC)
    )


def _iso(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None
