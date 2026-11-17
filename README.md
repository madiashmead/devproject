# B4P Contact Finder

Building an outreach list used to take 20–30 minutes per organization: checking whether we'd
already contacted it, finding its website, then hunting for a usable email. This tool splits
that work in two:

1. **AI does the first search.** Describe what you're looking for once. The app builds a prompt
   for Claude.ai that includes your criteria and your already-contacted list. Claude searches
   public sources (organization websites, nonprofit directories, health center databases) and
   returns each organization's name, who it serves, address, phone, and any public email, along
   with **where each contact came from and how recent that source is**. You paste its reply back
   into the app.
2. **A person verifies before outreach.** A team member checks each contact against the
   organization's own website or with a quick call, and marks it **Verified**, **Needs a call**,
   **Outdated**, or **Not a fit**.

AI can surface old or wrong contacts, especially from directories, so nothing is emailed
without that human check.

**No API key needed.** Searches run in your own Claude.ai account, with web search turned on.

## What's in the app

| Tab | What it does |
|---|---|
| **1 · Find** | Write the criteria → **Copy prompt** → paste it into a new Claude.ai chat → paste Claude's reply back → **Add to Review**. |
| **2 · Review** | One card per organization with its contact info and source. Click *Start check* to time yourself, then mark the result. Once verified, track outreach: emailed, bounced, responded. Export verified contacts to CSV. |
| **Already contacted** | Paste your existing list (one org per line, optional `, website`). Orgs you email from the Review tab are added automatically. |
| **Results** | The proposal's measurements: team minutes per org, bounce rate, response rate, duplicates blocked, compared with the before-AI baseline you enter. |

Duplicate protection works in two layers: the prompt tells Claude to skip the contacted list,
and the app drops any pasted result whose name or website matches the contacted list or an
existing lead.

## Setup

You need Python 3.9+ and a Claude.ai account.

```bash
git clone https://github.com/madiashmead/devproject.git
cd devproject
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open <http://localhost:5050>. Leave the terminal running while you use the app; Ctrl+C stops it.

Data is stored in `data/contacts.db` (SQLite) on the computer running the app. It is not
committed to git. Back it up or use the CSV export to share results.

### Tips for the Claude.ai step

- Turn on **web search** in the chat before sending the prompt, or Claude will answer from memory.
- Start with a small number (3–5 organizations) to see how it goes.
- If Claude's reply doesn't end with a JSON block, ask it: *"Please give the results as the JSON block."*
- Paste the whole reply. The app finds the JSON in it.

## Optional: run searches automatically with an API key

If you later get a Claude API key (from <https://platform.claude.com>, billed separately from
Claude.ai), copy `.env.example` to `.env`, add `ANTHROPIC_API_KEY=...`, and restart. A
**Search automatically** button then appears on the Find tab. It does the same search without
copy and paste, capped at 20 web searches and 30 page reads per run (see `TOOLS` in `research.py`).

## Tests

```bash
python -m unittest discover tests
```

The tests don't need a key or a Claude.ai account.

## Files

- `app.py`: web server and API routes
- `research.py`: the search instructions, the Claude.ai prompt and reply parser, and the optional API call
- `db.py`: SQLite schema and duplicate matching
- `static/`: the web page (plain HTML/CSS/JS, no build step)
