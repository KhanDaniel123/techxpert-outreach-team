"""TechXpert Outreach - hosted team version (Vercel-ready Flask app).

Each user registers with email + password; sender accounts, campaigns and
leads are scoped to their login. No Google OAuth anywhere: login is built
in (auth.py) and sending is direct Gmail SMTP with per-user App Passwords
(smtp_mail.py). No background threads: sending and chunked jobs run via
POST /api/process-queue (cron).
"""
import csv
import hashlib
import io
import os
import re
import smtplib
import time
from datetime import datetime

from flask import Flask, request, redirect, url_for, session, render_template, jsonify, send_file, Response

import config
import db
import auth as authmod
import crypto as cryptomod
import leads as leadmod
import sender as sendermod
import smtp_mail
import followups as followupsmod
import queue_worker
import jobs as jobsmod
import ai_writer as aimod
import pipeline as pipelinemod
import decision_makers as decisionmakersmod
import unsubscribe as unsubmod

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"))
app.secret_key = config.SECRET_KEY

db.init_db()


def uid():
    return session.get("user_id")


def _safe_int(v, default):
    """Parse a form number, falling back to the default instead of 500ing
    when the user types text into a number field."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def current_user():
    return db.get_user(uid()) if uid() else None


def require_login():
    if not uid():
        return redirect(url_for("login"))
    return None


# ---------------- auth (built-in email + password) ----------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if uid():
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        user = authmod.verify_login(request.form.get("email", ""),
                                    request.form.get("password", ""))
        if user:
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        error = "Wrong email or password."
    return render_template("login.html", user=None, error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    if uid():
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        ok, payload = authmod.register_user(request.form.get("email", ""),
                                            request.form.get("name", ""),
                                            request.form.get("password", ""))
        if ok:
            session["user_id"] = payload["id"]
            return redirect(url_for("dashboard"))
        error = payload
    return render_template("register.html", user=None, error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    message = None
    error = None
    reset_link = None
    if request.method == "POST":
        email = request.form.get("email", "")
        ok, msg = authmod.request_password_reset(email)
        if ok:
            message = msg
        elif uid():
            # The account has no usable mail sender, but the requester
            # holds a valid session: show the one-time link on screen
            # instead of erroring. See auth.issue_reset_token_for_display.
            token, _user = authmod.issue_reset_token_for_display(email)
            if token:
                reset_link = url_for("reset_password", token=token,
                                     _external=True)
            else:
                message = authmod.RESET_GENERIC_MESSAGE
        else:
            error = msg
    return render_template("forgot_password.html", user=current_user(),
                           message=message, error=error, reset_link=reset_link)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if uid():
        return redirect(url_for("dashboard"))
    error = None
    if request.method == "POST":
        ok, msg = authmod.redeem_password_reset(token,
                                                request.form.get("password", ""))
        if ok:
            tip = ""
            trow = db.q("SELECT user_id FROM password_resets WHERE token_hash=?",
                        (hashlib.sha256(token.encode("utf-8")).hexdigest(),),
                        one=True)
            if trow and not db.q("SELECT id FROM sender_accounts "
                                 "WHERE user_id=? LIMIT 1", (trow["user_id"],),
                                 one=True):
                tip = (" Tip: connect a Gmail sender under Sender accounts so "
                       "future password resets can be emailed to you.")
            return render_template("message.html", title="Password updated",
                                   message=msg + " You can now log in." + tip,
                                   back=url_for("login"), user=None)
        error = msg
    return render_template("reset_password.html", user=None,
                           error=error, token=token)


@app.route("/")
def index():
    return redirect(url_for("dashboard") if uid() else url_for("login"))


@app.route("/dashboard")
def dashboard():
    r = require_login()
    if r:
        return r
    camps = db.q("SELECT * FROM campaigns WHERE user_id=? ORDER BY id DESC", (uid(),))
    accts = db.q("SELECT * FROM sender_accounts WHERE user_id=? ORDER BY id", (uid(),))
    stats = db.q("SELECT status, COUNT(*) c FROM send_log WHERE user_id=? GROUP BY status",
                 (uid(),))
    missing = config.check_prod() if os.environ.get("VERCEL") else []
    replied_leads = db.q("""
        SELECT l.id, l.business_name, l.email, l.campaign_id,
               c.name AS campaign_name,
               (SELECT n.snippet FROM notifications n
                 WHERE n.lead_id=l.id AND n.kind='reply'
                 ORDER BY n.id DESC LIMIT 1) AS snippet,
               COALESCE((SELECT n.created_at FROM notifications n
                 WHERE n.lead_id=l.id AND n.kind='reply'
                 ORDER BY n.id DESC LIMIT 1), 0) AS replied_at
        FROM leads l LEFT JOIN campaigns c ON c.id=l.campaign_id
        WHERE l.user_id=? AND l.replied=1 AND (l.handled IS NULL OR l.handled=0)
        ORDER BY 7 DESC, l.id DESC""", (uid(),))
    for row in replied_leads:
        row["when"] = (time.strftime("%b %d, %H:%M", time.localtime(row["replied_at"]))
                       if row["replied_at"] else "")
    return render_template("dashboard.html", user=current_user(), campaigns=camps,
                           accounts=accts, stats={s["status"]: s["c"] for s in stats},
                           missing=missing, replied_leads=replied_leads)


@app.context_processor
def _inject_unread():
    """Unread notification count for the header bell, on every page."""
    try:
        u = current_user()
        if u:
            n = db.q("SELECT COUNT(*) c FROM notifications "
                     "WHERE user_id=? AND read_at IS NULL", (u["id"],), one=True)
            return {"unread": (n["c"] if n else 0)}
    except Exception:
        pass
    return {"unread": 0}


@app.route("/notifications")
def notifications():
    r = require_login()
    if r:
        return r
    rows = db.q("""SELECT n.*, c.name AS campaign_name, l.business_name, l.email
                   FROM notifications n
                   LEFT JOIN campaigns c ON c.id=n.campaign_id
                   LEFT JOIN leads l ON l.id=n.lead_id
                   WHERE n.user_id=? ORDER BY n.id DESC LIMIT 100""", (uid(),))
    for row in rows:
        row["when"] = time.strftime("%b %d, %H:%M", time.localtime(row["created_at"]))
    # Opening the page marks everything read.
    db.w("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",
         (time.time(), uid()))
    return render_template("notifications.html", user=current_user(), notes=rows)


@app.route("/lead/<int:lid>/handled", methods=["POST"])
def lead_handled(lid):
    r = require_login()
    if r:
        return r
    lead = db.q("SELECT id FROM leads WHERE id=? AND user_id=?", (lid, uid()), one=True)
    if lead:
        db.w("UPDATE leads SET handled=1 WHERE id=?", (lid,))
    return redirect(url_for("dashboard"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    r = require_login()
    if r:
        return r
    if request.method == "POST":
        name = (request.form.get("company_name") or "").strip()
        addr = (request.form.get("company_address") or "").strip()
        db.save_user_settings(uid(), name, addr)
        return redirect(url_for("settings"))
    company_name, company_address = unsubmod.get_company_info(uid())
    return render_template("settings.html", user=current_user(),
                           ai_on=config.ai_enabled(), ai_model=config.ai_model_name(),
                           company_name=company_name, company_address=company_address,
                           address_placeholder=unsubmod.address_is_placeholder(uid()),
                           placeholder_text=unsubmod.ADDRESS_PLACEHOLDER)


# ---------------- self-serve connection tests ----------------

@app.route("/settings/test-gmail", methods=["POST"])
def settings_test_gmail():
    """Send a real test email through the user's connected Gmail account.

    This is NOT a campaign send: it only ever goes to the address the user
    types, and it works with dry-run on. The stored App Password is decrypted
    server-side only for the SMTP login and is never displayed or logged.
    """
    r = require_login()
    if r:
        return r
    to_addr = (request.form.get("to_addr") or "").strip()
    if not smtp_mail.EMAIL_RE.fullmatch(to_addr or ""):
        return render_template("message.html", title="Test not sent",
                               message="That does not look like a valid email address. "
                                       "Type a real address and try again.",
                               back=url_for("settings"), user=current_user())
    acct = db.q("SELECT * FROM sender_accounts WHERE user_id=? AND status='active' "
                "ORDER BY id LIMIT 1", (uid(),), one=True)
    if not acct:
        return render_template("message.html", title="Gmail not connected",
                               message="No Gmail account is connected yet. Connect one on "
                                       "the Accounts page, then run this test again.",
                               back=url_for("accounts"), user=current_user())
    subject = "TechXpert Outreach: Gmail connection test"
    body = ("This is a connection test from your TechXpert Outreach app.\n\n"
            "If you are reading this, your Gmail account is connected correctly "
            "and the app can send through it.\n\n"
            "No action needed. This was not sent to any lead.")
    try:
        smtp_mail.send_message(acct, to_addr, subject, body)
    except smtplib.SMTPAuthenticationError:
        return render_template("message.html", title="Gmail login failed",
                               message="Gmail rejected the login. The App Password may have "
                                       "changed or been revoked. Reconnect the account on the "
                                       "Accounts page and try again.",
                               back=url_for("accounts"), user=current_user())
    except Exception as e:
        return render_template("message.html", title="Could not reach Gmail",
                               message=f"The test email could not be sent: {str(e)[:160]} "
                                       "Check your connection and try again.",
                               back=url_for("settings"), user=current_user())
    return render_template("message.html", title="Test email sent",
                           message=f"A test email is on its way to {to_addr}. "
                                   "Check the inbox (and spam folder) in a minute.",
                           back=url_for("settings"), user=current_user())


SAMPLE_TEST_LEAD = {"business_name": "Sample Fitness Studio",
                    "category": "fitness studios",
                    "address": "Berlin, Germany",
                    "website": "https://example.com"}
SAMPLE_TEST_CAMPAIGN = {"niche": "fitness studios", "location": "Berlin, Germany"}
SAMPLE_TEST_FINDINGS = ["The website has no online booking page.",
                        "Class times are listed as a PDF download."]


@app.route("/settings/test-ai", methods=["POST"])
def settings_test_ai():
    """Generate a sample subject line + opener with the stored AI key.

    Uses a fictional sample business, displays the draft, never reveals the
    key. Works with dry-run on.
    """
    r = require_login()
    if r:
        return r
    if not config.ai_enabled():
        return render_template("message.html", title="AI writing is off",
                               message="No AI key is configured. Add a GEMINI_API_KEY "
                                       "(free from Google AI Studio) or an OPENAI_API_KEY "
                                       "in Vercel under Settings, then redeploy, and run "
                                       "this test again. Your key is never shown in the app.",
                               back=url_for("settings"), user=current_user())
    try:
        subject, body, _usage = aimod.generate_email(
            SAMPLE_TEST_LEAD, SAMPLE_TEST_CAMPAIGN, SAMPLE_TEST_FINDINGS)
    except Exception as e:
        return render_template("message.html", title="AI test failed",
                               message=f"The AI service did not return a draft: {str(e)[:160]} "
                                       "Check the API key and try again.",
                               back=url_for("settings"), user=current_user())
    return render_template("test_ai_result.html", user=current_user(),
                           model=config.ai_model_name(),
                           sample=SAMPLE_TEST_LEAD, findings=SAMPLE_TEST_FINDINGS,
                           subject=subject, body=body)


# ---------------- one-click unsubscribe (public, no login) ----------------

@app.route("/unsubscribe")
def unsubscribe():
    """Public one-click unsubscribe. The signed token identifies the lead;
    no login is required (recipients are not app users)."""
    token = (request.args.get("t") or "").strip()
    info = unsubmod.verify_token(token)
    if not info:
        return render_template("unsubscribe.html", ok=False), 400
    unsubmod.mark_unsubscribed(info["lead_id"], info["user_id"])
    return render_template("unsubscribe.html", ok=True)


# ---------------- sender accounts (Gmail SMTP via App Password) ----------------

@app.route("/accounts")
def accounts():
    r = require_login()
    if r:
        return r
    accts = db.q("SELECT * FROM sender_accounts WHERE user_id=? ORDER BY id", (uid(),))
    view = []
    for a in accts:
        a = dict(a)
        a.pop("password_enc", None)  # never render secrets
        a["eff_cap"] = sendermod.effective_cap(a)
        a["age_days"] = round(sendermod.account_age_days(a), 1)
        view.append(a)
    return render_template("accounts.html", accounts=view, user=current_user())


@app.route("/accounts/add", methods=["POST"])
def accounts_add():
    r = require_login()
    if r:
        return r
    email = (request.form.get("email", "") or "").strip().lower()
    raw_pw = request.form.get("app_password", "") or ""
    pw = smtp_mail.clean_app_password(raw_pw)
    if "@" not in email or "." not in email.split("@")[-1]:
        return render_template("message.html", title="Could not add account",
                               message="Enter a valid Gmail address.",
                               back=url_for("accounts"), user=current_user()), 400
    if not smtp_mail.valid_app_password(pw):
        return render_template("message.html", title="Could not add account",
                               message=("That does not look like a 16-character App Password. "
                                        "Create one at myaccount.google.com > Security > App "
                                        "passwords, then paste all 16 characters (spaces are fine)."),
                               back=url_for("accounts"), user=current_user()), 400
    ok, err = smtp_mail.verify_credentials(email, pw)
    if not ok:
        return render_template("message.html", title="Gmail login failed", message=err,
                               back=url_for("accounts"), user=current_user()), 400
    pw_enc = cryptomod.encrypt_token(pw)
    now = time.time()
    existing = db.q("SELECT id FROM sender_accounts WHERE user_id=? AND email=?",
                    (uid(), email), one=True)
    if existing:
        db.w("UPDATE sender_accounts SET password_enc=?, status='active' WHERE id=?",
             (pw_enc, existing["id"]))
    else:
        db.w("""INSERT INTO sender_accounts
                (user_id, email, password_enc, daily_cap, warmup_enabled,
                 warmup_start, status, sent_today, sent_date, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
             (uid(), email, pw_enc, 30, 1, now, "active", 0, "", now))
    return redirect(url_for("accounts"))


def _own_account(aid):
    return db.q("SELECT * FROM sender_accounts WHERE id=? AND user_id=?",
                (aid, uid()), one=True)


@app.route("/account/<int:aid>/pause", methods=["POST"])
def account_pause(aid):
    r = require_login()
    if r:
        return r
    if _own_account(aid):
        db.w("UPDATE sender_accounts SET status='paused' WHERE id=?", (aid,))
    return redirect(url_for("accounts"))


@app.route("/account/<int:aid>/resume", methods=["POST"])
def account_resume(aid):
    r = require_login()
    if r:
        return r
    if _own_account(aid):
        db.w("UPDATE sender_accounts SET status='active' WHERE id=?", (aid,))
    return redirect(url_for("accounts"))


@app.route("/account/<int:aid>/cap", methods=["POST"])
def account_cap(aid):
    r = require_login()
    if r:
        return r
    if _own_account(aid):
        try:
            cap = max(1, min(500, int(request.form.get("daily_cap", 30))))
        except ValueError:
            cap = 30
        warmup = 1 if request.form.get("warmup_enabled") else 0
        db.w("UPDATE sender_accounts SET daily_cap=?, warmup_enabled=? WHERE id=?",
             (cap, warmup, aid))
    return redirect(url_for("accounts"))


@app.route("/account/<int:aid>/bounces", methods=["POST"])
def account_bounces(aid):
    acct = _own_account(aid)
    if not acct:
        return redirect(url_for("accounts"))
    try:
        res = sendermod.check_account_bounces(uid(), aid)
        # Reply detection rides the same inbox visit: any lead address seen in
        # recent non-bounce mail is marked replied, stopping their sequence.
        # Newly replied leads also get a notification row + an email to the user.
        import smtp_mail as _sm
        new_replies, notified = [], 0
        try:
            new_replies = sendermod.mark_replies(uid(), _sm.scan_replies(acct))
            notified = sendermod.notify_replies(uid(), acct, new_replies)
        except Exception:
            pass
        msg = (f"Checked {res['looked_at']} recent sends: {res['bounced_marked']} "
               f"bounces marked, rate {res['bounce_rate']*100:.1f}%; "
               f"{len(new_replies)} lead(s) marked as replied, "
               f"{notified} notification(s) created."
               + (" Account auto-paused." if res["paused"] else ""))
    except Exception as e:
        msg = f"Inbox scan failed: {str(e)[:200]}"
    return render_template("message.html", title="Inbox scan", message=msg,
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
             _safe_int(request.form.get("delay_min") or 60, 60),
             _safe_int(request.form.get("delay_max") or 180, 180),
             request.form.get("window_start", "09:00"), request.form.get("window_end", "17:00"),
             1 if request.form.get("dry_run") else 0, "draft", time.time()))
        # Seed the 5 default follow-up steps so the sequence works out of the box.
        followupsmod.seed_defaults(cid, followupsmod.DEFAULT_COUNT)
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
    fu_count = camp.get("followup_count") or followupsmod.DEFAULT_COUNT
    fu_list = followupsmod.get_followups(cid, fu_count)
    fu_map = {f["step"]: f for f in fu_list}
    seq = _lead_seq_status(cid, fu_count)
    ai_on = config.ai_enabled()
    ai_cost, ai_written = (aimod.avg_cost_per_lead(cid) if camp.get("autopilot")
                           else (0.0, 0))
    pipe_stats = pipelinemod.stats(cid)
    dm_lead_counts = decisionmakersmod.lead_dm_counts(cid)
    pipe_stage = (camp.get("pipeline_stage") or "discover").strip() or "discover"
    pipe_stage_label = pipelinemod.STAGE_LABELS.get(pipe_stage, pipe_stage)
    import crm as _crm
    activity = _crm.campaign_activity(cid)
    return render_template("campaign.html", campaign=camp, leads=lead_rows,
                           qstat={s["status"]: s["c"] for s in qstat}, job=job,
                           fu_list=fu_list, fu_map=fu_map, fu_count=fu_count,
                           fu_defaults=followupsmod.DEFAULT_FOLLOWUPS, seq=seq,
                           ai_on=ai_on, ai_cost=ai_cost, ai_written=ai_written,
                           pipe=pipe_stats, pipe_stage=pipe_stage,
                           pipe_stage_label=pipe_stage_label,
                           dm_lead_counts=dm_lead_counts, activity=activity,
                           user=current_user())


@app.route("/campaign/<int:cid>/export/daily")
def campaign_export_daily(cid):
    """Download today's discovered leads as CSV, for manual LinkedIn/IG outreach.

    Columns: business, website, city/address, discovery date, official social
    URLs (self-published on the business's own site), verified decision makers
    (name + title), email + verdict. Only leads discovered today (server date).
    """
    r = require_login()
    if r:
        return r
    camp = _own_campaign(cid)
    if not camp:
        return "Campaign not found", 404
    day_start = datetime.now().replace(hour=0, minute=0, second=0,
                                       microsecond=0).timestamp()
    rows = db.q("SELECT * FROM leads WHERE campaign_id=? AND created_at>=? ORDER BY id",
                (cid, day_start))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["business_name", "website", "city/address", "discovery_date",
                "linkedin_url", "instagram_url", "facebook_url",
                "decision_makers", "email", "email_verdict"])
    for l in rows:
        dms = [c for c in decisionmakersmod.get_contacts(l["id"]) if c.get("verified")]
        dm_str = "; ".join(
            ((c.get("name") or "").strip()
             + (", " + (c.get("title") or "").strip() if (c.get("title") or "").strip() else ""))
            for c in dms if (c.get("name") or "").strip())
        w.writerow([
            l.get("business_name") or "", l.get("website") or "",
            l.get("address") or "",
            datetime.fromtimestamp(l.get("created_at") or 0).strftime("%Y-%m-%d"),
            l.get("linkedin_url") or "", l.get("instagram_url") or "",
            l.get("facebook_url") or "", dm_str,
            l.get("email") or "", l.get("email_verdict") or ""])
    slug = re.sub(r"[^a-z0-9]+", "-", (camp.get("name") or "campaign").lower()).strip("-") or "campaign"
    fname = f"{slug}-{datetime.now().strftime('%Y-%m-%d')}.csv"
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.route("/lead/<int:lid>")
def lead_detail(lid):
    r = require_login()
    if r:
        return r
    lead = db.q("SELECT * FROM leads WHERE id=?", (lid,), one=True)
    if not lead:
        return "Lead not found", 404
    camp = db.q("SELECT * FROM campaigns WHERE id=? AND user_id=?",
                (lead["campaign_id"], uid()), one=True)
    if not camp:
        return "Lead not found", 404
    contacts = decisionmakersmod.get_contacts(lid)
    import crm as _crm
    events = _crm.lead_timeline(lid)
    return render_template("lead.html", lead=lead, campaign=camp,
                           contacts=contacts, events=events, user=current_user())


def _lead_seq_status(cid, fu_count):
    """Per-lead sequence state for the campaign page: which step each lead is on."""
    rows = db.q(
        """SELECT l.id AS lid, l.replied,
             (SELECT MAX(step) FROM send_log s WHERE s.campaign_id=? AND s.lead_id=l.id
               AND s.status IN ('sent','dry-run')) AS max_sent,
             (SELECT MIN(step) FROM send_queue q WHERE q.campaign_id=? AND q.lead_id=l.id
               AND q.status='pending') AS next_step,
             (SELECT COUNT(*) FROM send_log b WHERE b.campaign_id=? AND b.lead_id=l.id
               AND b.status='bounced') AS bounced
           FROM leads l WHERE l.campaign_id=?""",
        (cid, cid, cid, cid))

    def name(s):
        return "Initial" if s == 0 else f"F{s}"

    out = {}
    for r in rows:
        if r["replied"]:
            out[r["lid"]] = ("Replied", "ok")
        elif r["bounced"]:
            out[r["lid"]] = ("Bounced", "bad")
        else:
            ms, ns = r["max_sent"], r["next_step"]
            if ns is not None:
                out[r["lid"]] = ((f"{name(ms)} sent, {name(ns)} queued" if ms is not None
                                  else f"{name(ns)} queued"), "mut")
            elif ms is not None:
                out[r["lid"]] = ((f"{name(ms)} sent, done" if ms >= fu_count
                                  else f"{name(ms)} sent"), "ok")
            else:
                out[r["lid"]] = ("-", "mut")
    return out


@app.route("/campaign/<int:cid>/import", methods=["POST"])
def campaign_import(cid):
    r = require_login()
    if r:
        return r
    if not _own_campaign(cid):
        return "Campaign not found", 404
    f = request.files.get("csvfile")
    if not f or not f.filename:
        return ("Choose a CSV file first, then click Import. "
                "No file was uploaded, so nothing was imported."), 400
    n, merged, errors = leadmod.import_csv(uid(), cid, f.stream)
    msg = f"Imported {n} leads."
    if merged:
        msg += f" {merged} duplicate row(s) merged into existing leads."
    return render_template("message.html", title="CSV import",
                           message=msg + (f" Errors: {'; '.join(errors[:5])}" if errors else ""),
                           back=url_for("campaign", cid=cid), user=current_user())


@app.route("/campaign/<int:cid>/sample", methods=["POST"])
def campaign_sample(cid):
    if not _own_campaign(cid):
        return "Campaign not found", 404
    n, merged, errors = leadmod.import_csv(uid(), cid, io.BytesIO(leadmod.SAMPLE_CSV.encode()), source="sample")
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
                                     "email", "category", "notes", "personalized_line"]})
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


@app.route("/campaign/<int:cid>/leads/delete", methods=["POST"])
def campaign_delete_leads(cid):
    """Delete selected leads (by id) from a campaign.

    Only leads belonging to this user's campaign are touched. Each
    deleted lead also loses its unsent queue items and cached AI
    drafts, so no orphans remain. Send history (send_log) is kept.
    """
    r = require_login()
    if r:
        return r
    if not _own_campaign(cid):
        return "Campaign not found", 404
    user_id = uid()
    ids = []
    for x in request.form.getlist("lead_id"):
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            continue
    if ids:
        placeholders = ",".join("?" for _ in ids)
        owned = [row["id"] for row in db.q(
            f"SELECT id FROM leads WHERE campaign_id=? AND user_id=? AND id IN ({placeholders})",
            (cid, user_id, *ids))]
        if owned:
            ph = ",".join("?" for _ in owned)
            import crm as _crm
            for row in db.q(
                    f"SELECT id, business_name FROM leads WHERE id IN ({ph}) AND user_id=?",
                    (*owned, user_id)):
                _crm.log_event(row["id"], "suppressed",
                               f"Lead suppressed (deleted) by the user: "
                               f"{row['business_name'] or 'unnamed lead'}",
                               meta={"campaign_id": cid})
            db.w(f"DELETE FROM send_queue WHERE lead_id IN ({ph}) AND status='pending'", owned)
            db.w(f"DELETE FROM ai_content WHERE lead_id IN ({ph})", owned)
            db.w(f"DELETE FROM leads WHERE id IN ({ph}) AND user_id=?", (*owned, user_id))
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


@app.route("/campaign/<int:cid>/autopilot", methods=["POST"])
def campaign_autopilot(cid):
    r = require_login()
    if r:
        return r
    camp = _own_campaign(cid)
    if not camp:
        return "Campaign not found", 404
    if not config.ai_enabled():
        return render_template("message.html", title="AI writing is off",
                               message="Add GEMINI_API_KEY (free from Google AI Studio) or "
                                       "OPENAI_API_KEY in Vercel (Settings > Environment "
                                       "Variables) and redeploy to turn on AI writing.",
                               back=url_for("campaign", cid=cid), user=current_user())
    if not camp.get("autopilot"):
        return render_template("message.html", title="Autopilot is off",
                               message="Turn on the Autopilot toggle in the campaign settings "
                                       "first, then start AI writing.",
                               back=url_for("campaign", cid=cid), user=current_user())
    jobsmod.start_job(uid(), cid, "autopilot")
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/pipeline_save", methods=["POST"])
def campaign_pipeline_save(cid):
    r = require_login()
    if r:
        return r
    if not _own_campaign(cid):
        return "Campaign not found", 404
    try:
        target = int(request.form.get("pipeline_target_leads", 50) or 50)
    except ValueError:
        target = 50
    target = max(5, min(500, target))
    try:
        daily = int(request.form.get("daily_discovery_target", 100) or 100)
    except ValueError:
        daily = 100
    daily = max(10, min(1000, daily))
    try:
        dm_max = int(request.form.get("dm_max_contacts", 3) or 3)
    except ValueError:
        dm_max = 3
    dm_max = max(1, min(4, dm_max))
    db.w("UPDATE campaigns SET niche=?, location=?, pipeline_target_leads=?, "
         "daily_discovery_target=?, dm_enabled=?, dm_max_contacts=? "
         "WHERE id=? AND user_id=?",
         (request.form.get("niche", "").strip()[:120],
          request.form.get("location", "").strip()[:120],
          target, daily,
          1 if request.form.get("dm_enabled") else 0, dm_max,
          cid, uid()))
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/pipeline_start", methods=["POST"])
def campaign_pipeline_start(cid):
    r = require_login()
    if r:
        return r
    camp = _own_campaign(cid)
    if not camp:
        return "Campaign not found", 404
    if not (camp.get("niche") or "").strip() or not (camp.get("location") or "").strip():
        return render_template("message.html", title="Niche and location needed",
                               message="Type a niche and a location in the pipeline box "
                                       "first, then start. The pipeline searches the web "
                                       "for \"{niche} {location}\".",
                               back=url_for("campaign", cid=cid), user=current_user())
    db.w("UPDATE campaigns SET pipeline_enabled=1, pipeline_stage='discover', "
         "pipeline_cursor='{}' WHERE id=? AND user_id=?", (cid, uid()))
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/pipeline_pause", methods=["POST"])
def campaign_pipeline_pause(cid):
    r = require_login()
    if r:
        return r
    if not _own_campaign(cid):
        return "Campaign not found", 404
    db.w("UPDATE campaigns SET pipeline_enabled=0 WHERE id=? AND user_id=?",
         (cid, uid()))
    return redirect(url_for("campaign", cid=cid))


@app.route("/job/<int:jid>/status")
def job_status(jid):
    r = require_login()
    if r:
        return r
    job = db.q("SELECT * FROM jobs WHERE id=? AND user_id=?", (jid, uid()), one=True)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": job["status"], "total": job["total"],
                    "done": job["done"], "result": job["result"]})


@app.route("/campaign/<int:cid>/template", methods=["POST"])
def campaign_template(cid):
    r = require_login()
    if r:
        return r
    if not _own_campaign(cid):
        return "Campaign not found", 404
    db.w("""UPDATE campaigns SET subject_tpl=?, body_tpl=?, delay_min=?, delay_max=?,
            window_start=?, window_end=?, dry_run=? WHERE id=? AND user_id=?""",
         (request.form.get("subject_tpl", ""), request.form.get("body_tpl", ""),
          _safe_int(request.form.get("delay_min") or 60, 60),
          _safe_int(request.form.get("delay_max") or 180, 180),
          request.form.get("window_start", "09:00"), request.form.get("window_end", "17:00"),
          1 if request.form.get("dry_run") else 0, cid, uid()))
    # Follow-up sequence settings. Only touch follow-up rows when the form
    # actually carried follow-up fields (so API-style saves can't wipe them).
    if "followup_count" in request.form:
        fu_enabled = 1 if request.form.get("followups_enabled") else 0
        try:
            fu_count = int(request.form.get("followup_count", 5) or 5)
        except ValueError:
            fu_count = 5
        fu_count = max(1, min(followupsmod.MAX_STEPS, fu_count))
        try:
            delay_h = int(request.form.get("followup_delay_hours", 40) or 40)
        except ValueError:
            delay_h = 40
        delay_h = max(1, min(720, delay_h))
        db.w("UPDATE campaigns SET followups_enabled=?, followup_count=?, "
             "followup_delay_hours=? WHERE id=? AND user_id=?",
             (fu_enabled, fu_count, delay_h, cid, uid()))
        fu_count = followupsmod.set_count(cid, fu_count)
        if any(f"fu_body_{s}" in request.form for s in range(1, followupsmod.MAX_STEPS + 1)):
            for s in range(1, fu_count + 1):
                followupsmod.upsert_followup(cid, s, request.form.get(f"fu_subject_{s}", ""),
                                             request.form.get(f"fu_body_{s}", ""))
    # Autopilot settings. The toggle only sticks when an API key is present;
    # without one the campaign silently stays on normal templates.
    autopilot = 1 if (request.form.get("autopilot") and config.ai_enabled()) else 0
    mode = request.form.get("followup_mode", "fixed")
    if mode not in ("fixed", "until_reply"):
        mode = "fixed"
    try:
        max_touches = int(request.form.get("max_touches", 7) or 7)
    except ValueError:
        max_touches = 7
    max_touches = max(1, min(20, max_touches))
    db.w("UPDATE campaigns SET autopilot=?, followup_mode=?, max_touches=? "
         "WHERE id=? AND user_id=?", (autopilot, mode, max_touches, cid, uid()))
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/preview", methods=["POST"])
def campaign_preview(cid):
    r = require_login()
    if r:
        return r
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
                                   f"randomized delays inside the sending window, rotating sender "
                                   f"accounts, never exceeding caps. Follow-ups (if enabled) send "
                                   f"one step at a time after the per-campaign interval, and stop "
                                   f"for any lead that replies or bounces."
                                   + (" DRY-RUN is ON: nothing will actually be sent." if dry else ""),
                           back=url_for("campaign", cid=cid), user=current_user())


@app.route("/campaign/<int:cid>/stop", methods=["POST"])
def campaign_stop(cid):
    if _own_campaign(cid):
        db.w("UPDATE campaigns SET status='stopped' WHERE id=?", (cid,))
    return redirect(url_for("campaign", cid=cid))


@app.route("/campaign/<int:cid>/resume", methods=["POST"])
def campaign_resume(cid):
    """Set a stopped campaign back to sending so the scheduler picks it up."""
    if _own_campaign(cid):
        db.w("UPDATE campaigns SET status='sending' WHERE id=?", (cid,))
    return redirect(url_for("campaign", cid=cid))


def _delete_campaign_data(cid, user_id):
    """Remove a campaign and everything that belongs to it.

    Removed: the campaign row, its leads, its queued (unsent) items,
    its follow-up templates, and cached AI drafts for its leads.
    Kept: send history (send_log), notifications, and job records, so the
    past is never rewritten. Returns (leads, queued, followups) counts.
    """
    lead_ids = [r["id"] for r in db.q(
        "SELECT id FROM leads WHERE campaign_id=? AND user_id=?", (cid, user_id))]
    queued = (db.q("SELECT COUNT(*) c FROM send_queue WHERE campaign_id=?",
                   (cid,), one=True) or {}).get("c", 0)
    followups = (db.q("SELECT COUNT(*) c FROM followups WHERE campaign_id=?",
                      (cid,), one=True) or {}).get("c", 0)
    db.w("DELETE FROM send_queue WHERE campaign_id=?", (cid,))
    if lead_ids:
        placeholders = ",".join("?" for _ in lead_ids)
        db.w(f"DELETE FROM ai_content WHERE lead_id IN ({placeholders})", lead_ids)
    db.w("DELETE FROM followups WHERE campaign_id=?", (cid,))
    db.w("DELETE FROM leads WHERE campaign_id=? AND user_id=?", (cid, user_id))
    db.w("DELETE FROM campaigns WHERE id=? AND user_id=?", (cid, user_id))
    return len(lead_ids), queued, followups


@app.route("/campaign/<int:cid>/delete", methods=["GET"])
def campaign_delete_confirm(cid):
    r = require_login()
    if r:
        return r
    camp = _own_campaign(cid)
    if not camp:
        return "Campaign not found", 404
    one = lambda sql, args=(): ((db.q(sql, args, one=True) or {}).get("c")) or 0
    counts = {
        "leads": one("SELECT COUNT(*) c FROM leads WHERE campaign_id=? AND user_id=?",
                     (cid, uid())),
        "queued": one("SELECT COUNT(*) c FROM send_queue WHERE campaign_id=?", (cid,)),
        "followups": one("SELECT COUNT(*) c FROM followups WHERE campaign_id=?", (cid,)),
    }
    return render_template("campaign_delete.html", campaign=camp, counts=counts,
                           user=current_user(), error=None)


@app.route("/campaign/<int:cid>/delete", methods=["POST"])
def campaign_delete(cid):
    r = require_login()
    if r:
        return r
    camp = _own_campaign(cid)
    if not camp:
        return "Campaign not found", 404
    typed = (request.form.get("confirm_name") or "").strip()
    if typed != camp["name"]:
        one = lambda sql, args=(): ((db.q(sql, args, one=True) or {}).get("c")) or 0
        counts = {
            "leads": one("SELECT COUNT(*) c FROM leads WHERE campaign_id=? AND user_id=?",
                         (cid, uid())),
            "queued": one("SELECT COUNT(*) c FROM send_queue WHERE campaign_id=?", (cid,)),
            "followups": one("SELECT COUNT(*) c FROM followups WHERE campaign_id=?", (cid,)),
        }
        return render_template("campaign_delete.html", campaign=camp, counts=counts,
                               user=current_user(),
                               error="The typed name does not match. Nothing was deleted.")
    _delete_campaign_data(cid, uid())
    return redirect(url_for("dashboard"))


@app.route("/logs")
def logs():
    r = require_login()
    if r:
        return r
    rows = db.q("""SELECT l.*, c.name AS campaign_name, a.email AS account_email,
                          le.business_name AS business
                   FROM send_log l
                   LEFT JOIN campaigns c ON c.id=l.campaign_id
                   LEFT JOIN sender_accounts a ON a.id=l.account_id
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


@app.route("/api/process-full", methods=["POST"])
def api_process_full():
    """Manual 'run pipeline to sends' (login required, no cron secret).
    Loops pipeline ticks until done or ~50s budget, processing sends
    after each round."""
    r = require_login()
    if r:
        return r
    try:
        return jsonify(queue_worker.process_pipeline_full())
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
