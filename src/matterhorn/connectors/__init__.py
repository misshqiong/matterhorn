"""Host-owned network connectors feeding Matterhorn's public write facade."""

from matterhorn.connectors.mail import (
    MAIL_INTERVALS,
    MAIL_PROVIDERS,
    MailAuthError,
    MailboxResetError,
    MailConfig,
    MailConnector,
    MailRuntime,
    MailRuntimeRegistry,
    MailSyncError,
    MailSyncReport,
    load_mail_config,
    load_mail_configs,
    save_mail_config,
    save_mail_configs,
)

__all__ = [
    "MAIL_INTERVALS",
    "MAIL_PROVIDERS",
    "MailAuthError",
    "MailConfig",
    "MailConnector",
    "MailRuntime",
    "MailRuntimeRegistry",
    "MailSyncError",
    "MailSyncReport",
    "MailboxResetError",
    "load_mail_config",
    "load_mail_configs",
    "save_mail_config",
    "save_mail_configs",
]
