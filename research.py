"""AI research step: Claude searches public sources and returns organizations with sourced contacts.

Two ways to run it:
- Claude.ai (no API key): build_chat_prompt() makes a prompt to paste into Claude.ai, and
  parse_pasted_results() reads Claude's answer when it's pasted back into the app.
- API (optional): run_research() calls Claude directly with web_search / web_fetch and a
  structured submit_organizations tool.

Either way the result is a draft for a human to verify. Nothing here sends any email.
"""

import json
import os
import re
import time

try:
    import anthropic
except ImportError:  # the API path is optional
    anthropic = None

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")

GUIDANCE = """\
You research outreach contacts for B4P's partnerships team. The team wants to reach free or \
low-cost health and social service organizations by email. A team member will verify every \
contact you return before anyone sends a message, so your job is to make that check fast: \
for each piece of contact information, say exactly where you found it and how current that \
source looks.

How to work
- Search public sources: the organizations' own websites first, then nonprofit directories, \
health center databases (for example HRSA's Find a Health Center, the Free Clinics of Michigan \
directory, 211 listings), county and city resource pages.
- Open the organization's own website whenever one exists and look for a contact or \
about page. An email on the organization's own site is much better than one in a directory.
- Only report an email address you actually saw on a page. Never construct or guess one \
(no "info@" patterns). If the only public option is a phone number or a contact form, \
leave email empty and give the form URL.
- Note how recent the source is: a "last updated" date, a copyright year, a dated post, or \
"unknown". Flag directories or pages that look old.
- Skip every organization on the already-contacted list, including obvious variants of the \
same name or the same website. Also skip organizations that clearly don't match the criteria.
- Prefer organizations that are clearly operating today.
"""

SYSTEM_PROMPT = GUIDANCE + """
When you're done researching, call submit_organizations once with everything you found. \
Use an empty string for anything you couldn't find. Keep notes short and factual.
"""

FIELDS = [
    "name", "org_type", "serves", "address", "phone", "email", "website", "contact_form_url",
    "source_url", "source_type", "source_date", "confidence", "notes",
]
SOURCE_TYPES = ("org_website", "directory", "government", "news", "other")
CONFIDENCES = ("high", "medium", "low")

_EXAMPLE_ORG = {
    "name": "Official organization name",
    "org_type": "free clinic | sliding-scale clinic | FQHC | shelter | ...",
    "serves": "Who it serves, one sentence",
    "address": "",
    "phone": "",
    "email": "Only an email you saw on a page; empty if none",
    "website": "The organization's own website",
    "contact_form_url": "Contact form URL if there is no email",
    "source_url": "Page where the email (or phone) was found",
    "source_type": "org_website | directory | government | news | other",
    "source_date": "e.g. updated 2026-03, (c) 2024, or unknown",
    "confidence": "high | medium | low",
    "notes": "Anything the verifier should check",
}

SUBMIT_TOOL = {
    "name": "submit_organizations",
    "description": (
        "Submit the final list of organizations found. Call this exactly once, after "
        "researching, with every organization that matches the criteria and is not on the "
        "already-contacted list."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["organizations", "summary"],
        "properties": {
            "summary": {
                "type": "string",
                "description": "Two or three sentences: where you searched and anything the verifier should know.",
            },
            "organizations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "name", "org_type", "serves", "address", "phone", "email", "website",
                        "contact_form_url", "source_url", "source_type", "source_date",
                        "confidence", "notes",
                    ],
                    "properties": {
                        "name": {"type": "string", "description": "Official organization name."},
                        "org_type": {
                            "type": "string",
                            "description": "e.g. free clinic, sliding-scale clinic, FQHC, shelter, domestic violence shelter.",
                        },
                        "serves": {"type": "string", "description": "Who it serves, in one sentence."},
                        "address": {"type": "string"},
                        "phone": {"type": "string"},
                        "email": {
                            "type": "string",
                            "description": "A public email seen on a page. Empty if none was found. Never guessed.",
                        },
                        "website": {"type": "string", "description": "The organization's own website."},
                        "contact_form_url": {"type": "string", "description": "Contact form URL if there is no email."},
                        "source_url": {
                            "type": "string",
                            "description": "The page where the email (or, if none, the phone) was found.",
                        },
                        "source_type": {
                            "type": "string",
                            "enum": ["org_website", "directory", "government", "news", "other"],
                        },
                        "source_date": {
                            "type": "string",
                            "description": "How recent the source is, e.g. 'updated 2026-03', '© 2024', or 'unknown'.",
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": "How likely the email/phone still works. high = on the org's own current site.",
                        },
                        "notes": {"type": "string", "description": "Anything the verifier should check."},
                    },
                },
            },
        },
    },
}

TOOLS = [
    {"type": "web_search_20260209", "name": "web_search", "max_uses": 20},
    {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 30},
    SUBMIT_TOOL,
]

MAX_ROUNDS = 12  # pause_turn continuations + one reminder to submit


class ResearchError(Exception):
    pass


def build_prompt(criteria, target_count, contacted_names, already_found_names):
    contacted = "\n".join(f"- {n}" for n in contacted_names) or "(none yet)"
    found = "\n".join(f"- {n}" for n in already_found_names) or "(none yet)"
    return f"""\
Find up to {target_count} organizations that match these criteria:

<criteria>
{criteria}
</criteria>

Already contacted. Do not include these:
<already_contacted>
{contacted}
</already_contacted>

Already in our research list from earlier searches. Do not include these either; find new ones:
<already_found>
{found}
</already_found>
"""


def build_chat_prompt(criteria, target_count, contacted_names, already_found_names):
    """A self-contained prompt to paste into Claude.ai (web search must be on)."""
    example = json.dumps({"summary": "Where you searched, anything the verifier should know",
                          "organizations": [_EXAMPLE_ORG]}, indent=2)
    return (
        GUIDANCE
        + "\nUse web search for this. Don't answer from memory.\n\n"
        + build_prompt(criteria, target_count, contacted_names, already_found_names)
        + f"""
When you're done, reply with one JSON code block in exactly this format and nothing after it.
Use an empty string for anything you couldn't find. Keep notes short and factual.

```json
{example}
```
"""
    )


class ParseError(Exception):
    pass


def _extract_json(text):
    """Find the JSON in a pasted Claude reply: a ```json block, or the outermost {...} / [...]."""
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.S | re.I)
    candidates = [b.strip() for b in blocks] + [text.strip()]
    for start, end in (("{", "}"), ("[", "]")):
        i, j = text.find(start), text.rfind(end)
        if i != -1 and j > i:
            candidates.append(text[i:j + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise ParseError(
        "Couldn't find the results in what you pasted. Copy Claude's whole reply, including the "
        "JSON block at the end. If Claude didn't give one, ask it: \"Please give the results as the JSON block.\""
    )


def parse_pasted_results(text):
    """Turn a pasted Claude.ai reply into {'organizations': [...], 'summary': str}."""
    if not (text or "").strip():
        raise ParseError("Paste Claude's reply first.")
    data = _extract_json(text)
    if isinstance(data, list):
        data = {"organizations": data, "summary": ""}
    if not isinstance(data, dict) or not isinstance(data.get("organizations"), list):
        raise ParseError("The pasted JSON doesn't have an \"organizations\" list.")

    orgs = []
    for raw in data["organizations"]:
        if not isinstance(raw, dict):
            continue
        org = {f: str(raw.get(f) or "").strip() for f in FIELDS}
        if not org["name"]:
            continue
        if org["source_type"] not in SOURCE_TYPES:
            org["source_type"] = "other" if org["source_type"] else ""
        if org["confidence"].lower() not in CONFIDENCES:
            org["confidence"] = ""
        org["confidence"] = org["confidence"].lower()
        # Drop placeholder text copied straight from the example.
        if org["email"] and "@" not in org["email"]:
            org["email"] = ""
        orgs.append(org)
    if not orgs:
        raise ParseError("No organizations found in what you pasted.")
    return {"organizations": orgs, "summary": str(data.get("summary") or "").strip()}


def run_research(criteria, target_count, contacted_names, already_found_names, client=None):
    """Run one research pass through the API. Returns dict with organizations, summary, usage, seconds."""
    if anthropic is None:
        raise ResearchError("The anthropic package isn't installed. Run: pip install anthropic")
    client = client or anthropic.Anthropic()
    messages = [
        {
            "role": "user",
            "content": build_prompt(criteria, target_count, contacted_names, already_found_names),
        }
    ]
    usage = {"input_tokens": 0, "output_tokens": 0, "web_searches": 0}
    started = time.monotonic()
    reminded = False

    for _ in range(MAX_ROUNDS):
        try:
            response = client.beta.messages.create(
                model=MODEL,
                max_tokens=32000,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": "high"},
                # If a safety classifier declines, the API retries on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise ResearchError(
                "Claude API key missing or invalid. Set ANTHROPIC_API_KEY in your .env file."
            ) from e
        except anthropic.RateLimitError as e:
            raise ResearchError("Claude API rate limit hit. Wait a minute and try again.") from e
        except anthropic.APIStatusError as e:
            raise ResearchError(f"Claude API error ({e.status_code}): {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ResearchError("Couldn't reach the Claude API. Check your internet connection.") from e

        u = response.usage
        usage["input_tokens"] += (u.input_tokens or 0) + (u.cache_read_input_tokens or 0) + (
            u.cache_creation_input_tokens or 0
        )
        usage["output_tokens"] += u.output_tokens or 0
        if u.server_tool_use:
            usage["web_searches"] += u.server_tool_use.web_search_requests or 0

        if response.stop_reason == "refusal":
            raise ResearchError("Claude declined this request. Try rewording the criteria.")

        submit = next(
            (b for b in response.content if b.type == "tool_use" and b.name == SUBMIT_TOOL["name"]),
            None,
        )
        if submit is not None:
            data = submit.input if isinstance(submit.input, dict) else {}
            return {
                "organizations": data.get("organizations") or [],
                "summary": data.get("summary") or "",
                "usage": usage,
                "seconds": round(time.monotonic() - started, 1),
            }

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "pause_turn":
            # Server-side search loop hit its iteration limit; re-send to let it continue.
            continue

        if response.stop_reason == "max_tokens":
            raise ResearchError("The research ran out of room before finishing. Try a smaller target count.")

        # Ended without submitting: ask once, then give up.
        if reminded:
            break
        reminded = True
        messages.append(
            {
                "role": "user",
                "content": "Please call submit_organizations now with what you've found so far.",
            }
        )

    raise ResearchError("Claude finished without returning a list. Try running the search again.")
