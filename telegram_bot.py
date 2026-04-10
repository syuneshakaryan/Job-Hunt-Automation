"""
telegram_bot.py
===============
NODE 5a of the Job Hunter pipeline — Notification & Control Layer.

Responsibilities:
  1. Send a job match alert to your personal Telegram chat:
       • Formatted message with company, score, emails, tech stack
       • Attaches the tailored PDF resume
       • Inline keyboard: [ 🚀 Auto-Apply ] [ ⏭ Skip ] [ 📋 View JD ]

  2. Run a persistent bot that handles button callbacks:
       • 🚀 Auto-Apply → triggers ats_apply.py for the given job URL
       • ⏭  Skip       → marks job as 'skipped' in DB, no action
       • 📋 View JD    → sends the raw job description as a follow-up message

  3. Send a daily digest summary (pipeline stats).

Design notes:
  - Uses python-telegram-bot v21 (async, PTB-style).
  - Callback data is a compact JSON string: {"action":"apply","job_id":42}
  - The bot listens for callbacks in a background thread so the Prefect
    flow can fire-and-forget notifications while the bot handles responses.
  - State is in SQLite (database.py) — no in-memory state needed.
"""

import asyncio
import json
import logging
from pathlib import Path

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from config import settings
from database import (
    fetch_jobs_for_review,
    get_pipeline_stats,
    update_job,
)

logger = logging.getLogger("telegram_bot")


# ─────────────────────────────────────────────────────────────────────────────
# Callback data helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_callback(action: str, job_id: int, job_url: str = "") -> str:
    """
    Pack callback data into a compact JSON string.
    Telegram limits callback_data to 64 bytes — keep it tight.
    """
    data = {"a": action, "id": job_id}
    if action == "apply" and job_url:
        # Truncate URL to stay within 64-byte limit
        data["u"] = job_url[:30]
    return json.dumps(data, separators=(",", ":"))


def _parse_callback(raw: str) -> tuple[str, int, str]:
    """Returns (action, job_id, job_url)."""
    try:
        d = json.loads(raw)
        return d.get("a", ""), int(d.get("id", 0)), d.get("u", "")
    except Exception:
        return "", 0, ""


# ─────────────────────────────────────────────────────────────────────────────
# Message formatter
# ─────────────────────────────────────────────────────────────────────────────

def _format_match_message(
    job_id:      int,
    company:     str,
    job_title:   str,
    job_url:     str,
    fit_score:   int,
    tech_stack:  list[str],
    emails:      list[str],
    strengths:   list[str],
    gaps:        list[str],
    is_remote:   bool | None,
    seniority:   str,
) -> str:
    """
    Build the Telegram MarkdownV2 alert message for a job match.
    Every dynamic string is passed through _escape() before insertion.
    Special tokens like backticks and asterisks used for formatting are
    written literally — only dynamic content is escaped.
    """
    score_bar = _score_emoji(fit_score)

    # Build remote tag — escape the whole string including the dash
    if is_remote is True:
        remote_tag = _escape(" 🌐 Remote")
    elif is_remote is False:
        remote_tag = _escape(" 🏢 On-site")
    else:
        remote_tag = ""

    # Tech stack in backticks — backticks are format chars, not escaped;
    # the tech name inside is escaped
    stack_items = [f"`{_escape(t)}`" for t in tech_stack[:6]]
    stack_str   = ", ".join(stack_items) if stack_items else "_unknown_"

    # Emails in backticks — same rule
    email_items = [f"  📧 `{_escape(e)}`" for e in emails[:3]]
    emails_str  = "\n".join(email_items) if email_items else "  _none found_"

    strengths_str = "\n".join(f"  ✅ {_escape(s)}" for s in strengths[:3])
    gaps_str      = "\n".join(f"  ⚠️ {_escape(g)}" for g in gaps[:2])

    # job_id contains digits only — safe, but escape() handles it anyway
    lines = [
        f"🔥 *New Job Match \\— Job \\#{job_id}*",
        "",
        f"🏢 *Company:*  {_escape(company)}",
        f"💼 *Role:*     {_escape(job_title)}{remote_tag}",
        f"📊 *Fit Score:* {score_bar} *{fit_score}/100*",
        f"🎯 *Seniority:* {_escape(seniority.title())}",
        "",
        "*🛠 Tech Stack:*",
        f"  {stack_str}",
        "",
        "*📬 Extracted Emails:*",
        emails_str,
    ]

    if strengths_str:
        lines += ["", "*💪 Why You Match:*", strengths_str]

    if gaps_str:
        lines += ["", "*🔸 Watch Out For:*", gaps_str]

    # URL goes inside a Markdown link — the URL itself must NOT be escaped,
    # only the display text. job_url is used raw inside the parentheses.
    lines += [
        "",
        f"🔗 [View Job Posting]({job_url})",
        "",
        "👇 *Choose an action:*",
    ]

    return "\n".join(lines)


def _score_emoji(score: int) -> str:
    if score >= 90: return "🟢🟢🟢🟢🟢"
    if score >= 80: return "🟢🟢🟢🟢⚪"
    if score >= 70: return "🟢🟢🟢⚪⚪"
    if score >= 60: return "🟡🟡🟡⚪⚪"
    return              "🔴🔴⚪⚪⚪"


def _escape(text: str) -> str:
    """
    Escape ALL special characters for Telegram MarkdownV2.
    Full list per Telegram docs: _ * [ ] ( ) ~ ` > # + - = | { } . !
    Must be applied to EVERY dynamic string inserted into a MarkdownV2 message.
    """
    special = r"_*[]()~`>#+-=|{}.!\\"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


# ─────────────────────────────────────────────────────────────────────────────
# Core send functions
# ─────────────────────────────────────────────────────────────────────────────

async def send_job_alert(
    job_id:      int,
    company:     str,
    job_title:   str,
    job_url:     str,
    fit_score:   int,
    tech_stack:  list[str],
    emails:      list[str],
    resume_path: Path,
    strengths:   list[str] = None,
    gaps:        list[str] = None,
    is_remote:   bool | None = None,
    seniority:   str = "unknown",
    description: str = "",
) -> bool:
    """
    Send a job match alert to your Telegram chat with:
      - Formatted message (score, stack, emails, fit analysis)
      - Attached tailored PDF resume
      - Inline keyboard: Auto-Apply | Skip | View JD

    Returns True on success, False on error.
    """
    bot = Bot(token=settings.telegram_bot_token)

    message = _format_match_message(
        job_id=job_id,
        company=company,
        job_title=job_title,
        job_url=job_url,
        fit_score=fit_score,
        tech_stack=tech_stack,
        emails=emails,
        strengths=strengths or [],
        gaps=gaps or [],
        is_remote=is_remote,
        seniority=seniority,
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🚀 Auto-Apply",
                callback_data=_make_callback("apply", job_id, job_url),
            ),
            InlineKeyboardButton(
                "⏭ Skip",
                callback_data=_make_callback("skip", job_id),
            ),
        ],
        [
            InlineKeyboardButton(
                "📋 View Full JD",
                callback_data=_make_callback("jd", job_id),
            ),
            InlineKeyboardButton(
                "🔗 Open in Browser",
                url=job_url,
            ),
        ],
    ])

    try:
        # ── Send the tailored PDF resume ──────────────────────────────────────
        # Convert to absolute Path so it works regardless of cwd on Windows
        pdf_path = Path(resume_path).resolve() if resume_path else None

        if pdf_path and pdf_path.exists():
            with open(pdf_path, "rb") as pdf_file:
                await bot.send_document(
                    chat_id=settings.telegram_chat_id,
                    document=InputFile(pdf_file, filename=pdf_path.name),
                    caption=f"📄 Tailored CV — {company} · {job_title}",
                )
            logger.info(f"CV sent: {pdf_path.name}")
        else:
            logger.warning(
                f"CV not attached — path missing or not found: {resume_path!r}"
            )

        # ── Send the alert card with inline keyboard ──────────────────────────
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=message,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

        logger.info(f"Job alert sent: job_id={job_id} company={company!r} score={fit_score}")
        return True

    except TelegramError as exc:
        logger.error(f"Telegram send_job_alert failed: {exc}")
        return False


async def send_daily_digest() -> bool:
    """
    Send a daily pipeline summary to your Telegram chat.
    Called by the Prefect scheduler at end of each daily run.
    """
    bot = Bot(token=settings.telegram_bot_token)
    stats = get_pipeline_stats()

    text = (
        "📊 *Daily Pipeline Digest*\n\n"
        f"🏢 Companies scanned:  `{stats['scraped']} / {stats['total_companies']}`\n"
        f"💼 Total jobs found:   `{stats['total_jobs']}`\n"
        f"🎯 High\\-score jobs:   `{stats['high_score_jobs']}` \\(≥{settings.fit_score_threshold}\\)\n"
        f"🚀 Applications sent:  `{stats['applied']}`\n"
        f"⏳ Pending your review: `{stats['pending_review']}`\n"
    )

    try:
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        logger.info("Daily digest sent")
        return True
    except TelegramError as exc:
        logger.error(f"Daily digest failed: {exc}")
        return False


async def send_apply_result(
    job_id:  int,
    company: str,
    success: bool,
    message: str = "",
) -> None:
    """Notify the result of an auto-apply attempt."""
    bot  = Bot(token=settings.telegram_bot_token)
    icon = "✅" if success else "❌"
    status = "Application submitted\\!" if success else f"Apply failed: {_escape(message)}"

    text = (
        f"{icon} *Job \\#{job_id} — {_escape(company)}*\n\n"
        f"{status}"
    )
    try:
        await bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except TelegramError as exc:
        logger.warning(f"send_apply_result failed: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Callback query handlers (bot listener)
# ─────────────────────────────────────────────────────────────────────────────

async def _handle_apply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    job_id: int,
    job_url: str,
) -> None:
    """
    User pressed 🚀 Auto-Apply.
    Marks job as 'approved' in DB, then triggers ats_apply.
    """
    query = update.callback_query

    # Acknowledge the button press immediately
    await query.answer("🚀 Launching auto-apply...")
    await query.edit_message_text(
        text=f"⏳ *Applying to Job \\#{job_id}\\.\\.\\.*\n\nPlease wait\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # Mark as approved in DB
    update_job(job_id, applied_status="applying")

    # Trigger the ATS apply bot
    try:
        from ats_apply import apply_to_job
        success, detail = await apply_to_job(job_id=job_id, job_url=job_url)
    except Exception as exc:
        success = False
        detail  = str(exc)
        logger.error(f"ats_apply raised exception for job_id={job_id}: {exc}")

    if success:
        from datetime import datetime, timezone
        update_job(
            job_id,
            applied_status="applied",
            applied_at=datetime.now(timezone.utc).isoformat(),
        )
        await query.edit_message_text(
            text=f"✅ *Applied to Job \\#{job_id}\\!*\n\n{_escape(detail)}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        logger.info(f"Auto-apply succeeded: job_id={job_id}")
    else:
        update_job(job_id, applied_status="failed")

        # FIX: Try the company contact form as a fallback
        contact_result = ""
        try:
            from ats_apply import try_contact_form
            from database import get_connection as _gc
            # Look up company domain for this job
            with _gc() as _conn:
                _row = _conn.execute(
                    """SELECT c.domain FROM jobs j
                       JOIN companies c ON c.id = j.company_id
                       WHERE j.job_id = ?""",
                    (job_id,),
                ).fetchone()
            if _row and _row["domain"]:
                contact_ok, contact_detail = await try_contact_form(
                    company_domain=_row["domain"],
                    job_id=job_id,
                    headless=True,
                )
                if contact_ok:
                    contact_result = f"\n\n✉️ *Contact form sent instead\\!* {_escape(contact_detail)}"
                else:
                    contact_result = f"\n\n⚠️ Contact form also failed: {_escape(contact_detail)}"
        except Exception as contact_exc:
            logger.warning(f"Contact form fallback failed: {contact_exc}")
            contact_result = ""

        await query.edit_message_text(
            text=(
                f"❌ *Auto\\-apply failed for Job \\#{job_id}*\n\n"
                f"Reason: {_escape(detail)}\n\n"
                f"Apply manually: [Open Job]({_escape(job_url)})"
                f"{contact_result}"
            ),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        logger.warning(f"Auto-apply failed: job_id={job_id} detail={detail!r}")


async def _handle_skip(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    job_id: int,
) -> None:
    """User pressed ⏭ Skip."""
    query = update.callback_query
    update_job(job_id, applied_status="skipped")
    await query.answer("⏭ Skipped.")
    await query.edit_message_text(
        text=f"⏭ *Job \\#{job_id} skipped\\.*",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    logger.info(f"Job skipped: job_id={job_id}")


async def _handle_view_jd(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    job_id: int,
) -> None:
    """User pressed 📋 View Full JD — send description as a follow-up message."""
    query = update.callback_query
    await query.answer("📋 Fetching JD...")

    from database import get_connection
    with get_connection() as conn:
        row = conn.execute(
            "SELECT title, description, url FROM jobs WHERE job_id = ?",
            (job_id,)
        ).fetchone()

    if not row or not row["description"]:
        await query.answer("No description stored for this job.", show_alert=True)
        return

    desc_preview = row["description"][:3000]
    text = (
        f"📋 *{_escape(row['title'])}*\n\n"
        f"{_escape(desc_preview)}"
        + ("\n\n_\\[truncated\\]_" if len(row["description"]) > 3000 else "")
    )
    bot = context.bot
    await bot.send_message(
        chat_id=settings.telegram_chat_id,
        text=text,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )


async def callback_router(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Route all inline keyboard callbacks to the correct handler."""
    query = update.callback_query
    if not query or not query.data:
        return

    action, job_id, job_url = _parse_callback(query.data)
    logger.info(f"Callback received: action={action!r} job_id={job_id}")

    if action == "apply":
        # Need full URL from DB since callback truncated it
        if not job_url or len(job_url) < 10:
            from database import get_connection
            with get_connection() as conn:
                row = conn.execute(
                    "SELECT url FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            job_url = row["url"] if row else ""

        await _handle_apply(update, context, job_id, job_url)

    elif action == "skip":
        await _handle_skip(update, context, job_id)

    elif action == "jd":
        await _handle_view_jd(update, context, job_id)

    else:
        await query.answer(f"Unknown action: {action}", show_alert=True)


# ─────────────────────────────────────────────────────────────────────────────
# /status and /pending commands
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — show pipeline stats."""
    stats = get_pipeline_stats()
    text = (
        "📊 *Pipeline Status*\n\n"
        f"🏢 Companies: `{stats['scraped']}/{stats['total_companies']}` scraped\n"
        f"💼 Jobs found: `{stats['total_jobs']}`\n"
        f"🎯 High\\-score: `{stats['high_score_jobs']}`\n"
        f"🚀 Applied: `{stats['applied']}`\n"
        f"⏳ Pending: `{stats['pending_review']}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pending — send full job cards for every un-actioned high-score job."""
    jobs = fetch_jobs_for_review()

    if not jobs:
        await update.message.reply_text(
            "✅ No pending jobs — inbox is clear\\!",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    total = len(jobs)
    # Cap at 10 cards to avoid Telegram flood limits; show a header first
    shown = jobs[:10]

    await update.message.reply_text(
        f"📋 *{total} pending job{'s' if total != 1 else ''}* — sending cards now\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    for i, job in enumerate(shown, 1):
        job     = dict(job)
        job_id  = job["job_id"]
        score   = job.get("fit_score", 0)
        title   = job.get("title", "Unknown Role")
        domain  = job.get("domain", "")
        url     = job.get("url", "")
        emails  = json.loads(job.get("extracted_emails_json") or "[]")
        seniority = job.get("seniority", "") or "unknown"
        is_remote = job.get("is_remote_friendly")

        # Reconstruct tech stack from stored JSON
        raw_stack = job.get("tech_stack") or "[]"
        try:
            tech_stack = json.loads(raw_stack) if isinstance(raw_stack, str) else raw_stack
        except Exception:
            tech_stack = []

        # Build a compact but informative card
        remote_tag = ""
        if is_remote is True:   remote_tag = " 🌐 Remote"
        elif is_remote is False: remote_tag = " 🏢 On\\-site"

        stack_str  = ", ".join(f"`{t}`" for t in tech_stack[:5]) or "_unknown_"
        emails_str = "\n".join(f"  📧 `{e}`" for e in emails[:3]) or "  _none found_"
        score_bar  = _score_emoji(score)

        card = (
            f"{'─'*30}\n"
            f"*\\[{i}/{total}\\] Job \\#{job_id}*\n\n"
            f"🏢 *{_escape(domain)}*\n"
            f"💼 {_escape(title)}{_escape(remote_tag) if remote_tag else ''}\n"
            f"📊 {score_bar} *{score}/100*  ·  {_escape(seniority.title())}\n\n"
            f"🛠 {stack_str}\n\n"
            f"📬 Emails:\n{emails_str}\n\n"
            f"🔗 [View Posting]({url})"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🚀 Auto-Apply",
                    callback_data=_make_callback("apply", job_id, url),
                ),
                InlineKeyboardButton(
                    "⏭ Skip",
                    callback_data=_make_callback("skip", job_id),
                ),
            ],
            [
                InlineKeyboardButton(
                    "📋 View Full JD",
                    callback_data=_make_callback("jd", job_id),
                ),
                InlineKeyboardButton(
                    "🔗 Open in Browser",
                    url=url,
                ),
            ],
        ])

        await update.message.reply_text(
            text=card,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )

    if total > 10:
        await update.message.reply_text(
            f"_Showing 10 of {total} — run /pending again after actioning these\\._",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bot application factory & runner
# ─────────────────────────────────────────────────────────────────────────────

def build_application() -> Application:
    """Build and wire the telegram bot Application."""
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CallbackQueryHandler(callback_router))
    return app


def run_bot() -> None:
    """
    Start the bot in polling mode (blocking).
    Run this in a separate terminal alongside your Prefect server:
        python telegram_bot.py
    """
    logger.info("Starting Telegram bot (polling)...")
    app = build_application()
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Convenience wrapper — sync send for use from Prefect tasks
# ─────────────────────────────────────────────────────────────────────────────

def notify_job_match(
    job_id:      int,
    company:     str,
    job_title:   str,
    job_url:     str,
    fit_score:   int,
    tech_stack:  list[str],
    emails:      list[str],
    resume_path: Path,
    strengths:   list[str] = None,
    gaps:        list[str] = None,
    is_remote:   bool | None = None,
    seniority:   str = "unknown",
) -> bool:
    """
    Synchronous wrapper around send_job_alert.
    Always spawns a fresh thread with its own event loop so it works
    correctly inside Prefect tasks (which already have a running loop).
    """
    if settings.telegram_bot_token == "CHANGE_ME":
        logger.warning("Telegram token not set — skipping notification")
        return False

    import concurrent.futures

    def _run() -> bool:
        # Each thread gets a completely fresh event loop — no conflicts
        return asyncio.run(
            send_job_alert(
                job_id=job_id,
                company=company,
                job_title=job_title,
                job_url=job_url,
                fit_score=fit_score,
                tech_stack=tech_stack,
                emails=emails,
                resume_path=resume_path,
                strengths=strengths,
                gaps=gaps,
                is_remote=is_remote,
                seniority=seniority,
            )
        )

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run)
            return future.result(timeout=45)
    except Exception as exc:
        logger.error(f"notify_job_match failed: {exc}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if "--bot" in sys.argv:
        # python telegram_bot.py --bot
        # Runs the full interactive bot in polling mode
        run_bot()
    else:
        # Quick unit tests (no Telegram token needed)
        print("=" * 60)
        print("TELEGRAM BOT — Unit Tests (no token required)")
        print("=" * 60)

        # Test 1: callback data encode/decode round-trip
        print("\n[1] Callback data round-trip")
        for action, job_id, url in [
            ("apply", 42, "https://example.com/jobs/python-dev"),
            ("skip",  99, ""),
            ("jd",    7,  ""),
        ]:
            raw = _make_callback(action, job_id, url)
            assert len(raw.encode()) <= 64, f"Callback too long: {len(raw.encode())} bytes"
            a, i, u = _parse_callback(raw)
            assert a == action and i == job_id
            print(f"  ✅  {action:6s} job_id={job_id}  encoded={raw!r}  ({len(raw.encode())}B)")

        # Test 2: score emoji
        print("\n[2] Score emoji bar")
        for score in [95, 82, 75, 65, 40]:
            bar = _score_emoji(score)
            print(f"  {score}/100  {bar}")
        print("  ✅  Score bar OK")

        # Test 3: MarkdownV2 escaping
        print("\n[3] MarkdownV2 escaping")
        raw_text = "Acme Corp. (Inc.) — 3+ years req."
        escaped  = _escape(raw_text)
        assert "\\." in escaped and "\\(" in escaped and "\\+" in escaped
        print(f"  Input:  {raw_text!r}")
        print(f"  Output: {escaped!r}")
        print("  ✅  Escaping OK")

        # Test 4: Message format
        print("\n[4] Message formatting")
        msg = _format_match_message(
            job_id=1,
            company="Acme Corp",
            job_title="Backend Python Developer",
            job_url="https://acme.com/jobs/backend",
            fit_score=87,
            tech_stack=["Python", "FastAPI", "PostgreSQL"],
            emails=["hr@acme.com", "cto@acme.com"],
            strengths=["Strong Python match", "Startup experience valued"],
            gaps=["May need Docker proficiency"],
            is_remote=True,
            seniority="mid",
        )
        assert "Acme Corp" in msg
        assert "87" in msg
        assert "hr" in msg
        print(f"  ✅  Message formatted ({len(msg)} chars)")

        # Test 5: Token check
        print("\n[5] Token configuration check")
        if settings.telegram_bot_token == "CHANGE_ME":
            print("  ⚠️   TELEGRAM_BOT_TOKEN not set in .env")
            print("       1. Message @BotFather on Telegram → /newbot")
            print("       2. Copy the token to .env → TELEGRAM_BOT_TOKEN=...")
            print("       3. Message @userinfobot → copy your ID to TELEGRAM_CHAT_ID=...")
            print("       4. Run:  python telegram_bot.py --bot")
        else:
            print(f"  ✅  Token configured ({settings.telegram_bot_token[:8]}...)")

        print("\n" + "=" * 60)
        print("All unit tests passed ✅")
        print("=" * 60)
        print("\nTo start the bot:  python telegram_bot.py --bot")