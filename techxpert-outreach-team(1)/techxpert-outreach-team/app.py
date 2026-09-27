"""TechXpert Outreach - hosted team version (Vercel-ready Flask app).

Each user signs in with Google; Gmail connections, campaigns and leads are
scoped to their Google identity. No background threads: sending and chunked
jobs run via POST /api/process-queue (cron).
"""
import io
import os
import time

from flask import Flask, request, redirect, url_for, session, render_template, jsonify, send_file

import config
import db
import auth as authmod
import crypto as cryptomod
import leads as leadmod
import sender as sendermod
import queue_worker
import jobs as jobsmod
import gmail_oauth

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
app.secret_key = config.SECRET_KEY

db.init_db()


def uid():
    return session.get("user_id")


def current_user():
    return db.get_user(uid()) if uid() else None


def require_login():
    if not uid():
        return redirect(url_for("login"))
    return None


# ---------------- auth ----------------

@app.route("/login")
def login():
    if uid():
        return redirect(url_for("dashboard"))
    return render_template("login.html", user=None)


@app.route("/login/start")
def login_start():
    if uid():
        return redirect(url_for("dashboard"))
    try:
        flow = authmod.make_login_flow()
    except RuntimeError as e:
        return render_template("message.html", title="Setup needed", message=str(e),
                               back=None, user=None), 500
    auth_url, state = flow.authorization_url(access_type="online",
                                             include_granted_scopes="false")
    session["login_state"] = state
    return redirect(auth_url)


@app.route("/login/callback")
def login_callback():
    try:
        flow = authmod.make_login_flow(state=session.get("login_state"))
        flow.fetch_token(authorization_response=request.url)
        sub, email, name = authmod.fetch_userinfo(flow.credentials)
    except Exception as e:
        return render_template("message.html", title="Sign-in failed",
                               message=f"Google sign-in failed: {str(e)[:200]}",
                               back=url_for("login"), user=None), 400
    user = db.get_or_create_user(sub, email, name)
    session["user_id"] = user["id"]
    session.pop("login_state", None)
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def index():
    return redirect(url_for("dashboard") if uid() else url_for("login"))


@app.route("/dashboard")
def dashboard():
    r = require_login()
    if r:
        return r
    camps = db.q("SELECT * FROM campaigns WHERE user_id=? ORDER BY id DESC", (uid(),))
    accts = db.q("SELECT * FROM gmail_accounts WHERE user_id=? ORDER BY id", (uid(),))
    stats = db.q("SELECT status, COUNT(*) c FROM send_log WHERE user_id=? GROUP BY status",
                 (uid(),))
    missing = config.check_prod() if os.environ.get("VERCEL") else []
    return render_template("dashboard.html", user=current_user(), campaigns=camps,
                           accounts=accts, stats={s["status"]: s["c"] for s in stats},
                           missing=missing)


# ---------------- Gmail accounts ----------------

@app.route("/accounts")
def accounts():
    r = require_login()
    if r:
        return r
    accts = db.q("SELECT * FROM gmail_accounts WHERE user_id=? ORDER BY id", (uid(),))
    view = []
    for a in accts:
        a = dict(a)
        a.pop("token_enc", None)  # never render tokens
        a["eff_cap"] = sendermod.effective_cap(a)
        a["age_days"] = round(sendermod.account_age_days(a), 1)
        view.append(a)
    return render_template("accounts.html", accounts=view, oauth_ready=config.OAUTH_READY,
                           user=current_user())


@app.route("/oauth/start")
def oauth_start():
    r = require_login()
    if r:
        return r
    try:
        flow = gmail_oauth.make_flow()
    except RuntimeError as e:
        return render_template("message.html", title="OAuth not configured",
                               message=str(e), back=url_for("accounts"),
                               user=current_user()), 400
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent",
                                             include_granted_scopes="true")
    session["oauth_state"] = state
    return redirect(auth_url)


@app.route("/oauth/callback")
def oauth_callback():
    r = require_login()
    if r:
        return r
    try:
        flow = gmail_oauth.make_flow(state=session.get("oauth_state"))
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        # Build a throwaway service to learn the account's email address.
        # The token is encrypted before storage; nothing sensitive is logged.
        import gmail_oauth as go
        service = go.build_service(go.token_json_from_credentials(creds))
        email = go.get_profile_email(service)
        token_enc = cryptomod.encrypt_token(go.token_json_from_credentials(creds))
    except Exception as e:
        return render_template("message.html", title="Gmail connect failed",
                               message=f"Could not complete Google authorization: {str(e)[:200]}",
                               back=url_for("accounts"), user=current_user()), 400
    now = time.time()
    existing = db.q("SELECT id FROM gmail_accounts WHERE user_id=? AND email=?",
                    (uid(), email), one=True)
    if existing:
        db.w("UPDATE gmail_accounts SET token_enc=?, status='active' WHERE id=?",
             (token_enc, existing["id"]))
    else:
        db.w("""INSERT INTO gmail_accounts
                (user_id, email, token_enc, daily_cap, warmup_enabled,
                 warmup_start, status, sent_today, sent_date, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
             (uid(), email, token_enc, 30, 1, now, "active", 0, "", now))
    session.pop("oauth_state", None)
    return redirect(url_for("accounts"))


def _own_account(aid):
    return db.q("SELECT * FROM gmail_accounts WHERE id=? AND user_id=?",
                (aid, uid()), one=True)


@app.route("/account/<int:aid>/pause", methods=["POST"])
def account_pause(aid):
    if _own_account(aid):
        db.w("UPDATE gmail_accounts SET status='paused' WHERE id=?", (aid,))
    return redirect(url_for("accounts"))


@app.route("/account/<int:aid>/resume", methods=["POST"])
def account_resume(aid):
    if _own_account(aid):
        db.w("UPDATE gmail_accounts SET status='active' WHERE id=?", (aid,))
    return redirect(url_for("accounts"))


@app.route("/account/<int:aid>/cap", methods=["POST"])
def account_cap(aid):
    if _own_account(aid):
        try:
            cap = max(1, min(500, int(request.form.get("daily_cap", 30))))
        except ValueError:
            cap = 30
        warmup = 1 if request.form.get("warmup_enabled") else 0
        db.w("UPDATE gmail_accounts SET daily_cap=?, warmup_enabled=? WHERE id=?",
             (cap, warmup, aid))
    return redirect(url_for("accounts"))


@app.route("/account/<int:aid>/bounces", methods=["POST"])
def account_bounces(aid):
    acct = _own_account(aid)
    if not acct:
        return redirect(url_for("accounts"))
    try:
        service = gmail_oauth.get_service(acct)
        res = sendermod.check_account_bounces(uid(), aid, service=service)
        msg = (f"Checked {res['looked_at']} recent sends: {res['bounced_marked']} "
               f"bounces marked, rate {res['bounce_rate']*100:.1f}%."
               + (" Account auto-paused." if res["paused"] else ""))
    except Exception as e:
        msg = f"Bounce scan failed: {str(e)[:200]}"
    return render_template("message.html", title="Bounce scan", message=msg,
                           back=url_for("accounts"), user=current_user())


# ---------------- campaigns & leads ----------------

@app.route("/campaign/new", methods=["GET", "POST"])
def campaign_new():
    r = require_login()
    if r:
        return r
    if request.method == "POST":
        cid = db.w(
            """INSERT INTO campaigns (user_id, name, niche, location, icp_notes,
               subject_tpl, body_tpl, delay_min, delay_max, window_start, window_end,
               dry_run, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (uid(), request.form.get("name", "Untitled campaign"),
             request.form.get("niche", ""), request.form.get("location", ""),
             request.form.get("icp_notes", ""),
             request.form.get("subject_tpl", "Quick question for {business_name}"),
             request.form.get("body_tpl",
                              "Hi {business_name} team,\n\n{Quick question|Had a quick question} about how you handle {niche} jobs in {location}.\n\nWorth a 10-minute chat?\n\nBest"),
             int(request.form.get("delay_min", 60) or 60),
             int(request.form.get("delay_max", 180) or 180),
             request.form.get("window_start", "09:00"), request.form.get("window_end", "17:00"),
             1 if request.form.get("dry_run") else 0, "draft", time.time()))
        return redirect(url_for("campaign", cid=cid))
    return render_template("campaign_new.html", user=current_user())


def _own_campaign(cid):
    return db.q("SELECT * FROM campaigns WHERE id=? AND user_id=?", (cid, uid()), one=True)


@app.route("/campaign/<int:cid>")
def campaign(cid):
    r = require_login()
    if r:
        return r
    camp = _own_campaign(cid)
    if not camp:
        return "Campaign not found", 404
    lead_rows = db.q("SELECT * FROM leads WHERE campaign_id=? ORDER BY id", (cid,))
    qstat = db.q("SELECT status, COUNT(*) c FROM send_queue WHERE campaign_id=? GROUP BY status", (cid,))
    job = db.q("SELECT * FROM jobs WHERE campaign_id=? ORDER BY id DESC LIMIT 1", (cid,), one=True)
    return render_template("campaign.html", campaign=camp, leads=lead_rows,
                           qstat={s["status"]: s["c"] for s in qstat}, job=job,
                           user=current_user())


@app.route("/campaign/<int:cid>/import", methods=["POST"])
def campaign_import(cid):
    r = require_login()
    if r:
        return r
    if not _own_campaign(cid):
        return "Campaign not found", 404
    f = request.files.get("csvfile")
    if not f:
        return "No file", 400
    n, errors = leadmod.import_csv(uid(), cid, f.stream)
    return render_template("message.html", title="CSV import",
                           message=f"Imported {n} leads." + (f" Errors: {'; '.join(errors[:5])}" if errors else ""),
                           back=url_for("campaign", cid=cid), user=current_user())


@app.route("/campaign/<int:cid>/sample", methods=["POST"])
def campaign_sample(cid):
    if not _own_campaign(cid):
        return "Campaign not found", 404
    n, errors = leadmod.import_csv(uid(), cid, io.BytesIO(leadmod.SAMPLE_CSV.encode()), source="sample")
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/websearch", methods=["POST"])
def campaign_websearch(cid):
    r = require_login()
    if r:
        return r
    if not _own_campaign(cid):
        return "Campaign not found", 404
    jobsmod.start_job(uid(), cid, "websearch")
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/add_lead", methods=["POST"])
def campaign_add_lead(cid):
    if not _own_campaign(cid):
        return "Campaign not found", 404
    leadmod.add_manual(uid(), cid, {k: request.form.get(k, "") for k in
                                    ["business_name", "address", "phone", "website",
                                     "email", "category", "notes"]})
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/toggle_lead", methods=["POST"])
def campaign_toggle_lead(cid):
    if not _own_campaign(cid):
        return "Campaign not found", 404
    lid = request.form.get("lead_id")
    sel = 1 if request.form.get("selected") else 0
    # scope the update to this user's leads only
    db.w("UPDATE leads SET selected=? WHERE id=? AND user_id=?", (sel, lid, uid()))
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/validate", methods=["POST"])
def campaign_validate(cid):
    r = require_login()
    if r:
        return r
    if not _own_campaign(cid):
        return "Campaign not found", 404
    jobsmod.start_job(uid(), cid, "validate")
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/enrich", methods=["POST"])
def campaign_enrich(cid):
    r = require_login()
    if r:
        return r
    if not _own_campaign(cid):
        return "Campaign not found", 404
    jobsmod.start_job(uid(), cid, "enrich")
    return redirect(url_for("campaign", cid=cid))


@app.route("/job/<int:jid>/status")
def job_status(jid):
    job = db.q("SELECT * FROM jobs WHERE id=? AND user_id=?", (jid, uid()), one=True)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": job["status"], "total": job["total"],
                    "done": job["done"], "result": job["result"]})


@app.route("/campaign/<int:cid>/template", methods=["POST"])
def campaign_template(cid):
    if not _own_campaign(cid):
        return "Campaign not found", 404
    db.w("""UPDATE campaigns SET subject_tpl=?, body_tpl=?, delay_min=?, delay_max=?,
            window_start=?, window_end=?, dry_run=? WHERE id=? AND user_id=?""",
         (request.form.get("subject_tpl", ""), request.form.get("body_tpl", ""),
          int(request.form.get("delay_min", 60) or 60),
          int(request.form.get("delay_max", 180) or 180),
          request.form.get("window_start", "09:00"), request.form.get("window_end", "17:00"),
          1 if request.form.get("dry_run") else 0, cid, uid()))
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/preview", methods=["POST"])
def campaign_preview(cid):
    camp = _own_campaign(cid)
    if not camp:
        return jsonify({"error": "not found"}), 404
    lead = db.q("SELECT * FROM leads WHERE campaign_id=? AND selected=1 ORDER BY id LIMIT 1",
                (cid,), one=True)
    if not lead:
        return jsonify({"error": "no leads"})
    import random as _r
    rng = _r.Random(0)
    return jsonify({
        "to": lead["email"], "business": lead["business_name"],
        "subject": sendermod.render_template(camp["subject_tpl"], lead, rng),
        "body": sendermod.render_template(camp["body_tpl"], lead, rng),
    })


@app.route("/campaign/<int:cid>/enqueue", methods=["POST"])
def campaign_enqueue(cid):
    camp = _own_campaign(cid)
    if not camp:
        return "Campaign not found", 404
    n = queue_worker.enqueue_campaign(uid(), cid)
    dry = db.q("SELECT dry_run FROM campaigns WHERE id=?", (cid,), one=True)["dry_run"]
    return render_template("message.html", title="Sending started",
                           message=f"Queued {n} leads. The scheduler cron processes them: "
                                   f"randomized delays inside the sending window, rotating Gmail "
                                   f"accounts, never exceeding caps."
                                   + (" DRY-RUN is ON: nothing will actually be sent." if dry else ""),
                           back=url_for("campaign", cid=cid), user=current_user())


@app.route("/campaign/<int:cid>/stop", methods=["POST"])
def campaign_stop(cid):
    if _own_campaign(cid):
        db.w("UPDATE campaigns SET status='stopped' WHERE id=?", (cid,))
    return redirect(url_for("campaign", cid=cid))


@app.route("/logs")
def logs():
    r = require_login()
    if r:
        return r
    rows = db.q("""SELECT l.*, c.name AS campaign_name, a.email AS account_email,
                          le.business_name AS business
                   FROM send_log l
                   LEFT JOIN campaigns c ON c.id=l.campaign_id
                   LEFT JOIN gmail_accounts a ON a.id=l.account_id
                   LEFT JOIN leads le ON le.id=l.lead_id
                   WHERE l.user_id=? ORDER BY l.id DESC LIMIT 300""", (uid(),))
    return render_template("logs.html", logs=rows, user=current_user())


@app.route("/sample.csv")
def sample_csv():
    return send_file(io.BytesIO(leadmod.SAMPLE_CSV.encode()),
                     mimetype="text/csv", as_attachment=True,
                     download_name="sample_leads.csv")


# ---------------- cron endpoint ----------------

def _cron_authorized():
    """Vercel Cron sends `Authorization: Bearer $CRON_SECRET` automatically when
    CRON_SECRET is set. External crons (cron-job.org) can send the same header
    or X-Cron-Secret."""
    if not config.CRON_SECRET:
        return False
    auth = request.headers.get("Authorization", "")
    if auth == f"Bearer {config.CRON_SECRET}":
        return True
    return request.headers.get("X-Cron-Secret", "") == config.CRON_SECRET


@app.route("/api/process-queue", methods=["GET", "POST"])
def api_process_queue():
    if not _cron_authorized():
        return jsonify({"error": "unauthorized"}), 401
    try:
        return jsonify(queue_worker.process_all())
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/api/process-now", methods=["POST"])
def api_process_now():
    """Manual trigger for local dev / debugging (login required, no cron secret)."""
    r = require_login()
    if r:
        return r
    try:
        return jsonify(queue_worker.process_all())
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  TechXpert Outreach (team) -> http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
