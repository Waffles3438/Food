# U of T Free Food Finder

A local dashboard that watches St. George student clubs' public Instagram posts and accessible Stories for upcoming events offering complimentary food. The dashboard runs only on your computer. It uses Instagram through the unofficial Instaloader library; Instagram may restrict access, challenge logins, or change its interfaces. Review the coverage and last-scan status in the app.

## Quick start (Windows)

1. Install 64-bit Python 3.11, 3.12, or 3.13 from [python.org](https://www.python.org/downloads/windows/), and select **Add python.exe to PATH**. Codex desktop's bundled CPython runtime is also detected if available.
2. Double-click `setup.ps1` from PowerShell, or run `powershell -ExecutionPolicy Bypass -File .\setup.ps1`. Setup creates `.venv` and installs the app and OCR dependencies. It detects Intel Arc graphics and installs the Intel GPU (XPU) build of PyTorch; otherwise it installs the CPU build. An existing XPU installation is preserved. PyTorch and the English OCR models require a sizeable one-time download.
3. Run `powershell -ExecutionPolicy Bypass -File .\login-instagram.ps1` and sign in to an Instagram account you control. Instagram may ask for two-factor verification. The password is not stored; Instaloader stores its reusable session outside this repository in its user config directory.
   If Instagram rejects the direct login but you can sign in through a browser, first finish any Instagram security prompts there, then import that browser session explicitly, for example `powershell -ExecutionPolicy Bypass -File .\login-instagram.ps1 -BrowserCookie firefox`. Supported browser names include `edge`, `chrome`, and `firefox`. This reads Instagram cookies from the selected browser and saves the resulting session in the app's local data folder. Recent Chrome on Windows may prevent external tools from decrypting its cookies; if that happens, keep Chrome's encryption enabled and use Firefox for the import instead.
4. Run `powershell -ExecutionPolicy Bypass -File .\start.ps1` and open `http://127.0.0.1:8000`.
5. Select **Scan now** to discover clubs and start a scan. Scans continue in the background and preserve progress if interrupted; remaining accounts are picked up at the next scheduled scan. The app schedules a scan every six hours while it is running; club-directory discovery refreshes weekly.

OCR weights download from EasyOCR's model host on first use and are cached locally. Use `--help` with `.venv\Scripts\python.exe -m foodfinder` for the command-line tools.

Keep the PowerShell window open to follow timestamped scan progress. It shows directory pages and club profiles, the current account number, post links, caption-only decisions, image downloads, OCR device and image number, Instagram pacing waits, and a final summary. Post counts also update in the dashboard as each post is saved. If an operation has no new progress for 30 seconds, the terminal repeats its last activity with the elapsed time; that means the process is still running, not that the request has succeeded. Scan messages do not print passwords, session cookies, or post captions. Use **Ctrl+C** in the PowerShell window to stop the app; saved results remain available.

To import a specific browser account, run `./login-instagram.ps1 -BrowserCookie firefox -Username uoftfoodscraper` after signing into that account in Firefox. The import checks the account identity before saving and refuses a different account. Only unexpired Instagram cookies are imported; failed verification leaves the saved session unchanged.

## What it can access

- The app walks all 36 pages currently linked by the University of Toronto Student Organization Portal and keeps St. George entries. It also reads the UTSU club directory. Clubs without an Instagram handle can be linked manually from **Clubs & scans**.
- Posts are checked caption-first. A clear complimentary-food offer with a date, time, and location skips image downloads and OCR. Food or event captions missing these details get image OCR, including all carousel images and Reel covers, using an available Intel GPU with CPU fallback. Empty captions and accessible Stories still get image OCR. Unrelated captions skip images, so offers mentioned only in those images can be missed. Audio is not transcribed.
- Repeat scans fetch timeline entries to discover new posts and compare captions. Successfully processed, unchanged post IDs skip downloads, OCR, and event extraction. Edited captions are processed again, reusing successful saved image text when available. Incomplete reads are retried. Stories with saved successful IDs and unchanged captions also skip media processing; saved event evidence stays available. Post counts for each scan count newly processed or edited posts, not cached skips; the terminal logs skips explicitly.
- Existing records from before this cache update are processed once more because they did not record whether OCR succeeded. The cache is saved after each processed source, so later interruptions retain completed work. Image-only changes under the same post ID are not detected by caption comparisons. The scanner still inspects up to 100 entries rather than stopping at the first saved or old post, since pinned and collaborative posts can appear out of date order.
- Collection starts with Instaloader's authenticated post-timeline query. It does not call `Profile.from_username()` or `web_profile_info` before reading posts: that profile endpoint has returned immediate 429 responses even on single-account checks. Pagination and rate management still use Instaloader. The query matches the pinned 4.15.3 release; Instagram can change it or refuse access.
- Stories use the requested club's account ID from its timeline. Empty or restricted timelines that supply no ID are marked as incomplete Story coverage. Collaborative posts never cause the app to check another organizer's Stories.
- Stories are checked for accounts available to the signed-in user. A Story's media is saved locally as evidence when a matching event is detected, though its Instagram link may expire. Scans happen every six hours, so Stories that disappear between scans may be missed.
- Collection inspects up to 100 timeline entries per club account, reading posts published in the last 60 days. Old pinned posts are skipped without hiding newer announcements. Reaching the limit is reported as partial coverage. Private, inactive, inaccessible, or Instagram-restricted accounts cannot be guaranteed.
- Paid or members-only events with complimentary food appear as well. Entry costs and restrictions are shown separately. Uncertain food or event dates appear in **Needs review**.

All event dates use the `America/Toronto` time zone. Date-only results show no guessed time. Relative dates are based on the source post. When a calendar date omits its year, the post timestamp in Toronto supplies the year. No event date is rolled forward into another year to make it appear upcoming. Explicit event dates in the caption take priority over image dates; where separately described activities have their own dates, dates in the food-offer paragraph take priority. Duration text such as "4-16 month" is not treated as a date.

## Privacy and local data

The SQLite database is saved under `%LOCALAPPDATA%\UofTFreeFoodFinder\foodfinder.sqlite3`; OCR downloads use EasyOCR's local model cache. Post images are used temporarily for OCR and are discarded. Story media is saved only when an event detection needs evidence. Nothing is sent to a hosted AI service. `.gitignore` excludes the SQLite file, collected media, Instagram sessions, Python environments, and OCR weights.

## Troubleshooting

- **Automatic recovery:** Keep the app open. Failed or interrupted scans automatically start a fresh scan after five minutes, including consecutive failures. HTTP 429 rate limits instead wait six hours. Saved history determines the delay across app restarts; unchecked accounts stay first in the queue. Successful runs restore the normal six-hour cadence. The terminal and dashboard display the next retry time. The scheduler checks once per minute, and missed intervals produce one catch-up scan.
- **Error details:** New scan failures retain the category, exception type, and recognizable HTTP status without retaining raw error responses or session data. Older ambiguous access errors use the five-minute delay unless the saved message identifies HTTP 429.

- **Python was not found:** Install Python 3.11 or newer and reopen PowerShell.
- **No session / expired login:** Run `login-instagram.ps1` again. Do not paste your Instagram password into the dashboard.
- **Login challenge or throttling:** Complete login challenges in your browser, reimport the session, then select **Scan now**. Authentication failures also retry after five minutes, but may still need you to complete verification or reimport the session. A 429 stops the current queue, including when it occurs during media downloads. HTTP 429 waits six hours; other failures, including please-wait restrictions and access refusals, wait five minutes. Each individual request still gets only one attempt. **Scan now** respects active retry delays. Six hours is the app's retry interval, not a promise that Instagram will allow access then.
- **Missing poster text:** A media-download failure preserves the caption and marks the account as partially checked. A missing or malformed timeline is reported as a failure rather than a successful empty feed.
- **OCR initialization or image-reading failure:** The caption and any successfully read poster text are retained, and coverage is marked partial. Model-download progress output is disabled.
- **401 with “please wait”:** This is treated as a temporary access restriction, rather than proof that the session expired. Collection stops. Finish any prompts in the browser for the same scraper account before reimporting its session.
- **OCR did not load:** Confirm setup finished and run `setup.ps1` again. PyTorch and the OCR models may take several minutes to download.
- **No events yet:** Add missing club handles under **Clubs & scans**, scan again, and check **Needs review**. Detection is heuristic; it is not a guarantee of event availability.

## Food keywords

The shared list in `foodfinder/food_keywords.py` includes hamburgers/burgers, BBQ/barbecue, cookies, pizza, hot dogs, donuts/doughnuts, pastries, bagels, muffins, cupcakes, brownies, sandwiches, wraps, tacos, burritos, sushi, dumplings, noodles, pasta, fries, poutine, popcorn, chips, ice cream, candy, fruit, coffee, tea, bubble tea/boba, hot chocolate, juice, lemonade, drinks, snacks, refreshments, and meals. Singular/plural forms and spelling variants are listed explicitly; edit that file and restart the app to extend it.

Matching is case-insensitive. Examples include `free hamburger`, `FREE COOKIES!`, `complimentary BBQ`, `pizza included with admission`, and `#FreeCookies`. A food keyword alone prompts image inspection but does not prove the food is complimentary. `Free cookies!` without a date is retained in **Needs review** if image OCR cannot resolve it. Negated offers, dietary phrases such as `gluten-free pizza`, and unrelated offers such as `free play` are not treated as confirmed free food. Food that is merely "available" needs review. Missing admission or membership details remain unspecified.

## Intel GPU OCR

Run `powershell -ExecutionPolicy Bypass -File .\setup.ps1 -TorchBackend xpu` to explicitly install Intel GPU support. The pinned PyTorch 2.7.1 XPU build supports Intel Arc graphics on Windows with a compatible graphics driver. Hardware detection alone does not guarantee driver or OCR compatibility; the app checks GPU availability at runtime.

`FOODFINDER_OCR_DEVICE` defaults to `auto`, which uses Intel XPU when available and CPU otherwise. To force CPU for a launch, set `$env:FOODFINDER_OCR_DEVICE = "cpu"` in PowerShell before running `start.ps1`. Use `"auto"` to restore automatic selection. `.env.example` documents these settings but is not loaded automatically.

EasyOCR 1.7.2 assumes CUDA in its GPU model loader, so the app loads unquantized models on CPU and moves both text detection and recognition to Intel XPU. A GPU initialization or image-processing failure logs a warning and falls back to CPU; the affected image is retried. If CPU also fails, the existing partial-coverage reporting applies. This speeds up local image processing when supported; Instagram request limits still apply.

Optional GPU image batching is controlled by `$env:FOODFINDER_OCR_BATCH_SIZE = "4"` (range 1-16; default 1). It batches text detection across images from the same post/carousel or Story, then recognizes text in image order. It does not combine different posts or send parallel Instagram requests. Mixed image sizes are padded with white space rather than stretched. Batches above 16 million padded pixels, or batches that fail, are retried as individual images; CPU fallback remains available. The terminal shows batch progress when enabled.

The default stays at **1** because it was fastest on this computer. The Intel Arc 140V benchmark used eight generated posters of mixed 1080x1080 and 1080x1350 sizes, warm-up passes, and two timed passes per setting:

| Images per batch | Average time for eight posters | Peak allocated GPU memory |
| --- | --- | --- |
| 1 | 7.75 seconds | 1,211 MiB |
| 4 | 8.75 seconds | 4,551 MiB |
| 8 | 8.88 seconds | 9,005 MiB |

All settings returned identical text and ran on XPU, with detector batch dimensions verified during inference. These are local fixture measurements, not an estimate for all Instagram posts; image size, padding, and poster complexity affect throughput. More GPU memory allowed larger batches but did not make these images faster.

## Development

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe -m foodfinder discover
& .\.venv\Scripts\python.exe -m foodfinder login
& .\.venv\Scripts\python.exe -m foodfinder scan
```

The application defaults to a loopback-only HTTP listener (`127.0.0.1:8000`). Do not expose it to a public network: it is a personal, local-use tool.
