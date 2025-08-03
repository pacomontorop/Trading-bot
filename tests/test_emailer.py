#test_emailer.py

import os
from email import message_from_string
from types import SimpleNamespace
from unittest.mock import patch

import importlib

# Reload module to ensure patched env vars
import utils.emailer as emailer


def test_send_email_attaches_events_log(tmp_path, monkeypatch):
    # Prepare dummy log file
    log_dir = tmp_path
    log_path = log_dir / "events.log"
    content = "line1\nline2"
    log_path.write_text(content)

    # Patch emailer configuration
    monkeypatch.setattr(emailer, "EMAIL_SENDER", "sender@example.com", raising=False)
    monkeypatch.setattr(emailer, "EMAIL_RECEIVER", "receiver@example.com", raising=False)
    monkeypatch.setattr(emailer, "EMAIL_PASSWORD", "pwd", raising=False)
    monkeypatch.setattr(emailer, "log_dir", str(log_dir), raising=False)
    monkeypatch.setattr(emailer, "log_event", lambda msg: None)

    sent_messages = []

    class DummyServer:
        def login(self, *a, **k):
            pass
        def sendmail(self, sender, receiver, msg):
            sent_messages.append(msg)

    class DummySMTP:
        def __init__(self, *a, **k):
            pass
        def __enter__(self):
            return DummyServer()
        def __exit__(self, *a, **k):
            pass

    monkeypatch.setattr(emailer.smtplib, "SMTP_SSL", lambda *a, **k: DummySMTP())

    emailer.send_email("subj", "body", attach_log=True)

    assert sent_messages, "No email was sent"
    msg = message_from_string(sent_messages[0])
    attachments = [part for part in msg.walk() if part.get_content_disposition() == "attachment"]
    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment.get_filename() == "events.log"
    assert attachment.get_payload(decode=True).decode() == content
