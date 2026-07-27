# Clip-Engine

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-2EAD33?logo=playwright&logoColor=white)
![Groq](https://img.shields.io/badge/Groq-F55036?logo=groq&logoColor=white)

> A two-part short-form content workflow: find the campaigns worth clipping for, then produce the clips.

Clip-Engine bundles two standalone Python tools. **Scout** researches which paid campaigns are worth your time; **Clipper** turns the footage into finished vertical clips. Each lives in its own folder with a full, detailed README.

## Table of Contents

- [Tools](#tools)
- [clipper](#clipper)
- [scout](#scout)
- [How They Fit Together](#how-they-fit-together)
- [License](#license)

## Tools

| Folder | What it does | Stack |
|--------|--------------|-------|
| [`clipper/`](clipper) | Short-form clipping pipeline | Python, faster-whisper, ffmpeg, Groq |
| [`scout/`](scout) | Whop campaign research | Python, Playwright |

## clipper

An on-demand pipeline that turns a campaign brief plus footage links into finished vertical clips. It downloads everything, finds the strong moments, writes rules-checked captions, and renders clips with the watermark burned in. Groq does the volume work; you review, post, and submit.

```bash
cd clipper
./setup.sh
export GROQ_API_KEY=your-key-here
python scripts/selftest.py   # offline self-test, no key needed
```

Full command reference: [`clipper/README.md`](clipper/README.md).

## scout

A personal research tool that scrapes your own logged-in Whop Content Rewards campaigns at human pace, once a day, into a JSON file plus a readable Markdown summary. Deliberately not a crawler: one visible browser window, low volume, human timing, and it stops the moment anything looks like a block.

```bash
cd scout
python -m venv .venv
.\.venv\Scripts\Activate.ps1     # Windows PowerShell
pip install -r requirements.txt
python -m playwright install chromium
python scout.py --probe          # manual login, then confirm selectors
```

Full setup and the probe workflow: [`scout/README.md`](scout/README.md).

## How They Fit Together

Scout surfaces which campaigns are worth the effort; Clipper produces the clips for the ones you take on. Together they cover the research and production halves of the same short-form content workflow.

## License

© 2026 Precious G. All rights reserved. This repository is public for viewing and portfolio purposes only; please do not copy, reuse, or redistribute the code without permission.
