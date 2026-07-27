# Clip-Engine

Two tools that power a short-form content workflow, from finding the campaigns worth
clipping for to producing the clips themselves. Each lives in its own folder with its
own detailed README.

## [`clipper/`](clipper): short-form clipping pipeline

An on-demand pipeline that turns a campaign brief plus footage links into finished
vertical clips. It downloads everything, finds the strong moments, writes captions
(rules-checked against the campaign), and renders clips with the watermark burned in.
Groq does the volume work (moment scanning, caption first-drafts); you review, post,
and submit. Built with Python, faster-whisper, ffmpeg, and Groq.

See [`clipper/README.md`](clipper/README.md) for the full command reference.

## [`scout/`](scout): campaign research

A personal research tool that scrapes your own logged-in Whop Content Rewards campaigns
at human pace, once a day, and writes them to a JSON file plus a readable Markdown
summary. Deliberately not a crawler: one visible browser window, low volume, erratic
human timing, and it stops the moment anything looks like a block. Built with Python
and Playwright.

See [`scout/README.md`](scout/README.md) for setup and the selector-confirmation probe.

## How they fit together

Scout surfaces which campaigns are worth the effort; Clipper produces the clips for the
ones you take on. Together they cover the research and the production halves of the same
short-form content workflow.
