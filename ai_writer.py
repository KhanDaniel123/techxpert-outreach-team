"""Autopilot AI writer: generates one personalized cold email + 3 follow-ups
per lead, grounded ONLY in observed website findings.

No new dependencies: talks to an OpenAI-style chat-completions endpoint with
urllib. The backend is chosen by config.ai_provider(): Google's Gemini
(GEMINI_API_KEY, free from AI Studio, preferred when set) or OpenAI
(OPENAI_API_KEY). Both speak the same request format. Every generation is
stored in the `ai_content` table once and cached, so a lead is never
generated twice and costs stay predictable.

If no API key is set, nothing here runs: the autopilot toggle is hidden
in the UI and campaigns fall back to normal templates.
"""
import json
import time
import urllib.request
import urllib.error

import config
import db
import gap_analysis

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
TIMEOUT_S = 60
MAX_BODY_WORDS = 120
MAX_FU_WORDS = 60

# Prefixes cycled for follow-up steps beyond the 3 AI-written ones
# (step 4+). {business_name} is rendered with the lead's data at send time.
FOLLOWUP_CYCLE_PREFIXES = (
    "Circling back once more: ",
    "One more nudge on this: ",
    "Still thinking about {business_name}: ",
)

SYSTEM_PROMPT = """You write short, plain-text cold emails for TechXpert, a web agency.
You are given OBSERVED facts about a small business and its website.

Hard rules you must never break:
- Mention ONLY the observed facts you were given. Never invent metrics, revenue,
  traffic, pain, problems, or capabilities that are not in the facts.
- Never claim the business is losing customers, money, or leads. State the
  observation plainly (e.g. "I noticed there's no booking page on your site").
- No hype words (no "game-changer", "revolutionize", "skyrocket"), no exclamation marks.
- Plain, friendly, human tone. Short sentences.

Email structure:
1. Greet the business by name.
2. One sentence naming ONE observed gap, as a simple observation, not an accusation.
3. "I'm Waleed with TechXpert. We build websites and AI systems for {category} businesses."
   (Use the category you were given; if none was given, say "small businesses".)
4. One low-pressure ask for a 10-minute chat.

Keep the email under 120 words. Reply with JSON only:
{"subject": "...", "body": "..."}"""

FOLLOWUP_PROMPT = """You write short, plain-text follow-up emails for TechXpert, a web agency.
You are given OBSERVED facts about a small business and its website, plus the
first email that was sent.

Hard rules you must never break:
- Mention ONLY the observed facts you were given. Never invent metrics, revenue,
  traffic, pain, or capabilities.
- Never claim the business is losing anything. No guilt trips.
- No hype words, no exclamation marks. Plain, friendly, human.
- Each follow-up takes a DIFFERENT angle: (1) a brief bump referencing the same
  observation, (2) a one-line question about the observation, (3) a graceful
  close-the-loop note that leaves the door open.

Each follow-up is under 60 words. Reply with JSON only:
{"followups": [{"subject": "...", "body": "..."},
               {"subject": "...", "body": "..."},
               {"subject": "...", "body": "..."}]}"""


def _provider_call_info():
    """(url, api_key, model) for the configured AI backend."""
    provider = config.ai_provider()
    if provider == "gemini":
        return GEMINI_URL, config.GEMINI_API_KEY, config.GEMINI_MODEL
    return OPENAI_URL, config.OPENAI_API_KEY, config.AI_MODEL


def _call_openai(messages):
    """POST to the chat-completions endpoint of the configured backend
    (Gemini's OpenAI-compatible endpoint or OpenAI itself).
    Returns (parsed_json, usage_dict).
    Raises RuntimeError on any failure (network, auth, bad JSON)."""
    if not config.ai_enabled():
        raise RuntimeError("AI writing is not turned on (no API key).")
    url, api_key, model = _provider_call_info()
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    last_error = None
    data = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode())
            break
        except urllib.error.HTTPError as e:
            # 429/503 are transient (rate limit / overloaded): retry once
            # with backoff before giving up, since the pipeline makes
            # many calls.
            last_error = e
            if e.code in (429, 503) and attempt == 0:
                time.sleep(4)
                continue
            break
        except Exception as e:
            raise RuntimeError(f"AI request failed: {str(e)[:120]}")
    if data is None:
        e = last_error
        try:
            detail = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        raise RuntimeError(f"AI service error ({e.code}): {detail or 'request failed'}".strip())
    try:
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except Exception:
        raise RuntimeError("AI returned an unreadable response.")
    usage = data.get("usage") or {}
    return parsed, {"input": usage.get("prompt_tokens", 0),
                    "output": usage.get("completion_tokens", 0)}


def _cap_words(text, n):
    """Hard-trim to n words, preferably at a sentence boundary."""
    words = (text or "").split()
    if len(words) <= n:
        return text or ""
    cut = " ".join(words[:n])
    for end in (".", "!", "?"):
        i = cut.rfind(end + " ")
        if i > len(cut) * 0.5:
            return cut[:i + 1]
    return cut.rstrip(",;:") + "..."


def build_user_message(lead, campaign, findings):
    """The grounded brief the model sees. Findings pass through verbatim."""
    lead = lead or {}
    campaign = campaign or {}
    lines = [
        f"Business name: {lead.get('business_name') or '(unknown)'}",
        f"Category: {lead.get('category') or campaign.get('niche') or '(unknown)'}",
        f"Location: {lead.get('address') or campaign.get('location') or '(unknown)'}",
        f"Website: {lead.get('website') or '(none)'}",
        "Observed facts about this business (use ONLY these):",
    ]
    for f in findings or []:
        lines.append(f"- {f}")
    if not (findings or []):
        lines.append("- (no observations available)")
    return "\n".join(lines)


def generate_email(lead, campaign, findings):
    """Returns (subject, body, usage)."""
    user_msg = build_user_message(lead, campaign, findings)
    parsed, usage = _call_openai([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ])
    subject = (parsed.get("subject") or "").strip()
    body = _cap_words((parsed.get("body") or "").strip(), MAX_BODY_WORDS)
    if not subject or not body:
        raise RuntimeError("AI returned an empty email.")
    return subject, body, usage


def generate_followups(lead, campaign, findings, first_body):
    """Returns ([(subject, body) x3], usage)."""
    user_msg = build_user_message(lead, campaign, findings)
    user_msg += f"\n\nThe first email sent was:\n{first_body}\n\nNow write the 3 follow-ups."
    parsed, usage = _call_openai([
        {"role": "system", "content": FOLLOWUP_PROMPT},
        {"role": "user", "content": user_msg},
    ])
    fus = parsed.get("followups") or []
    out = []
    for fu in fus[:3]:
        subj = ((fu or {}).get("subject") or "").strip()
        body = _cap_words(((fu or {}).get("body") or "").strip(), MAX_FU_WORDS)
        if subj and body:
            out.append((subj, body))
    if len(out) < 3:
        raise RuntimeError("AI returned incomplete follow-ups.")
    return out, usage


def get_ai_content(lead_id):
    return db.q("SELECT * FROM ai_content WHERE lead_id=?", (lead_id,), one=True)


def estimate_cost_usd(tokens_in, tokens_out):
    """Approx USD cost for a token count, using config's price constants.

    Gemini's free tier covers this app's volume, so cost is reported as
    zero on the Gemini backend rather than a fabricated number."""
    if config.ai_provider() == "gemini":
        return 0.0
    return ((tokens_in or 0) / 1_000_000) * config.AI_PRICE_IN_PER_M + \
           ((tokens_out or 0) / 1_000_000) * config.AI_PRICE_OUT_PER_M


def avg_cost_per_lead(campaign_id):
    """(avg_usd_per_lead, leads_written) from stored token counts."""
    row = db.q("SELECT COUNT(*) c, COALESCE(SUM(tokens_in),0) ti, "
               "COALESCE(SUM(tokens_out),0) tout FROM ai_content ac "
               "JOIN leads l ON l.id=ac.lead_id WHERE l.campaign_id=?",
               (campaign_id,), one=True)
    n = (row["c"] if row else 0) or 0
    if not n:
        return 0.0, 0
    total = estimate_cost_usd(row["ti"], row["tout"])
    return total / n, n


def _set_status(lead_id, status, note=""):
    db.w("UPDATE leads SET ai_status=?, ai_note=? WHERE id=?",
         (status, note or "", lead_id))


def generate_and_store(lead, campaign):
    """Generate + cache AI content for one lead.

    Returns (status, note): status is 'ready', 'skipped', or 'error' and
    note is plain-English. Cached rows are never regenerated.
    """
    lead_id = lead["id"]
    if get_ai_content(lead_id):
        if (lead.get("ai_status") or "") != "ready":
            _set_status(lead_id, "ready", "")
        return "ready", ""
    if not config.ai_enabled():
        _set_status(lead_id, "error", "AI writing is not turned on.")
        return "error", "AI writing is not turned on."
    has_site = bool((lead.get("website") or "").strip())
    has_info = bool((lead.get("business_name") or "").strip()
                    or (lead.get("category") or "").strip())
    if not has_site and not has_info:
        note = "Not enough business info, so the normal template will be used."
        _set_status(lead_id, "skipped", note)
        return "skipped", note
    try:
        analysis = gap_analysis.analyze_website(lead.get("website"))
        findings = analysis["findings"]
        subject, body, u1 = generate_email(lead, campaign, findings)
        fus, u2 = generate_followups(lead, campaign, findings, body)
        db.w("""INSERT INTO ai_content
                (lead_id, subject, body, fu1_subj, fu1_body, fu2_subj, fu2_body,
                 fu3_subj, fu3_body, findings_json, tokens_in, tokens_out, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (lead_id, subject, body,
              fus[0][0], fus[0][1], fus[1][0], fus[1][1], fus[2][0], fus[2][1],
              json.dumps(findings), u1["input"] + u2["input"],
              u1["output"] + u2["output"], time.time()))
        _set_status(lead_id, "ready", "")
        return "ready", ""
    except Exception as e:
        note = f"AI writing failed ({str(e)[:120]}). The normal template will be used."
        _set_status(lead_id, "error", note)
        return "error", note
