"""Reading the central mailbox over IMAP. See docs/fetcher.md#imap.

Read only: folders are opened with EXAMINE and messages fetched with BODY.PEEK[], so
nothing in the mailbox changes (no read flags, no moves, no deletes). The originals stay
in the mailbox as the archive the database can be rebuilt from.

Progress is a per-folder cursor (UIDVALIDITY + last UID) in the folder_cursors table,
not the "unread" flag. The cursor moves past a message only after it was processed; when
UIDVALIDITY changes the folder is read again from the start and Message-ID dedupe keeps
it harmless.

Uses the standard library's imaplib; tests pass a fake in place of IMAP4_SSL.
"""

from __future__ import annotations

import imaplib
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from autobill.config import FetcherConfig
from autobill.fetch.mime import addressed_to, is_own_report
from autobill.fetch.source import RawMail
from autobill.store.db import now

PASSWORD_ENV = "AUTOBILL_IMAP_PASSWORD"
TIMEOUT_SECONDS = 60
ImapFactory = Callable[..., imaplib.IMAP4]


class MailboxError(RuntimeError):
    """Login, folder or protocol trouble, worded for the author."""


def imap_password() -> str | None:
    return os.environ.get(PASSWORD_ENV) or None


def _quote(folder: str) -> str:
    return '"' + folder + '"'


def _check(status, data, what: str) -> list:
    if status != "OK":
        detail = b" ".join(d for d in data if isinstance(d, bytes)).decode("utf-8", "replace")
        raise MailboxError(f"{what}失败：{detail or status}")
    return data


class Mailbox:
    """A logged-in, read-only IMAP session. Use as a context manager."""

    def __init__(
        self, config: FetcherConfig, password: str, factory: ImapFactory | None = None
    ) -> None:
        self.config = config
        self._password = password
        self._factory = factory or imaplib.IMAP4_SSL
        self._imap: imaplib.IMAP4 | None = None

    def __repr__(self) -> str:  # never show the password, even by accident
        return f"Mailbox({self.config.username}@{self.config.imap_server})"

    def __enter__(self) -> Mailbox:
        cfg = self.config
        try:
            self._imap = self._factory(cfg.imap_server, cfg.imap_port, timeout=TIMEOUT_SECONDS)
        except OSError as exc:
            raise MailboxError(f"连不上 {cfg.imap_server}:{cfg.imap_port}（{exc}）") from None
        try:
            self._imap.login(cfg.username, self._password)
        except imaplib.IMAP4.error as exc:
            raise MailboxError(f"登录被拒绝（{exc}）") from None
        return self

    def __exit__(self, *exc) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

    @property
    def imap(self) -> imaplib.IMAP4:
        assert self._imap is not None, "use Mailbox as a context manager"
        return self._imap

    def folders(self) -> list[str]:
        """Folder names as the server lists them (last quoted or plain token of each line)."""
        data = _check(*self.imap.list(), "列出文件夹")
        names = []
        for line in data:
            if not isinstance(line, bytes):
                continue
            text = line.decode("utf-8", "replace")
            match = re.search(r'"([^"]*)"\s*$', text) or re.search(r"(\S+)\s*$", text)
            if match:
                names.append(match.group(1))
        return names

    def resolve(self, wanted: list[str]) -> tuple[list[str], list[str]]:
        """(folders found, folders missing). Names match ignoring case, so "AutoBill" in
        config finds a folder created as "Autobill"; the server's spelling is returned."""
        existing = {name.lower(): name for name in self.folders()}
        found, missing = [], []
        for name in wanted:
            actual = existing.get(name.lower())
            if actual is None:
                missing.append(name)
            elif actual not in found:
                found.append(actual)
        return found, missing

    def examine(self, folder: str) -> tuple[int, int]:
        """Open a folder read only: (UIDVALIDITY, message count)."""
        data = _check(*self.imap.select(_quote(folder), readonly=True), f"打开文件夹 {folder}")
        count = int(data[0]) if data and data[0] else 0
        _, validity = self.imap.response("UIDVALIDITY")
        values = [v for v in validity if v]
        if not values:
            raise MailboxError(f"文件夹 {folder} 没有返回 UIDVALIDITY")
        return int(values[-1]), count

    def uids_after(self, last_uid: int) -> list[int]:
        data = _check(*self.imap.uid("SEARCH", None, f"UID {last_uid + 1}:*"), "查找新邮件")
        uids = [int(u) for u in b" ".join(d for d in data if d).split()]
        # "n:*" always includes the newest message, even when its UID is below n.
        return sorted(u for u in uids if u > last_uid)

    def fetch(self, uid: int, what: str = "BODY.PEEK[]") -> bytes:
        data = _check(*self.imap.uid("FETCH", str(uid), f"({what})"), f"读取邮件 {uid}")
        for item in data:
            if isinstance(item, tuple) and len(item) == 2:
                return item[1]
        raise MailboxError(f"邮件 {uid} 没有内容")


@dataclass
class FolderCursor:
    folder: str
    uidvalidity: int
    last_uid: int


def load_cursor(conn: sqlite3.Connection, folder: str) -> FolderCursor | None:
    row = conn.execute(
        "SELECT uidvalidity, last_uid FROM folder_cursors WHERE folder = ?", (folder,)
    ).fetchone()
    return FolderCursor(folder, row[0], row[1]) if row else None


def save_cursor(conn: sqlite3.Connection, cursor: FolderCursor) -> None:
    conn.execute(
        "INSERT INTO folder_cursors (folder, uidvalidity, last_uid, updated_at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT (folder) DO UPDATE SET"
        " uidvalidity = excluded.uidvalidity, last_uid = excluded.last_uid,"
        " updated_at = excluded.updated_at",
        (cursor.folder, cursor.uidvalidity, cursor.last_uid, now()),
    )


@dataclass
class FetchStats:
    seen: int = 0
    skipped: dict[str, int] = field(default_factory=dict)  # reason -> count
    missing_folders: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


class ImapSource:
    """New messages from the configured folders, oldest UID first.

    The cursor of a folder moves past a message when the consumer asks for the next one,
    i.e. after the previous message was processed; if processing raises, the message is
    read again next run.
    """

    def __init__(self, mailbox: Mailbox, conn: sqlite3.Connection) -> None:
        self.mailbox = mailbox
        self.conn = conn
        self.stats = FetchStats()

    def iter_new(self) -> Iterator[RawMail]:
        only_to = self.mailbox.config.only_to
        folders, self.stats.missing_folders = self.mailbox.resolve(self.mailbox.config.folders)
        if not folders:
            wanted = "、".join(self.mailbox.config.folders)
            raise MailboxError(f"邮箱里没有配置的文件夹（{wanted}）")
        for folder in folders:
            validity, _ = self.mailbox.examine(folder)
            cursor = load_cursor(self.conn, folder)
            if cursor is None or cursor.uidvalidity != validity:
                cursor = FolderCursor(folder, validity, 0)  # new or renumbered: read it all
            for uid in self.mailbox.uids_after(cursor.last_uid):
                data = self.mailbox.fetch(uid)
                self.stats.seen += 1
                if is_own_report(data):
                    self.stats.skip("AutoBill 自己的报表")
                elif only_to and not addressed_to(data, only_to):
                    self.stats.skip(f"不是发给 {only_to} 的")
                else:
                    yield RawMail(data, f"imap:{folder}:{validity}:{uid}")
                cursor.last_uid = uid
                save_cursor(self.conn, cursor)
            save_cursor(self.conn, cursor)
