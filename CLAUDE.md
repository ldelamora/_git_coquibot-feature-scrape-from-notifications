# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commit Convention

All commits must follow this format:
```
YYYYMMDD_Short imperative description
```
Examples: `20260313_Add notification scheduler`, `20260314_Fix token refresh bug`

## Committing and Pushing

After completing any meaningful unit of work, commit and push to GitHub immediately so progress is never lost. This includes after each feature addition, bug fix, or significant change — not just at the end of a session. Use clean, descriptive commit messages following the convention above. Always push after committing (`git push`).

## Project

`coquibot` is a Python bot with notification capabilities. It includes a Flask web UI that triggers Playwright browser automation to log into the Puerto Rico court system (SUMAC).

## Stack

- Python, Flask (web server), Playwright (browser automation)
- `sumac.txt` — credentials file (line 1: username, line 2: password). Gitignored; fill it in before running.

## Running

```bash
pip install -r requirements.txt
python -m playwright install chromium   # one-time setup
python app.py                           # starts server at http://localhost:5000
```

Open `http://localhost:5000` and click the button to trigger login.
