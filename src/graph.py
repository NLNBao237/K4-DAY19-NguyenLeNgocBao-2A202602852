"""Knowledge Graph (Neo4j) + GraphRAG over two drug-topic knowledge bases.

Contract (fixed — bench_kg.py and the tests rely on it):
    link_entity(name, known)                       -> one of `known` or None          (TODO KG-1)
    build_graph(graph, law_docs, news_docs, llm_fn)   load both KBs into Neo4j      (TODO KG-2)
        every node created from ONE document carries the property `doc_id`
    Neo4jGraph.context(question, doc_ids)         -> list[str] facts               (TODO KG-3)
    GraphRAGAgent.answer(question, top_k)         -> str                           (TODO KG-4)

Everything else in this file is a HINT: one possible ontology (below). Use it as is, change it,
or design your own — your own ontology + report/ONTOLOGY.md earns the bonus (see SUBMISSION.md).

This submission uses its OWN ontology (documented in report/ONTOLOGY.md, built by `_build_graph_own`):

    (:Article {id, title, law, doc_id, base_penalty, max_penalty})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text, doc_id})
    (:Clause)-[:HAS_THRESHOLD]->(:Threshold {id, point, text, unit, min_qty, max_qty, doc_id})-[:OF_SUBSTANCE]->(:Substance)
    (:Substance {name, aliases})-[:CLASSIFIED_AS]->(:Substance)          e.g. Ketamine -> "chất ma túy khác ở thể rắn"
    (:Source {doc_id, title, url, published})-[:REPORTS]->(:Case {id, name, summary, date})
    (:Case)-[:CHARGED_WITH]->(:Crime)          (:Case)-[:INVOLVES {amount, grams}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, stage, sentence}]->(:Case)
    (:Person)-[:ACCUSED_OF]->(:Crime)

Set KG_ONTOLOGY=hint to build/query the suggested ontology instead (baseline for the comparison in the report).

Suggested ontology (Crime is the bridge between the law KB and the news KB):

    (:Article {id, title, law, doc_id})-[:DEFINES]->(:Crime {name})
    (:Article)-[:HAS_CLAUSE]->(:Clause {id, number, penalty, text})-[:MENTIONS]->(:Substance {name})
    (:Case {name, summary, date, doc_id})-[:CHARGED_WITH]->(:Crime)
    (:Case)-[:INVOLVES {amount}]->(:Substance)
    (:Case)-[:LOCATED_IN]->(:Location {name})
    (:Person {name, aliases})-[:INVOLVED_IN {role, sentence, charge}]->(:Case)
"""

from __future__ import annotations

import difflib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Callable

from .models import Document
from .store import EmbeddingStore

# Canonical substance names: the ones BLHS Chương XX lists, plus common ones in Vietnamese news.
SUBSTANCES = ["Heroine", "Cocaine", "Methamphetamine", "Amphetamine", "MDMA", "XLR-11", "Ketamine",
              "cần sa", "thuốc phiện", "côca"]
CLAUSE_START = re.compile(r"^(\d+)\.\s", re.MULTILINE)
FOOTNOTE = re.compile(r"\[\d+\]")

def load_markdown_docs(folder: str | Path) -> list[Document]:
    """Read crawler output (.md with a flat `key: "value"` front matter) into Documents."""
    docs = []
    for path in sorted(Path(folder).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        _, front, body = raw.split("---", 2)
        metadata = {k: json.loads(v) for k, v in re.findall(r'^(\w+): (".*")$', front, re.MULTILINE)}
        docs.append(Document(id=metadata.get("doc_id", path.stem), content=body.strip(), metadata=metadata))
    return docs

def normalize_crime(name: str) -> str:
    """'Tội Mua bán trái phép chất ma túy' -> 'mua bán trái phép chất ma túy'."""
    name = re.sub(r"\s+", " ", name.strip().strip("\"'“”").lower())
    return name.removeprefix("tội ").strip()

def link_entity(name: str, known: list[str], normalize: Callable[[str], str] = normalize_crime) -> str | None:
    """Map a free-text mention (e.g. a charge written by a journalist) onto one canonical name in `known`."""
    def norm(text: str) -> str:
        return normalize(unicodedata.normalize("NFC", text or ""))

    target = norm(name)
    if not target:
        return None
    by_norm: dict[str, str] = {}
    for candidate in known:
        by_norm.setdefault(norm(candidate), candidate)
    if target in by_norm:
        return by_norm[target]
    close = difflib.get_close_matches(target, list(by_norm), n=1, cutoff=0.8)
    return by_norm[close[0]] if close else None   # not close enough: a wrong link is worse than no link

def find_substances(text: str) -> list[str]:
    lowered = text.lower()
    return [name for name in SUBSTANCES if name.lower() in lowered]

# ----------------------------------------------------------------------------------------------
# HINT — suggested ontology: extraction helpers
# ----------------------------------------------------------------------------------------------

def parse_law_article(doc: Document) -> dict[str, Any]:
    """Deterministic (regex) extraction for one 'Điều' — law text is regular enough to skip the LLM."""
    article_id = doc.metadata["article"]                       # "Điều 251 BLHS"
    title = doc.metadata["title"].split(". ", 1)[-1]           # "Tội mua bán trái phép chất ma túy"
    body = FOOTNOTE.sub("", doc.content)
    starts = list(CLAUSE_START.finditer(body))
    clauses = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        text = body[start.start():end].strip()
        first_line = text.splitlines()[0]
        penalty = re.search(r"\bbị ((?:phạt|tù|cảnh cáo).+?)(?::|$)", first_line)
        clauses.append({
            "id": f"{article_id} khoản {start.group(1)}",
            "number": int(start.group(1)),
            "penalty": penalty.group(1).rstrip(".") if penalty else "",
            "text": text,
            "substances": find_substances(text),
        })
    return {
        "id": article_id,
        "law": doc.metadata.get("law", ""),
        "title": title,
        "doc_id": doc.id,
        "crime": normalize_crime(title) if title.startswith("Tội ") else None,
        "clauses": clauses,
    }

NEWS_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD hoặc chuỗi rỗng",
  "location": "tỉnh/thành phố, chuỗi rỗng nếu không rõ",
  "charges": ["tội danh, BẮT BUỘC chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên chuẩn trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng nếu có"}}],
  "people": [{{"name": "họ tên", "aliases": ["biệt danh"], "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH) hoặc chuỗi rỗng",
               "sentence": "mức án nếu có, ví dụ: tử hình, 8 năm tù"}}]
}}]}}
Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

Tiêu đề: {title}
Nội dung:
{content}"""

def extract_news_cases(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one news article; charges are re-linked to law-KB crimes in code."""
    prompt = NEWS_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(SUBSTANCES),
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    for case in cases:
        case["charges"] = sorted({c for c in (link_entity(x, known_crimes) for x in case.get("charges", [])) if c})
        for person in case.get("people", []):
            person["charge"] = link_entity(person.get("charge") or "", known_crimes) or ""
    return cases

# ----------------------------------------------------------------------------------------------
# Own ontology — extraction helpers (see report/ONTOLOGY.md)
# ----------------------------------------------------------------------------------------------

def use_hint_ontology() -> bool:
    return os.getenv("KG_ONTOLOGY", "own").strip().lower() == "hint"

# Canonical substance -> names the news uses for it. The canonical side is what BLHS Chương XX writes.
SUBSTANCE_ALIASES = {
    "Heroine": ["heroin", "bạch phiến"],
    "Cocaine": ["cocain"],
    "Methamphetamine": ["ma túy đá", "hàng đá", "meth", "methamphetamin"],
    "Amphetamine": ["amphetamin"],
    "MDMA": ["thuốc lắc", "ecstasy", "kẹo", "ma túy kẹo"],
    "XLR-11": [],
    "Ketamine": ["ketamin", "ke", "ma túy ke"],
    "cần sa": ["cỏ", "bồ đà", "cây cần sa", "cần sa khô"],
    "nhựa thuốc phiện": ["thuốc phiện"],
    "nhựa cần sa": [],
    "cao côca": [],
    "lá cây côca": [],
    "lá khát": [],
    "quả thuốc phiện khô": [],
    "quả thuốc phiện tươi": [],
    "chất ma túy khác ở thể rắn": [],
    "chất ma túy khác ở thể lỏng": [],
    "ma túy (không rõ loại)": ["ma túy", "chất ma túy", "ma tuý"],
}
# How a threshold line ("điểm") in BLHS names its substances -> canonical name.
LAW_SUBSTANCE_PHRASES = [
    ("nhựa thuốc phiện", "nhựa thuốc phiện"), ("nhựa cần sa", "nhựa cần sa"), ("cao côca", "cao côca"),
    ("lá cây côca", "lá cây côca"), ("lá khát", "lá khát"), ("cây cần sa", "cần sa"),
    ("quả thuốc phiện khô", "quả thuốc phiện khô"), ("quả thuốc phiện tươi", "quả thuốc phiện tươi"),
    ("các chất ma túy khác ở thể rắn", "chất ma túy khác ở thể rắn"),
    ("các chất ma túy khác ở thể lỏng", "chất ma túy khác ở thể lỏng"),
    ("heroine", "Heroine"), ("cocaine", "Cocaine"), ("methamphetamine", "Methamphetamine"),
    ("amphetamine", "Amphetamine"), ("mdma", "MDMA"), ("xlr-11", "XLR-11"),
]
# Substances BLHS does not name: they fall under the catch-all threshold lines.
SUBSTANCE_CLASSES = {"Ketamine": "chất ma túy khác ở thể rắn"}

POINT_LINE = re.compile(r"^([a-zđ])\)\s+(.+)$", re.MULTILINE)
LAW_RANGE = re.compile(r"từ (\d[\d.,]*) (gam|kilôgam|mililít) đến dưới (\d[\d.,]*) (gam|kilôgam|mililít)")
LAW_MIN = re.compile(r"(\d[\d.,]*) (gam|kilôgam|mililít) trở lên")
NEWS_QUANTITY = re.compile(r"(\d+(?:[.,]\d+)*)\s*(kg|kilôgam|kilogam|ký|kí|gam|gram|gr|g)\b", re.IGNORECASE)
PRINCIPAL_ROLES = {"bị cáo", "bị can", "nghi phạm"}
# Old-style vs new-style tone mark placement: the same word, two different strings.
OLD_TONE_PLACEMENT = {"uý": "úy", "uỳ": "ùy", "uỷ": "ủy", "uỹ": "ũy", "uỵ": "ụy",
                      "oà": "òa", "oá": "óa", "oả": "ỏa", "oã": "õa", "oạ": "ọa"}

def normalize_name(name: str) -> str:
    """Case/whitespace/Unicode-insensitive key for people and substances ('ma tuý' and 'ma túy' are one key)."""
    key = re.sub(r"\s+", " ", unicodedata.normalize("NFC", name or "").strip().strip("\"'“”‘’").lower())
    for old, new in OLD_TONE_PLACEMENT.items():
        key = key.replace(old, new)
    return key

def parse_number(text: str) -> float:
    """Vietnamese number -> float: '9,6' -> 9.6, '1.000' -> 1000, '05' -> 5."""
    text = text.rstrip(".,")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", text):
        return float(text.replace(".", ""))
    return float(text.replace(".", "").replace(",", ".")) if "," in text else float(text)

def amount_to_grams(amount: str) -> float | None:
    """'hơn 9,6kg' -> 9600.0, 'gần 406g' -> 406.0, '5 viên' -> None (cannot be compared with a threshold)."""
    match = NEWS_QUANTITY.search(amount or "")
    if not match:
        return None
    value = parse_number(match.group(1))
    return value * 1000 if match.group(2).lower().startswith("k") else value

def link_substance(name: str) -> str | None:
    """Map a substance mention from the news onto a canonical name; unknown substances keep their own name."""
    key = normalize_name(name)
    if not key:
        return None
    for canonical, aliases in SUBSTANCE_ALIASES.items():
        if key == canonical.lower() or key in aliases:
            return canonical
    # unknown substance: keep it, lower-cased so "Etomidate" and "etomidate" stay one node
    return link_entity(name, list(SUBSTANCE_ALIASES), normalize=normalize_name) or key

def substances_in_text(text: str) -> list[str]:
    """Canonical substances a question mentions, by name or alias (whole words only)."""
    lowered = normalize_name(text)
    found = []
    for canonical, aliases in SUBSTANCE_ALIASES.items():
        if canonical.startswith("ma túy ("):
            continue
        if any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", lowered) for n in [canonical.lower(), *aliases]):
            found.append(canonical)
    return found

def parse_thresholds(clause: dict) -> list[dict]:
    """Regex: one Threshold per 'điểm' that states a quantity range for named substances."""
    thresholds = []
    for point in POINT_LINE.finditer(clause["text"]):
        letter, line = point.group(1), point.group(2).strip()
        head = re.split(r"có khối lượng|có thể tích", line.lower())[0]
        substances = [canonical for phrase, canonical in LAW_SUBSTANCE_PHRASES if phrase in head]
        low = high = unit = None
        if (m := LAW_RANGE.search(line)):
            low, unit, high = parse_number(m.group(1)), m.group(2), parse_number(m.group(3))
            if m.group(2) == "kilôgam":
                low *= 1000
            if m.group(4) == "kilôgam":
                high *= 1000
        elif (m := LAW_MIN.search(line)):
            low, unit = parse_number(m.group(1)), m.group(2)
            if unit == "kilôgam":
                low *= 1000
        if not substances or low is None:
            continue
        thresholds.append({
            "id": f"{clause['id']} điểm {letter}", "point": letter, "text": line.rstrip(";."),
            "unit": "ml" if unit == "mililít" else "g", "min_qty": low, "max_qty": high, "substances": substances,
        })
    return thresholds

def parse_law_article_own(doc: Document) -> dict[str, Any]:
    """parse_law_article + quantity thresholds per clause + base/maximum imprisonment of the article."""
    article = parse_law_article(doc)
    for clause in article["clauses"]:
        clause["thresholds"] = parse_thresholds(clause)
    main = [c for c in article["clauses"] if re.search(r"(?<!\w)tù(?!\w)", c["penalty"])]   # skip fines-only clauses
    article["base_penalty"] = main[0]["penalty"] if main else ""
    article["max_penalty"] = main[-1]["penalty"] if main else ""
    return article

OWN_EXTRACTION_PROMPT = """Bạn trích xuất knowledge graph từ một bài báo tiếng Việt về ma túy.
Chỉ dùng thông tin có trong bài, không suy đoán. Trả về JSON đúng dạng:
{{"cases": [{{
  "name": "tên ngắn của vụ việc, ví dụ: Vụ mua bán 36kg ma túy tại TP.HCM",
  "summary": "1-2 câu tóm tắt: ai, làm gì, kết quả tố tụng",
  "date": "ngày xảy ra/xét xử nếu có, dạng YYYY-MM-DD; ngày không ghi năm thì lấy năm của NGÀY ĐĂNG BÀI",
  "location": "tỉnh/thành phố",
  "charges": ["tội danh, chọn đúng nguyên văn từ DANH SÁCH TỘI DANH"],
  "substances": [{{"name": "tên chất, dùng tên trong DANH SÁCH CHẤT nếu khớp", "amount": "khối lượng của RIÊNG chất này kèm đơn vị như bài viết, ví dụ: hơn 9,6kg"}}],
  "people": [{{"name": "họ tên đầy đủ (tên thật)", "aliases": ["biệt danh riêng, ví dụ: Hoàng Nato"],
               "role": "bị cáo|bị can|nghi phạm|người liên quan|cán bộ",
               "stage": "giai đoạn tố tụng mới nhất của người này: bắt giữ|khởi tố|truy tố|xét xử sơ thẩm|xét xử phúc thẩm",
               "charge": "tội danh của người này (từ DANH SÁCH TỘI DANH)",
               "sentence": "mức án đã tuyên, ví dụ: tử hình, 36 tháng tù"}}]
}}]}}
Quy tắc:
- Trường nào bài không nêu thì để "" (hoặc [] với danh sách), không ghi chữ thay thế.
- Chỉ ghi người được nêu tên trong bài; không tự đặt tên ("Người Trung Quốc 1"). Chức danh, nghề nghiệp
  ("viện trưởng", "TikToker") không phải biệt danh.
- Tội danh ghép ("tàng trữ, mua bán trái phép chất ma túy") thì tách thành từng tội trong DANH SÁCH TỘI DANH.
  Tội không thuộc danh sách (không phải tội về ma túy) thì bỏ qua.
- Tên lóng của chất ("kẹo", "thuốc lắc", "đá", "ke") thì đổi sang tên trong DANH SÁCH CHẤT.
- Người chỉ có biệt danh thì ghi biệt danh vào "name". Không đưa cán bộ, luật sư, phóng viên vào "people" trừ khi họ phạm tội.
- Đoạn cuối bài có thể là tin liên quan về một VỤ KHÁC: tách thành case riêng, không trộn người và chất của hai vụ.
- Bài không nói về vụ việc cụ thể (tuyên truyền, hội nghị, mô hình điểm...) thì trả về {{"cases": []}}.

DANH SÁCH TỘI DANH: {crimes}
DANH SÁCH CHẤT: {substances}

NGÀY ĐĂNG BÀI: {published}
Tiêu đề: {title}
Nội dung:
{content}"""

BLANKS = {"", "chuỗi rỗng", "không rõ", "không có", "none", "null", "n/a"}
ROLES = {"bị cáo", "bị can", "nghi phạm", "người liên quan", "cán bộ"}
PLACEHOLDER_PERSON = re.compile(r"^(người|đối tượng|nghi phạm|bị cáo|bị can) .*[0-9]+$", re.IGNORECASE)

def normalize_role(role: str, stage: str, charge: str) -> str:
    """The LLM sometimes writes a job title ('điều dưỡng') as the role: derive the procedural role instead."""
    if role in ROLES:
        return role
    if stage.startswith("xét xử"):
        return "bị cáo"
    if stage in {"khởi tố", "truy tố"}:
        return "bị can"
    return "nghi phạm" if charge or stage == "bắt giữ" else "người liên quan"

def clean_text(value: Any) -> str:
    """LLM output field -> stripped string; placeholder words for 'nothing' become ''."""
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFC", str(value or "")).strip().strip("\"'“”‘’"))
    return "" if text.lower() in BLANKS else text

def extract_news_cases_own(doc: Document, llm_fn: Callable[[str], str], known_crimes: list[str]) -> list[dict]:
    """LLM extraction for one article; every name the LLM returns is re-linked to a canonical one in code."""
    named = [s for s in SUBSTANCE_ALIASES if not s.startswith(("chất ma túy khác", "ma túy ("))]
    prompt = OWN_EXTRACTION_PROMPT.format(
        crimes="; ".join(known_crimes), substances=", ".join(named),
        published=doc.metadata.get("document_version", "")[:10],
        title=doc.metadata.get("title", ""), content=doc.content[:12000],
    )
    try:
        cases = json.loads(llm_fn(prompt)).get("cases", [])
    except (json.JSONDecodeError, AttributeError):
        return []
    cleaned = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        people = []
        for person in case.get("people") or []:
            name = clean_text(person.get("name"))
            if not name or PLACEHOLDER_PERSON.match(name):         # "Người Trung Quốc 1" is not a person entity
                continue
            charge = link_entity(clean_text(person.get("charge")), known_crimes) or ""
            stage = clean_text(person.get("stage")).lower()
            aliases = [clean_text(a) for a in person.get("aliases") or []]
            people.append({
                "name": name,
                # an alias that is just part of the real name ("Thành") would match unrelated questions
                "aliases": sorted({a for a in aliases if len(a) >= 3 and normalize_name(a) not in normalize_name(name)}),
                "role": normalize_role(clean_text(person.get("role")).lower(), stage, charge), "stage": stage,
                "charge": charge, "sentence": clean_text(person.get("sentence")),
            })
        substances: dict[str, str] = {}
        for item in case.get("substances") or []:
            canonical = link_substance(clean_text(item.get("name")))
            amount = clean_text(item.get("amount"))
            if not re.search(r"\d", amount) or re.search(r"đồng|USD", amount):   # "không xác định", a price
                amount = ""
            if canonical and not substances.get(canonical):
                substances[canonical] = amount
        # one quantity repeated on several substances is a shared total ("100g ma túy các loại"), not each one's
        shared = {a for a in substances.values() if a and list(substances.values()).count(a) > 1}
        charges = {c for c in (link_entity(x, known_crimes) for x in case.get("charges") or []) if c}
        charges |= {p["charge"] for p in people if p["charge"]}        # a person's charge is also the case's
        cleaned.append({
            "name": clean_text(case.get("name")) or doc.metadata.get("title", doc.id),
            "summary": clean_text(case.get("summary")), "date": clean_text(case.get("date")),
            "location": clean_text(case.get("location")), "charges": sorted(charges), "people": people,
            "substances": [{"name": n, "amount": a, "grams": None if a in shared else amount_to_grams(a)}
                           for n, a in substances.items()],
            "sources": [doc.id],
        })
    return cleaned

def resolve_cases(cases: list[dict]) -> list[dict]:
    """Entity resolution across articles, in code (no LLM).

    1. People: a person whose name is another person's alias is the same person ('Hoàng Nato' = 'Dương Minh Tuấn').
    2. Cases: two extracted cases that share an accused person are the same real-world case -> merged into one.
    """
    display: dict[str, str] = {}      # normalized name -> spelling used as the Person key
    alias_of: dict[str, str] = {}     # normalized alias -> normalized real name
    for case in cases:
        for person in case["people"]:
            display.setdefault(normalize_name(person["name"]), person["name"])
            for alias in person["aliases"]:
                alias_of.setdefault(normalize_name(alias), normalize_name(person["name"]))
    for case in cases:
        for person in case["people"]:
            key = normalize_name(person["name"])
            if key in alias_of and alias_of[key] != key:           # this article only knew the nickname
                person["aliases"] = sorted({*person["aliases"], person["name"]})
                key = alias_of[key]
            person["name"] = display[key]

    parent = list(range(len(cases)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_seen: dict[str, int] = {}
    for index, case in enumerate(cases):
        for person in case["people"]:
            if person["role"] in PRINCIPAL_ROLES:
                key = normalize_name(person["name"])
                if key in first_seen:
                    parent[find(index)] = find(first_seen[key])
                else:
                    first_seen[key] = index

    merged: dict[int, dict] = {}
    for index, case in enumerate(cases):
        root = find(index)
        if root not in merged:
            merged[root] = {**case, "people": {}, "substances": {}, "charges": set(), "sources": [], "summaries": []}
        target = merged[root]
        target["charges"] |= set(case["charges"])
        target["sources"] += [s for s in case["sources"] if s not in target["sources"]]
        if case["summary"] and case["summary"] not in target["summaries"]:
            target["summaries"].append(case["summary"])
        for field in ("date", "location"):
            target[field] = target[field] or case[field]
        for substance in case["substances"]:
            known = target["substances"].setdefault(substance["name"], substance)
            if known["grams"] is None and substance["grams"] is not None:
                target["substances"][substance["name"]] = substance
        for person in case["people"]:
            known = target["people"].setdefault(person["name"], person)
            known["aliases"] = sorted({*known["aliases"], *person["aliases"]})
            for field in ("role", "stage", "charge", "sentence"):
                known[field] = person[field] or known[field]        # later article wins when it says something
    result = []
    for case in merged.values():
        principals = sorted(normalize_name(p["name"]) for p in case["people"].values() if p["role"] in PRINCIPAL_ROLES)
        case_id = "case:" + principals[0] if principals else f"case:{case['sources'][0]}:{normalize_name(case['name'])}"
        result.append({**case, "id": case_id, "summary": " ".join(case["summaries"][:3]),
                       "people": list(case["people"].values()), "substances": list(case["substances"].values()),
                       "charges": sorted(case["charges"])})
    return result

# ----------------------------------------------------------------------------------------------
# Neo4j
# ----------------------------------------------------------------------------------------------

class Neo4jGraph:
    """Thin wrapper over the official neo4j driver."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        from neo4j import GraphDatabase

        self.driver = GraphDatabase.driver(uri, auth=(user, password), notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def close(self) -> None:
        self.driver.close()

    def run(self, cypher: str, **params: Any) -> list[dict]:
        records, _, _ = self.driver.execute_query(cypher, params)
        return [record.data() for record in records]

    def reset(self) -> None:
        """Delete every node, relationship and constraint (bench_kg.py calls this before build_graph)."""
        self.run("MATCH (n) DETACH DELETE n")
        for row in self.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            self.run(f"DROP CONSTRAINT `{row['name']}` IF EXISTS")

    def stats(self) -> dict[str, int]:
        nodes = self.run("MATCH (n) RETURN count(n) AS n")[0]["n"]
        rels = self.run("MATCH ()-[r]->() RETURN count(r) AS n")[0]["n"]
        return {"nodes": nodes, "relationships": rels}

    def seed_facts(self, question: str, doc_ids: list[str], skip_labels: tuple[str, ...] = (),
                   limit: int = 60) -> tuple[list[str], list[str]]:
        """Ontology-independent first step: seed nodes + their 1-hop edges as text facts.

        Seeds = nodes whose `doc_id` is in doc_ids, or whose `name`/`aliases` appear in the question.
        Returns (seed elementIds, facts). Nodes with a label in skip_labels are left out of the facts.
        """
        seeds = self.run(
            """
            MATCH (n)
            WHERE n.doc_id IN $doc_ids
               OR (n.name IS :: STRING AND size(n.name) >= 3 AND toLower($q) CONTAINS toLower(n.name))
               OR any(a IN coalesce(n.aliases, []) WHERE size(a) >= 3 AND toLower($q) CONTAINS toLower(a))
            RETURN elementId(n) AS id
            """,
            q=question, doc_ids=doc_ids,
        )
        seed_ids = [row["id"] for row in seeds]
        edges = self.run(
            """
            MATCH (s)-[r]-(m)
            WHERE elementId(s) IN $ids
              AND none(l IN labels(s) + labels(m) WHERE l IN $skip)
            WITH DISTINCT r LIMIT $limit
            WITH startNode(r) AS a, r, endNode(r) AS b
            RETURN labels(a)[0] AS a_label, coalesce(a.name, a.id) AS a_name, type(r) AS rel,
                   properties(r) AS props, labels(b)[0] AS b_label, coalesce(b.name, b.id) AS b_name
            """,
            ids=seed_ids, skip=list(skip_labels), limit=limit,
        )
        facts = []
        for e in edges:
            props = ", ".join(f"{k}: {v}" for k, v in e["props"].items() if v)
            facts.append(f"({e['a_label']}: {e['a_name']}) -[{e['rel']}{' {' + props + '}' if props else ''}]-> "
                         f"({e['b_label']}: {e['b_name']})")
        return seed_ids, facts

    # ---------------------------------------------------------------- HINT — suggested ontology: writes

    def suggested_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Crime", "name"), ("Case", "name"),
                           ("Substance", "name"), ("Person", "name"), ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_law_article(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id}) SET a.title = $title, a.law = $law, a.doc_id = $doc_id
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (s IN clause.substances | MERGE (sub:Substance {name: s}) MERGE (cl)-[:MENTIONS]->(sub))
            """,
            **article,
        )

    def add_news_case(self, case: dict, doc: Document) -> None:
        self.run(
            """
            MERGE (k:Case {name: $name})
              SET k.summary = $summary, k.date = $date, k.doc_id = $doc_id, k.source_title = $title
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = coalesce(p.aliases, [])
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.charge = p.charge, r.sentence = p.sentence)
            """,
            name=case.get("name") or doc.metadata.get("title", doc.id),
            summary=case.get("summary", ""), date=case.get("date", ""), location=case.get("location", ""),
            charges=case.get("charges", []), people=[p for p in case.get("people", []) if p.get("name")],
            substances=[s for s in case.get("substances", []) if s.get("name")],
            doc_id=doc.id, title=doc.metadata.get("title", ""),
        )

    # ---------------------------------------------------------------- own ontology: writes

    def own_constraints(self) -> None:
        for label, key in [("Article", "id"), ("Clause", "id"), ("Threshold", "id"), ("Crime", "name"),
                           ("Substance", "name"), ("Source", "doc_id"), ("Case", "id"), ("Person", "name"),
                           ("Location", "name")]:
            self.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    def add_substances(self) -> None:
        """Substance catalogue first, so law and news MERGE onto the same nodes (with their news aliases)."""
        self.run("UNWIND $rows AS row MERGE (s:Substance {name: row.name}) SET s.aliases = row.aliases",
                 rows=[{"name": n, "aliases": [a for a in aliases if len(a) >= 3]}
                       for n, aliases in SUBSTANCE_ALIASES.items() if not n.startswith("ma túy (")])
        self.run("UNWIND $rows AS row MATCH (s:Substance {name: row.name}) MATCH (c:Substance {name: row.class}) "
                 "MERGE (s)-[:CLASSIFIED_AS]->(c)",
                 rows=[{"name": n, "class": c} for n, c in SUBSTANCE_CLASSES.items()])

    def add_law_article_own(self, article: dict) -> None:
        self.run(
            """
            MERGE (a:Article {id: $id})
              SET a.title = $title, a.law = $law, a.doc_id = $doc_id,
                  a.base_penalty = $base_penalty, a.max_penalty = $max_penalty
            FOREACH (crime IN CASE WHEN $crime IS NULL THEN [] ELSE [$crime] END |
                MERGE (c:Crime {name: crime}) MERGE (a)-[:DEFINES]->(c))
            WITH a
            UNWIND $clauses AS clause
            MERGE (cl:Clause {id: clause.id})
              SET cl.number = clause.number, cl.penalty = clause.penalty, cl.text = clause.text, cl.doc_id = $doc_id
            MERGE (a)-[:HAS_CLAUSE]->(cl)
            FOREACH (t IN clause.thresholds |
                MERGE (th:Threshold {id: t.id})
                  SET th.point = t.point, th.text = t.text, th.unit = t.unit, th.min_qty = t.min_qty,
                      th.max_qty = t.max_qty, th.doc_id = $doc_id
                MERGE (cl)-[:HAS_THRESHOLD]->(th)
                FOREACH (s IN t.substances | MERGE (sub:Substance {name: s}) MERGE (th)-[:OF_SUBSTANCE]->(sub)))
            """,
            **article,
        )

    def add_source(self, doc: Document) -> None:
        self.run("MERGE (s:Source {doc_id: $doc_id}) SET s.title = $title, s.url = $url, s.published = $published",
                 doc_id=doc.id, title=doc.metadata.get("title", ""), url=doc.metadata.get("source_url", ""),
                 published=doc.metadata.get("document_version", "")[:10])

    def add_case_own(self, case: dict) -> None:
        self.run(
            """
            MERGE (k:Case {id: $id}) SET k.name = $name, k.summary = $summary, k.date = $date
            FOREACH (loc IN CASE WHEN $location = '' THEN [] ELSE [$location] END |
                MERGE (l:Location {name: loc}) MERGE (k)-[:LOCATED_IN]->(l))
            FOREACH (crime IN $charges | MERGE (c:Crime {name: crime}) MERGE (k)-[:CHARGED_WITH]->(c))
            FOREACH (s IN $substances | MERGE (sub:Substance {name: s.name}) MERGE (k)-[r:INVOLVES]->(sub)
                SET r.amount = s.amount, r.grams = s.grams)
            FOREACH (p IN $people | MERGE (person:Person {name: p.name})
                SET person.aliases = [a IN coalesce(person.aliases, []) WHERE NOT a IN p.aliases] + p.aliases
                MERGE (person)-[r:INVOLVED_IN]->(k) SET r.role = p.role, r.stage = p.stage, r.sentence = p.sentence
                FOREACH (crime IN CASE WHEN p.charge = '' THEN [] ELSE [p.charge] END |
                    MERGE (c:Crime {name: crime}) MERGE (person)-[:ACCUSED_OF]->(c)))
            WITH k
            UNWIND $sources AS doc_id
            MATCH (s:Source {doc_id: doc_id})
            MERGE (s)-[:REPORTS]->(k)
            """,
            id=case["id"], name=case["name"], summary=case["summary"], date=case["date"], location=case["location"],
            charges=case["charges"], substances=case["substances"], people=case["people"], sources=case["sources"],
        )

    # ---------------------------------------------------------------- KG-3

    def context(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Graph facts for a question: seeds + 1 hop, then the legal basis of every case reached."""
        if use_hint_ontology():
            return self._context_hint(question, doc_ids, max_facts)
        return self._context_own(question, doc_ids, max_facts)

    def _context_own(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Multi-hop over the own ontology: Person/Source -> Case -> Crime -> Article -> Clause -> Threshold."""
        # The generic 1-hop facts of seed_facts repeat what is rendered below, so only the seed ids are kept.
        seed_ids, _ = self.seed_facts(question, doc_ids, limit=1)
        facts: list[str] = []

        # 1. Which cases is the question about? People named in the question win over retrieved documents:
        #    a retrieved chunk may belong to an unrelated article, a named person does not.
        cases = self.run(
            """
            MATCH (p:Person)-[:INVOLVED_IN]->(k:Case) WHERE elementId(p) IN $ids
            RETURN k.id AS id, collect(DISTINCT p.name) AS asked
            """, ids=seed_ids)
        asked_people = sorted({name for row in cases for name in row["asked"]})
        if not cases:
            cases = self.run(
                """
                MATCH (s:Source)-[:REPORTS]->(k:Case) WHERE s.doc_id IN $doc_ids
                RETURN k.id AS id, count(*) AS hits ORDER BY hits DESC, id LIMIT 3
                """, doc_ids=doc_ids)
        case_ids = [row["id"] for row in cases]

        # 2. News side: one block per case (people with role/stage/charge/sentence, substances with quantity).
        for row in self.run(
            """
            MATCH (k:Case) WHERE k.id IN $case_ids
            OPTIONAL MATCH (k)-[:LOCATED_IN]->(l:Location)
            WITH k, collect(l.name)[0] AS location
            RETURN k.name AS name, k.summary AS summary, k.date AS date, location,
                   [(k)-[:CHARGED_WITH]->(c:Crime) | c.name] AS crimes,
                   [(k)-[i:INVOLVES]->(s:Substance) | {name: s.name, amount: i.amount, grams: i.grams}] AS substances,
                   [(p:Person)-[r:INVOLVED_IN]->(k) | {name: p.name, aliases: p.aliases, role: r.role, stage: r.stage,
                        sentence: r.sentence, crimes: [(p)-[:ACCUSED_OF]->(pc:Crime) | pc.name]}] AS people
            ORDER BY name
            """, case_ids=case_ids):
            where_when = ", ".join(x for x in (row["location"], row["date"]) if x)
            facts.append(f"Vụ việc '{row['name']}'{' (' + where_when + ')' if where_when else ''}: {row['summary']}")
            if row["crimes"]:
                facts.append(f"Vụ việc '{row['name']}' - tội danh: {'; '.join(sorted(row['crimes']))}")
            for sub in sorted(row["substances"], key=lambda x: x["name"]):
                grams = f" (= {sub['grams']:g} gam)" if sub["grams"] is not None else ""
                facts.append(f"Vụ việc '{row['name']}' - chất ma túy: {sub['name']}"
                             f"{' ' + sub['amount'] if sub['amount'] else ''}{grams}")
            people = sorted(row["people"], key=lambda p: (p["name"] not in asked_people, p["name"]))
            for person in people[:12]:
                alias = f" (tên gọi khác: {', '.join(person['aliases'])})" if person["aliases"] else ""
                detail = [person["role"], f"giai đoạn: {person['stage']}" if person["stage"] else "",
                          f"tội danh: {'; '.join(person['crimes'])}" if person["crimes"] else "",
                          f"mức án: {person['sentence']}" if person["sentence"] else ""]
                facts.append(f"Vụ việc '{row['name']}' - {person['name']}{alias}: {', '.join(d for d in detail if d)}")

        # 3. Bridge to the law KB. Crimes of the people asked about (ACCUSED_OF) are more precise than the
        #    case-level CHARGED_WITH when co-accused face different charges; fall back to the case otherwise.
        articles = self.run(
            """
            MATCH (p:Person)-[:INVOLVED_IN]->(k:Case) WHERE k.id IN $case_ids AND p.name IN $people
            MATCH (p)-[:ACCUSED_OF]->(:Crime)<-[:DEFINES]-(a:Article)
            RETURN DISTINCT a.id AS id
            """, case_ids=case_ids, people=asked_people)
        if not articles:
            articles = self.run(
                "MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article) WHERE k.id IN $case_ids "
                "RETURN DISTINCT a.id AS id", case_ids=case_ids)
        article_ids = {row["id"] for row in articles}
        # Articles the question names directly ("Điều 251").
        numbers = re.findall(r"[Đđ]iều (\d+)", question)
        article_ids |= {row["id"] for row in self.run(
            "MATCH (a:Article) WHERE any(n IN $numbers WHERE a.id STARTS WITH 'Điều ' + n + ' ') RETURN a.id AS id",
            numbers=numbers)}

        # 4. Law side: the whole penalty ladder of each article, one short line per clause (penalty only, not the
        #    full clause text), so both "khung cơ bản" and "tối đa" questions are covered at low token cost.
        for row in self.run(
            """
            MATCH (a:Article) WHERE a.id IN $article_ids
            RETURN a.id AS id, a.title AS title, a.base_penalty AS base, a.max_penalty AS max,
                   [(a)-[:HAS_CLAUSE]->(cl:Clause) WHERE cl.penalty <> '' | {number: cl.number, penalty: cl.penalty}] AS clauses
            ORDER BY id
            """, article_ids=sorted(article_ids)):
            label = f"[{row['id']} - {row['title']}]"
            for clause in sorted(row["clauses"], key=lambda c: c["number"]):
                facts.append(f"{label} khoản {clause['number']}: {clause['penalty']}")
            if row["base"]:
                facts.append(f"{label} khung cơ bản (khoản 1): {row['base']}; khung cao nhất: {row['max']}")

        # 5. Threshold match: the clause that applies to the quantity seized in the case (grams vs [min, max)).
        for row in self.run(
            """
            MATCH (k:Case)-[i:INVOLVES]->(s:Substance) WHERE k.id IN $case_ids AND i.grams IS NOT NULL
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)-[:HAS_THRESHOLD]->(t:Threshold)-[:OF_SUBSTANCE]->(ts:Substance)
            WHERE a.id IN $article_ids AND (ts = s OR (s)-[:CLASSIFIED_AS]->(ts))
              AND t.unit = 'g' AND t.min_qty <= i.grams AND (t.max_qty IS NULL OR i.grams < t.max_qty)
            RETURN k.name AS case, s.name AS substance, i.amount AS amount, i.grams AS grams, a.id AS article,
                   cl.number AS clause, cl.penalty AS penalty, t.point AS point, t.text AS text
            ORDER BY article, clause DESC, substance
            """, case_ids=case_ids, article_ids=sorted(article_ids)):
            facts.append(f"Áp dụng ngưỡng khối lượng: {row['substance']} {row['amount']} (= {row['grams']:g} gam) trong vụ "
                         f"'{row['case']}' thuộc điểm {row['point']} khoản {row['clause']} {row['article']} "
                         f"(\"{row['text']}\"): {row['penalty']}")

        # 6. Substances the question names: thresholds of the named articles, and every case involving them
        #    (aggregation questions need ALL cases in the graph, not just those the vector search surfaced).
        asked_substances = substances_in_text(question)
        if asked_substances and numbers and not case_ids:
            for row in self.run(
                """
                MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)-[:HAS_THRESHOLD]->(t:Threshold)-[:OF_SUBSTANCE]->(s:Substance)
                WHERE a.id IN $article_ids AND s.name IN $substances
                RETURN a.id AS article, cl.number AS clause, t.point AS point, t.text AS text, cl.penalty AS penalty
                ORDER BY article, clause
                """, article_ids=sorted(article_ids), substances=asked_substances):
                facts.append(f"[{row['article']}] khoản {row['clause']} điểm {row['point']}: {row['text']} -> {row['penalty']}")
        if asked_substances and not asked_people:
            for row in self.run(
                """
                MATCH (k:Case)-[i:INVOLVES]->(s:Substance) WHERE s.name IN $substances
                RETURN s.name AS substance, k.name AS case, i.amount AS amount, k.summary AS summary,
                       [(p:Person)-[r:INVOLVED_IN]->(k) WHERE r.role IN $roles | p.name][..3] AS people,
                       [(src:Source)-[:REPORTS]->(k) | src.title][0] AS source
                ORDER BY substance, case
                """, substances=asked_substances, roles=sorted(PRINCIPAL_ROLES)):
                people = f", người liên quan chính: {', '.join(row['people'])}" if row["people"] else ""
                facts.append(f"Vụ việc có {row['substance']}{' (' + row['amount'] + ')' if row['amount'] else ''}: "
                             f"'{row['case']}'{people} - {row['summary']} [nguồn: {row['source']}]")
        return list(dict.fromkeys(facts))[:max_facts]

    def _context_hint(self, question: str, doc_ids: list[str], max_facts: int = 60) -> list[str]:
        """Baseline retrieval on the suggested ontology, exactly the 4 steps of LAB_GUIDE Bước 5."""
        seed_ids, facts = self.seed_facts(question, doc_ids, skip_labels=("Clause",), limit=max_facts // 2)
        cases = self.run(
            """
            MATCH (k:Case)
            WHERE elementId(k) IN $ids OR EXISTS { MATCH (s)--(k) WHERE elementId(s) IN $ids }
            RETURN elementId(k) AS id, k.name AS name, k.summary AS summary
            """, ids=seed_ids)
        facts += [f"Vụ việc '{row['name']}': {row['summary']}" for row in cases]
        clauses = self.run(
            """
            MATCH (k:Case)-[:CHARGED_WITH]->(:Crime)<-[:DEFINES]-(a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE elementId(k) IN $case_ids
              AND (cl.number = 1 OR EXISTS { (k)-[:INVOLVES]->(:Substance)<-[:MENTIONS]-(cl) })
            RETURN DISTINCT a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            UNION
            MATCH (a:Article)-[:HAS_CLAUSE]->(cl:Clause)
            WHERE any(n IN $numbers WHERE a.id STARTS WITH 'Điều ' + n + ' ')
              AND (cl.number = 1 OR EXISTS { (cl)-[:MENTIONS]->(s:Substance) WHERE s.name IN $substances })
            RETURN DISTINCT a.id AS article, a.title AS title, cl.number AS number, cl.text AS text
            """,
            case_ids=[row["id"] for row in cases], numbers=re.findall(r"[Đđ]iều (\d+)", question),
            substances=find_substances(question))
        for row in sorted(clauses, key=lambda r: (r["article"], r["number"])):
            facts.append(f"[{row['article']} - {row['title']}] khoản {row['number']}: {row['text']}")
        return list(dict.fromkeys(facts))[:max_facts]

# ---------------------------------------------------------------------------------------------- KG-2

def build_graph(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                llm_fn: Callable[..., str]) -> None:
    """Load both KBs into an empty graph. llm_fn(prompt, json_mode=False) -> str (metered OpenAI chat)."""
    if use_hint_ontology():
        _build_graph_hint(graph, law_docs, news_docs, llm_fn)
    else:
        _build_graph_own(graph, law_docs, news_docs, llm_fn)

def _build_graph_own(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                     llm_fn: Callable[..., str]) -> None:
    """Own ontology. Nodes made from ONE document carry doc_id (Article, Clause, Threshold, Source);
    Crime, Substance, Case, Person, Location are shared between documents and deliberately do not."""
    graph.own_constraints()
    graph.add_substances()
    articles = [parse_law_article_own(doc) for doc in law_docs]              # law: regex, no LLM
    for article in articles:
        graph.add_law_article_own(article)
    crimes = [a["crime"] for a in articles if a["crime"]]
    extracted = []
    for doc in news_docs:                                                     # news: one LLM call per article
        graph.add_source(doc)
        extracted += extract_news_cases_own(doc, lambda prompt: llm_fn(prompt, json_mode=True), crimes)
    for case in resolve_cases(extracted):                                     # merge people/cases across articles
        graph.add_case_own(case)

def _build_graph_hint(graph: Neo4jGraph, law_docs: list[Document], news_docs: list[Document],
                      llm_fn: Callable[..., str]) -> None:
    """Suggested ontology, unchanged (KG_ONTOLOGY=hint) - the baseline in report/ONTOLOGY.md mục 7."""
    graph.suggested_constraints()
    articles = [parse_law_article(doc) for doc in law_docs]
    for article in articles:
        graph.add_law_article(article)
    crimes = [a["crime"] for a in articles if a["crime"]]
    for doc in news_docs:
        for case in extract_news_cases(doc, lambda prompt: llm_fn(prompt, json_mode=True), crimes):
            graph.add_news_case(case, doc)

# ---------------------------------------------------------------------------------------------- KG-4

GRAPH_PROMPT = """Trả lời câu hỏi chỉ dựa trên ngữ cảnh (đoạn văn bản và dữ kiện từ knowledge graph).
Nêu rõ số Điều luật khi có. Nếu ngữ cảnh không đủ, nói không đủ thông tin.

Dữ kiện knowledge graph:
{facts}

Đoạn văn bản:
{chunks}

Câu hỏi: {question}
Trả lời:"""

class GraphRAGAgent:
    """Hybrid GraphRAG: the same vector top-k as flat RAG, plus facts expanded from the graph."""

    def __init__(self, store: EmbeddingStore, graph: Neo4jGraph, llm_fn: Callable[[str], str]) -> None:
        self.store = store
        self.graph = graph
        self.llm_fn = llm_fn

    def answer(self, question: str, top_k: int = 3) -> str:
        chunks = self.store.search(question, top_k=top_k)
        doc_ids = list(dict.fromkeys(c["metadata"]["doc_id"] for c in chunks if c["metadata"].get("doc_id")))
        facts = self.graph.context(question, doc_ids)
        prompt = GRAPH_PROMPT.format(
            facts="\n".join(f"- {fact}" for fact in facts) or "(không có)",
            chunks="\n\n".join(f"[{i}] {chunk['content']}" for i, chunk in enumerate(chunks, start=1)),
            question=question,
        )
        return self.llm_fn(prompt)
