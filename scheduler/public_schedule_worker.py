"""Fetch public GameSheet schedules in an isolated headless browser.

Since Nov 2026 gamesheetstats.com sits behind a Cloudflare JS challenge
(`cf-mitigated: challenge`), so plain HTTP clients get HTTP 403 regardless of
Python/TLS version. Verified behaviour (2026-11-10): a fresh headless Chromium
context clears the challenge on a normal page load and its first in-page call
to the public JSON API succeeds; follow-up calls in the same context are
challenged again. So this worker uses one fresh context per season and asks for
the whole date range in that single call (the API accepts multi-week ranges).

Input (stdin JSON):  {"tasks": [{"season": 15008, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}, ...]}
Output (stdout JSON): {"results": [{"season", "start", "end", "status_code", "payload"}]}

It accepts only dates and integer season IDs (never URLs, CRM data or
secrets) and deliberately has no Django/project imports. It runs under any
Python that has `playwright` installed (see OPPORTUNITIES_PUBLIC_PYTHON).
"""
import json
import sys
from datetime import date

API = "https://gamesheetstats.com/api/unified-games/{season}?dateStart={start}&dateEnd={end}"
PAGE = "https://gamesheetstats.com/seasons/{season}/games"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
ATTEMPTS = 3

FETCH_JS = """
async (u) => {
  try {
    const r = await fetch(u, {headers: {"Accept": "application/json"}});
    return [r.status, await r.text()];
  } catch (e) {
    return [0, String(e)];
  }
}
"""


def _validated_tasks(raw):
    tasks = raw.get("tasks") if isinstance(raw, dict) else None
    if not isinstance(tasks, list) or not tasks or len(tasks) > 500:
        raise ValueError("Invalid task list")
    clean = []
    for task in tasks:
        season = int(task["season"])
        if not 0 < season < 10000000:
            raise ValueError("Invalid public season")
        start = date.fromisoformat(task["start"])
        end = date.fromisoformat(task["end"])
        if end < start or (end - start).days > 400:
            raise ValueError("Invalid date range")
        clean.append((season, start.isoformat(), end.isoformat()))
    return clean


def _fetch_season(browser, season, start, end):
    status, text = 0, ""
    for _ in range(ATTEMPTS):
        ctx = browser.new_context(user_agent=UA, locale="en-US")
        try:
            page = ctx.new_page()
            page.goto(PAGE.format(season=season), wait_until="domcontentloaded", timeout=60000)
            for _ in range(40):
                if "just a moment" not in (page.title() or "").lower():
                    break
                page.wait_for_timeout(500)
            status, text = page.evaluate(FETCH_JS, API.format(season=season, start=start, end=end))
        except Exception as exc:  # navigation timeout etc. -> retry with a fresh context
            status, text = 0, type(exc).__name__
        finally:
            ctx.close()
        if status == 200:
            try:
                return 200, json.loads(text)
            except ValueError:
                status = 598  # 200 but not JSON (e.g. a challenge page)
    return status, None


def main():
    tasks = _validated_tasks(json.load(sys.stdin))
    from playwright.sync_api import sync_playwright

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for season, start, end in tasks:
                status, payload = _fetch_season(browser, season, start, end)
                results.append({"season": season, "start": start, "end": end, "status_code": status, "payload": payload})
        finally:
            browser.close()
    json.dump({"results": results}, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Do not print request headers, environment, or exception URLs.
        print(json.dumps({"error": type(exc).__name__}))
        sys.exit(1)
