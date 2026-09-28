# U of T Free Food Finder

A local dashboard that watches St. George student clubs' public Instagram posts and accessible Stories for upcoming events offering complimentary food. The dashboard runs only on your computer. It uses Instagram through the unofficial Instaloader library; Instagram may restrict access, challenge logins, or change its interfaces. Review the coverage and last-scan status in the app.

## Quick start (Windows)

1. Install 64-bit Python 3.11, 3.12, or 3.13 from [python.org](https://www.python.org/downloads/windows/), and select **Add python.exe to PATH**. Codex desktop's bundled CPython runtime is also detected if available.
2. Double-click `setup.ps1` from PowerShell, or run `powershell -ExecutionPolicy Bypass -File .\setup.ps1`. Setup creates `.venv`, installs the app and CPU-only OCR dependencies, and downloads English OCR weights at first run. The PyTorch CPU wheels and OCR models require a sizeable one-time download.
3. Run `powershell -ExecutionPolicy Bypass -File .\login-instagram.ps1` and sign in to an Instagram account you control. Instagram may ask for two-factor verification. The password is not stored; Instaloader stores its reusable session outside this repository in its user config directory.
   If Instagram rejects the direct login but you can sign in through a browser, first finish any Instagram security prompts there, then import that browser session explicitly, for example `powershell -ExecutionPolicy Bypass -File .\login-instagram.ps1 -BrowserCookie firefox`. Supported browser names include `edge`, `chrome`, and `firefox`. This reads Instagram cookies from the selected browser and saves the resulting session in the app's local data folder. Recent Chrome on Windows may prevent external tools from decrypting its cookies; if that happens, keep Chrome's encryption enabled and use Firefox for the import instead.
4. Run `powershell -ExecutionPolicy Bypass -File .\start.ps1` and open `http://127.0.0.1:8000`.
5. Select **Scan now** to discover clubs and start a scan. Scans continue in the background and preserve progress if interrupted; remaining accounts are picked up at the next scheduled scan. The app schedules a scan every six hours while it is running; club-directory discovery refreshes weekly.

OCR weights download from EasyOCR's model host on first use and are cached locally. Use `--help` with `.venv\Scripts\python.exe -m foodfinder` for the command-line tools.

To import a specific browser account, run `./login-instagram.ps1 -BrowserCookie firefox -Username uoftfoodscraper` after signing into that account in Firefox. The import checks the account identity before saving and refuses a different account. Only unexpired Instagram cookies are imported; failed verification leaves the saved session unchanged.

## What it can access

- The app walks all 36 pages currently linked by the University of Toronto Student Organization Portal and keeps St. George entries. It also reads the UTSU club directory. Clubs without an Instagram handle can be linked manually from **Clubs & scans**.
- Posts include captions, carousel images, and the displayed covers on video posts. The app OCRs posters locally on CPU and uses local rules to find food, dates, costs, and restrictions. It does not transcribe audio.
- Collection starts with Instaloader's authenticated post-timeline query. It does not call `Profile.from_username()` or `web_profile_info` before reading posts: that profile endpoint has returned immediate 429 responses even on single-account checks. Pagination and rate management still use Instaloader. The query matches the pinned 4.15.3 release; Instagram can change it or refuse access.
- Stories use the requested club's account ID from its timeline. Empty or restricted timelines that supply no ID are marked as incomplete Story coverage. Collaborative posts never cause the app to check another organizer's Stories.
- Stories are checked for accounts available to the signed-in user. A Story's media is saved locally as evidence when a matching event is detected, though its Instagram link may expire. Scans happen every six hours, so Stories that disappear between scans may be missed.
- Collection inspects up to 100 timeline entries per club account, reading posts published in the last 60 days. Old pinned posts are skipped without hiding newer announcements. Reaching the limit is reported as partial coverage. Private, inactive, inaccessible, or Instagram-restricted accounts cannot be guaranteed.
- Paid or members-only events with complimentary food appear as well. Entry costs and restrictions are shown separately. Uncertain food or event dates appear in **Needs review**.

All event dates use the `America/Toronto` time zone. Date-only results show no guessed time. Relative dates are based on the source post. No event date is rolled forward into another year to make it appear upcoming.

## Privacy and local data

The SQLite database is saved under `%LOCALAPPDATA%\UofTFreeFoodFinder\foodfinder.sqlite3`; OCR downloads use EasyOCR's local model cache. Post images are used temporarily for OCR and are discarded. Story media is saved only when an event detection needs evidence. Nothing is sent to a hosted AI service. `.gitignore` excludes the SQLite file, collected media, Instagram sessions, Python environments, and OCR weights.

## Troubleshooting

- **Python was not found:** Install Python 3.11 or newer and reopen PowerShell.
- **No session / expired login:** Run `login-instagram.ps1` again. Do not paste your Instagram password into the dashboard.
- **Login challenge or throttling:** Let Instagram finish the login challenge in your browser, then retry later. A 429 pauses the current queue, including when it occurs while downloading an image or Reel cover. Requests use one attempt; authentication failures, access refusals, and unreadable API responses also pause the queue. **Scan now** is blocked after a rate limit or interrupted run until the next six-hour scan window. Six hours is the app's retry interval, not a promise that Instagram will allow access then. Do not repeatedly restart or use Instagram while it is rate-limiting requests.
- **Missing poster text:** A media-download failure preserves the caption and marks the account as partially checked. A missing or malformed timeline is reported as a failure rather than a successful empty feed.
- **OCR initialization or image-reading failure:** The caption and any successfully read poster text are retained, and coverage is marked partial. Model-download progress output is disabled.
- **401 with “please wait”:** This is treated as a temporary access restriction, rather than proof that the session expired. Collection stops. Finish any prompts in the browser for the same scraper account before reimporting its session.
- **OCR did not load:** Confirm setup finished and run `setup.ps1` again. The CPU-only PyTorch dependency and OCR model may take several minutes to download.
- **No events yet:** Add missing club handles under **Clubs & scans**, scan again, and check **Needs review**. Detection is heuristic; it is not a guarantee of event availability.

## Development

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe -m foodfinder discover
& .\.venv\Scripts\python.exe -m foodfinder login
& .\.venv\Scripts\python.exe -m foodfinder scan
```

The application defaults to a loopback-only HTTP listener (`127.0.0.1:8000`). Do not expose it to a public network: it is a personal, local-use tool.
