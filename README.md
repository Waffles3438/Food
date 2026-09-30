# U of T Free Food Finder

A dashboard for free food events posted by University of Toronto St. George clubs. It runs on your computer and checks public Instagram posts and Stories your account can access.

## Set it up on Windows

1. Install 64-bit [Python 3.12](https://www.python.org/downloads/windows/).
2. Download this project from GitHub using **Code → Download ZIP**, then extract the folder. Or clone it with Git:

   ```powershell
   git clone https://github.com/Waffles3438/Food.git
   cd Food
   ```

3. Open PowerShell in the `Food` folder and run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1
   ```

   Setup installs the app and image-reading tools. The first setup can take a while because it downloads these packages and models.

4. Sign in to Instagram:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\login-instagram.ps1
   ```

   Sign in with an account you control and complete any Instagram verification prompts. Your password is not saved.

5. Start the app:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\start.ps1
   ```

   The dashboard opens at [http://127.0.0.1:8000](http://127.0.0.1:8000). Keep the PowerShell window open while using it.

## Use the app

- Select **Scan now** to check clubs. The app also scans about every six hours while it is open.
- The dashboard shows upcoming food events, events that need review, and scan progress.
- Use **Clubs & scans** to search by club name or Instagram handle (with or without `@`), see account status, or add a missing handle.
- Click a club's pencil, then **Remove club** to remove it from the directory and future scans. Directory refreshes will not add it back; saved events remain available.
- Select **Not scraped yet · valid Instagram handle** to show clubs with a correctly formatted handle that have never been successfully checked. Failed attempts remain included; accounts successfully checked before a later error are excluded. This filter works with club search and does not verify account existence on Instagram.
- Press **Ctrl+C** in the PowerShell window to stop the app. Saved results remain available next time.

### Import and export events

Use **Export upcoming** above the event tabs to download all upcoming events as a JSON file, regardless of the selected tab or filters. **Import events** loads that file into this app or another copy of Free Food Finder. Details, captions, club labels, and supporting source links are included; saved images, local file paths, and Instagram sessions are excluded.

Import adds events without replacing existing records. Existing event IDs (including dismissed events) and past dates are skipped, and the result shows how many were imported or skipped. Invalid files are rejected without importing any events. Files can contain up to 5,000 events and must be at most 10 MB. Newly imported accounts are paused, so importing a list does not start Instagram scans.

The scanner reads captions first and checks images when event or food details are missing. It uses an available Intel GPU for image reading when supported, with CPU fallback. Scans inspect up to 100 recent timeline entries from the last 60 days. Stories may disappear before a scan can read them.

Future scans check feeds for new posts and Stories, then skip anything already successfully scraped, even if its caption changes. This history survives restarts. Failed or incomplete reads are retried; saved events and manual edits are kept.

Accounts are checked once per pass before any are checked again, with accounts never successfully scraped first. Pauses and restarts resume the unfinished pass. Failed attempts count as a turn and can be retried in the next pass; Instagram restrictions still pause the queue. Newly linked accounts join the unfinished pass.

## Instagram limits

Network and other ordinary failures retry after one minute. Instagram “please wait” restrictions wait 30 minutes, doubling after repeated restrictions up to six hours. HTTP 429 and login failures wait six hours. Cooldowns survive restarts. Complete any Instagram verification prompts or sign in again for login failures. The dashboard shows the next retry time.

“Total scraped accounts” counts accounts successfully checked at least once across all scans. It refreshes every two seconds and increases when a previously unchecked account is successfully scraped. Rescanning an account does not count it twice, and later errors do not reduce the total. Current errors remain visible under Clubs & scans.

After refreshing your Instagram session, you can explicitly skip the saved wait for one scan. Stop the running app with **Ctrl+C**, then run `./start.ps1 -IgnoreCooldown`. This starts a scan immediately; if Instagram restricts it again, the scan stops and normal cooldowns apply to subsequent retries. It does not clear Instagram's own restrictions.

If PowerShell says port 8000 is already in use, the app may already be running. Open [http://127.0.0.1:8000](http://127.0.0.1:8000) instead of starting another copy.

## Your data

The database and Instagram session are kept on your computer, outside this project folder. Post images are used temporarily for text reading; images saved from Stories are kept as event evidence. The app does not send images to a hosted AI service. Do not share your Instagram session files.

All event times use Toronto time. Times from 1 to before 9 without AM/PM default to PM; explicit AM/PM is respected. The scanner can miss events or misread posters, so confirm uncertain details under **Needs review**.
