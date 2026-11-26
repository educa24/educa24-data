#!/usr/bin/env python3
"""Educa24 «مساري المهني» robot (runs free on GitHub Actions).

Every run:
 1. Reads fresh news (Google News RSS searches + the sources in sources.json).
 2. Keeps the items about lists/results the app follows (promotion lists,
    professional exam, transfer movements, abroad, inspection, guidance and
    planning, educational administration) and the announcements (registration
    open for the movement or the exam).
 3. Downloads the list (PDF, Google Drive), reads its text and keeps ONLY the
    salted SHA-256 hashes of the PPR numbers (no names, no PPR in clear).
    For movement results it also keeps where each PPR moves (destination).
 4. Writes masari.json, sends one Firebase notification to the topic of the
    list (national or per academy), and commits.

Secrets (GitHub → Settings → Secrets → Actions):
  GEMINI_API_KEY        optional, key from a Google project WITHOUT billing
  FCM_SERVICE_ACCOUNT   JSON of the Firebase service account (educa24-app)
Nothing is published when a check fails; the run log says why.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "masari.json"
STATE = ROOT / "robot" / "state.json"
SOURCES = ROOT / "robot" / "sources.json"
UA = {"User-Agent": "Mozilla/5.0 (Educa24 robot; +https://www.educa24.ma)"}
DRY = "--dry" in sys.argv
INIT = "--init" in sys.argv

ACADEMIES = [
    "طنجة تطوان الحسيمة", "الشرق", "فاس مكناس", "الرباط سلا القنيطرة",
    "بني ملال خنيفرة", "الدار البيضاء سطات", "مراكش آسفي", "درعة تافيلالت",
    "سوس ماسة", "كلميم واد نون", "العيون الساقية الحمراء", "الداخلة وادي الذهب",
]
# Short words that identify each academy in a title (index = position above).
ACADEMY_WORDS = [
    ["طنجة", "تطوان", "الحسيمة"], ["الشرق", "وجدة"], ["فاس", "مكناس"],
    ["الرباط", "سلا", "القنيطرة"], ["بني ملال", "خنيفرة"], ["الدار البيضاء", "سطات"],
    ["مراكش", "آسفي"], ["درعة", "تافيلالت", "الرشيدية"], ["سوس", "ماسة", "أكادير"],
    ["كلميم", "واد نون"], ["العيون", "الساقية الحمراء"], ["الداخلة", "وادي الذهب"],
]

TYPES = {
    "promotion_lists": ["الترقية بالاختيار", "لوائح الترقية", "الترقي بالاختيار", "جداول الترقي"],
    "professional_exam": ["امتحان الكفاءة المهنية", "الامتحانات المهنية", "الامتحان المهني"],
    "movement": ["حركة الانتقالية", "حركات الانتقالية", "حركة انتقالية"],
    "abroad": ["الجالية المغربية", "الأساتذة بالخارج", "تدريس اللغة العربية والثقافة المغربية", "مؤسسة الحسن الثاني"],
    "inspection": ["مركز تكوين مفتشي التعليم", "سلك المفتشين", "التفتيش التربوي"],
    "guidance": ["مركز التوجيه والتخطيط", "التوجيه والتخطيط التربوي"],
    "administration": ["الإدارة التربوية", "أطر الإدارة التربوية", "المناصب الإدارية"],
}
TYPE_IDS = {"guidance": "guidance_planning"}  # ids used by the app
RESULT_WORDS = ["نتائج", "لوائح", "لائحة", "المستفيدين", "الناجحين", "الناجحات", "المقبولين"]
ANNOUNCE_WORDS = ["فتح باب الترشيح", "بدء التسجيل", "انطلاق عملية", "المذكرة الإطار", "مذكرة وزارية"]


# ── helpers ─────────────────────────────────────────────────────────────────

def log(*a):
    print(*a, flush=True)


def load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def get(url: str, **kw) -> requests.Response | None:
    for i in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=40, **kw)
            if r.status_code == 200:
                return r
            log(f"  HTTP {r.status_code} {url}")
            return None
        except Exception as e:  # network hiccup: retry
            log(f"  retry {i + 1} {url}: {e}")
            time.sleep(4 * (i + 1))
    return None


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def school_year_ok(text: str) -> bool:
    """Only this year's (and last year's) lists."""
    y = date.today().year
    return any(str(v) in text for v in (y - 1, y, y + 1))


# ── 1. news items ───────────────────────────────────────────────────────────

def rss(url: str, days: int = 4) -> list[dict]:
    """Items of an RSS feed published in the last [days] days."""
    from email.utils import parsedate_to_datetime
    r = get(url)
    if not r:
        return []
    items = []
    now = datetime.now(timezone.utc)
    for m in re.finditer(r"<item>(.*?)</item>", r.text, re.S):
        it = m.group(1)
        t = re.search(r"<title>(.*?)</title>", it, re.S)
        l = re.search(r"<link>(.*?)</link>", it, re.S)
        d = re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
        if not (t and l):
            continue
        try:
            if d and (now - parsedate_to_datetime(d.group(1).strip())).days > days:
                continue
        except Exception:
            pass
        title = strip_tags(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", t.group(1), flags=re.S))
        items.append({"title": title, "url": strip_tags(l.group(1)), "fresh": True})
    return items


def page_links(url: str) -> list[dict]:
    r = get(url)
    if not r:
        return []
    out = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S | re.I):
        href, text = m.group(1), strip_tags(m.group(2))
        if len(text) < 8:
            continue
        out.append({"title": text, "url": urllib.parse.urljoin(r.url, href)})
    return out


def classify_words(title: str) -> tuple[str | None, str | None]:
    """(type, kind) from keywords; kind = result | announce."""
    t = title.replace("ـ", "")
    typ = None
    for k, words in TYPES.items():
        if any(w in t for w in words):
            typ = TYPE_IDS.get(k, k)
            break
    if not typ:
        return None, None
    if any(w in t for w in RESULT_WORDS):
        return typ, "result"
    if any(w in t for w in ANNOUNCE_WORDS):
        return typ, "announce"
    return typ, None


def academy_of(text: str) -> int:
    for i, words in enumerate(ACADEMY_WORDS):
        if any(w in text for w in words):
            return i
    return -1


def scope_of(text: str) -> str | None:
    if "المحلية" in text:
        return "local"
    if "الجهوية" in text:
        return "regional"
    if "الوطنية" in text:
        return "national"
    return None


# ── 2. Gemini (optional, free tier) ─────────────────────────────────────────

GEMINI = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def gemini_json(prompt: str) -> dict | None:
    if not GEMINI:
        return None
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    for i in range(3):
        try:
            r = requests.post(url, json=body, timeout=90,
                              headers={"x-goog-api-key": GEMINI})
            if r.status_code == 429:
                time.sleep(30 * (i + 1))
                continue
            r.raise_for_status()
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(txt)
        except Exception as e:
            log(f"  gemini: {e}")
            time.sleep(5)
    return None


def gemini_check(title: str, text: str) -> dict | None:
    return gemini_json(
        "أنت تساعد تطبيقاً للأساتذة في المغرب. هذا عنوان ومقتطف من وثيقة.\n"
        f"العنوان: {title}\nالمقتطف:\n{text[:3500]}\n\n"
        "أجب بـ JSON فقط: {\"relevant\": true/false (هل هي لائحة نتائج أو مستفيدين رسمية لموظفي التربية الوطنية لهذه السنة)، "
        "\"type\": واحد من promotion_lists, professional_exam, movement, abroad, inspection, guidance_planning, administration, "
        "\"scope\": national أو regional أو local أو null، \"academy\": رقم الأكاديمية 1-12 حسب الترتيب "
        + "، ".join(ACADEMIES) + " أو 0 إذا كانت وطنية، \"title\": عنوان عربي قصير وواضح للائحة}")


# ── 3. lists → hashes ───────────────────────────────────────────────────────

def drive_direct(url: str) -> str:
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=)([\w-]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    m = re.search(r"docs\.google\.com/document/d/([\w-]+)", url)
    if m:
        return f"https://docs.google.com/document/d/{m.group(1)}/export?format=pdf"
    return url


def pdf_urls(article_url: str) -> list[str]:
    """PDF / Drive links inside an article (or the URL itself if it is one)."""
    if re.search(r"\.pdf($|\?)|drive\.google|docs\.google", article_url, re.I):
        return [drive_direct(article_url)]
    r = get(article_url)
    if not r:
        return []
    urls = []
    for m in re.finditer(r'href="([^"]+)"', r.text):
        u = urllib.parse.urljoin(r.url, html.unescape(m.group(1)))
        if re.search(r"\.pdf($|\?)|drive\.google\.com/(file|open)|docs\.google\.com/document", u, re.I):
            u = drive_direct(u)
            if u not in urls:
                urls.append(u)
    return urls[:30]


def pdf_text(url: str) -> str:
    r = get(url)
    if not r or not r.content.startswith(b"%PDF"):
        return ""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "l.pdf"
        p.write_bytes(r.content)
        try:
            out = subprocess.run(["pdftotext", "-layout", str(p), "-"],
                                 capture_output=True, timeout=300)
            return out.stdout.decode("utf-8", "ignore")
        except Exception as e:
            log(f"  pdftotext: {e}")
            return ""


PPR_RE = re.compile(r"(?<![\d/.-])(\d{5,10})(?![\d/.-])")


def norm_ppr(s: str) -> str:
    s = s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    s = re.sub(r"\D", "", s)
    return s.lstrip("0") or s


def pprs_by_line(text: str) -> list[tuple[str, str]]:
    """(ppr, line) for lines that look like a person row: a 5-10 digit number
    and some letters (the name). Numbers repeated 3+ times are codes, not PPR."""
    text = text.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    rows, count = [], {}
    for line in text.splitlines():
        if not re.search(r"[A-Za-z؀-ۿ]{2,}", line):
            continue
        for n in PPR_RE.findall(line):
            if n.startswith(("19", "20")) and len(n) == 8 and n[4:6] <= "12":
                continue  # 20260115 = a date
            rows.append((norm_ppr(n), line))
            count[norm_ppr(n)] = count.get(norm_ppr(n), 0) + 1
    return [(p, l) for p, l in rows if count[p] < 3]


def destinations(rows: list[tuple[str, str]]) -> dict[str, str]:
    """Movement lists: learn the destination columns from a sample (one Gemini
    call), then apply them to every row of the same shape."""
    if not rows or not GEMINI:
        return {}
    split = [re.split(r"\s{2,}", l.strip()) for _, l in rows]
    widths = {}
    for cols in split:
        widths[len(cols)] = widths.get(len(cols), 0) + 1
    width = max(widths, key=widths.get)
    sample = [c for c in split if len(c) == width][:12]
    ans = gemini_json(
        "هذه صفوف من لائحة نتائج الحركة الانتقالية لأساتذة المغرب، كل صف مقسم إلى أعمدة (JSON):\n"
        + json.dumps(sample, ensure_ascii=False)
        + "\nأعطني بـ JSON فقط أرقام الأعمدة (تبدأ من 0) التي تصف مكان الانتقال الجديد (المؤسسة/الجماعة/المديرية المستقبلة)، "
        "بالترتيب الذي يُقرأ به: {\"dest\": [..]}. إذا لم تكن واضحة أعط {\"dest\": []}.")
    cols = [i for i in (ans or {}).get("dest", []) if isinstance(i, int) and 0 <= i < width]
    if not cols:
        return {}
    out = {}
    for (ppr, _), c in zip(rows, split):
        if len(c) == width:
            d = "، ".join(c[i].strip() for i in cols if c[i].strip() and not PPR_RE.fullmatch(c[i].strip()))
            if 3 <= len(d) <= 160:
                out[ppr] = d
    # Sanity: most rows of the modal shape must have a destination.
    return out if len(out) >= 0.6 * widths[width] else {}


# ── 4. Firebase notification ────────────────────────────────────────────────

def fcm_send(topic: str, title: str, body: str) -> None:
    raw = os.environ.get("FCM_SERVICE_ACCOUNT", "").strip()
    if not raw or DRY:
        log(f"  (no push) {topic}: {title}")
        return
    from google.oauth2 import service_account
    import google.auth.transport.requests as gtr
    info = json.loads(raw)
    cred = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
    cred.refresh(gtr.Request())
    msg = {"message": {
        "topic": topic,
        "notification": {"title": title, "body": body},
        "data": {"screen": "masari-results", "masari": "1"},
        "android": {"priority": "high", "notification": {"channel_id": "educa24_masari"}},
    }}
    r = requests.post(
        f"https://fcm.googleapis.com/v1/projects/{info['project_id']}/messages:send",
        json=msg, headers={"Authorization": f"Bearer {cred.token}"}, timeout=30)
    log(f"  push {topic}: {r.status_code}")


PUSH_TEXT = {
    "promotion_lists": "صدرت لوائح الترقية بالاختيار",
    "professional_exam": "صدرت نتائج امتحان الكفاءة المهنية",
    "movement": "صدرت نتائج الحركة الانتقالية",
    "abroad": "صدرت نتائج انتقاء أساتذة الجالية",
    "inspection": "صدرت نتائج مباراة التفتيش",
    "guidance_planning": "صدرت نتائج مباراة التوجيه والتخطيط",
    "administration": "صدرت نتائج الإدارة التربوية",
}


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    data = load(DATA, None)
    if not data:
        log("masari.json missing")
        return 1
    state = load(STATE, {"seen": []})
    seen = set(state["seen"])
    cfg = load(SOURCES, {"queries": [], "pages": []})

    items = []
    for q in cfg.get("feeds", []):
        items += rss(q)
    for p in cfg.get("pages", []):
        items += page_links(p)
    log(f"{len(items)} items")

    results = data.setdefault("results", [])
    known = {r.get("url") for r in results}
    changed = False
    for it in items:
        key = it["url"]
        if key in seen:
            continue
        seen.add(key)
        typ, kind = classify_words(it["title"])
        if not typ or not kind:
            continue
        if not it.get("fresh") and not school_year_ok(it["title"]):
            continue
        if INIT:
            continue  # first run: only remember what already exists
        log(f"* {typ}/{kind}: {it['title']}")
        if kind == "announce":
            changed |= announce(data, typ, it)
            continue
        for u in pdf_urls(it["url"]) or [None]:
            if u in known:
                continue
            text = pdf_text(u) if u else ""
            rows = pprs_by_line(text)
            g = gemini_check(it["title"], text or it["title"]) if GEMINI else None
            if g is not None and not g.get("relevant", False):
                log(f"  not relevant: {u}")
                continue
            typ2 = (g or {}).get("type") or typ
            if typ2 not in PUSH_TEXT:
                typ2 = typ
            # The academy comes from the title only: the rows of a national
            # list are full of city names.
            acad = int((g or {}).get("academy") or 0) - 1 if g else academy_of(it["title"])
            if not -1 <= acad <= 11:
                acad = -1
            scope = ((g or {}).get("scope") or scope_of(it["title"])) if typ2 == "movement" else None
            if scope == "national" or "الوطني" in it["title"]:
                acad = -1
            salt = secrets.token_hex(8)
            h = lambda p: hashlib.sha256(f"{salt}:{p}".encode()).hexdigest()
            entry = {
                "id": hashlib.sha1((u or key).encode()).hexdigest()[:12],
                "type": typ2,
                "title": ((g or {}).get("title") or it["title"])[:120],
                "published": date.today().isoformat(),
                "academy": acad,
                "url": u or key,
                "salt": salt,
                "hashes": sorted({h(p) for p, _ in rows}),
            }
            if scope:
                entry["scope"] = scope
            if typ2 == "movement" and rows:
                dest = destinations(rows)
                if dest:
                    entry["details"] = {h(p): d for p, d in dest.items()}
            if rows and len(entry["hashes"]) < 3:
                entry["hashes"] = []  # not a real list
            log(f"  + {entry['title']} · academy {acad} · {len(entry['hashes'])} PPR"
                f" · {len(entry.get('details', {}))} destinations")
            results.insert(0, entry)
            known.add(u)
            changed = True
            topic = f"masari_{typ2}" + (f"_{acad + 1}" if acad >= 0 else "")
            where = f" ({ACADEMIES[acad]})" if acad >= 0 else ""
            fcm_send(topic, PUSH_TEXT[typ2] + where,
                     "افتح «نتائجي»: هاتفك يبحث عن رقم تأجيرك في اللائحة.")

    # keep the file small: results of the last 400 days only
    limit = date.today().toordinal() - 400
    data["results"] = [r for r in results
                       if date.fromisoformat(r["published"]).toordinal() >= limit]
    state["seen"] = sorted(seen)[-5000:]
    if not DRY:
        save(STATE, state)
    if changed:
        data["updated"] = date.today().isoformat()
        if not DRY:
            save(DATA, data)
        log("masari.json updated")
    return 0


def announce(data: dict, typ: str, it: dict) -> bool:
    """Registration opened (movement / exam): mark the deadline and notify."""
    kind = {"movement": "movement", "professional_exam": "exam"}.get(typ)
    if not kind:
        return False
    want = {"regional": "movement_regional", "local": "movement_local"}.get(
        scope_of(it["title"]) or "", "movement_2") if kind == "movement" else "exam"
    for d in data.get("deadlines", []):
        if d.get("kind") == kind and want in d.get("id", "") and d.get("url") != it["url"]:
            d["label"] = "صدرت المذكرة: " + datetime.now(timezone.utc).strftime("%d/%m/%Y")
            d["url"] = it["url"]
            d.pop("date", None)
            fcm_send(f"masari_{typ}", it["title"][:90],
                     "افتح «مواعيدي الإدارية» في مساري المهني لترى التفاصيل والرابط.")
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
