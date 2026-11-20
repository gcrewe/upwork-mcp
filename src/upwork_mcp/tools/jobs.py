"""Job search and details tools for Upwork MCP."""

import asyncio
import re
import urllib.parse
from pydantic import BaseModel, Field
from ..browser.client import get_browser


class JobSearchParams(BaseModel):
    """Parameters for job search."""
    query: str = Field(description="Search keywords")
    category: str | None = Field(default=None, description="Job category filter (best-effort; Upwork drives categories client-side)")
    experience_level: str | None = Field(default=None, description="Experience level: entry, intermediate, or expert")
    job_type: str | None = Field(default=None, description="Job type: hourly or fixed")
    budget_min: int | None = Field(default=None, description="Minimum budget in USD (best-effort filter)")
    budget_max: int | None = Field(default=None, description="Maximum budget in USD (best-effort filter)")
    limit: int = Field(default=10, ge=1, le=50, description="Maximum number of results")


class JobDetailsParams(BaseModel):
    """Parameters for getting job details."""
    job_url: str = Field(description="Full Upwork job URL or job ID")


# Lines on a job tile/section that are candidate skill tags (short, title-case,
# non-meta). Upwork's new feed no longer renders `section` elements, so skills
# are recovered from the leading short lines of each tile's text.
_SKILL_SKIP = re.compile(
    r"^(Posted|Proposals|Verified|Payment|Rating|Location|spent|hours|hrs/week|member|"
    r"Est\.|Hourly|Fixed|Project|Apply|Save|Flag|More|Less|About|Skills|Top|General|"
    r"Review|Recommend|Actively|Hiring|Bid|Median|Interview|View|See|Learn|Terms|Privacy|"
    r"Client|Jobs|Total|Avg|Active|Open|Close|Cancel|Search|Home|Messages|Opportunities|"
    r"Contracts|Finances|Uma|Sort|Clear|Advanced|Category|Experience|Number|Custom|USD|"
    r"Entry|Intermediate|Expert|Invites|Saved|Post|Referrals|Offers|AI|React|Node|Python|"
    r"WordPress|Web|Data|Marketing|Business|Customer|Software|DevOps|Design|Content)",
    re.I,
)


def _tile_jobs_js() -> str:
    """JS that walks the search feed and returns raw job tiles (title/url/text)."""
    return """
    (() => {
      const out = [];
      const seen = new Set();
      const links = Array.from(document.querySelectorAll('h2 a[href*="/jobs/"], h3 a[href*="/jobs/"]'));
      for (const l of links) {
        const url = (l.href || '').split('?')[0];
        if (seen.has(url)) continue;
        let node = l.parentElement, tile = null;
        while (node && node !== document.body) {
          const t = (node.innerText || '').trim();
          if (t.length > 250 && /(\\$|hourly|fixed)/i.test(t)) { tile = node; break; }
          node = node.parentElement;
        }
        if (!tile) continue;
        seen.add(url);
        out.push({ title: (l.textContent || '').trim(), url, text: (tile.innerText || '').trim() });
      }
      return out;
    })()
    """


def _extract_skills(text: str, limit: int = 12) -> list[str]:
    plain_locations = {
        "united states", "usa", "worldwide", "remote", "canada", "india", "ukraine",
        "pakistan", "australia", "israel", "latvia", "china", "italy", "brazil",
        "united kingdom", "uae", "united arab emirates", "germany", "france",
    }
    skills: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if len(line) < 2 or len(line) > 30:
            continue
        if re.search(r"[\$%]|https?://|^[a-z]", line):
            continue
        if re.fullmatch(r"[\d.,]+", line) or re.fullmatch(r"\d\.\d", line):
            continue
        if "," in line or ":" in line:
            continue
        if line.lower() in plain_locations:
            continue
        if _SKILL_SKIP.match(line):
            continue
        if line not in skills:
            skills.append(line)
        if len(skills) >= limit:
            break
    return skills


def _description_snippet(text: str, n: int = 300) -> str:
    """First sentence-like paragraph of a tile's text (post-header)."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if len(line) > 90 and not re.search(r"Posted|Proposals|Hourly|Fixed|Verified|Payment|Location|Est\.", line):
            start = i
            break
    if start is None:
        return ""
    part = re.sub(r"\s+", " ", " ".join(x.strip() for x in lines[start:] if x.strip()))
    return part[:n]


async def search_jobs(params: JobSearchParams) -> list[dict]:
    """Search for jobs on Upwork matching the specified criteria.

    Returns a list of job summaries with title, budget, and URL.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    # Upwork's search UI lives under /nx/search/jobs/; the old best-matches URL
    # ignores ?q= and returns the personalized feed.
    base_url = "https://www.upwork.com/nx/search/jobs/"
    query_params = {"q": params.query}

    if params.job_type:
        query_params["t"] = "0" if params.job_type.lower() == "hourly" else "1"

    level_map = {"entry": "1", "intermediate": "2", "expert": "3"}
    if params.experience_level and params.experience_level.lower() in level_map:
        query_params["contractor_tier"] = level_map[params.experience_level.lower()]

    if params.budget_min:
        query_params["rate_from"] = str(int(params.budget_min))
    if params.budget_max:
        query_params["rate_to"] = str(int(params.budget_max))

    url = f"{base_url}?{urllib.parse.urlencode(query_params)}"
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_function(
            "Array.from(document.querySelectorAll('h2 a[href*=\"/jobs/\"]')).length > 0",
            timeout=25000,
        )
    except Exception:
        pass
    await asyncio.sleep(2.5)

    raw = await page.evaluate(_tile_jobs_js())
    jobs: list[dict] = []
    for j in raw[: params.limit]:
        text = j["text"]
        job: dict = {"title": j["title"], "url": j["url"]}

        m = re.search(r"Est\. budget:\s*\$[\d,]+(?:\.\d{2})?", text)
        if not m:
            m = re.search(r"\$\d{2,3}(?:\.\d{2})?\s*\n\s*-\s*\n\s*\$\d{2,3}(?:\.\d{2})?", text)
        if not m:
            m = re.search(r"\$\d{2,3}(?:\.\d{2})?/hr", text)
        if m:
            job["budget"] = re.sub(r"\s+", " ", m.group(0)).strip()

        m = re.search(r"\b(Entry Level|Intermediate|Expert)\b", text)
        if m:
            job["experience_level"] = m.group(0)

        job["job_type"] = (
            "Fixed" if re.search(r"\bFixed[- ]price\b", text, re.I)
            else ("Hourly" if re.search(r"\bHourly\b|\$[\d.]+\s*[-–]\s*\$?[\d.]+|/hr", text, re.I) else "")
        )

        m = re.search(r"Posted [^\n]{0,40}", text)
        if m:
            job["posted"] = m.group(0).strip()

        m = re.search(r"Proposals:\s*[\d\s]+to\s+\d+|\bProposals:\s*\d+", text)
        if m:
            job["proposals"] = re.sub(r"\s+", " ", m.group(0)).strip()

        skills = _extract_skills(text)
        if skills:
            job["skills"] = skills

        desc = _description_snippet(text)
        if desc:
            job["description"] = desc

        jobs.append(job)

    return jobs


async def get_job_details(params: JobDetailsParams) -> dict:
    """Get detailed information about a specific Upwork job posting.

    Returns comprehensive job details including description, client history,
    skills required, and application requirements.
    """
    browser = get_browser()
    await browser.ensure_logged_in()
    page = await browser.get_page()

    url = params.job_url
    if not url.startswith("http"):
        # Allow a bare job id: ~0221234... or 0221234...
        job_id = url.strip("/").lstrip("~")
        if re.fullmatch(r"\d+", job_id):
            url = f"https://www.upwork.com/jobs/~{job_id}/"
        else:
            url = f"https://www.upwork.com/jobs/{url}"

    await page.goto(url, wait_until="domcontentloaded")
    await asyncio.sleep(3)

    # Gentle scroll: triggers lazy-rendered blocks (client info, connects)
    try:
        for _ in range(5):
            await page.evaluate("window.scrollBy(0, 500)")
            await page.wait_for_timeout(250)
        await page.wait_for_timeout(700)
    except Exception:
        pass

    final_url = page.url
    if not final_url.startswith("https://www.upwork.com/jobs/"):
        final_url = url
    job: dict = {"url": final_url}

    title = ""
    title_el = await page.query_selector("h1")
    if title_el:
        title = (await title_el.text_content() or "").strip()
    if not title:
        doc_title = await page.title()
        title = re.split(r"\s+[-–—]\s+", doc_title.replace(" | Upwork", "").replace(" - Upwork", ""))[0].strip()
    if title:
        job["title"] = title

    text = await page.evaluate("document.body ? document.body.innerText : ''") or ""

    # --- Description block: from Summary/Project description to Skills/About the client ---
    def _block(start_markers: list[str], end_markers: list[str]) -> str:
        start = -1
        for marker in start_markers:
            idx = text.find(marker)
            if idx >= 0:
                start = idx
                break
        if start < 0:
            return ""
        ends = [text.find(marker) for marker in end_markers if text.find(marker) > start]
        end = min(ends) if ends else len(text)
        return re.sub(r"\n{3,}", "\n\n", text[start:end]).strip()

    desc = _block(
        ["Project description", "Project Description", "Summary", "About this job"],
        ["Skills and Expertise", "Skills & Expertise", "About the client", "Activity on this job"],
    )
    if desc:
        job["description"] = desc

    m = re.search(r"Est\. budget:\s*\$[\d,]+(?:\.\d{2})?", text)
    if not m:
        m = re.search(r"\$\d{2,3}(?:\.\d{2})?\s*\n\s*-\s*\n\s*\$\d{2,3}(?:\.\d{2})?", text)
    if not m:
        m = re.search(r"\$\d{2,3}(?:\.\d{2})?/hr", text)
    if m:
        job["budget"] = re.sub(r"\s+", " ", m.group(0)).strip()

    m = re.search(r"\b(Entry Level|Intermediate|Expert)\b", text)
    if m:
        job["experience_level"] = m.group(0)

    m = re.search(r"\b(Fixed[- ]price|Hourly|Project Type:[^\n]*)", text, re.I)
    if m:
        job["job_type"] = m.group(0).strip()

    for pattern, key in [
        (r"Posted [^\n]{0,40}", "posted"),
        (r"Proposals:\s*[\d\s]+to\s+\d+|\bProposals:\s*\d+", "proposals"),
        (r"(Less than|More than) \d+ (month|week)s?|\bOngoing\b|\b1 to 3 months\b|\b3 to 6 months\b", "project_length"),
        (r"\d+\+?\s*hrs/week", "hours_per_week"),
    ]:
        m = re.search(pattern, text, re.I)
        if m:
            job[key] = re.sub(r"\s+", " ", m.group(0)).strip()

    # Skills: prefer the dedicated block between the heading and the CTAs
    skills_block = _block(
        ["Skills and Expertise", "Skills & Expertise"],
        ["Apply now", "About the client", "Activity on this job", "How to apply"],
    )
    skills = _extract_skills(skills_block if skills_block else text, limit=15)
    skill_els = await page.query_selector_all('[class*="skill"][class*="tag"], [class*="skill-tag"]')
    for el in skill_els[:15]:
        t = (await el.text_content() or "").strip()
        if t and len(t) <= 40 and t not in skills:
            skills.append(t)
    if skills:
        job["skills"] = skills[:15]

    # --- Client block ---
    client = {}
    ci = _block(["About the client"], ["Activity on this job", "Bid range", "How to apply"])
    if ci:
        client["summary"] = re.sub(r"\n{3,}", "\n", ci.replace("About the client", "").strip())[:600]
    m = re.search(r"Payment method verified|Payment verified", text)
    if m:
        client["payment_verified"] = True
    m = re.search(r"Rating is ([0-9.]+) out of 5|([0-9.]+)\s*of\s*\d+\s*reviews", text)
    if m:
        client["rating"] = m.group(1) or m.group(2)
    m = re.search(r"(\$[\d,]+[KMB]?) total spent|\$[\d,]+[KMB]?\s*\n\s*\d+ hires", text)
    if m:
        client["total_spent"] = m.group(0).strip()
    m = re.search(r"(\d+) jobs posted|(\d+)% hire rate", text)
    if m:
        client["jobs_posted"] = m.group(1) if m.group(1) else None
    m = re.search(r"(\d+)% hire rate", text)
    if m:
        client["hire_rate"] = f"{m.group(1)}%"
    m = re.search(r"Member since ([A-Za-z]{3} \d{1,2}, \d{4})", text)
    if m:
        client["member_since"] = m.group(1)
    m = re.search(r"([A-Z][a-zA-Z]+)\s*\n?\s*\d{1,2}:\d{2}\s*[AP]M", text)
    if m:
        client["location"] = m.group(1)
    if client:
        job["client"] = client

    # --- Connects & screening ---
    m = re.search(r"[Rr]equired [Cc]onnects[^\d]{0,40}(\d+)", text)
    if m:
        job["connects_required"] = int(m.group(1))
    m = re.search(r"Available Connects:\s*(\d+)", text)
    if m:
        job["connects_available"] = int(m.group(1))
    q = _block(["Screening questions"], ["How to apply", "About the client", "Activity on this job"])
    if q and len(q) > 30:
        job["screening_questions"] = re.sub(r"\n{3,}", "\n", q).strip()[:1200]

    return job