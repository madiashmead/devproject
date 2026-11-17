"""B4P Contact Finder: web server.

Run:  python app.py   then open http://localhost:5050
"""

import csv
import io
import os
import threading
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request, send_from_directory

import db
import research

HERE = os.path.dirname(os.path.abspath(__file__))


def load_dotenv(path=os.path.join(HERE, ".env")):
    """Minimal .env loader so no extra dependency is needed."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()
app = Flask(__name__, static_folder="static", static_url_path="/static")
db.init_db()
with db.connect() as _conn:
    # A search can't survive a server restart; mark any leftovers as interrupted.
    _conn.execute(
        "UPDATE searches SET status='error', error='Interrupted by a server restart.' WHERE status='running'"
    )


def bad_request(message):
    return jsonify({"error": message}), 400


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(os.path.join(HERE, "static"), "index.html")


# ---------------------------------------------------------------------------
# Settings (criteria + baselines)
# ---------------------------------------------------------------------------

@app.get("/api/settings")
def get_settings():
    with db.connect() as conn:
        settings = db.get_settings(conn)
    settings["api_key_configured"] = bool(
        os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )
    return jsonify(settings)


@app.put("/api/settings")
def put_settings():
    with db.connect() as conn:
        db.update_settings(conn, request.get_json(force=True) or {})
        return jsonify(db.get_settings(conn))


# ---------------------------------------------------------------------------
# Already-contacted list
# ---------------------------------------------------------------------------

@app.get("/api/contacted")
def list_contacted():
    with db.connect() as conn:
        return jsonify(db.rows(conn.execute("SELECT * FROM contacted ORDER BY name COLLATE NOCASE")))


@app.post("/api/contacted")
def add_contacted():
    """Body: {"name", "website"?, "notes"?}  or  {"bulk": "one org per line, optional ', website'"}"""
    body = request.get_json(force=True) or {}
    added = 0
    with db.connect() as conn:
        if "bulk" in body:
            for line in str(body["bulk"]).splitlines():
                line = line.strip().strip("-•*").strip()
                if not line:
                    continue
                name, _, website = line.partition(",")
                before = conn.total_changes
                db.add_contacted(conn, name, website.strip(), origin="import")
                added += conn.total_changes - before
        else:
            if not (body.get("name") or "").strip():
                return bad_request("Name is required.")
            before = conn.total_changes
            db.add_contacted(conn, body["name"], body.get("website", ""), body.get("notes", ""))
            added = conn.total_changes - before
    return jsonify({"added": added})


@app.delete("/api/contacted/<int:cid>")
def delete_contacted(cid):
    with db.connect() as conn:
        conn.execute("DELETE FROM contacted WHERE id = ?", (cid,))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# AI searches
# ---------------------------------------------------------------------------

_running_lock = threading.Lock()


def _run_search_job(search_id, criteria, target_count):
    try:
        _search_and_save(search_id, criteria, target_count)
    except Exception as e:  # noqa: BLE001 - any failure is reported on the search row
        message = str(e) if isinstance(e, research.ResearchError) else f"Unexpected error: {e}"
        with db.connect() as conn:
            conn.execute(
                "UPDATE searches SET status='error', error=?, finished_at=? WHERE id=?",
                (message, db.now_iso(), search_id),
            )
    finally:
        _running_lock.release()


def _exclusion_lists():
    with db.connect() as conn:
        contacted = [r["name"] for r in conn.execute("SELECT name FROM contacted ORDER BY name")]
        found = [r["name"] for r in conn.execute("SELECT name FROM leads ORDER BY name")]
    return contacted, found


def _search_and_save(search_id, criteria, target_count):
    contacted, found = _exclusion_lists()
    result = research.run_research(criteria, target_count, contacted, found)
    _save_results(search_id, result)


def _save_results(search_id, result):
    """Store new organizations as leads, skipping duplicates. Marks the search row done."""
    saved = duplicates = 0
    with db.connect() as conn:
        for org in result["organizations"]:
            name = (org.get("name") or "").strip()
            if not name:
                continue
            # Safety net: even if the AI missed it, never save an org we've already contacted.
            if db.find_duplicate(conn, name, org.get("website", "")):
                duplicates += 1
                continue
            conn.execute(
                """INSERT INTO leads(search_id, name, name_key, serves, org_type, address, phone,
                       email, website, domain, contact_form_url, source_url, source_type,
                       source_date, confidence, ai_notes, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    search_id, name, db.name_key(name), org.get("serves", ""), org.get("org_type", ""),
                    org.get("address", ""), org.get("phone", ""), org.get("email", "").strip(),
                    org.get("website", ""), db.domain_of(org.get("website", "")),
                    org.get("contact_form_url", ""), org.get("source_url", ""),
                    org.get("source_type", ""), org.get("source_date", ""),
                    org.get("confidence", ""), org.get("notes", ""), db.now_iso(),
                ),
            )
            saved += 1
        u = result.get("usage") or {}
        conn.execute(
            """UPDATE searches SET status='done', finished_at=?, ai_seconds=?, found_count=?,
                   duplicate_count=?, summary=?, input_tokens=?, output_tokens=?, web_searches=?
               WHERE id=?""",
            (
                db.now_iso(), result.get("seconds"), saved, duplicates, result["summary"],
                u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("web_searches", 0), search_id,
            ),
        )
    return saved, duplicates


def _criteria_and_target(body):
    with db.connect() as conn:
        settings = db.get_settings(conn)
    criteria = (body.get("criteria") or settings["criteria"]).strip()
    try:
        target = max(1, min(25, int(body.get("target_count") or settings["target_count"])))
    except (TypeError, ValueError):
        raise ValueError("Target count must be a number.")
    if not criteria:
        raise ValueError("Write the search criteria first.")
    with db.connect() as conn:
        db.update_settings(conn, {"criteria": criteria, "target_count": target})
    return criteria, target


# --- Claude.ai flow (no API key): copy a prompt, paste the reply back ------

@app.post("/api/prompt")
def make_prompt():
    try:
        criteria, target = _criteria_and_target(request.get_json(force=True) or {})
    except ValueError as e:
        return bad_request(str(e))
    contacted, found = _exclusion_lists()
    return jsonify({"prompt": research.build_chat_prompt(criteria, target, contacted, found)})


@app.post("/api/import")
def import_results():
    """Body: {"text": pasted Claude reply, "criteria", "target_count", "started_at"?: ISO time prompt was copied}"""
    body = request.get_json(force=True) or {}
    try:
        criteria, target = _criteria_and_target(body)
        result = research.parse_pasted_results(body.get("text", ""))
    except (ValueError, research.ParseError) as e:
        return bad_request(str(e))

    seconds = None
    try:
        started = datetime.fromisoformat(str(body.get("started_at")).replace("Z", "+00:00"))
        seconds = (datetime.now(timezone.utc) - started).total_seconds()
        seconds = round(seconds, 1) if 0 < seconds < 6 * 3600 else None
    except ValueError:
        pass
    result["seconds"] = seconds

    with db.connect() as conn:
        search_id = conn.execute(
            "INSERT INTO searches(criteria, target_count, status, method, started_at) VALUES (?,?, 'running', 'claude_ai', ?)",
            (criteria, target, db.now_iso()),
        ).lastrowid
    saved, duplicates = _save_results(search_id, result)
    return jsonify({"id": search_id, "found_count": saved, "duplicate_count": duplicates})


# --- API flow (optional, needs ANTHROPIC_API_KEY) ---------------------------


@app.post("/api/searches")
def start_search():
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return bad_request("No Claude API key configured. Add ANTHROPIC_API_KEY to the .env file and restart.")
    try:
        criteria, target = _criteria_and_target(request.get_json(force=True) or {})
    except ValueError as e:
        return bad_request(str(e))
    if not _running_lock.acquire(blocking=False):
        return bad_request("A search is already running. Wait for it to finish.")

    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO searches(criteria, target_count, status, method, started_at) VALUES (?,?, 'running', 'api', ?)",
            (criteria, target, db.now_iso()),
        )
        search_id = cur.lastrowid
    threading.Thread(target=_run_search_job, args=(search_id, criteria, target), daemon=True).start()
    return jsonify({"id": search_id}), 202


@app.get("/api/searches")
def list_searches():
    with db.connect() as conn:
        return jsonify(db.rows(conn.execute("SELECT * FROM searches ORDER BY id DESC")))


@app.get("/api/searches/<int:sid>")
def get_search(sid):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM searches WHERE id = ?", (sid,)).fetchone()
    return (jsonify(dict(row)), 200) if row else (jsonify({"error": "Not found"}), 404)


# ---------------------------------------------------------------------------
# Leads: human verification + outreach tracking
# ---------------------------------------------------------------------------

@app.get("/api/leads")
def list_leads():
    status = request.args.get("status")
    with db.connect() as conn:
        if status:
            cur = conn.execute("SELECT * FROM leads WHERE status = ? ORDER BY id DESC", (status,))
        else:
            cur = conn.execute("SELECT * FROM leads ORDER BY id DESC")
        return jsonify(db.rows(cur))


EDITABLE_LEAD_FIELDS = {
    "name", "serves", "org_type", "address", "phone", "email", "website", "contact_form_url",
    "notes", "status", "verified_by", "verify_minutes", "outreach",
}


@app.patch("/api/leads/<int:lid>")
def update_lead(lid):
    body = request.get_json(force=True) or {}
    updates = {k: v for k, v in body.items() if k in EDITABLE_LEAD_FIELDS}
    if "status" in updates and updates["status"] not in db.LEAD_STATUSES:
        return bad_request("Unknown status.")
    if "outreach" in updates and updates["outreach"] not in db.OUTREACH_STATUSES:
        return bad_request("Unknown outreach status.")
    if "verify_minutes" in updates:
        try:
            updates["verify_minutes"] = float(updates["verify_minutes"]) if updates["verify_minutes"] not in ("", None) else None
        except (TypeError, ValueError):
            return bad_request("Verification minutes must be a number.")

    with db.connect() as conn:
        lead = conn.execute("SELECT * FROM leads WHERE id = ?", (lid,)).fetchone()
        if not lead:
            return jsonify({"error": "Not found"}), 404
        if updates.get("status") in ("verified", "needs_call", "outdated", "rejected") and updates["status"] != lead["status"]:
            updates["verified_at"] = db.now_iso()
        if "outreach" in updates and updates["outreach"] != lead["outreach"]:
            updates["outreach_at"] = db.now_iso()
            if updates["outreach"] != "not_sent":
                # Once we've emailed them, they belong on the contacted list for future searches.
                db.add_contacted(conn, lead["name"], lead["website"], "Added after outreach", origin="outreach")
        if "name" in updates:
            updates["name_key"] = db.name_key(updates["name"])
        if "website" in updates:
            updates["domain"] = db.domain_of(updates["website"])
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", (*updates.values(), lid))
        return jsonify(dict(conn.execute("SELECT * FROM leads WHERE id = ?", (lid,)).fetchone()))


@app.delete("/api/leads/<int:lid>")
def delete_lead(lid):
    with db.connect() as conn:
        conn.execute("DELETE FROM leads WHERE id = ?", (lid,))
    return jsonify({"ok": True})


@app.get("/api/leads.csv")
def export_leads():
    status = request.args.get("status")
    with db.connect() as conn:
        if status:
            leads = db.rows(conn.execute("SELECT * FROM leads WHERE status = ? ORDER BY name", (status,)))
        else:
            leads = db.rows(conn.execute("SELECT * FROM leads ORDER BY name"))
    columns = [
        "name", "org_type", "serves", "address", "phone", "email", "website", "contact_form_url",
        "status", "verified_by", "verified_at", "outreach", "source_url", "source_type",
        "source_date", "confidence", "ai_notes", "notes",
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(leads)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=b4p-leads-{stamp}.csv"},
    )


# ---------------------------------------------------------------------------
# Results: the measurements from the proposal
# ---------------------------------------------------------------------------

def _pct(numerator, denominator):
    return round(100 * numerator / denominator, 1) if denominator else None


@app.get("/api/metrics")
def metrics():
    with db.connect() as conn:
        settings = db.get_settings(conn)
        leads = db.rows(conn.execute("SELECT * FROM leads"))
        searches = db.rows(conn.execute("SELECT * FROM searches WHERE status = 'done'"))
        contacted_count = conn.execute("SELECT COUNT(*) FROM contacted").fetchone()[0]

    by_status = {s: 0 for s in db.LEAD_STATUSES}
    by_outreach = {s: 0 for s in db.OUTREACH_STATUSES}
    for lead in leads:
        by_status[lead["status"]] = by_status.get(lead["status"], 0) + 1
        by_outreach[lead["outreach"]] = by_outreach.get(lead["outreach"], 0) + 1

    reviewed = [l for l in leads if l["status"] != "new"]
    verify_times = [l["verify_minutes"] for l in reviewed if l["verify_minutes"] is not None]
    ai_minutes = sum((s["ai_seconds"] or 0) for s in searches) / 60
    orgs_found = sum(s["found_count"] for s in searches)
    avg_verify = sum(verify_times) / len(verify_times) if verify_times else None
    # Team time per org = minutes spent verifying. The AI run happens in the background,
    # but we report its time per org too so the comparison is honest.
    ai_per_org = ai_minutes / orgs_found if orgs_found else None

    sent = by_outreach["sent"] + by_outreach["bounced"] + by_outreach["responded"]

    def num(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return jsonify({
        "leads_total": len(leads),
        "by_status": by_status,
        "by_outreach": by_outreach,
        "contacted_count": contacted_count,
        "searches_run": len(searches),
        "duplicates_blocked": sum(s["duplicate_count"] for s in searches),
        "with_email": sum(1 for l in leads if l["email"]),
        "verified_with_email": sum(1 for l in leads if l["email"] and l["status"] == "verified"),
        "time": {
            "avg_verify_minutes": round(avg_verify, 1) if avg_verify is not None else None,
            "ai_minutes_per_org": round(ai_per_org, 1) if ai_per_org is not None else None,
            "baseline_minutes_per_org": num(settings.get("baseline_minutes_per_org")),
            "verifications_timed": len(verify_times),
        },
        "outreach": {
            "sent": sent,
            "bounce_rate": _pct(by_outreach["bounced"], sent),
            "response_rate": _pct(by_outreach["responded"], sent),
            "baseline_bounce_rate": num(settings.get("baseline_bounce_rate")),
            "baseline_response_rate": num(settings.get("baseline_response_rate")),
        },
        "tokens": {
            "input": sum(s["input_tokens"] for s in searches),
            "output": sum(s["output_tokens"] for s in searches),
            "web_searches": sum(s["web_searches"] for s in searches),
        },
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(host="127.0.0.1", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
