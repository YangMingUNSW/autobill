"""Test doubles shared by several test modules."""

import json
from datetime import date

import httpx


class FakeFrankfurter:
    """Answers like api.frankfurter.dev: weekends map to Friday, unknown days are 404."""

    def __init__(self, rates: dict[tuple[str, str], str], down: bool = False):
        self.rates = rates  # (currency, published day) -> rate
        self.down = down
        self.calls: list[str] = []

    def __call__(self, url: str) -> str:
        self.calls.append(url)
        if self.down:
            raise httpx.ConnectError("offline")
        day = date.fromisoformat(url.rsplit("/", 1)[1].split("?")[0])
        currency = url.split("base=")[1].split("&")[0]
        while day.weekday() >= 5:  # Saturday/Sunday -> previous Friday
            day = date.fromordinal(day.toordinal() - 1)
        rate = self.rates.get((currency, day.isoformat()))
        if rate is None:
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404))
        return json.dumps(
            {"amount": 1.0, "base": currency, "date": day.isoformat(), "rates": {"CNY": rate}}
        ).replace(f'"{rate}"', rate)


class FakeSMTP:
    """Stands in for smtplib.SMTP_SSL: records logins and messages, never connects."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, fail_on_send=False):
        self.host, self.port, self.timeout = host, port, timeout
        self.logins: list[tuple[str, str]] = []
        self.sent = []
        self.fail_on_send = fail_on_send
        self.tls_started = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.tls_started = True

    def login(self, user, password):
        self.logins.append((user, password))

    def send_message(self, message):
        if self.fail_on_send:
            import smtplib

            raise smtplib.SMTPServerDisconnected("server went away")
        self.sent.append(message)


class FakeIMAP:
    """Stands in for imaplib.IMAP4_SSL: an in-memory mailbox that records every command.

    `folders` maps a folder name to (UIDVALIDITY, {uid: raw bytes}). Build one with
    FakeIMAP.factory(...) and pass it (or monkeypatch imaplib.IMAP4_SSL with it).
    """

    instances: list["FakeIMAP"] = []

    def __init__(self, host, port, timeout=None, folders=None, password="app-password"):
        self.host, self.port, self.timeout = host, port, timeout
        self.folders = folders if folders is not None else {}
        self.password = password
        self.commands: list[tuple] = []
        self.selected = None
        self.readonly = None
        FakeIMAP.instances.append(self)

    @classmethod
    def factory(cls, folders, password="app-password"):
        def make(host, port, timeout=None):
            return cls(host, port, timeout, folders, password)

        return make

    def login(self, user, password):
        import imaplib

        self.commands.append(("LOGIN", user))
        if password != self.password:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Authentication failed.")
        return "OK", [b"LOGIN completed"]

    def list(self):
        self.commands.append(("LIST",))
        return "OK", [f'() "/" "{name}"'.encode() for name in ["INBOX", *self.folders]]

    def select(self, mailbox, readonly=False):
        name = mailbox.strip('"')
        self.commands.append(("SELECT", name, readonly))
        if name not in self.folders:
            return "NO", [b"Mailbox does not exist"]
        self.selected, self.readonly = name, readonly
        return "OK", [str(len(self.folders[name][1])).encode()]

    def response(self, code):
        validity = self.folders[self.selected][0]
        return code, [str(validity).encode()]

    def uid(self, command, *args):
        self.commands.append(("UID", command, *args))
        messages = self.folders[self.selected][1]
        if command == "SEARCH":
            start = int(args[1].split()[1].split(":")[0])
            found = sorted(u for u in messages if u >= start)
            if not found and messages:
                found = [max(messages)]  # like a real server, "n:*" matches the newest
            return "OK", [" ".join(map(str, found)).encode()]
        if command == "FETCH":
            uid = int(args[0])
            assert args[1] == "(BODY.PEEK[])", "must not mark messages as read"
            data = messages[uid]
            return "OK", [(f"{uid} (UID {uid} BODY[] {{{len(data)}}}".encode(), data), b")"]
        raise AssertionError(f"unexpected IMAP command {command}: the fetcher must be read only")

    def logout(self):
        self.commands.append(("LOGOUT",))
        return "BYE", [b"logging out"]
