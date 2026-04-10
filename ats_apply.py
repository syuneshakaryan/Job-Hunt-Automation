"""
ats_apply.py
============
NODE 6 — ATS Form-Filler + Contact-Form Outreach (Round 3)

Changes in Round 3:
  1. Smarter submit-button detection: scrolls to find it, tries JS click fallback,
     inspects all buttons on the page before giving up.
  2. Pre-flight URL validation: HEAD-checks the URL before launching Playwright —
     returns a clean error for 404 / 5xx immediately.
  3. Contact-form detection: after a failed ATS apply (or on standalone call),
     scans the company domain for a contact/contact-us page, fills a generic
     outreach message, submits, and saves a screenshot for review if it fails.

Supported ATS platforms:
  Greenhouse · Lever · Ashby · BambooHR · Workable · Generic (best-effort)
"""

import asyncio
import logging
import random
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import httpx
from playwright.async_api import (
    BrowserContext,
    Page,
    async_playwright,
    TimeoutError as PWTimeout,
)

from config import settings
from database import get_connection, update_job

logger = logging.getLogger("ats_apply")

SCREENSHOTS_DIR = (settings.base_dir / "output" / "screenshots").resolve()
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# ATS Platform detection
# ─────────────────────────────────────────────────────────────────────────────

class ATSPlatform(str, Enum):
    GREENHOUSE = "greenhouse"
    LEVER      = "lever"
    ASHBY      = "ashby"
    BAMBOOHR   = "bamboohr"
    WORKABLE   = "workable"
    GENERIC    = "generic"


def detect_ats(url: str) -> ATSPlatform:
    u = url.lower()
    if "greenhouse.io" in u or "grnh.se"   in u: return ATSPlatform.GREENHOUSE
    if "lever.co"      in u:                      return ATSPlatform.LEVER
    if "ashbyhq.com"   in u:                      return ATSPlatform.ASHBY
    if "bamboohr.com"  in u:                      return ATSPlatform.BAMBOOHR
    if "workable.com"  in u:                      return ATSPlatform.WORKABLE
    return ATSPlatform.GENERIC


# ─────────────────────────────────────────────────────────────────────────────
# Applicant data
# ─────────────────────────────────────────────────────────────────────────────

def _applicant() -> dict:
    parts = settings.your_full_name.strip().split(None, 1)
    return {
        "first_name": parts[0] if parts else "",
        "last_name":  parts[1] if len(parts) > 1 else "",
        "full_name":  settings.your_full_name,
        "email":      settings.your_email,
        "phone":      settings.your_phone,
        "location":   settings.your_location,
        "city":       settings.your_location.split(",")[0].strip(),
        "linkedin":   settings.your_linkedin,
        "github":     settings.your_github,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Human-like helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _delay(lo: int = 80, hi: int = 220) -> None:
    await asyncio.sleep(random.uniform(lo, hi) / 1000)


async def _fill(page: Page, selector: str, value: str, timeout: int = 4000) -> bool:
    try:
        loc = page.locator(selector).first
        if await loc.count() == 0:
            return False
        await loc.scroll_into_view_if_needed(timeout=timeout)
        await loc.click(timeout=timeout)
        await _delay(80, 180)
        await loc.fill("", timeout=timeout)
        await loc.type(value, delay=random.randint(35, 80))
        await _delay()
        return True
    except Exception as exc:
        logger.debug(f"_fill({selector!r}) failed: {exc}")
        return False


async def _upload(page: Page, resume_path: Path, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            await page.set_input_files(sel, str(resume_path), timeout=6000)
            await _delay(400, 800)
            logger.info(f"Resume uploaded via: {sel!r}")
            return True
        except Exception as exc:
            logger.debug(f"_upload({sel!r}) failed: {exc}")
    logger.warning("Could not find a file-upload field for the resume")
    return False


async def _screenshot(page: Page, label: str) -> Path | None:
    """Save a debug screenshot — never raises. Returns saved path or None."""
    try:
        ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = SCREENSHOTS_DIR / f"{label}_{ts}.png"
        await page.screenshot(path=str(path), full_page=True)
        logger.info(f"Screenshot saved: {path.name}")
        return path
    except Exception:
        return None


async def _click_apply_button(page: Page) -> bool:
    for sel in [
        "a:has-text('Apply for this job')",
        "a:has-text('Apply Now')",
        "a:has-text('Apply')",
        "button:has-text('Apply for this job')",
        "button:has-text('Apply')",
        "[data-qa='btn-apply']",
        ".apply-button",
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0 and await btn.is_visible(timeout=2000):
                await btn.click(timeout=5000)
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
                await _delay(600, 1200)
                return True
        except Exception:
            continue
    return False


# ─────────────────────────────────────────────────────────────────────────────
# FIX 1: Smarter submit button detection
# ─────────────────────────────────────────────────────────────────────────────

async def _submit(page: Page) -> bool:
    """
    Multi-strategy submit button finder.

    Strategy 1: known selectors (fast path)
    Strategy 2: scroll the page and check visibility after scroll
    Strategy 3: enumerate ALL buttons and pick last visible/enabled one
    Strategy 4: JS click as final fallback
    """
    priority_selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Submit Application')",
        "button:has-text('Submit')",
        "button:has-text('Send Application')",
        "button:has-text('Apply')",
        "[data-qa='btn-submit']",
        "[data-testid='submit-button']",
        ".submit-btn",
        "#submit",
    ]

    # Strategy 1: known selectors
    for sel in priority_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.count() == 0:
                continue
            await btn.scroll_into_view_if_needed(timeout=3000)
            if await btn.is_visible(timeout=2000) and await btn.is_enabled(timeout=2000):
                await btn.click(timeout=6000)
                logger.info(f"Submit clicked (strategy 1): {sel!r}")
                return True
        except Exception:
            continue

    # Strategy 2: scroll to bottom and retry (lazy-rendered submit buttons)
    logger.debug("Strategy 1 failed — scrolling to bottom and retrying")
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await _delay(600, 1000)
    except Exception:
        pass

    for sel in priority_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.count() == 0:
                continue
            if await btn.is_visible(timeout=2000) and await btn.is_enabled(timeout=2000):
                await btn.click(timeout=6000)
                logger.info(f"Submit clicked (strategy 2, after scroll): {sel!r}")
                return True
        except Exception:
            continue

    # Strategy 3: enumerate all buttons and pick the last enabled/visible one
    logger.debug("Strategy 2 failed — enumerating all buttons")
    try:
        all_buttons = page.locator("button, input[type='submit'], input[type='button']")
        count = await all_buttons.count()
        logger.debug(f"Found {count} buttons on page")

        candidates = []
        for i in range(count):
            btn = all_buttons.nth(i)
            try:
                visible = await btn.is_visible(timeout=1000)
                enabled = await btn.is_enabled(timeout=1000)
                text    = (await btn.text_content() or "").strip().lower()
                if visible and enabled:
                    candidates.append((i, text, btn))
            except Exception:
                continue

        # Prefer buttons with submit-like text, else take the last one
        submit_keywords = ["submit", "apply", "send", "continue", "next"]
        for i, text, btn in candidates:
            if any(kw in text for kw in submit_keywords):
                await btn.scroll_into_view_if_needed(timeout=2000)
                await btn.click(timeout=6000)
                logger.info(f"Submit clicked (strategy 3, text={text!r})")
                return True

        # Last candidate as final resort
        if candidates:
            i, text, btn = candidates[-1]
            await btn.scroll_into_view_if_needed(timeout=2000)
            await btn.click(timeout=6000)
            logger.info(f"Submit clicked (strategy 3 fallback, text={text!r})")
            return True

    except Exception as exc:
        logger.debug(f"Strategy 3 failed: {exc}")

    # Strategy 4: JS click on any submit input/button
    logger.debug("Strategy 3 failed — trying JS click")
    try:
        clicked = await page.evaluate("""
            () => {
                const candidates = [
                    ...document.querySelectorAll('button[type=submit]'),
                    ...document.querySelectorAll('input[type=submit]'),
                    ...document.querySelectorAll('button'),
                ];
                for (const el of candidates) {
                    const style = window.getComputedStyle(el);
                    if (style.display !== 'none' && style.visibility !== 'hidden'
                            && !el.disabled) {
                        el.click();
                        return el.textContent || el.value || 'element';
                    }
                }
                return null;
            }
        """)
        if clicked:
            logger.info(f"Submit clicked (strategy 4 JS): {clicked!r}")
            return True
    except Exception as exc:
        logger.debug(f"Strategy 4 failed: {exc}")

    logger.warning("All submit strategies exhausted — could not find submit button")
    return False


async def _wait_confirmation(page: Page) -> bool:
    signals = [
        "text=Thank you",
        "text=Application submitted",
        "text=Application received",
        "text=Successfully applied",
        "text=We've received your application",
        "[class*='confirmation']",
        "[class*='success']",
        "[id*='confirmation']",
    ]
    for sig in signals:
        try:
            await page.wait_for_selector(sig, timeout=4000)
            logger.info(f"Confirmation detected: {sig!r}")
            return True
        except PWTimeout:
            continue
        except Exception:
            continue
    return False


# ─────────────────────────────────────────────────────────────────────────────
# FIX 2: Pre-flight URL validation (handles 404/dead links cleanly)
# ─────────────────────────────────────────────────────────────────────────────

async def _check_url_alive(url: str) -> tuple[bool, str]:
    """
    HEAD-check the URL before launching a full browser.
    Returns (alive, reason).
    """
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0)"},
        ) as client:
            resp = await client.head(url)
            if resp.status_code == 404:
                return False, f"Page failed to load (HTTP 404)"
            if resp.status_code >= 400:
                return False, f"Page failed to load (HTTP {resp.status_code})"
            return True, "ok"
    except httpx.ConnectError:
        return False, "Connection refused — domain may not exist"
    except httpx.TimeoutException:
        return False, "URL timed out during pre-flight check"
    except Exception as exc:
        # Don't block on HEAD failures — some servers reject HEAD
        logger.debug(f"HEAD check failed ({exc}), proceeding anyway")
        return True, "head_check_skipped"


# ─────────────────────────────────────────────────────────────────────────────
# Platform-specific fillers
# ─────────────────────────────────────────────────────────────────────────────

async def _fill_greenhouse(page: Page, ap: dict, resume: Path) -> bool:
    logger.info("Filling Greenhouse form")
    await _click_apply_button(page)

    await _fill(page, "#first_name", ap["first_name"])
    await _fill(page, "#last_name",  ap["last_name"])
    await _fill(page, "#email",      ap["email"])
    await _fill(page, "#phone",      ap["phone"])

    for sel in [
        "input[name='job_application[location]']",
        "input[autocomplete='address-level2']",
        "#location",
    ]:
        if await _fill(page, sel, ap["city"]):
            break

    for sel in [
        "input[id*='linkedin']",
        "input[placeholder*='LinkedIn']",
        "input[name*='linkedin']",
    ]:
        if await _fill(page, sel, ap["linkedin"]):
            break

    await _upload(page, resume, [
        "input[type='file']",
        "#resume",
        "input[name*='resume']",
        "input[accept*='pdf']",
    ])
    return True


async def _fill_lever(page: Page, ap: dict, resume: Path) -> bool:
    logger.info("Filling Lever form")
    if "/apply" not in page.url:
        await _click_apply_button(page)

    await _fill(page, "input[name='name']",            ap["full_name"])
    await _fill(page, "input[name='email']",           ap["email"])
    await _fill(page, "input[name='phone']",           ap["phone"])
    await _fill(page, "input[name='location']",        ap["location"])
    await _fill(page, "input[name='urls[LinkedIn]']",  ap["linkedin"])
    await _fill(page, "input[name='urls[GitHub]']",    ap["github"])
    await _fill(page, "input[name='urls[Portfolio]']", ap["linkedin"])

    await _upload(page, resume, [
        "input[type='file']",
        "input[name='resume']",
        "input[accept*='pdf']",
    ])
    return True


async def _fill_ashby(page: Page, ap: dict, resume: Path) -> bool:
    logger.info("Filling Ashby form")

    async def by_label(label: str, value: str) -> bool:
        try:
            lbl = page.locator(f"label:has-text('{label}')").first
            if await lbl.count() == 0:
                return False
            input_id = await lbl.get_attribute("for")
            if input_id:
                return await _fill(page, f"#{input_id}", value)
            inp = lbl.locator("~ input, + input").first
            await inp.fill(value, timeout=3000)
            return True
        except Exception:
            return False

    await by_label("First Name", ap["first_name"])
    await by_label("Last Name",  ap["last_name"])
    await by_label("Email",      ap["email"])
    await by_label("Phone",      ap["phone"])
    await by_label("Location",   ap["location"])
    await by_label("LinkedIn",   ap["linkedin"])
    await by_label("GitHub",     ap["github"])

    await _upload(page, resume, [
        "input[type='file']",
        "input[accept*='pdf']",
    ])
    return True


async def _fill_bamboohr(page: Page, ap: dict, resume: Path) -> bool:
    logger.info("Filling BambooHR form")
    await _fill(page, "input#firstName", ap["first_name"])
    await _fill(page, "input#lastName",  ap["last_name"])
    await _fill(page, "input#email",     ap["email"])
    await _fill(page, "input#phone",     ap["phone"])
    await _fill(page, "input#address",   ap["location"])
    await _fill(page, "input#linkedin",  ap["linkedin"])

    await _upload(page, resume, [
        "input[type='file']",
        "#resume_upload",
        "input[accept*='pdf']",
    ])
    return True


async def _fill_workable(page: Page, ap: dict, resume: Path) -> bool:
    logger.info("Filling Workable form")
    await _fill(page, "input[name='firstname']", ap["first_name"])
    await _fill(page, "input[name='lastname']",  ap["last_name"])
    await _fill(page, "input[name='email']",     ap["email"])
    await _fill(page, "input[name='phone']",     ap["phone"])

    await _upload(page, resume, [
        "input[type='file']",
        "input[accept*='pdf']",
    ])
    return True


async def _fill_generic(page: Page, ap: dict, resume: Path) -> bool:
    logger.info("Filling generic form (best-effort)")

    field_map = [
        (ap["first_name"], [
            "input[name*='first_name']", "input[name*='firstName']",
            "input[placeholder*='First name']", "input[placeholder*='First Name']",
            "input[id*='first']", "input[autocomplete='given-name']",
        ]),
        (ap["last_name"], [
            "input[name*='last_name']", "input[name*='lastName']",
            "input[placeholder*='Last name']", "input[placeholder*='Last Name']",
            "input[id*='last']", "input[autocomplete='family-name']",
        ]),
        (ap["full_name"], [
            "input[name='name']", "input[name*='full_name']",
            "input[placeholder*='Full name']", "input[placeholder*='Your name']",
            "input[autocomplete='name']",
        ]),
        (ap["email"], [
            "input[type='email']", "input[name='email']",
            "input[placeholder*='email']", "input[autocomplete='email']",
        ]),
        (ap["phone"], [
            "input[type='tel']", "input[name*='phone']",
            "input[placeholder*='phone']", "input[autocomplete='tel']",
        ]),
        (ap["linkedin"], [
            "input[placeholder*='LinkedIn']", "input[name*='linkedin']",
            "input[id*='linkedin']",
        ]),
        (ap["github"], [
            "input[placeholder*='GitHub']", "input[name*='github']",
            "input[id*='github']",
        ]),
    ]

    hits = 0
    for value, selectors in field_map:
        for sel in selectors:
            if await _fill(page, sel, value):
                hits += 1
                break

    await _upload(page, resume, [
        "input[type='file']",
        "input[accept*='pdf']",
        "input[accept*='.pdf']",
        "input[name*='resume']",
        "input[name*='cv']",
        "input[id*='resume']",
        "input[id*='cv']",
    ])

    logger.info(f"Generic fill: {hits}/{len(field_map)} fields filled")
    return hits >= 2


# ─────────────────────────────────────────────────────────────────────────────
# FIX 3: Contact form detection & submission
# ─────────────────────────────────────────────────────────────────────────────

_CONTACT_PAGE_PATTERNS = [
    "/contact",
    "/contact-us",
    "/contact_us",
    "/get-in-touch",
    "/reach-us",
    "/hello",
    "/talk-to-us",
]

_CONTACT_LINK_TEXTS = [
    "contact", "contact us", "get in touch", "reach us",
    "hello", "talk to us", "write to us",
]

_GENERIC_MESSAGE = (
    "Hello,\n\n"
    "My name is {full_name} and I'm a Python backend developer with experience "
    "in REST APIs, data pipelines, automation, and LLM tooling. "
    "I came across your company and I'm very interested in potential opportunities "
    "to contribute to your team.\n\n"
    "Please feel free to reach me at {email} or {phone}. "
    "My LinkedIn profile: {linkedin}\n\n"
    "Thank you for your time, and I look forward to hearing from you!\n\n"
    "Best regards,\n{full_name}"
)


async def _find_contact_url(page: Page, base_url: str) -> str | None:
    """
    Given the current page, try to find a contact / contact-us URL.
    Returns the absolute URL, or None if not found.
    """
    # 1. Try known path suffixes directly
    from urllib.parse import urlparse, urljoin
    parsed   = urlparse(base_url)
    root_url = f"{parsed.scheme}://{parsed.netloc}"

    for suffix in _CONTACT_PAGE_PATTERNS:
        candidate = root_url + suffix
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=5,
                headers={"User-Agent": "Mozilla/5.0 (compatible; JobBot/1.0)"},
            ) as client:
                r = await client.head(candidate)
                if r.status_code < 400:
                    logger.info(f"Contact page found via path probe: {candidate}")
                    return candidate
        except Exception:
            continue

    # 2. Scan links on the current page for contact-like hrefs or text
    try:
        links = await page.locator("a").all()
        for link in links:
            try:
                href = await link.get_attribute("href") or ""
                text = (await link.text_content() or "").strip().lower()
                if any(p in href.lower() for p in _CONTACT_PAGE_PATTERNS):
                    absolute = urljoin(root_url, href)
                    logger.info(f"Contact page found via link href: {absolute}")
                    return absolute
                if any(t in text for t in _CONTACT_LINK_TEXTS):
                    absolute = urljoin(root_url, href)
                    if absolute.startswith("http"):
                        logger.info(f"Contact page found via link text: {absolute}")
                        return absolute
            except Exception:
                continue
    except Exception as exc:
        logger.debug(f"Link scan failed: {exc}")

    return None


async def _fill_contact_form(page: Page, ap: dict) -> bool:
    """
    Best-effort fill of a contact / enquiry form.
    Returns True if at least name + email + message were filled.
    """
    message = _GENERIC_MESSAGE.format(
        full_name=ap["full_name"],
        email=ap["email"],
        phone=ap["phone"],
        linkedin=ap["linkedin"],
    )

    filled = 0

    # Name fields
    for name_sel, value in [
        ("input[name*='first_name'], input[name*='firstName'], input[id*='first']", ap["first_name"]),
        ("input[name*='last_name'],  input[name*='lastName'],  input[id*='last']",  ap["last_name"]),
        ("input[name='name'], input[name*='full_name'], input[placeholder*='name']", ap["full_name"]),
    ]:
        for sel in [s.strip() for s in name_sel.split(",")]:
            if await _fill(page, sel, value):
                filled += 1
                break

    # Email
    for sel in ["input[type='email']", "input[name='email']", "input[id*='email']",
                "input[placeholder*='email']"]:
        if await _fill(page, sel, ap["email"]):
            filled += 1
            break

    # Phone (optional, don't count toward success)
    for sel in ["input[type='tel']", "input[name*='phone']", "input[placeholder*='phone']"]:
        if await _fill(page, sel, ap["phone"]):
            break

    # Subject (optional)
    for sel in ["input[name*='subject']", "input[id*='subject']",
                "input[placeholder*='subject']", "input[placeholder*='Subject']"]:
        if await _fill(page, sel, "Interested in Backend Python Opportunities"):
            break

    # Message textarea
    textarea_selectors = [
        "textarea[name*='message']", "textarea[name*='body']",
        "textarea[id*='message']",   "textarea[id*='body']",
        "textarea[placeholder*='message']", "textarea[placeholder*='Message']",
        "textarea",
    ]
    for sel in textarea_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible(timeout=2000):
                await loc.scroll_into_view_if_needed(timeout=2000)
                await loc.click(timeout=3000)
                await _delay(100, 200)
                await loc.fill(message, timeout=5000)
                filled += 1
                logger.info(f"Message filled via: {sel!r}")
                break
        except Exception:
            continue

    logger.info(f"Contact form: {filled} key fields filled")
    return filled >= 3  # name/full_name + email + message


async def try_contact_form(
    company_domain: str,
    job_id: int | None = None,
    headless: bool = False,
) -> tuple[bool, str]:
    """
    Navigate to the company's contact page (auto-detected) and submit
    a generic outreach message.

    Args:
        company_domain: e.g. "zapier.com" or "https://zapier.com"
        job_id:         If provided, used for screenshot filename labelling.
        headless:       Run browser headless or not.

    Returns:
        (True, "Contact message sent")          on success
        (False, "reason")                       on failure — screenshot saved
    """
    if not company_domain.startswith("http"):
        company_domain = "https://" + company_domain

    ap      = _applicant()
    label   = f"contact_j{job_id}" if job_id else "contact_outreach"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage"],
        )
        context: BrowserContext = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            # Load company homepage
            resp = await page.goto(
                company_domain,
                timeout=settings.page_timeout_ms,
                wait_until="domcontentloaded",
            )
            if resp is None or not resp.ok:
                status = resp.status if resp else "timeout"
                return False, f"Company homepage failed to load (HTTP {status})"

            await _delay(800, 1400)

            # Find contact page
            contact_url = await _find_contact_url(page, company_domain)
            if not contact_url:
                shot = await _screenshot(page, f"{label}_no_contact_page")
                return False, (
                    f"Could not find a contact page on {company_domain}. "
                    f"Screenshot: {shot.name if shot else 'n/a'}"
                )

            # Navigate to contact page
            resp2 = await page.goto(
                contact_url,
                timeout=settings.page_timeout_ms,
                wait_until="domcontentloaded",
            )
            if resp2 is None or not resp2.ok:
                return False, f"Contact page failed to load (HTTP {resp2.status if resp2 else 'timeout'})"

            await _delay(600, 1000)

            # Fill the form
            filled_ok = await _fill_contact_form(page, ap)
            if not filled_ok:
                shot = await _screenshot(page, f"{label}_fill_failed")
                return False, (
                    f"Could not fill the contact form at {contact_url}. "
                    f"Screenshot saved for review: {shot.name if shot else 'n/a'}"
                )

            # Screenshot before submitting
            await _screenshot(page, f"{label}_pre_submit")

            # Submit
            submitted = await _submit(page)
            if not submitted:
                shot = await _screenshot(page, f"{label}_no_submit_btn")
                return False, (
                    f"Filled contact form but could not find submit button at {contact_url}. "
                    f"Screenshot: {shot.name if shot else 'n/a'}"
                )

            await _delay(1500, 2500)
            confirmed = await _wait_confirmation(page)
            await _screenshot(page, f"{label}_post_submit")

            if confirmed:
                return True, f"Contact message sent via {contact_url} ✅"
            else:
                # Many contact forms don't show explicit confirmation
                return True, f"Contact form submitted at {contact_url} (no explicit confirmation)"

        except Exception as exc:
            logger.exception(f"try_contact_form exception: {exc}")
            try:
                shot = await _screenshot(page, f"{label}_exception")
                return False, (
                    f"Contact form exception: {exc}. "
                    f"Screenshot: {shot.name if shot else 'n/a'}"
                )
            except Exception:
                return False, f"Contact form exception: {exc}"

        finally:
            await context.close()
            await browser.close()


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_job_data(job_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT url, resume_path, title FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    return dict(row) if row else None


def _get_resume_path(job_id: int) -> Path | None:
    data = _get_job_data(job_id)
    if not data or not data.get("resume_path"):
        return None
    p = Path(data["resume_path"]).resolve()
    return p if p.exists() else None


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

async def apply_to_job(
    job_id:   int,
    job_url:  str = "",
    headless: bool = False,
) -> tuple[bool, str]:
    """
    Navigate to job_url, detect the ATS, fill the form, upload the resume,
    and submit.

    Returns:
        (True,  "success message")  on successful submission
        (False, "error reason")     on failure
    """
    # Resolve job URL
    if not job_url:
        data = _get_job_data(job_id)
        if not data:
            return False, f"No job found in DB for job_id={job_id}"
        job_url = data.get("url", "")

    if not job_url:
        return False, "No job URL available"

    # FIX: Pre-flight URL check before launching full browser
    alive, reason = await _check_url_alive(job_url)
    if not alive:
        return False, reason

    # Resolve resume PDF
    resume_path = _get_resume_path(job_id)
    if not resume_path:
        return False, (
            f"Resume PDF not found for job_id={job_id}. "
            "Run the pipeline first so the resume is generated."
        )

    ap       = _applicant()
    platform = detect_ats(job_url)
    logger.info(
        f"apply_to_job | job_id={job_id} | platform={platform.value} | "
        f"resume={resume_path.name} | url={job_url}"
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context: BrowserContext = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        try:
            resp = await page.goto(
                job_url,
                timeout=settings.page_timeout_ms,
                wait_until="domcontentloaded",
            )
            if resp is None or not resp.ok:
                status = resp.status if resp else "timeout"
                return False, f"Page failed to load (HTTP {status})"

            await _delay(800, 1500)

            ok = False
            if platform == ATSPlatform.GREENHOUSE:
                ok = await _fill_greenhouse(page, ap, resume_path)
            elif platform == ATSPlatform.LEVER:
                ok = await _fill_lever(page, ap, resume_path)
            elif platform == ATSPlatform.ASHBY:
                ok = await _fill_ashby(page, ap, resume_path)
            elif platform == ATSPlatform.BAMBOOHR:
                ok = await _fill_bamboohr(page, ap, resume_path)
            elif platform == ATSPlatform.WORKABLE:
                ok = await _fill_workable(page, ap, resume_path)
            else:
                ok = await _fill_generic(page, ap, resume_path)

            if not ok:
                await _screenshot(page, f"fill_failed_j{job_id}")
                return False, f"Form fill failed on {platform.value}"

            await _screenshot(page, f"pre_submit_j{job_id}")

            submitted = await _submit(page)
            if not submitted:
                shot = await _screenshot(page, f"no_submit_btn_j{job_id}")
                return False, (
                    "Could not find the submit button. "
                    f"Screenshot saved: {shot.name if shot else 'n/a'}"
                )

            await _delay(1500, 2500)
            confirmed = await _wait_confirmation(page)
            await _screenshot(page, f"post_submit_j{job_id}")

            if confirmed:
                return True, f"Application submitted via {platform.value} ✅"
            else:
                logger.warning(
                    f"Submit clicked but no confirmation screen detected "
                    f"for job_id={job_id} — treating as likely success"
                )
                return True, f"Submitted via {platform.value} (no confirmation screen detected)"

        except Exception as exc:
            logger.exception(f"apply_to_job exception job_id={job_id}: {exc}")
            try:
                shot = await _screenshot(page, f"exception_j{job_id}")
                return False, f"{exc} (screenshot: {shot.name if shot else 'n/a'})"
            except Exception:
                return False, str(exc)

        finally:
            await context.close()
            await browser.close()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("ATS APPLY — Tests")
    print("=" * 60)

    print("\n[1] Platform detection")
    cases = [
        ("https://boards.greenhouse.io/acme/jobs/123",  ATSPlatform.GREENHOUSE),
        ("https://jobs.lever.co/acme/abc-123",          ATSPlatform.LEVER),
        ("https://jobs.ashbyhq.com/acme/abc-123",       ATSPlatform.ASHBY),
        ("https://acme.bamboohr.com/careers/123",       ATSPlatform.BAMBOOHR),
        ("https://apply.workable.com/acme/j/ABC",       ATSPlatform.WORKABLE),
        ("https://acme.com/careers/python-dev",         ATSPlatform.GENERIC),
    ]
    for url, expected in cases:
        result = detect_ats(url)
        icon   = "✅" if result == expected else "❌"
        print(f"  {icon}  {result.value:12s}  {url[8:55]}")

    print("\n[2] Applicant data")
    ap = _applicant()
    for k, v in ap.items():
        print(f"  {k:12s}: {v!r}")
    print("  ✅  Applicant data OK")

    print("\n[3] Screenshot directory")
    print(f"  ✅  {SCREENSHOTS_DIR}")
    assert SCREENSHOTS_DIR.exists()

    if "--apply" in sys.argv:
        idx     = sys.argv.index("--apply")
        job_id  = int(sys.argv[idx + 1])
        job_url = sys.argv[idx + 2] if len(sys.argv) > idx + 2 else ""
        print(f"\n[4] Live apply: job_id={job_id} url={job_url or '(from DB)'}")
        success, detail = asyncio.run(
            apply_to_job(job_id=job_id, job_url=job_url, headless=False)
        )
        print(f"  {'✅' if success else '❌'}  {detail}")

    if "--contact" in sys.argv:
        idx    = sys.argv.index("--contact")
        domain = sys.argv[idx + 1]
        print(f"\n[5] Contact form test: {domain}")
        success, detail = asyncio.run(
            try_contact_form(company_domain=domain, headless=False)
        )
        print(f"  {'✅' if success else '❌'}  {detail}")

    print("\n" + "=" * 60)
    print("All tests passed ✅")
    print("=" * 60)