# Contributing to JoeBro

Thanks for wanting to help. JoeBro is a local-first, own-your-AI workspace, and
contributions that keep it simple and genuinely useful to non-engineers are very
welcome.

## Ground rules

- **Keep changes focused.** One PR = one fix or feature. Small, reviewable diffs
  get merged; sprawling ones stall.
- **Stay in the spirit of the project:** simple, local-first, no telemetry, no
  accounts, nothing that phones home.
- **Don't add dependencies lightly.** Prefer the standard library and native
  platform features. If a few lines do the job, write the few lines.
- **No secrets in commits.** API keys live in the user's local database
  (`~/Library/Application Support/JoeBro/`), never in source.

## Submitting a pull request

1. Fork the repo and create a branch off `main`.
2. Make your change. Build it in Xcode and run it — confirm the thing you
   touched actually works.
3. Open a PR against `main` with a short description of *what* changed and
   *why*. Screenshots help for anything visual.
4. I'll review as soon as I can. Expect a few rounds of feedback — that's
   normal, not a rejection.

Good first contributions: bug fixes, small UX polish, docs, and provider/model
support. If you want to tackle something larger, open an issue first so we can
agree on the shape before you spend time on it.

## Reporting bugs / requesting features

Open a [GitHub issue](https://github.com/joexk1/joebro/issues) with:

- what you expected, what actually happened, and steps to reproduce
- your macOS version and how you're running the backend
- relevant logs if it's a crash or backend problem

## Getting in touch with me directly

For anything that doesn't fit an issue or PR — questions, ideas, "is this worth
doing before I build it" — reach me directly:

- **GitHub:** open an issue or send a message via [@joexk1](https://github.com/joexk1)

I'd rather you ask first than burn a weekend on something I can't
merge. Don't be shy.
