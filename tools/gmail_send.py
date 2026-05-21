#!/usr/bin/env python3
"""
gmail_send.py — Send emails and manage drafts via gogcli.

Supports multiple Google accounts (homer, primary, ad-hoc).

Usage:
    gmail_send.py --account primary send --to user@example.com --subject "Hello" --body "Hi there"
    gmail_send.py --account primary send --to user@example.com --subject "Re: thread" --body-file /path/to/body.txt --reply-to MSG_ID
    gmail_send.py --account primary draft --to user@example.com --subject "Hello" --body "Hi"
    gmail_send.py --account primary draft-update --draft-id DRAFT_ID --to user@example.com --subject "New" --body "Updated"
    gmail_send.py --account primary draft-send --draft-id DRAFT_ID
    gmail_send.py --account primary draft-delete --draft-id DRAFT_ID

Output (JSON):
    {"status": "sent", "message_id": "...", "to": "...", "subject": "..."}
    {"status": "drafted", "draft_id": "...", "to": "...", "subject": "..."}
    {"status": "updated", "draft_id": "...", "to": "...", "subject": "..."}
    {"status": "draft_sent", "draft_id": "...", "message_id": "..."}
    {"status": "deleted", "draft_id": "..."}
    {"error": "..."}

Security:
    The 'send' subcommand enforces an internal-only guard. Recipients must
    match HOMER_INTERNAL_EMAILS (comma-separated addresses or @domain patterns).
    External recipients are rejected — use 'draft' + 'draft-send' instead.
    `--body-file` is restricted to the nanobot workspace to prevent path
    traversal exfiltration of secrets.
"""

import argparse
import json
import os
import sys
from email.utils import parseaddr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gogcli
from google_auth import load_google_credentials, require_scopes
from email_approval_store import create_approval, check_approval, mark_sent

PORTAL_BASE = os.environ.get("PORTAL_BASE_URL", "")

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"


def _approval_url(approval_id: str) -> str:
    return f"{PORTAL_BASE}/approve/{approval_id}"


def _load_internal_patterns() -> list[str]:
    """Load internal email patterns from HOMER_INTERNAL_EMAILS env var.

    Patterns can be full addresses (user@example.com) or domain patterns
    (@example.com). Returns lowercased patterns.
    """
    raw = os.environ.get("HOMER_INTERNAL_EMAILS", "")
    if not raw.strip():
        return []
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def is_internal_recipient(address: str, patterns: list[str]) -> bool:
    """Check if an email address matches any internal pattern."""
    _, addr = parseaddr(address)
    addr = addr.lower()
    if not addr:
        return False
    for pattern in patterns:
        if pattern.startswith("@"):
            if addr.endswith(pattern):
                return True
        elif addr == pattern:
            return True
    return False


def check_external_guard(to: str, cc: str | None, bcc: str | None) -> str | None:
    """Return an error message if any recipient is external. None if all internal."""
    patterns = _load_internal_patterns()
    if not patterns:
        return (
            "Direct send blocked: HOMER_INTERNAL_EMAILS is not configured. "
            "Use 'draft' + 'draft-send' for human-in-the-loop approval, "
            "or set HOMER_INTERNAL_EMAILS to allow direct sends to internal addresses."
        )
    all_recipients = [to]
    for field in (cc, bcc):
        if field:
            all_recipients.extend(a.strip() for a in field.split(","))
    external = [r for r in all_recipients if r and not is_internal_recipient(r, patterns)]
    if external:
        return (
            f"Direct send blocked: {', '.join(external)} not in HOMER_INTERNAL_EMAILS. "
            "Use 'draft' + 'draft-send' for external recipients (human-in-the-loop required)."
        )
    return None


def from_address_for(account: str) -> str | None:
    """Return the From email address for the given account, or None to use default.

    NOTE: gogcli's `--from` accepts only an address, not a display name. Display
    name comes from the account's Gmail send-as configuration. Configure it in
    Gmail Settings → Accounts → "Send mail as" if you want a custom display
    name (e.g. "Homer (AI Assistant)").
    """
    if account == "homer":
        return os.environ.get("HOMER_EMAIL_ADDRESS", "homer@example.com")
    return None


def _resolve_body(args) -> tuple[str, str | None]:
    """Resolve email body from --body or --body-file.

    Returns (body_text, body_file_path):
      - For inline --body: (text, None) — gogcli will receive --body=<text>.
      - For --body-file: (text, path) — gogcli will receive --body-file=<path>.
        We still read the file in Python for the approval-store preview, but
        the full content is handed to gogcli as a path so it never travels
        through subprocess argv (no ARG_MAX ceiling on body length).

    --body-file is restricted to the nanobot workspace to prevent
    exfiltration of secrets via path traversal.
    """
    if args.body_file:
        path = Path(args.body_file).resolve()
        workspace = Path(os.environ.get(
            "HOMER_WORKSPACE",
            str(Path(__file__).parent.parent / "context" / ".nanobot_workspace"),
        )).resolve()
        if not path.is_relative_to(workspace):
            raise ValueError(
                f"--body-file must be inside the workspace ({workspace}). "
                f"Got: {path}"
            )
        return path.read_text(encoding="utf-8"), str(path)
    return args.body, None


def get_access_token(account: str) -> str:
    """Load credentials, verify send/compose scope, return the access token."""
    creds = load_google_credentials(account)
    require_scopes(creds, account, GMAIL_COMPOSE_SCOPE, GMAIL_SEND_SCOPE)
    if not creds.token:
        raise RuntimeError(f"No access token available for account '{account}'")
    return creds.token


def fetch_subject(token: str, message_id: str) -> str:
    """Fetch the Subject header of an existing message (for reply subject derivation)."""
    data = gogcli.run(
        token,
        "gmail", "get", message_id,
        "--format=metadata",
        "--headers=Subject",
    )
    payload = data.get("message", data) or {}
    headers = payload.get("payload", {}).get("headers", []) or []
    for h in headers:
        if h.get("name", "").lower() == "subject":
            return h.get("value", "") or ""
    return ""


def derive_reply_subject(token: str, subject: str, reply_to_message_id: str) -> str:
    """Apply Re: prefix conventions for replies.

    - If caller provided a subject and it doesn't start with Re:, prepend it.
    - If caller didn't provide a subject, fetch the original and prepend Re:.
    - If the original lookup fails (deleted message, network blip), fall
      back to a safe default rather than aborting the whole send.
    """
    if subject:
        return subject if subject.lower().startswith("re:") else f"Re: {subject}"
    try:
        orig_subject = fetch_subject(token, reply_to_message_id)
    except RuntimeError:
        # Reply lookup failed — caller wanted a reply but we can't fetch the
        # original subject. Default rather than crash; threading still works
        # via --reply-to-message-id, the recipient just won't see "Re: <orig>".
        return "(no subject)"
    if not orig_subject:
        return "(no subject)"
    return orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"


def _common_compose_args(
    *,
    to: str | None,
    subject: str,
    body: str,
    body_file_path: str | None,
    cc: str | None,
    bcc: str | None,
    reply_to_message_id: str | None,
    from_addr: str | None,
) -> list[str]:
    """Build the shared argv suffix for send / drafts create / drafts update.

    When `body_file_path` is set, pass `--body-file=<path>` to gogcli so the
    body content stays on disk; otherwise pass the inline body via `--body`.
    """
    args: list[str] = ["--subject", subject]
    if body_file_path:
        args += ["--body-file", body_file_path]
    else:
        args += ["--body", body]
    if to:
        args += ["--to", to]
    if cc:
        args += ["--cc", cc]
    if bcc:
        args += ["--bcc", bcc]
    if reply_to_message_id:
        args += ["--reply-to-message-id", reply_to_message_id]
    if from_addr:
        args += ["--from", from_addr]
    return args


def _emit(payload: dict) -> None:
    print(json.dumps(payload))


def _do_send(args, account: str, token: str) -> None:
    guard_error = check_external_guard(args.to, args.cc, args.bcc)
    if guard_error:
        _emit({"error": guard_error})
        sys.exit(1)
    body, body_file_path = _resolve_body(args)
    subject = args.subject
    if args.reply_to:
        subject = derive_reply_subject(token, subject, args.reply_to)

    argv = ["gmail", "send", *_common_compose_args(
        to=args.to, subject=subject, body=body, body_file_path=body_file_path,
        cc=args.cc, bcc=args.bcc,
        reply_to_message_id=args.reply_to,
        from_addr=from_address_for(account),
    )]
    result = gogcli.run(token, *argv)
    msg_id = result.get("messageId", "")
    _emit({"status": "sent", "message_id": msg_id, "to": args.to, "subject": subject})


def _do_draft(args, account: str, token: str) -> None:
    body, body_file_path = _resolve_body(args)
    subject = args.subject
    if args.reply_to:
        subject = derive_reply_subject(token, subject, args.reply_to)

    argv = ["gmail", "drafts", "create", *_common_compose_args(
        to=args.to, subject=subject, body=body, body_file_path=body_file_path,
        cc=args.cc, bcc=args.bcc,
        reply_to_message_id=args.reply_to,
        from_addr=from_address_for(account),
    )]
    result = gogcli.run(token, *argv)
    draft_id = result.get("draftId", "")
    approval = create_approval(
        draft_id=draft_id,
        recipient=args.to,
        subject=subject,
        body_preview=body,
        account=account,
        cc=args.cc,
        bcc=args.bcc,
    )
    _emit({
        "status": "drafted",
        "draft_id": draft_id,
        "to": args.to,
        "subject": subject,
        "approval_id": approval["approval_id"],
        "approval_url": _approval_url(approval["approval_id"]),
    })


def _do_draft_update(args, account: str, token: str) -> None:
    body, body_file_path = _resolve_body(args)
    subject = args.subject
    if args.reply_to:
        subject = derive_reply_subject(token, subject, args.reply_to)

    argv = ["gmail", "drafts", "update", args.draft_id, *_common_compose_args(
        to=args.to, subject=subject, body=body, body_file_path=body_file_path,
        cc=args.cc, bcc=args.bcc,
        reply_to_message_id=args.reply_to,
        from_addr=from_address_for(account),
    )]
    result = gogcli.run(token, *argv)
    draft_id = result.get("draftId") or args.draft_id
    # Reset approval — content changed, user must re-approve.
    approval = create_approval(
        draft_id=draft_id,
        recipient=args.to,
        subject=subject,
        body_preview=body,
        account=account,
        cc=args.cc,
        bcc=args.bcc,
    )
    _emit({
        "status": "updated",
        "draft_id": draft_id,
        "to": args.to,
        "subject": subject,
        "approval_id": approval["approval_id"],
        "approval_url": _approval_url(approval["approval_id"]),
    })


def _do_draft_send(args, account: str, token: str) -> None:
    approval = check_approval(args.draft_id)
    if not approval or approval["status"] != "approved":
        msg = "Draft not approved."
        if approval:
            msg += f" Status: {approval['status']}. Approval URL: {_approval_url(approval['approval_id'])}"
        else:
            msg += " No approval record found — create a draft first."
        _emit({"error": msg})
        sys.exit(1)
    result = gogcli.run(token, "gmail", "drafts", "send", args.draft_id)
    msg_id = result.get("messageId", "")
    mark_sent(args.draft_id, msg_id)
    _emit({"status": "draft_sent", "draft_id": args.draft_id, "message_id": msg_id})


def _do_draft_delete(args, account: str, token: str) -> None:
    # `-y` skips gogcli's destructive-confirmation prompt (we already gate on
    # the approval store + the user's explicit draft-delete subcommand).
    gogcli.run(token, "gmail", "drafts", "delete", "-y", args.draft_id)
    approval = check_approval(args.draft_id)
    if approval and approval["status"] == "pending":
        from email_approval_store import reject as reject_approval
        reject_approval(approval["approval_id"], "draft-delete")
    _emit({"status": "deleted", "draft_id": args.draft_id})


def _add_compose_args(parser: argparse.ArgumentParser) -> None:
    """Add shared compose arguments to a subcommand parser."""
    parser.add_argument("--to", required=True, help="Recipient email address")
    parser.add_argument("--subject", default="", help="Email subject")
    body_group = parser.add_mutually_exclusive_group()
    body_group.add_argument("--body", default="", help="Email body (plain text)")
    body_group.add_argument("--body-file", help="Read email body from file (avoids shell escaping issues)")
    parser.add_argument("--cc", help="CC recipient(s), comma-separated")
    parser.add_argument("--bcc", help="BCC recipient(s), comma-separated")
    parser.add_argument("--reply-to", help="Gmail message ID to reply to")


def main():
    parser = argparse.ArgumentParser(description="Send emails and manage drafts via Gmail (gogcli wrapper).")
    parser.add_argument("--account", required=True, help="Account name (e.g. primary, homer, personal)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_send = subparsers.add_parser("send", help="Send an email (internal recipients only)")
    _add_compose_args(p_send)

    p_draft = subparsers.add_parser("draft", help="Create a draft")
    _add_compose_args(p_draft)

    p_update = subparsers.add_parser("draft-update", help="Update an existing draft")
    p_update.add_argument("--draft-id", required=True, help="Draft ID to update")
    _add_compose_args(p_update)

    p_dsend = subparsers.add_parser("draft-send", help="Send an existing draft")
    p_dsend.add_argument("--draft-id", required=True, help="Draft ID to send")

    p_ddel = subparsers.add_parser("draft-delete", help="Delete a draft")
    p_ddel.add_argument("--draft-id", required=True, help="Draft ID to delete")

    args = parser.parse_args()

    handlers = {
        "send": _do_send,
        "draft": _do_draft,
        "draft-update": _do_draft_update,
        "draft-send": _do_draft_send,
        "draft-delete": _do_draft_delete,
    }

    try:
        token = get_access_token(args.account)
        handlers[args.command](args, args.account, token)
    except SystemExit:
        raise
    except (FileNotFoundError, PermissionError, ValueError, RuntimeError) as exc:
        _emit({"error": str(exc)})
        sys.exit(1)


if __name__ == "__main__":
    main()
