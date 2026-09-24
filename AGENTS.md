# Instructions for coding agents

## Project basics

- This is a local Windows app. Use PowerShell commands and the existing `.venv` when available.
- Read `README.md` for setup, architecture, data locations, and user-facing behavior before changing those areas.
- Keep Instagram sessions, passwords, downloaded media, and the SQLite database out of the repository. Never print or commit credentials or session cookies.

## Testing and Instagram access

- Use local fixtures and mocks for routine development and tests; do not send live Instagram requests as a test.
- If the user explicitly requests a live Instagram check, use one scanner instance, respect any active cooldown, and stop after a 429 instead of retrying or moving through more accounts.
- Run the project test suite after relevant code changes:

  ```powershell
  & .\.venv\Scripts\python.exe -m unittest discover -s tests -v
  ```

- Do not reinstall dependencies unless dependency files changed or the user asks.
