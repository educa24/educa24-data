#!/usr/bin/env python3
"""Educa24 «مساري المهني» robot (runs free on GitHub Actions).

Every run:
 1. Reads fresh news from sources.json: RSS feeds, WordPress site searches
    (site/?s=words&feed=rss2) and Bing News searches, plus a few pages.
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
REPORT = ROOT / "robot" / "report.json"
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"),
    "Accept-Language": "ar,fr;q=0.8,en;q=0.6",
}
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
    "professional_exam": ["امتحان الكفاءة المهنية", "الامتحانات المهنية", "الامتحان المهني",
                          "امتحانات الكفاءة المهنية", "الكفاءة المهنية"],
    "movement": ["حركة الانتقالية", "حركات الانتقالية", "حركة انتقالية", "الحركة المحلية",
                 "الحركة الجهوية", "الحركة الوطنية", "طلبات الانتقال"],
    "abroad": ["الجالية المغربية", "الأساتذة بالخارج", "تدريس اللغة العربية والثقافة المغربية", "مؤسسة الحسن الثاني"],
    "inspection": ["مركز تكوين مفتشي التعليم", "سلك المفتشين", "التفتيش التربوي"],
    "guidance": ["مركز التوجيه والتخطيط", "التوجيه والتخطيط التربوي"],
    "administration": ["الإدارة التربوية", "أطر الإدارة التربوية", "المناصب الإدارية"],
}
TYPE_IDS = {"guidance": "guidance_planning"}  # ids used by the app
RESULT_WORDS = ["نتائج", "لوائح", "لائحة", "المستفيدين", "المستفيدات", "الناجحين",
                "الناجحات", "المقبولين", "الفائزين"]
ANNOUNCE_WORDS = ["فتح باب الترشيح", "بدء التسجيل", "انطلاق عملية", "المذكرة الإطار",
                  "مذكرة وزارية", "المذكرة المنظمة", "آخر أجل", "تمديد", "إجراء الحركة",
                  "المشاركة في الحركة", "التسجيل في", "الترشح"]
# Professional exam: the memo is often titled only «مذكرة … تنظيم امتحانات …»
EXAM_ANNOUNCE_WORDS = ["مذكرة", "تنظيم", "إجراء", "موعد", "تاريخ", "مواعيد", "بلاغ",
                  "فتح التسجيل", "الإعلان عن", "منصة", "دورة"]
# News that talks about the exam/movement but is not the memo yet.
NOT_YET_WORDS = ["مرتقب", "تأجيل", "تؤجل", "يؤجل", "جدل", "يطالب", "تطالب", "احتجاج"]


EDUCA24_FEED = "https://www.educa24.ma/feeds/posts/default?alt=rss&max-results=25"


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


LAST_ERROR: dict[str, str] = {}
DEAD: dict[str, int] = {}  # failures per site during this run


def get(url: str, tries: int = 3, timeout: int = 40, **kw) -> requests.Response | None:
    for i in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=timeout, **kw)
            if r.status_code == 200:
                return r
            log(f"  HTTP {r.status_code} {url}")
            LAST_ERROR[url] = f"HTTP {r.status_code}"
            return None
        except Exception as e:  # network hiccup: retry
            log(f"  retry {i + 1} {url}: {e}")
            LAST_ERROR[url] = type(e).__name__
            time.sleep(4 * (i + 1))
    return None


def strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def school_year_ok(text: str) -> bool:
    """Only this year's (and last year's) lists."""
    y = date.today().year
    return any(str(v) in text for v in (y - 1, y, y + 1))


# ── 1. news items ───────────────────────────────────────────────────────────

STATS: dict[str, dict] = {}  # per source: items read or the error (report.json)


def _stat(url: str, n: int | None, err: str = "") -> None:
    STATS[url] = {"items": n, **({"error": err} if err else {})}


def real_url(link: str) -> str:
    """Bing News links are redirects: the article is in the url= parameter."""
    if "bing.com/news/apiclick" in link:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        if q.get("url"):
            return q["url"][0]
    return link


def rss(url: str, days: int = 4) -> list[dict]:
    """Items of an RSS / Atom feed published in the last [days] days."""
    from email.utils import parsedate_to_datetime
    host = urllib.parse.urlparse(url).netloc
    if DEAD.get(host, 0) >= 2:  # this site failed twice in this run: skip it
        _stat(url, None, "skipped (site not answering)")
        return []
    r = get(url, tries=1, timeout=25)
    if not r:
        DEAD[host] = DEAD.get(host, 0) + 1
        _stat(url, None, LAST_ERROR.get(url, "no answer"))
        return []
    items = []
    now = datetime.now(timezone.utc)
    blocks = re.findall(r"<item[\s>](.*?)</item>", r.text, re.S) or \
        re.findall(r"<entry[\s>](.*?)</entry>", r.text, re.S)
    for it in blocks:
        t = re.search(r"<title[^>]*>(.*?)</title>", it, re.S)
        l = re.search(r"<link>(.*?)</link>", it, re.S) or \
            re.search(r'<link[^>]+href="([^"]+)"', it)
        d = re.search(r"<(?:pubDate|published|updated)>(.*?)</", it, re.S)
        if not (t and l):
            continue
        try:
            if d:
                raw = d.group(1).strip()
                when = (datetime.fromisoformat(raw.replace("Z", "+00:00")) if "T" in raw[:20]
                        else parsedate_to_datetime(raw))
                if (now - when).days > days:
                    continue
        except Exception:
            pass
        title = strip_tags(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", t.group(1), flags=re.S))
        link = real_url(html.unescape(strip_tags(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", l.group(1)))))
        items.append({"title": title, "url": link, "fresh": True})
    _stat(url, len(items))
    return items


def search_feeds(cfg: dict) -> list[str]:
    """WordPress searches (site/?s=words&feed=rss2) and Bing News searches."""
    out = []
    for site in cfg.get("wordpress_sites", []):
        for w in cfg.get("search_words", []):
            out.append(f"{site.rstrip('/')}/?s={urllib.parse.quote(w)}&feed=rss2")
    for w in cfg.get("bing_news", []):
        out.append("https://www.bing.com/news/search?format=rss&setlang=ar&cc=MA&q="
                   + urllib.parse.quote(w))
    return out


def page_links(url: str) -> list[dict]:
    r = get(url)
    if not r:
        _stat(url, None, LAST_ERROR.get(url, "no answer"))
        return []
    out = []
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S | re.I):
        href, text = m.group(1), strip_tags(m.group(2))
        if len(text) < 8:
            continue
        out.append({"title": text, "url": urllib.parse.urljoin(r.url, href)})
    _stat(url, len(out))
    return out


def norm_title(t: str) -> str:
    """Same news on several sites: compare titles without punctuation/spaces."""
    t = re.sub(r"[\u064B-\u0652ـ]", "", t)  # harakat, tatweel
    t = re.sub(r"[^\w]+", " ", t).strip()
    return " ".join(t.split()[:12])


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
    words = ANNOUNCE_WORDS + (EXAM_ANNOUNCE_WORDS if typ == "professional_exam" else [])
    if any(w in t for w in words) and not any(w in t for w in NOT_YET_WORDS):
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
    # New or edited sources: this run only remembers what they already list,
    # so old news found by a new source never triggers a notification.
    global INIT
    sig = hashlib.sha1(SOURCES.read_bytes()).hexdigest()[:12] if SOURCES.exists() else ""
    if state.get("sources_sig") != sig:
        if state.get("sources_sig"):
            log("sources changed: this run only remembers the existing news")
        INIT = True
        state["sources_sig"] = sig

    items = []
    for q in cfg.get("feeds", []) + search_feeds(cfg):
        items += rss(q)
    # Educa24's own blog often posts the memo first. Kept here (not in
    # sources.json) so adding it does not restart the robot's memory.
    items += rss(EDUCA24_FEED, days=1)
    for p in cfg.get("pages", []):
        items += page_links(p)
    log(f"{len(items)} items")
    titles = state.setdefault("titles", {})  # normalized title -> day seen
    today_n = date.today().toordinal()
    for k in [k for k, v in titles.items() if today_n - v > 30]:
        del titles[k]

    results = data.setdefault("results", [])
    known = {r.get("url") for r in results}
    changed = False
    for it in items:
        key = it["url"]
        typ, kind = classify_words(it["title"])
        # An announce already read once is checked again: an older robot may
        # have skipped it (title words), and announce() never notifies twice.
        if key in seen and kind != "announce":
            continue
        seen.add(key)
        if not typ or not kind:
            continue
        nt = norm_title(it["title"])
        if nt in titles:
            continue  # the same news already read on another site
        titles[nt] = today_n
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
            if rows:
                fp = hashlib.sha256("|".join(sorted({p for p, _ in rows})).encode()).hexdigest()[:20]
                fps = state.setdefault("lists", {})
                if fp in fps:
                    log(f"  same list already published: {u}")
                    known.add(u)
                    continue
                fps[fp] = today_n
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
    state["lists"] = dict(sorted(state.get("lists", {}).items(), key=lambda kv: kv[1])[-300:])
    ok = sum(1 for v in STATS.values() if v.get("items"))
    report = {
        "run": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "sources_answering": f"{ok}/{len(STATS)}",
        "items": len(items),
        "gemini": bool(GEMINI),
        "sources": STATS,
    }
    log(f"sources answering: {ok}/{len(STATS)}")
    if not DRY:
        save(STATE, state)
        save(REPORT, report)
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
            last = d.get("announced")
            if last and (date.today() - date.fromisoformat(last)).days < 30:
                return False  # already announced (same news on another site)
            d["announced"] = date.today().isoformat()
            d["label"] = "صدرت المذكرة: " + datetime.now(timezone.utc).strftime("%d/%m/%Y")
            d["url"] = it["url"]
            d.pop("date", None)
            fcm_send(f"masari_{typ}", it["title"][:90],
                     "افتح «مواعيدي الإدارية» في مساري المهني لترى التفاصيل والرابط.")
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
