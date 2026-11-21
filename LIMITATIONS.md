# Limitations

What this tool does not do, on purpose or not yet.

## Will not do

- **No logins.** It does not sign in, hold sessions, or send cookies or passwords. Data behind an account is out of scope.
- **No CAPTCHA handling.** If a site shows a challenge, the job stops there.
- **No anti-bot evasion.** No proxy rotation, no fake browser user-agent, no headless-browser stealth, no header spoofing. The tool identifies itself as `web-data-extractor/<version>` with the repo URL on every request.
- **No ignoring robots.txt.** There is no switch for it. A disallowed URL is refused before any request is made, and so is a redirect that lands on one.
- **No turning off the delay.** The delay between requests to one host cannot go below 0.5 seconds, and backoff cannot go below 0.5 seconds. A config that tries is rejected before the job starts.
- **No way around a block.** Three answers of 403 or 429 in a row, a block, CAPTCHA or consent page, or a `Retry-After` longer than the backoff cap end the job for that host. The tool does not retry from another address, change its user-agent, or come back early.
- **No personal data collection.** It is built for catalog-style public records (products, listings, prices). It is not for building lists of people. Personal data here means named individuals, personal or named work emails, phone numbers, profile URLs and photos of people. The tool cannot tell which fields are personal: that is decided when the config is written, and those fields are left out.
- **No bulk copying of written content.** It is for facts (names of products, prices, stock, ratings). It is not for copying article text, reviews, site-written descriptions or images in bulk.
- **Not a terms-of-service check.** robots.txt is checked in code. A site's terms of use are not. A person reads them before a new site is added and records the URL, date and clause in the config's `terms_check` block. The tool refuses to run without that record, except against this machine and the two sandbox sites, but it cannot check that the record is true. The bundled sandbox configs have no such record.
- **Not the first choice when an API exists.** If a site offers an API or a bulk download, that is the right source, and this tool does not read APIs.

## Out of scope for now

- **JavaScript-rendered pages.** The tool reads the HTML the server sends. If the data only appears after scripts run in a browser, the selectors will find nothing and the Summary sheet will say the page had no matching items. No Playwright or browser option is included.
- **Non-HTML sources.** No JSON APIs, PDFs, sitemaps or feeds.
- **One level of detail pages.** A list page can link to a detail page. A detail page cannot link on to a third level.
- **CSS selectors only.** No XPath.
- **Field types** are text, number, integer and url. No dates, no currencies as a separate column, no unit conversion.
- **Little parallelism, on purpose.** By default one request at a time. `max_concurrency_per_host` allows up to 4 in flight per host, but the delay between request starts still applies, so a large crawl stays slow by design. List pages are read one after another either way, since each one names the next.
- **Next-link or numbered pagination only.** No infinite scroll, no "load more" buttons, no form posts.

## Known rough edges

- **Number parsing is strict, and has one ambiguity.** `1,299.00` and `1.299,00` are both read as 1299. A value like `1.299` with a single dot is read as 1.299, not 1299. A string with two numbers in it (a price range) is rejected, not guessed.
- **robots.txt matching** follows RFC 9309 (longest match, `*` and `$`), but compares paths as written. It does not normalize percent-encoding first.
- **robots.txt that cannot be read** (server error, timeout, 401, 403) is treated as "disallow everything", so the job fetches nothing from that host. A 404 is treated as "no rules".
- **Block-page detection is a phrase list.** It reads the page title, and the visible text of a list page that has no items, for phrases such as "Access Denied", "Just a moment", "captcha" and "unusual traffic". A block page worded differently is not caught by this check; it shows up as a failed run-health gate. A real page whose title happens to contain one of those phrases would stop the job.
- **The personal-data guard is a pattern match.** It catches values that look like an email address with a non-shared mailbox, or a phone number in common formats. It does not recognize names of people, postal addresses, profile links or photos. Leaving those fields out of the config is still the real control.
- **Run-health thresholds are set by hand** in each config. There is no comparison with the previous run, so a slow drop in row count over several runs is not flagged.
- **No page archive.** The disk cache holds pages for a day so a rerun costs no requests, but it is not a complete record of the run and there is no command to rebuild a result from saved pages. Each row carries a SHA-256 of its source page, which shows whether a page changed, not what it said.
- **No check against the site's own result count**, no JSON-LD reading, and no resume after an interrupted run.
- **The 403 and 429 count is per host and resets on any good page.** A site that blocks every other request is not caught by the streak rule.
- **Crawl-delay above 300 seconds** makes the tool skip the host and say so, not wait.
- **The cache keeps pages for 24 hours by default.** robots.txt is never kept longer than 24 hours, whatever the cache setting, and a job that runs longer than that reads it again. A rule the site changed in the last day is picked up when the cached copy expires, or at once with `--no-cache`.
- **Encoding** is whatever `httpx` detects from the response. A page that declares the wrong charset will come out garbled.
- **Duplicate detection** is exact match on the `unique_by` fields after cleanup. Near-duplicates (a renamed product) are not caught.
- **The benchmark site was written by the same author as the tool.** See the note at the bottom of BENCHMARK.md.
- **Selectors break when a site changes its layout.** The symptom is rows moving to the Rejected sheet with "not found on the page", not a crash.
