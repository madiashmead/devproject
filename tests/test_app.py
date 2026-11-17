"""Tests that run without a real API key: Claude is replaced by a fake client.

Run:  python -m unittest discover tests
"""

import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ANTHROPIC_API_KEY"] = "test-key"

import app as app_module  # noqa: E402
import db  # noqa: E402
import research  # noqa: E402

ORGS = [
    {
        "name": "Example Free Clinic", "org_type": "free clinic", "serves": "Uninsured adults",
        "address": "1 Main St, Ypsilanti, MI", "phone": "555-0100", "email": "hello@example.org",
        "website": "https://example.org", "contact_form_url": "", "source_url": "https://example.org/contact",
        "source_type": "org_website", "source_date": "updated 2026-09", "confidence": "high", "notes": "",
    },
    {
        # Same website as an org already on the contacted list: must be filtered out.
        "name": "Corner Health (Teen Clinic)", "org_type": "clinic", "serves": "Youth",
        "address": "", "phone": "", "email": "", "website": "www.cornerhealth.org",
        "contact_form_url": "", "source_url": "", "source_type": "directory", "source_date": "unknown",
        "confidence": "low", "notes": "",
    },
]


def usage():
    return SimpleNamespace(
        input_tokens=100, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=3),
    )


class FakeMessages:
    """First call pauses (server-side search loop limit), second call submits the list."""

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return SimpleNamespace(stop_reason="pause_turn", usage=usage(),
                                   content=[SimpleNamespace(type="text", text="searching")])
        submit = SimpleNamespace(type="tool_use", name="submit_organizations",
                                 input={"organizations": ORGS, "summary": "Searched HRSA and 211."})
        return SimpleNamespace(stop_reason="tool_use", usage=usage(), content=[submit])


def fake_client():
    messages = FakeMessages()
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


class ResearchTests(unittest.TestCase):
    def test_handles_pause_turn_and_submit(self):
        client, messages = fake_client()
        result = research.run_research("clinics", 5, ["Packard Health"], [], client=client)
        self.assertEqual(len(messages.calls), 2)
        self.assertEqual(len(result["organizations"]), 2)
        self.assertEqual(result["usage"]["web_searches"], 6)
        prompt = messages.calls[0]["messages"][0]["content"]
        self.assertIn("Packard Health", prompt)
        self.assertEqual(messages.calls[0]["model"], "claude-opus-5-5")

    def test_name_key_and_domain(self):
        self.assertEqual(db.name_key("The Corner Health Center, Inc."), "corner health center")
        self.assertEqual(db.domain_of("https://www.CornerHealth.org/about"), "cornerhealth.org")


class PasteFlowTests(unittest.TestCase):
    def test_prompt_includes_criteria_and_exclusions(self):
        prompt = research.build_chat_prompt("shelters in Ann Arbor", 4, ["Packard Health"], ["Some Clinic"])
        for text in ("shelters in Ann Arbor", "up to 4", "Packard Health", "Some Clinic", "```json", "web search"):
            self.assertIn(text, prompt)

    def test_parse_reply_with_prose_and_code_block(self):
        reply = (
            "I searched HRSA and 211. Here's what I found:\n\n```json\n"
            + json.dumps({"summary": "Searched HRSA.", "organizations": [
                {**ORGS[0], "confidence": "High", "source_type": "blog"},
                {"name": "", "email": "x@y.org"},  # no name: dropped
                {"name": "Placeholder Org", "email": "Only an email you saw on a page; empty if none"},
            ]})
            + "\n```\nLet me know if you want more."
        )
        result = research.parse_pasted_results(reply)
        self.assertEqual([o["name"] for o in result["organizations"]], ["Example Free Clinic", "Placeholder Org"])
        self.assertEqual(result["organizations"][0]["confidence"], "high")
        self.assertEqual(result["organizations"][0]["source_type"], "other")
        self.assertEqual(result["organizations"][1]["email"], "")  # placeholder text isn't an email
        self.assertEqual(result["summary"], "Searched HRSA.")

    def test_parse_bare_list_and_errors(self):
        self.assertEqual(len(research.parse_pasted_results(json.dumps(ORGS))["organizations"]), 2)
        for bad in ("", "Sorry, I couldn't find anything.", '{"foo": 1}'):
            with self.assertRaises(research.ParseError):
                research.parse_pasted_results(bad)

    def test_import_route(self):
        c = app_module.app.test_client()
        c.post("/api/contacted", json={"name": "Already Emailed Clinic"})
        self.assertEqual(c.post("/api/prompt", json={"criteria": "x", "target_count": 3}).status_code, 200)
        orgs = [dict(ORGS[0], name="Paste Test Clinic", website="https://paste-test.org"),
                dict(ORGS[0], name="Already Emailed Clinic", website="")]
        r = c.post("/api/import", json={
            "criteria": "x", "target_count": 3, "text": json.dumps({"organizations": orgs, "summary": ""}),
            "started_at": "2020-01-01T00:00:00.000Z",  # too old: time is ignored
        })
        body = r.get_json()
        self.assertEqual((body["found_count"], body["duplicate_count"]), (1, 1))
        search = c.get(f"/api/searches/{body['id']}").get_json()
        self.assertEqual((search["method"], search["status"], search["ai_seconds"]), ("claude_ai", "done", None))
        self.assertEqual(c.post("/api/import", json={"text": "no json here"}).status_code, 400)


class AppFlowTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_full_flow(self):
        c = self.client
        r = c.post("/api/contacted", json={"bulk": "Packard Health\nCorner Health Center, cornerhealth.org\nPackard Health"})
        self.assertEqual(r.get_json()["added"], 2)

        fake, _ = fake_client()
        original = research.run_research
        research.run_research = lambda *a, **k: original(*a, client=fake, **k)
        try:
            r = c.post("/api/searches", json={"criteria": "free clinics", "target_count": 5})
            self.assertEqual(r.status_code, 202)
            sid = r.get_json()["id"]
            for _ in range(50):
                s = c.get(f"/api/searches/{sid}").get_json()
                if s["status"] != "running":
                    break
                time.sleep(0.05)
        finally:
            research.run_research = original

        self.assertEqual(s["status"], "done", s.get("error"))
        self.assertEqual(s["found_count"], 1)
        self.assertEqual(s["duplicate_count"], 1)

        leads = c.get("/api/leads?status=new").get_json()
        self.assertEqual([l["name"] for l in leads], ["Example Free Clinic"])
        lid = leads[0]["id"]

        r = c.patch(f"/api/leads/{lid}", json={"status": "verified", "verified_by": "Sam", "verify_minutes": 3})
        self.assertEqual(r.get_json()["status"], "verified")
        r = c.patch(f"/api/leads/{lid}", json={"outreach": "bounced"})
        self.assertEqual(r.get_json()["outreach"], "bounced")
        self.assertEqual(c.patch(f"/api/leads/{lid}", json={"status": "nope"}).status_code, 400)

        names = [x["name"] for x in c.get("/api/contacted").get_json()]
        self.assertIn("Example Free Clinic", names)  # added automatically after outreach

        m = c.get("/api/metrics").get_json()
        self.assertEqual(m["time"]["avg_verify_minutes"], 3.0)
        self.assertEqual(m["outreach"]["bounce_rate"], 100.0)
        self.assertEqual(m["duplicates_blocked"], 1)

        csv_text = c.get("/api/leads.csv?status=verified").get_data(as_text=True)
        self.assertIn("hello@example.org", csv_text)


if __name__ == "__main__":
    unittest.main()
