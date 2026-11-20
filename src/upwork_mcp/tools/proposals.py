"""Proposal tools for Upwork MCP."""

import asyncio
import re
from pydantic import BaseModel, Field
from ..browser.client import get_browser


class ProposalsParams(BaseModel):
    """Parameters for getting proposals."""
    status: str = Field(
        default="active",
        description="Filter by status: active, submitted, archived, or all"
    )
    limit: int = Field(default=20, ge=1, le=50, description="Maximum number of results")


class SubmitProposalParams(BaseModel):
    """Parameters for submitting a proposal."""
    job_url: str = Field(description="Full Upwork job URL")
    cover_letter: str = Field(description="Cover letter content")
    rate: float | None = Field(default=None, description="Proposed hourly rate (for hourly jobs)")
    bid: float | None = Field(default=None, description="Bid amount (for fixed-price jobs)")
    answers: list[str] | None = Field(default=None, description="Answers to screening questions")


# Rows in the new proposals UI live inside tables.up-proposals-list__block, one
# per section card ("Active proposals", "Submitted proposal", "Offers", ...).
_PROPOSALS_JS = """
(() => {
  const out = [];
  const seen = new Set();
  const cards = Array.from(document.querySelectorAll('h2'));
  for (const h of cards) {
    const heading = (h.innerText || '').trim();
    let card = h;
    for (let i = 0; i < 5 && card && card !== document.body; i++) {
      if (/card|proposal/i.test((card.className || '').toString()) || card.tagName === 'SECTION') break;
      card = card.parentElement;
    }
    if (!card || card === document.body) continue;
    const section = heading.replace(/\\(\\d+\\)/g, '').trim();
    const rows = card.querySelectorAll('tr');
    for (const r of rows) {
      const a = r.querySelector('a[href*="/nx/proposals/"]');
      if (!a) continue;
      const url = 'https://www.upwork.com' + a.getAttribute('href');
      if (seen.has(url)) continue;
      seen.add(url);
      const time = r.querySelector('td');
      out.push({
        section,
        title: (a.textContent || '').trim(),
        url,
        initiated: (time ? (time.innerText || '').replace(/\\s+/g, ' ').trim() : '')
      });
    }
  }
  return out;
})()
"""


async def get_proposals(params: ProposalsParams) -> list[dict]:
    """Get your submitted proposals on Upwork.

    Returns a list of proposals with job title, status, bid amount, and dates.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    await page.goto("https://www.upwork.com/nx/proposals/", wait_until="domcontentloaded")
    try:
        await page.wait_for_function(
            "Array.from(document.querySelectorAll('a[href*=\"/nx/proposals/\"]')).length > 0 "
            "|| (document.body.innerText||'').includes('proposal')",
            timeout=20000,
        )
    except Exception:
        pass
    await asyncio.sleep(2.5)

    raw = await page.evaluate(_PROPOSALS_JS)

    wanted = params.status.lower()
    status_map = {
        "active": "active proposals",
        "submitted": "submitted proposal",
        "archived": "archived",
    }

    proposals: list[dict] = []
    for row in raw:
        section_lower = row["section"].lower()
        if wanted != "all":
            expected = status_map.get(wanted, wanted)
            if expected not in section_lower:
                continue
        proposals.append({
            "job_title": row["title"],
            "job_url": row["url"],
            "status": row["section"],
            "submitted": row["initiated"],
        })
        if len(proposals) >= params.limit:
            break

    return proposals


async def get_proposal_details(proposal_url: str) -> dict:
    """Get detailed information about a specific proposal.

    Args:
        proposal_url: URL to the proposal

    Returns details including cover letter, bid, and any messages.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    url = proposal_url
    if url.startswith("/"):
        url = f"https://www.upwork.com{url}"

    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(3.5)

    details: dict = {"url": url}

    title_el = await page.query_selector("h1, [class*='job-title']")
    if title_el:
        details["job_title"] = (await title_el.text_content() or "").strip()

    text = await page.evaluate("document.body ? document.body.innerText : ''") or ""

    m = re.search(r"\$[\d.,]+\s*(?:/hr|/hour)", text)
    if m:
        details["bid"] = m.group(0).strip()

    m = re.search(r"\b(Submitted|Active|Withdrawn|Archived|Interviewing)\b", text)
    if m:
        details["status"] = m.group(1)

    # Cover letter block usually sits under a "Cover letter" heading
    start = text.find("Cover letter")
    if start >= 0:
        block = text[start:]
        for marker in ("Activity", "About the client", "Job posting", "Interview", "Messages"):
            idx = block.find(marker, 30)
            if idx > 0:
                block = block[:idx]
                break
        details["cover_letter"] = re.sub(r"\n{3,}", "\n", block.replace("Cover letter", "").strip())[:2000]

    return details


async def _set_native_value(page, selector: str, value) -> bool:
    """Set a React-controlled input/textarea value via the native setter."""
    el = await page.query_selector(selector)
    if not el:
        return False
    try:
        await el.evaluate("""
        (node, v) => {
          const proto = node.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
          setter.call(node, v);
          node.dispatchEvent(new Event('input', { bubbles: true }));
          node.dispatchEvent(new Event('change', { bubbles: true }));
        }
        """, value)
        await asyncio.sleep(1.0)
        return True
    except Exception:
        return False


async def _pick_dropdown_item(page, toggle_contains: str, item_text: str) -> bool:
    """Open an air3 dropdown and pick an exact menu item via real input events."""
    opened = await page.evaluate("""
    (needle) => {
      const toggles = Array.from(document.querySelectorAll('.air3-dropdown-toggle')).filter(e => e.offsetParent);
      const t = toggles.find(x => (x.innerText || '').includes(needle));
      if (!t) return false;
      t.click();
      return true;
    }
    """, toggle_contains)
    if not opened:
        return False
    await page.wait_for_timeout(600)
    picked = await page.evaluate("""
    (target) => {
      const items = Array.from(document.querySelectorAll('li, button, [role="option"], a'))
        .filter(e => e.offsetParent && (e.innerText || '').trim() === target);
      const el = items[items.length - 1];
      if (!el) return false;
      const b = el.getBoundingClientRect();
      return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
    }
    """, item_text)
    if not picked:
        await page.keyboard.press("Escape")
        return False
    await page.mouse.click(picked["x"], picked["y"])
    await page.wait_for_timeout(900)
    return True


async def _set_rate_increase(page) -> bool:
    """Set the (now mandatory) 'Schedule a rate increase' section to the least
    binding legal choice: frequency Every 12 months, percent 5%."""
    ok1 = await _pick_dropdown_item(page, "Select a frequency", "Every 12 months")
    ok2 = await _pick_dropdown_item(page, "Select a percent", "5%")
    await page.mouse.click(400, 160)  # blur dropdowns
    await page.wait_for_timeout(900)
    return ok1 and ok2


async def submit_proposal(params: SubmitProposalParams) -> dict:
    """Submit a proposal to an Upwork job.

    IMPORTANT: This is a sensitive action that will spend Connects.
    Make sure the cover letter and rate/bid are correct before submitting.

    Returns submission status and connects used.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    url = params.job_url
    if not url.startswith("http"):
        job_id = url.strip("/").lstrip("~")
        url = f"https://www.upwork.com/jobs/~{job_id}/" if re.fullmatch(r"\d+", job_id) else f"https://www.upwork.com/jobs/{url}"

    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(3)

    # --- Click "Apply now" (text-based; the old data-test selector is gone) ---
    clicked = await page.evaluate("""
    (() => {
      const els = Array.from(document.querySelectorAll('button, a'));
      const b = els.find(e => (e.innerText || '').trim().toLowerCase().startsWith('apply now'));
      if (b) { b.click(); return true; }
      return false;
    })()
    """)
    if not clicked:
        return {"status": "error", "message": "Apply button not found. Job may be closed or unavailable."}

    try:
        await page.wait_for_url(re.compile(r"/apply/"), timeout=20000)
    except Exception:
        pass
    await asyncio.sleep(4)

    body = await page.evaluate("document.body ? document.body.innerText : ''") or ""
    connects_required = 0
    m = re.search(r"(?:requires|Required Connects to submit a proposal:)\s*(\d+)", body)
    if m:
        connects_required = int(m.group(1))

    # --- Fill rate / bid ---
    if params.rate is not None:
        if not await _set_native_value(page, "#step-rate", str(params.rate)):
            if not await _set_native_value(page, "[data-test='currency-input']", str(params.rate)):
                return {"status": "error", "message": "Hourly rate input not found on apply form"}

    if params.bid is not None:
        if not await _set_native_value(page, "[data-test='currency-input']", str(params.bid)):
            if not await _set_native_value(page, "input[name*='bid'], input[name*='amount']", str(params.bid)):
                return {"status": "error", "message": "Bid input not found on apply form"}

    # --- Fill cover letter (the visible textarea on the apply page) ---
    letter_el = None
    try:
        letter_el = await page.evaluate_handle("""
        (() => Array.from(document.querySelectorAll('textarea')).find(x => x.offsetParent))
        """)
    except Exception:
        pass
    if letter_el is None:
        # fall back to query_selector
        letter_el = await page.query_selector("textarea")

    if letter_el is None:
        return {"status": "error", "message": "Cover letter textarea not found on apply form"}

    await letter_el.evaluate("""
    (node, v) => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
      setter.call(node, v);
      node.dispatchEvent(new Event('input', { bubbles: true }));
      node.dispatchEvent(new Event('change', { bubbles: true }));
    }
    """, params.cover_letter)
    await asyncio.sleep(1.2)

    # --- Screening question answers (textareas rendered below the letter) ---
    if params.answers:
        await page.evaluate("""
        (answers) => {
          const tas = Array.from(document.querySelectorAll('textarea')).filter(x => x.offsetParent);
          for (let i = 0; i < answers.length && i < tas.length - 1; i++) {
            const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
            setter.call(tas[i + 1], answers[i]);
            tas[i + 1].dispatchEvent(new Event('input', { bubbles: true }));
            tas[i + 1].dispatchEvent(new Event('change', { bubbles: true }));
          }
        }
        """, params.answers)
        await asyncio.sleep(1.0)

    # --- Mandatory "Schedule a rate increase" on hourly proposals ---
    if params.rate is not None:
        try:
            await _set_rate_increase(page)
        except Exception:
            pass

    # --- Submit ---
    submitted = await page.evaluate("""
    (() => {
      const btns = Array.from(document.querySelectorAll('button')).filter(e => e.offsetParent);
      const b = btns.find(x => (x.innerText || '').trim().toLowerCase() === 'submit proposal');
      if (b) { b.click(); return true; }
      return false;
    })()
    """)
    if not submitted:
        return {"status": "error", "message": "Submit button not found on apply form"}

    await asyncio.sleep(6)
    body = await page.evaluate("document.body ? document.body.innerText : ''") or ""

    if re.search(r"fix the errors below", body, re.I):
        return {
            "status": "error",
            "message": "Form validation errors on apply page — check rate, cover letter, and required fields (incl. rate increase schedule).",
        }

    if re.search(r"(proposal|proposals).{0,30}(submitted|sent)", body, re.I) or "proposals/active" in page.url:
        return {"status": "submitted", "connects_used": connects_required, "message": "Proposal submitted successfully"}

    return {"status": "unknown", "message": "Could not confirm submission status", "connects_required": connects_required}


async def withdraw_proposal(proposal_url: str) -> dict:
    """Withdraw a submitted proposal.

    Args:
        proposal_url: URL to the proposal to withdraw

    Returns withdrawal status.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    url = proposal_url
    if url.startswith("/"):
        url = f"https://www.upwork.com{url}"

    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(3)

    clicked = await page.evaluate("""
    (() => {
      const els = Array.from(document.querySelectorAll('button, a')).filter(e => e.offsetParent);
      const b = els.find(e => (e.innerText || '').trim().toLowerCase().startsWith('withdraw'));
      if (b) { b.click(); return true; }
      return false;
    })()
    """)
    if not clicked:
        return {"status": "error", "message": "Withdraw button not found. Proposal may already be closed."}

    await asyncio.sleep(1.5)
    confirmed = await page.evaluate("""
    (() => {
      const els = Array.from(document.querySelectorAll('button, a')).filter(e => e.offsetParent);
      const b = els.find(e => /^(yes|confirm|withdraw proposal)$/i.test((e.innerText || '').trim()));
      if (b) { b.click(); return true; }
      return false;
    })()
    """)
    if confirmed:
        await asyncio.sleep(2.5)

    body = await page.evaluate("document.body ? document.body.innerText : ''") or ""
    if re.search(r"withdraw", body, re.I) and re.search(r"(success|confirm|done)", body, re.I):
        return {"status": "withdrawn", "message": "Proposal withdrawn successfully"}
    return {"status": "unknown", "message": "Could not confirm withdrawal"}