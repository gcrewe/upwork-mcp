"""Profile and connects tools for Upwork MCP."""

import asyncio
import re
from ..browser.client import get_browser


async def get_my_profile() -> dict:
    """Get your Upwork freelancer profile information.

    Returns profile data including name, title, hourly rate, JSS score,
    availability status, and skill tags.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    # The old code scraped the *settings* page (h1 == "Settings"). The public
    # profile lives at an id-based URL (/freelancers/~<hash>) that redirects to
    # the username slug; locate that link from the settings left nav.
    await page.goto("https://www.upwork.com/freelancers/settings/profile", wait_until="domcontentloaded")
    await asyncio.sleep(2.5)
    profile_url = await page.evaluate("""
    (() => {
      const links = Array.from(document.querySelectorAll('a'));
      const l = links.find(a => /\\/freelancers\\/~[A-Za-z0-9]+$/.test(a.href));
      return l ? l.href : null;
    })()
    """)
    if not profile_url:
        profile_url = "https://www.upwork.com/freelancers/settings/profile"

    await page.goto(profile_url, wait_until="domcontentloaded")
    await asyncio.sleep(3)

    profile: dict = {}

    h1 = await page.query_selector("h1")
    if h1:
        profile["name"] = (await h1.text_content() or "").strip()

    text = await page.evaluate("document.body ? document.body.innerText : ''") or ""

    # Title + hourly rate typically render as "<Title>\n$<rate>/hr"
    m = re.search(r"([A-Z][^\n|]{2,60})\n\s*\$([\d.,]+)/hr", text)
    if m:
        profile["title"] = m.group(1).strip()
        profile["hourly_rate"] = f"${m.group(2)}/hr"
    else:
        doc_title = await page.title()
        m2 = re.match(r"^[^-]+\s*-\s*([^|]+?)\s*\|\s*Upwork", doc_title)
        if m2:
            profile["title"] = m2.group(1).strip()

    # Overview: the paragraph that follows the rate (until Portfolio/Work history)
    m = re.search(r"\$[\d.,]+/hr\n\n(.{20,700}?)\n\n(?:Portfolio|Work history|Skills|Certifications)", text, re.S)
    if m:
        profile["overview"] = re.sub(r"\n{2,}", "\n", m.group(1)).strip()

    # Skills: Upwork renders each tag inside [class*="skill"] elements
    skill_els = await page.query_selector_all('[class*="skill"]')
    seen: list[str] = []
    for el in skill_els:
        t = (await el.text_content() or "").strip()
        if not t or len(t) > 40:
            continue
        if re.fullmatch(r"(Skills|Self-reported)", t, re.I):
            continue
        if "self-reported" in t.lower() or "\n" in t or "skills" in t.lower():
            continue
        if t not in seen:
            seen.append(t)
    if seen:
        profile["skills"] = seen[:25]

    # Connects (also fetched from the hub as a fallback)
    m = re.search(r"Connects:\s*(\d+)", text)
    if m:
        profile["connects"] = {"available": int(m.group(1))}
    else:
        profile["connects"] = await get_connects_balance()

    # Location / availability / JSS (best effort)
    m = re.search(r"([A-Z][A-Za-z ,\-–]+?–\s*[\d:apm ]+local time)", text)
    if m:
        profile["location"] = m.group(1).strip()

    m = re.search(r"\b(Open for work|Available for work|Not available)\b", text, re.I)
    if m:
        profile["availability"] = m.group(1).strip()

    m = re.search(r"Job Success[^\n]{0,30}", text)
    if m:
        profile["job_success_score"] = m.group(0).strip()

    m = re.search(r"\b(English|Spanish):\s*([^\n]{1,40})", text)
    if m:
        profile["languages"] = [(m.group(1), m.group(2).strip())]

    return profile


async def get_connects_balance() -> dict:
    """Get current Upwork Connects balance and usage.

    Returns the number of available connects, pending connects,
    and connects balance details.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    # /nx/plans/connects redirects to the Connects Hub; balance is the number
    # next to the "Balance" label in the summary card.
    await page.goto("https://www.upwork.com/nx/plans/connects", wait_until="domcontentloaded")
    await asyncio.sleep(3)

    text = await page.evaluate("document.body ? document.body.innerText : ''") or ""
    connects: dict = {}

    m = re.search(r"Balance\s+([\d,]+)", text)
    if m:
        connects["available"] = int(m.group(1).replace(",", ""))

    m = re.search(r"Pending\s+([\d,]+)", text)
    if m:
        connects["pending"] = int(m.group(1).replace(",", ""))

    return connects


async def get_profile_stats() -> dict:
    """Get profile statistics including earnings and work history.

    Returns stats like total earnings, hours worked, jobs completed.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    await page.goto("https://www.upwork.com/nx/wm/contracts", wait_until="domcontentloaded")
    await asyncio.sleep(3.5)

    text = await page.evaluate("document.body ? document.body.innerText : ''") or ""
    stats: dict = {}

    m = re.search(r"(?:Total billings|Earnings)[^\n:]*[:$\n]{1,3}\s*\$?([\d.,]+[KMB]?)", text, re.I)
    if m:
        stats["total_earnings"] = m.group(0).strip()

    m = re.search(r"(\d+)\s*\n?\s*(active contracts|active contract)", text, re.I)
    if m:
        stats["active_contracts"] = int(m.group(1))

    m = re.search(r"(\d+)\s*\n?\s*(hours logged|total hours)", text, re.I)
    if m:
        stats["total_hours"] = int(m.group(1))

    return stats