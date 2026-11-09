#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Generate an OSCE-style JSON case from MIMIC-IV (ED + HOSP + NOTE + optional CXR).

Hard pre-treatment window (Criterion A):
  - Define t0_med = first medication time after admittime (EMAR > Pharmacy fallback)
  - Keep the admission only if:
      1) t0_med exists
      2) t0_med > admittime
  - HOSP lab/imaging evidence and CXR use [ed_intime or admittime, t0_med).
    The inherited full-stay ED extraction is an exception; see README.md.

Key behaviors:
  - Discharge summaries are charted after discharge; selected historical sections
    (HPI/PMH/ROS/etc.) are parsed and heuristically sanitized, not prospectively verified.
  - Radiology notes are filtered to the diagnostic window and include
    note-level details from note.radiology_detail.
  - Discharge note metadata (note_id/time + discharge_detail) is stored under:
      OSCE_Examination._debug.discharge_meta
  - CXR uses a separately initialized report-only database. No images are read.
  - CXR filtering uses study acquisition time, not report completion/signing time.
    See README.md for inherited retrospective-data limitations.

Usage:
  python osce_extractor.py --db /path/to/database.duckdb \
    --hadm_id <admission-id> --out /path/to/private-case/osce_<admission-id>.json \
    --cxr-db /path/to/private/cxr-reports.duckdb
"""

import argparse
import copy
import json
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import duckdb
from cxr_reports import nonempty_report, open_report_database, validate_report_database

# --------------------------
# Knowledge (ICD code -> knowledge.json)
# --------------------------

# Cache built indices per knowledge_json path to avoid re-loading in batch runs.
_KNOWLEDGE_INDEX_CACHE: Dict[str, Dict[str, Any]] = {}

# Token regexes (accept dots; we normalize by removing dots)
_ICD10_TOKEN_RE = re.compile(r"\b([A-Z][0-9]{2}[0-9A-Z]?(?:\.[0-9A-Z]{1,4})?)\b", re.IGNORECASE)
_ICD9CM_TOKEN_RE = re.compile(
    r"\b("
    r"(?:[0-9]{3}(?:\.[0-9]{1,2})?)"
    r"|(?:V[0-9]{2}(?:\.[0-9]{1,2})?)"
    r"|(?:E[0-9]{3}(?:\.[0-9]{1})?)"
    r")\b",
    re.IGNORECASE,
)

def _norm_code(code: str) -> str:
    """Normalize an ICD code to MIMIC-like form: uppercase, remove spaces and dots."""
    if code is None:
        return ""
    c = str(code).strip().upper()
    c = c.replace(" ", "").replace(".", "")
    return c

def _rollup_icd10_3char(code_norm: str) -> str:
    """Return ICD-10 3-char category (e.g., E119 -> E11, U071 -> U07)."""
    m = re.match(r"^([A-Z][0-9]{2})", code_norm or "")
    return m.group(1) if m else ""

def _rollup_icd9cm(code_norm: str) -> str:
    """Return an ICD-9-CM rollup key: numeric 3 digits, Vxx (3 chars), Exxx (4 chars)."""
    if not code_norm:
        return ""
    if code_norm[0].isdigit():
        return code_norm[:3] if len(code_norm) >= 3 else code_norm
    if code_norm.startswith("V"):
        return code_norm[:3] if len(code_norm) >= 3 else code_norm
    if code_norm.startswith("E"):
        return code_norm[:4] if len(code_norm) >= 4 else code_norm
    return ""

def _extract_tokens(value: Any, token_re: re.Pattern) -> List[str]:
    """Extract and normalize ICD tokens from a field value (string)."""
    if not is_valid_value(value):
        return []
    s = str(value)
    toks = token_re.findall(s)
    out: List[str] = []
    seen = set()
    for t in toks:
        n = _norm_code(t)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out

def build_knowledge_indices(knowledge_json_path: str) -> Dict[str, Any]:
    """
    Build indices for fast lookup:
      - exact_9cm: code -> [item]
      - exact_10cm: code -> [item]
      - rollup_9cm: rollup -> [item]
      - rollup_10: 3char -> [item]   (from ICD-10-CM and ICD-10 fields)
    """
    if knowledge_json_path in _KNOWLEDGE_INDEX_CACHE:
        return _KNOWLEDGE_INDEX_CACHE[knowledge_json_path]

    if not os.path.exists(knowledge_json_path):
        idx = {"_path": knowledge_json_path, "exact_9cm": {}, "exact_10cm": {}, "rollup_9cm": {}, "rollup_10": {}}
        _KNOWLEDGE_INDEX_CACHE[knowledge_json_path] = idx
        return idx

    with open(knowledge_json_path, "r", encoding="utf-8") as f:
        kb = json.load(f)
    if not isinstance(kb, list):
        raise ValueError(f"knowledge.json must be a list, got {type(kb)} from {knowledge_json_path}")

    exact_9cm: Dict[str, List[Dict[str, Any]]] = {}
    exact_10cm: Dict[str, List[Dict[str, Any]]] = {}
    rollup_9cm: Dict[str, List[Dict[str, Any]]] = {}
    rollup_10: Dict[str, List[Dict[str, Any]]] = {}

    def add(map_: Dict[str, List[Dict[str, Any]]], key: str, item: Dict[str, Any]) -> None:
        if not key:
            return
        map_.setdefault(key, []).append(item)

    for item in kb:
        if not isinstance(item, dict):
            continue

        # ICD-9-CM
        for c in _extract_tokens(item.get("ICD-9-CM", ""), _ICD9CM_TOKEN_RE):
            add(exact_9cm, c, item)
            add(rollup_9cm, _rollup_icd9cm(c), item)

        # ICD-10-CM
        for c in _extract_tokens(item.get("ICD-10-CM", ""), _ICD10_TOKEN_RE):
            add(exact_10cm, c, item)
            add(rollup_10, _rollup_icd10_3char(c), item)

        # ICD-10 (WHO 3-char / 4-char) — use as rollup only
        for c in _extract_tokens(item.get("ICD-10", ""), _ICD10_TOKEN_RE):
            add(rollup_10, _rollup_icd10_3char(c) or c, item)

        # (Optional) ICD-9 (WHO) — rarely useful for MIMIC, but could support rollup if present
        for c in _extract_tokens(item.get("ICD-9", ""), _ICD9CM_TOKEN_RE):
            add(rollup_9cm, _rollup_icd9cm(c), item)

    idx = {
        "_path": knowledge_json_path,
        "exact_9cm": exact_9cm,
        "exact_10cm": exact_10cm,
        "rollup_9cm": rollup_9cm,
        "rollup_10": rollup_10,
    }
    _KNOWLEDGE_INDEX_CACHE[knowledge_json_path] = idx
    return idx

def lookup_principal_knowledge(
    principal: Any,
    indices: Dict[str, Any],
    max_items: int = 3,
) -> Any:
    """
    Given Principal_Diagnosis dict {icd_version, icd_code, ...}, return a compact knowledge match record.

    Output schema:
      {
        "icd_version": 9/10,
        "icd_code": "...",
        "match_level": "exact_9cm|rollup_9cm|exact_10cm|rollup_10|none",
        "matched_knowledge": [<knowledge item dict>, ...]  # up to max_items
      }

    If principal is missing/empty -> return "" (keep style consistent with existing fields).
    """
    if not isinstance(principal, dict) or not is_valid_value(principal.get("icd_code")) or not is_valid_value(principal.get("icd_version")):
        return ""

    v = int(principal.get("icd_version"))
    code_norm = _norm_code(principal.get("icd_code"))

    matched: List[Dict[str, Any]] = []
    level = "none"

    if v == 9:
        matched = (indices.get("exact_9cm", {}).get(code_norm) or [])
        if matched:
            level = "exact_9cm"
        else:
            r = _rollup_icd9cm(code_norm)
            if r:
                matched = (indices.get("rollup_9cm", {}).get(r) or [])
                if matched:
                    level = "rollup_9cm"

    elif v == 10:
        matched = (indices.get("exact_10cm", {}).get(code_norm) or [])
        if matched:
            level = "exact_10cm"
        else:
            r = _rollup_icd10_3char(code_norm)
            if r:
                matched = (indices.get("rollup_10", {}).get(r) or [])
                if matched:
                    level = "rollup_10"

    # Limit payload size
    matched = matched[: max(0, int(max_items))] if max_items is not None else matched

    return {
        "icd_version": v,
        "icd_code": code_norm,
        "match_level": level,
        "matched_knowledge": matched,
    }


# --------------------------
# Generic helpers (DB + value utilities)
# --------------------------

BAD_VALUE_STRINGS = {"", "nan", "none", "null", "na", "n/a", "unknown", "unk"}


def rows(conn: duckdb.DuckDBPyConnection, sql: str, params: Tuple = ()) -> List[Dict[str, Any]]:
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def has_table(conn: duckdb.DuckDBPyConnection, qualified: str) -> bool:
    if "." not in qualified:
        raise ValueError("qualified must be like schema.table")
    sch, tb = qualified.split(".", 1)
    r = rows(
        conn,
        """
        SELECT COUNT(*) AS n
        FROM information_schema.tables
        WHERE table_schema = ? AND table_name = ?
        """,
        (sch, tb),
    )
    return bool(r and r[0]["n"] > 0)


def must_table(conn: duckdb.DuckDBPyConnection, qualified: str) -> str:
    if not has_table(conn, qualified):
        raise RuntimeError(f"Missing table: {qualified}")
    return qualified


def maybe_table(conn: duckdb.DuckDBPyConnection, qualified: str) -> Optional[str]:
    return qualified if has_table(conn, qualified) else None


def get_cols(conn: duckdb.DuckDBPyConnection, qualified: str) -> List[str]:
    info = rows(conn, f"PRAGMA table_info('{qualified}')")
    return [r["name"] for r in info if "name" in r]


def pick_first_existing(cols: List[str], candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c in cols:
            return c
    return None


def is_valid_value(v: Any) -> bool:
    if v is None:
        return False
    s = str(v).strip()
    if s == "":
        return False
    if s.lower() in BAD_VALUE_STRINGS:
        return False
    if set(s) <= {"_"}:
        return False
    return True


def dt_to_str(x: Any) -> Optional[str]:
    return None if x is None else str(x)


# --------------------------
# Note parsing helpers (for CC/HPI/PMH/ROS extraction)
# --------------------------

SECTION_HEADINGS = [
    # common discharge summary headings
    "Chief Complaint",
    "History of Present Illness",
    "HPI",
    "Brief Hospital Course",
    "Hospital Course",
    "Past Medical History",
    "PMH",
    "Social History",
    "Review of Systems",
    "ROS",
    "Physical Exam",
    "Physical Examination",
    "Vitals",
    "Vital Signs",

    # Headings which often appear in discharge summaries and should act as section boundaries.
    # We do NOT populate patient-visible content from these sections, but including them here
    # prevents over-capturing (e.g., Physical Exam accidentally swallowing "Pertinent Results").
    "Pertinent Results",
    "Results",
    "REPORTS",
    "Reports",
    "IMPRESSION",
    "Impression",
    "Discharge",
    "Discharge Exam",
    "DISCHARGE EXAM",
    "Admission Exam",
    "ADMISSION EXAM",
    "Discharge Diagnosis",
    "Discharge Diagnoses",
    "Discharge Medications",
    "Discharge Instructions",
    "Followup Instructions",
    "Follow-up Instructions",
    "Discharge Condition",
    "Discharge Disposition",
    "Medications on Admission",
    "Medications on Discharge",
    "Assessment and Plan",
    "A/P",
]


# --------------------------
# Discharge-summary sanitization (anti-leakage)
# --------------------------

# NOTE: Discharge summaries are inherently post-hoc. We only salvage a subset of sections
# (Chief Complaint, HPI, PMH/Social/Family/ROS) and aggressively remove sentences/lines that
# reveal results, diagnoses, treatments, or discharge-time information.

_HARD_DROP_SECTION_NAMES = {
    "brief_course",
}

# Section headings (as text markers) indicating post-hoc content. If they appear inside a block,
# we truncate BEFORE the marker.
_TRUNCATE_MARKERS_RE = re.compile(
    r"(?im)^(?:\s*)(Pertinent Results|REPORTS|Reports|IMPRESSION|Impression|DISCHARGE EXAM|Discharge Exam|Discharge:|Discharge Diagnosis|Discharge Medications|Discharge Instructions|Followup Instructions|Follow-up Instructions|Brief Hospital Course|Hospital Course|Assessment and Plan|A/P)\b.*$"
)

# Keywords which should NOT appear in the *sanitized HPI*.
_HPI_FORBIDDEN_RE = re.compile(
    r"(?i)\b(CT|CXR|X\s*-?\s*ray|MRI|ultra\s*sound|US\b|echo|TTE|TEE|cath|catheterization|angiogram|EKG|ECG|culture\s+grew|grew\s+\w+|diagnosed\s+with|discharge(?:d)?\s+on|treated\s+with|started\s+on|received|was\s+given|given\s+\w+|workup\s+revealed|revealed|showed|demonstrated|confirmed|impression|reports?)\b"
)

# Additional post-hoc / results-ish phrases to remove from any salvageable section.
_GENERAL_LEAK_RE = re.compile(
    r"(?i)\b(discharg(?:e|ed)|hospital\s+course|brief\s+hospital\s+course|pertinent\s+results|assessment\s+and\s+plan|follow\s*-?up\s+instructions|discharge\s+diagnos(?:is|es)|disposition|condition\b|on\s+discharge)\b"
)

# Chief-complaint diagnosis-like terms (heuristic). If CC looks like a diagnosis label, drop it.
_CC_DIAG_WORDS_RE = re.compile(
    r"(?i)\b(pneumonia|sepsis|stroke|infarct|myocardial\s+infarction|mi\b|chf\b|congestive\s+heart\s+failure|copd\b|uti\b|pyelonephritis|appendicitis|pancreatitis|dka\b|pulmonary\s+embol(?:us|ism)|pe\b|atrial\s+fibrillation|afib\b)\b"
)


def _truncate_on_markers(text: str) -> str:
    """Truncate a block BEFORE any obvious post-hoc marker line."""
    if not text:
        return text
    t = _normalize_note_text(text)
    m = _TRUNCATE_MARKERS_RE.search(t)
    if not m:
        return t
    return t[: m.start()].strip()


def _filter_lines(text: str, forbidden_res: List[re.Pattern]) -> str:
    """Line-based filtering; removes any line that matches any forbidden regex."""
    if not text:
        return text
    lines = _normalize_note_text(text).split("\n")
    kept: List[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            kept.append("")
            continue
        drop = False
        for rx in forbidden_res:
            if rx.search(s):
                drop = True
                break
        if not drop:
            kept.append(ln)
    # collapse excessive blank lines
    out = "\n".join(kept)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def sanitize_discharge_chief_complaint(chief: Optional[str]) -> Optional[str]:
    if not is_valid_value(chief):
        return None
    c = _normalize_note_text(str(chief))
    # Heuristic: if CC is a short diagnosis label, treat as contaminated.
    if len(c) <= 80 and _CC_DIAG_WORDS_RE.search(c):
        return None
    # Remove any trailing post-hoc phrases if present.
    c = re.sub(r"(?i)\b(admitted\s+for|discharge\s+diagnos(?:is|es)\b).*$", "", c).strip()
    return c if is_valid_value(c) else None


def sanitize_discharge_hpi(hpi: Optional[str]) -> Optional[str]:
    if not is_valid_value(hpi):
        return None
    # Light cleaning: only remove lines with explicit result/diagnosis cues.
    t = _normalize_note_text(str(hpi))
    t = _filter_lines(t, forbidden_res=[_HPI_FORBIDDEN_RE])
    return t if is_valid_value(t) else None


def sanitize_discharge_background(text: Optional[str]) -> Optional[str]:
    """For PMH/Social/Family/ROS: very light cleaning (normalize only)."""
    if not is_valid_value(text):
        return None
    t = _normalize_note_text(str(text))
    return t if is_valid_value(t) else None


def sanitize_discharge_physical_exam(phys_exam: Optional[str]) -> Optional[str]:
    """Keep PE text but drop lines that look like imaging/results."""
    if not is_valid_value(phys_exam):
        return None
    t = _normalize_note_text(str(phys_exam))
    # Keep only the ON ADMISSION portion; drop ON DISCHARGE to avoid leakage.
    adm_re = re.compile(r"(?im)^\s*(?:on admission|admission)\s*:?\s*$")
    dis_re = re.compile(r"(?im)^\s*(?:physical exam on discharge|on discharge)\s*:?\s*$")
    m_adm = adm_re.search(t)
    m_dis = dis_re.search(t)
    if m_adm:
        start = m_adm.end()
        end = m_dis.start() if m_dis and m_dis.start() > start else len(t)
        t = t[start:end].strip()
    elif m_dis:
        # No explicit admission marker; keep text before discharge marker.
        t = t[:m_dis.start()].strip()
    t = _filter_lines(t, forbidden_res=[re.compile(r"(?i)\b(CT|CXR|MRI|echo|TTE|TEE|cath|impression|reports?)\b")])
    return t if is_valid_value(t) else None


def sanitize_discharge_parsed(ds: Dict[str, Any]) -> Dict[str, Any]:
    """Apply your agreed anti-leakage rules to parsed discharge-summary fields."""
    if not isinstance(ds, dict):
        return ds

    out = dict(ds)
    out["chief"] = sanitize_discharge_chief_complaint(out.get("chief"))
    out["hpi"] = sanitize_discharge_hpi(out.get("hpi"))
    # Brief hospital course must be forbidden.
    out["brief_course"] = None
    out["pmh"] = sanitize_discharge_background(out.get("pmh"))
    out["social"] = sanitize_discharge_background(out.get("social"))
    out["ros"] = sanitize_discharge_background(out.get("ros"))
    out["phys_exam_text"] = sanitize_discharge_physical_exam(out.get("phys_exam_text"))

    return out

def _normalize_note_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def _find_all_headings(text: str, headings: List[str]) -> List[Tuple[int, str, int]]:
    """Return list of (start_index, heading_name, end_of_heading_line_index)."""
    out: List[Tuple[int, str, int]] = []
    escaped = [re.escape(h) for h in sorted(set(headings), key=len, reverse=True)]
    pat = r"(?im)^(?:\s*)(%s)\s*(?::|::)?\s*$" % "|".join(escaped)
    for m in re.finditer(pat, text):
        out.append((m.start(), m.group(1), m.end()))
    return sorted(out, key=lambda x: x[0])

def _extract_section(text: str, heading_variants: List[str], all_headings: List[str]) -> Optional[str]:
    """Extract section content for the FIRST occurrence of any variant."""
    if not text:
        return None
    text_n = _normalize_note_text(text)
    hs = _find_all_headings(text_n, all_headings)
    if not hs:
        return None

    variant_lower = {v.lower() for v in heading_variants}
    start_tuple = None
    for (pos, hname, hend) in hs:
        if hname.lower() in variant_lower:
            start_tuple = (pos, hname, hend)
            break
    if not start_tuple:
        return None

    start_pos, _hname, head_end = start_tuple
    content_start = head_end
    end_pos = len(text_n)
    for (pos, h2, _hend2) in hs:
        if pos > start_pos:
            # Skip nested repetition of same heading (e.g. "PHYSICAL EXAM:" inside Physical Exam section)
            if h2.lower() in variant_lower:
                continue
            end_pos = pos
            break
    content = text_n[content_start:end_pos].strip()
    return content if is_valid_value(content) else None

def _extract_inline_field(text: str, label: str) -> Optional[str]:
    """Extract inline 'Label: value' style on the same line."""
    if not text:
        return None
    text_n = _normalize_note_text(text)
    m = re.search(rf"(?im)^\s*{re.escape(label)}\s*(?::|::)\s*(.+?)\s*$", text_n)
    if not m:
        return None
    v = m.group(1).strip()
    return v if is_valid_value(v) else None

def _parse_vitals_from_text(text: str) -> Dict[str, Optional[str]]:
    """Best-effort extraction of vitals from a block of text."""
    out = {"heart_rate": None, "bp": None, "resp_rate": None, "temp": None, "spo2": None}
    if not text:
        return out
    t = _normalize_note_text(text)

    def pick(patterns):
        for p in patterns:
            m = re.search(p, t, flags=re.IGNORECASE | re.MULTILINE)
            if m:
                return m.group(1).strip()
        return None

    out["bp"] = pick([
        r"(?:BP|Blood Pressure)\s*[:=]?\s*([0-9]{2,3}\s*/\s*[0-9]{2,3})",
        r"\b([0-9]{2,3}\s*/\s*[0-9]{2,3})\s*(?:mmHg)?\b",
    ])
    out["heart_rate"] = pick([
        r"(?:HR|Heart Rate|Pulse)\s*[:=]?\s*([0-9]{2,3})\b",
    ])
    out["resp_rate"] = pick([
        r"(?:RR|Resp(?:iratory)? Rate)\s*[:=]?\s*([0-9]{1,2})\b",
    ])
    out["temp"] = pick([
        r"(?:Temp(?:erature)?)\s*[:=]?\s*([0-9]{2,3}(?:\.[0-9])?)\s*(?:[FC])?\b",
    ])
    out["spo2"] = pick([
        r"(?:SpO2|O2 Sat(?:uration)?|Oxygen Saturation)\s*[:=]?\s*([0-9]{2,3})\s*%?",
    ])
    return out

def _derive_symptoms(chief: Optional[str], hpi: Optional[str]) -> Tuple[Optional[str], List[str]]:
    """Heuristic: primary symptom = chief complaint; secondary symptoms from the first line of HPI."""
    primary = chief if is_valid_value(chief) else None
    secondary: List[str] = []
    if hpi and is_valid_value(hpi):
        first = _normalize_note_text(hpi).split("\n")[0]
        m = re.search(r"(?i)\bwith\b(.+)$", first)
        cand = m.group(1) if m else None
        if cand:
            cand = re.split(r"(?i)\bdenies\b|\bwithout\b|\bno\b", cand)[0]
            parts = re.split(r",|;|\band\b", cand)
            parts = [p.strip(" .:-") for p in parts if is_valid_value(p)]
            secondary = [p for p in parts if len(p) <= 80][:6]
    if primary:
        secondary = [s for s in secondary if s.lower() not in primary.lower()]
    return primary, secondary

def _build_objective_for_doctor(
    chief: Optional[str],
    primary_symptom: Optional[str],
    secondary_symptoms: List[str],
    hpi: Optional[str],
    demographics: Optional[Dict[str, Any]],
) -> str:
    def clean_one(v: Optional[str]) -> Optional[str]:
        if not is_valid_value(v):
            return None
        s = str(v).strip()
        s = re.sub(r"\s+", " ", s)
        return s if is_valid_value(s) else None

    parts: List[str] = []
    primary = clean_one(primary_symptom) or clean_one(chief)
    if primary:
        parts.append(primary)

    if secondary_symptoms:
        for s in secondary_symptoms:
            cs = clean_one(s)
            if cs and cs not in parts:
                parts.append(cs)
            if len(parts) >= 2:
                break

    if not parts and is_valid_value(hpi):
        h = clean_one(hpi)
        if h:
            first = h.split(".")[0].split("\n")[0].strip()
            if first:
                parts.append(first[:160])

    demo_prefix = ""
    if isinstance(demographics, dict):
        age = demographics.get("age")
        gender = demographics.get("gender")
        age_str = None
        try:
            if is_valid_value(age):
                age_str = f"{int(float(age))}-year-old"
        except Exception:
            age_str = None
        gender_str = None
        if is_valid_value(gender):
            g = str(gender).strip().lower()
            if g in ("m", "male"):
                gender_str = "male"
            elif g in ("f", "female"):
                gender_str = "female"
            else:
                gender_str = g
        if age_str and gender_str:
            demo_prefix = f"{age_str} {gender_str} "
        elif age_str:
            demo_prefix = f"{age_str} "
        elif gender_str:
            demo_prefix = f"{gender_str} "

    if not parts:
        return f"Assess and diagnose the {demo_prefix}patient based on the available clinical presentation.".strip()

    if len(parts) == 1:
        return f"Assess and diagnose the {demo_prefix}patient presenting with {parts[0]}."
    return f"Assess and diagnose the {demo_prefix}patient presenting with {parts[0]} and {parts[1]}."

def extract_discharge_summary_text(
    conn: duckdb.DuckDBPyConnection,
    hadm_id: int,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Fetch discharge summary note text (best-effort).

    Priority:
      1) note.discharge (MIMIC-IV-Note schema)
      2) fallback: other note.* tables that look like discharge summaries
    """
    if has_table(conn, "note.discharge"):
        r = rows(
            conn,
            """
            SELECT note_id, charttime, storetime, text
            FROM note.discharge
            WHERE hadm_id = ?
            ORDER BY charttime DESC NULLS LAST, storetime DESC NULLS LAST
            LIMIT 1
            """,
            (hadm_id,),
        )
        if r and is_valid_value(r[0].get("text")):
            meta = {k: r[0].get(k) for k in ("note_id", "charttime", "storetime")}
            details = extract_discharge_detail(conn, r[0].get("note_id"))
            if details:
                meta["details"] = details
            return str(r[0]["text"]), meta

    candidates = [
        "note.note",
        "note.physician",
        "note.nursing",
        "note.ed",
        "note.discharge_summary",
    ]
    for tb in candidates:
        if not has_table(conn, tb):
            continue
        cols = set(get_cols(conn, tb))
        cat_col = pick_first_existing(list(cols), ["category", "note_type", "note_category", "type"])
        text_col = pick_first_existing(list(cols), ["text", "note_text"])
        if not text_col:
            continue
        if cat_col:
            q = f"""
            SELECT note_id, charttime, storetime, {cat_col} AS category, {text_col} AS text
            FROM {tb}
            WHERE hadm_id = ?
              AND {text_col} IS NOT NULL
              AND lower({cat_col}) LIKE '%discharge%'
            ORDER BY charttime DESC NULLS LAST, storetime DESC NULLS LAST
            LIMIT 1
            """
        else:
            q = f"""
            SELECT note_id, charttime, storetime, NULL AS category, {text_col} AS text
            FROM {tb}
            WHERE hadm_id = ?
              AND {text_col} IS NOT NULL
            ORDER BY charttime DESC NULLS LAST, storetime DESC NULLS LAST
            LIMIT 1
            """
        r = rows(conn, q, (hadm_id,))
        if r and is_valid_value(r[0].get("text")):
            meta = {k: r[0].get(k) for k in ("note_id", "charttime", "storetime", "category") if k in r[0]}
            details = extract_discharge_detail(conn, r[0].get("note_id"))
            if details:
                meta["details"] = details
            return str(r[0]["text"]), meta

    return None, None

def extract_discharge_detail(
    conn: duckdb.DuckDBPyConnection,
    note_id: Any,
    max_rows: int = 2000,
) -> List[Dict[str, Any]]:
    """Extract discharge_detail rows for a given note_id."""
    detail = maybe_table(conn, "note.discharge_detail")
    if not detail or note_id is None:
        return []

    cols = set(get_cols(conn, detail))
    preferred = ["note_id", "subject_id", "field_name", "field_value", "field_ordinal"]
    sel = [c for c in preferred if c in cols] or ["*"]
    order_sql = "ORDER BY field_ordinal NULLS LAST" if "field_ordinal" in cols else ""
    sql = f"""
    SELECT {', '.join(sel)}
    FROM {detail}
    WHERE note_id = ?
    {order_sql}
    LIMIT {int(max_rows)}
    """
    return rows(conn, sql, (note_id,))

def parse_history_from_discharge_summary(ds_text: Optional[str]) -> Dict[str, Any]:
    """Parse desired fields from discharge summary text (best-effort)."""
    if not ds_text:
        return {
            "chief": None,
            "hpi": None,
            "brief_course": None,
            "pmh": None,
            "social": None,
            "ros": None,
            "phys_exam_text": None,
            "vitals": {"heart_rate": None, "bp": None, "resp_rate": None, "temp": None, "spo2": None},
        }

    all_h = SECTION_HEADINGS

    chief = _extract_inline_field(ds_text, "Chief Complaint") or _extract_section(ds_text, ["Chief Complaint"], all_h)
    hpi = _extract_section(ds_text, ["History of Present Illness", "HPI"], all_h)
    brief = _extract_section(ds_text, ["Brief Hospital Course", "Hospital Course"], all_h)
    pmh = _extract_section(ds_text, ["Past Medical History", "PMH"], all_h)
    social = _extract_section(ds_text, ["Social History"], all_h)
    phys_exam = _extract_section(ds_text, ["Physical Exam", "Physical Examination"], all_h)

    vitals = _parse_vitals_from_text(phys_exam or ds_text)

    return {
        "chief": chief,
        "hpi": hpi,
        "brief_course": brief,
        "pmh": pmh,
        "social": social,
        "ros": None,
        "phys_exam_text": phys_exam,
        "vitals": vitals,
    }

def _vitals_from_ed_triage(tri: Optional[Dict[str, Any]]) -> Dict[str, Optional[str]]:
    """Map ED triage vitals to case vitals dict."""
    out = {"heart_rate": None, "bp": None, "resp_rate": None, "temp": None, "spo2": None}
    if not isinstance(tri, dict) or not tri:
        return out

    def norm(v):
        if v is None:
            return None
        s = str(v).strip()
        return s if is_valid_value(s) else None

    temp = norm(tri.get("temperature"))
    hr = norm(tri.get("heartrate"))
    rr = norm(tri.get("resprate"))
    spo2 = norm(tri.get("o2sat"))
    sbp = norm(tri.get("sbp"))
    dbp = norm(tri.get("dbp"))
    bp = None
    if sbp and dbp:
        bp = f"{sbp}/{dbp}"

    out["temp"] = temp
    out["heart_rate"] = hr
    out["resp_rate"] = rr
    out["spo2"] = spo2
    out["bp"] = bp
    return out

def _merge_vitals(preferred: Dict[str, Optional[str]], fallback: Dict[str, Optional[str]]) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for k in ["heart_rate", "bp", "resp_rate", "temp", "spo2"]:
        v = preferred.get(k)
        out[k] = v if is_valid_value(v) else fallback.get(k)
    return out




# --------------------------
# ED linkage helpers (ED -> HOSP)
# --------------------------

def detect_ed_schema(conn: duckdb.DuckDBPyConnection) -> Optional[str]:
    """Detect the schema that contains the MIMIC-IV-ED tables (edstays, triage, ...).

    In your database, this is typically 'mimiciv_ed'. We auto-detect to avoid hard-coding.
    """
    r = rows(
        conn,
        """
        SELECT table_schema
        FROM information_schema.tables
        WHERE table_name = 'edstays'
        ORDER BY
          CASE WHEN table_schema = 'mimiciv_ed' THEN 0
               WHEN table_schema = 'ed' THEN 1
               ELSE 2 END,
          table_schema
        LIMIT 1
        """,
    )
    if not r:
        return None
    return str(r[0]["table_schema"])


def get_unique_ed_stay_for_hadm_or_raise(
    conn: duckdb.DuckDBPyConnection,
    ed_schema: str,
    hadm_id: int,
) -> Dict[str, Any]:
    """Return the unique ED stay row for this admission.

    Case eligibility requirement (1_stays_per_hadm):
      - The admission must be reachable from ED (edstays.hadm_id is not NULL)
      - There must be exactly ONE edstays row for this hadm_id
    """
    edstays = must_table(conn, f"{ed_schema}.edstays")
    rs = rows(
        conn,
        f"""
        SELECT *
        FROM {edstays}
        WHERE hadm_id = ?
        ORDER BY intime NULLS LAST, outtime NULLS LAST, stay_id
        """,
        (hadm_id,),
    )
    if not rs:
        raise ValueError(f"not ed+hosp: hadm_id={hadm_id} has no row in {ed_schema}.edstays")
    # Enforce 1_stays_per_hadm strictly.
    # (There is normally 1 row per stay_id; we require exactly one row here.)
    if len(rs) != 1:
        stay_ids = sorted({r.get("stay_id") for r in rs if r.get("stay_id") is not None})
        raise ValueError(
            f"violates 1_stays_per_hadm: hadm_id={hadm_id} maps to {len(rs)} ED rows (stay_ids={stay_ids[:10]})"
        )
    stay_id = rs[0].get("stay_id")
    if stay_id is None:
        raise ValueError(f"ED stay_id is NULL for hadm_id={hadm_id} in {ed_schema}.edstays")
    return rs[0]


def extract_ed_tables_for_stay(
    conn: duckdb.DuckDBPyConnection,
    ed_schema: str,
    stay_id: int,
    max_rows_per_table: int = 5000,
) -> Dict[str, Any]:
    """Extract all ED-module records for a given ED stay_id.

    Per your requirement, we do NOT exclude ED treatments/medications.
    We simply return the full ED records for the stay.
    """
    out: Dict[str, Any] = {}

    # Always include the anchor row.
    out["edstays"] = rows(
        conn,
        f"SELECT * FROM {ed_schema}.edstays WHERE stay_id = ? LIMIT 1",
        (stay_id,),
    )
    out["edstays"] = out["edstays"][0] if out["edstays"] else None

    # triage: usually 1 row
    if has_table(conn, f"{ed_schema}.triage"):
        tri = rows(conn, f"SELECT * FROM {ed_schema}.triage WHERE stay_id = ? LIMIT 1", (stay_id,))
        out["triage"] = tri[0] if tri else None
    else:
        out["triage"] = None

    # vitalsign: time series
    if has_table(conn, f"{ed_schema}.vitalsign"):
        vs = rows(
            conn,
            f"""
            SELECT *
            FROM {ed_schema}.vitalsign
            WHERE stay_id = ?
            ORDER BY charttime NULLS LAST
            LIMIT {int(max_rows_per_table)}
            """,
            (stay_id,),
        )
        # Remove rhythm field from ED vitals
        for r in vs:
            if isinstance(r, dict):
                r.pop("rhythm", None)
        out["vitalsign"] = vs
    else:
        out["vitalsign"] = []

    # diagnosis: coded ED diagnoses (ICD)
    if has_table(conn, f"{ed_schema}.diagnosis"):
        dx = rows(
            conn,
            f"""
            SELECT *
            FROM {ed_schema}.diagnosis
            WHERE stay_id = ?
            ORDER BY seq_num NULLS LAST
            LIMIT {int(max_rows_per_table)}
            """,
            (stay_id,),
        )
        out["diagnosis"] = dx
    else:
        out["diagnosis"] = []

    # medrecon: medication reconciliation
    if has_table(conn, f"{ed_schema}.medrecon"):
        mr = rows(
            conn,
            f"""
            SELECT *
            FROM {ed_schema}.medrecon
            WHERE stay_id = ?
            ORDER BY charttime NULLS LAST
            LIMIT {int(max_rows_per_table)}
            """,
            (stay_id,),
        )
        # Remove unused medrecon identifiers
        for r in mr:
            if isinstance(r, dict):
                r.pop("gsn", None)
                r.pop("ndc", None)
                r.pop("etc_rn", None)
                r.pop("etccode", None)
        out["medrecon"] = mr
    else:
        out["medrecon"] = []

    # pyxis: ED dispensation records
    if has_table(conn, f"{ed_schema}.pyxis"):
        px = rows(
            conn,
            f"""
            SELECT *
            FROM {ed_schema}.pyxis
            WHERE stay_id = ?
            ORDER BY charttime NULLS LAST
            LIMIT {int(max_rows_per_table)}
            """,
            (stay_id,),
        )
        # Remove unused pyxis identifiers
        for r in px:
            if isinstance(r, dict):
                r.pop("med_rn", None)
                r.pop("gsn_rn", None)
                r.pop("gsn", None)
        out["pyxis"] = px
    else:
        out["pyxis"] = []

    # Remove identifiers from all ED outputs
    def _drop_ed_ids(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("subject_id", None)
            node.pop("hadm_id", None)
            node.pop("stay_id", None)
            for v in node.values():
                _drop_ed_ids(v)
        elif isinstance(node, list):
            for v in node:
                _drop_ed_ids(v)

    _drop_ed_ids(out)
    return out



# --------------------------
# Time / filtering logic (Criterion A)
# --------------------------


def get_admission_row(conn: duckdb.DuckDBPyConnection, hadm_id: int) -> Dict[str, Any]:
    admissions = must_table(conn, "hosp.admissions")
    adm = rows(conn, f"SELECT * FROM {admissions} WHERE hadm_id = ? LIMIT 1", (hadm_id,))
    if not adm:
        raise ValueError(f"hadm_id={hadm_id} not found in hosp.admissions")
    return adm[0]


def get_first_hosp_med_time(conn: duckdb.DuckDBPyConnection, hadm_id: int, admittime: datetime) -> Optional[datetime]:
    """t0_med = first medication time after admittime. Fallback: EMAR > Pharmacy."""
    emar = maybe_table(conn, "hosp.emar")
    if emar:
        out = rows(
            conn,
            f"""
            SELECT MIN(charttime) AS t0
            FROM {emar}
            WHERE hadm_id = ?
              AND charttime IS NOT NULL
              AND charttime > ?
            """,
            (hadm_id, admittime),
        )
        t0 = out[0]["t0"] if out else None
        if t0 is not None:
            return t0

    pharmacy = maybe_table(conn, "hosp.pharmacy")
    if pharmacy:
        out = rows(
            conn,
            f"""
            SELECT MIN(starttime) AS t0
            FROM {pharmacy}
            WHERE hadm_id = ?
              AND starttime IS NOT NULL
              AND starttime > ?
            """,
            (hadm_id, admittime),
        )
        t0 = out[0]["t0"] if out else None
        if t0 is not None:
            return t0

    return None


def compute_diag_window_or_raise(
    conn: duckdb.DuckDBPyConnection,
    hadm_id: int,
    diagnostic_hours: float,
) -> Tuple[Dict[str, Any], datetime, datetime, datetime, Optional[Dict[str, Any]], Optional[str]]:
    """Return (admission_row, t_start, t_end, t0_med, ed_stay_row, ed_schema).

    Case eligibility requirements:
      - ED+hosp only: hadm_id must appear in ED edstays (hadm_id not NULL)
      - 1_stays_per_hadm: exactly one ED stay row maps to this hadm_id
      - Criterion A (hard pre-treatment window):
          * t0_med exists and t0_med > admittime

    Notes:
      - We keep the signature with diagnostic_hours for backward compatibility,
        but Criterion A does not depend on a fixed-hours window here; the window ends at t0_med.
    """
    # HOSP admission
    adm = get_admission_row(conn, hadm_id)
    admittime = adm.get("admittime")
    if admittime is None:
        raise ValueError("missing admittime")
    if not isinstance(admittime, datetime):
        admittime = datetime.fromisoformat(str(admittime))

    # ED linkage (optional; hosp_only cases allowed)
    ed_schema = detect_ed_schema(conn)
    ed_stay: Optional[Dict[str, Any]] = None
    if ed_schema:
        try:
            ed_stay = get_unique_ed_stay_for_hadm_or_raise(conn, ed_schema, hadm_id)
        except ValueError as e:
            # Allow hosp-only cases (no matching ED stay); other violations still raise.
            if "not ed+hosp" not in str(e):
                raise

    # Criterion A: first inpatient medication time
    t0_med = get_first_hosp_med_time(conn, hadm_id, admittime)
    if t0_med is None:
        raise ValueError("t0_med is NULL (no emar/pharmacy timing)")
    if not isinstance(t0_med, datetime):
        t0_med = datetime.fromisoformat(str(t0_med))
        # Hard requirement: must be strictly after admittime
    if t0_med <= admittime:
        raise ValueError(f"t0_med <= admittime (t0_med={t0_med}, admittime={admittime})")

    # HOSP window (kept for reference / debugging; evidence windows may differ)
    t_start = admittime
    t_end = t0_med

    return adm, t_start, t_end, t0_med, ed_stay, ed_schema


# --------------------------
# Dictionaries / maps (ICD + lab metadata)
# --------------------------


def build_icd_title_map(conn: duckdb.DuckDBPyConnection) -> Dict[Tuple[str, int], str]:
    m: Dict[Tuple[str, int], str] = {}
    d = maybe_table(conn, "hosp.d_icd_diagnoses")
    if not d:
        return m
    for r in rows(conn, f"SELECT icd_code, icd_version, long_title FROM {d}"):
        code = r.get("icd_code")
        ver = r.get("icd_version")
        title = r.get("long_title")
        if code is None or ver is None or title is None:
            continue
        try:
            ver_i = int(ver)
        except Exception:
            continue
        m[(str(code), ver_i)] = str(title)
    return m


def build_lab_item_map(conn: duckdb.DuckDBPyConnection) -> Dict[int, Dict[str, Any]]:
    d = maybe_table(conn, "hosp.d_labitems")
    if not d:
        return {}
    sample = rows(conn, f"SELECT * FROM {d} LIMIT 1")
    cols = set(sample[0].keys()) if sample else set()
    fields = ["itemid", "label"]
    for opt in ["fluid", "category", "loinc_code"]:
        if opt in cols:
            fields.append(opt)
    sql = f"SELECT {', '.join(fields)} FROM {d}"
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows(conn, sql):
        itemid = r.get("itemid")
        if itemid is None:
            continue
        try:
            iid = int(itemid)
        except Exception:
            continue
        out[iid] = {k: r.get(k) for k in fields if k != "itemid"}
    return out


# --------------------------
# Extraction functions (time filtered to diagnostic window)
# --------------------------


def extract_meta_and_demographics(
    conn: duckdb.DuckDBPyConnection,
    hadm_id: int,
    ed_stay: Optional[Dict[str, Any]],
    t_start: datetime,
    t_end: datetime,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    admissions = must_table(conn, "hosp.admissions")
    patients = must_table(conn, "hosp.patients")

    adm = rows(conn, f"SELECT * FROM {admissions} WHERE hadm_id = ? LIMIT 1", (hadm_id,))
    if not adm:
        raise ValueError(f"hadm_id={hadm_id} not found in hosp.admissions")
    adm = adm[0]
    subject_id = int(adm["subject_id"])

    pat = rows(conn, f"SELECT * FROM {patients} WHERE subject_id = ? LIMIT 1", (subject_id,))
    pat = pat[0] if pat else {}

    # Meta captures key identifiers + admission context (used throughout JSON)
    # When ed_stay exists: populate ED fields (align with au_with_cxr_reports).
    # When ed_stay is None (hosp-only): ED fields are null.
    meta = {
        "subject_id": subject_id,
        "hadm_id": hadm_id,
        "stay_id": int(ed_stay.get("stay_id")) if ed_stay and ed_stay.get("stay_id") is not None else None,
        "ed_intime": ed_stay.get("intime") if ed_stay else None,
        "ed_outtime": ed_stay.get("outtime") if ed_stay else None,
        "admittime": adm.get("admittime"),
        "dischtime": adm.get("dischtime"),
        "admission_type": adm.get("admission_type"),
        "admission_location": adm.get("admission_location"),
        "diagnostic_window": {"start": dt_to_str(t_start), "end": dt_to_str(t_end)},
    }

    demographics = {
        "gender": pat.get("gender") or (ed_stay.get("gender") if ed_stay else None),
        "age": pat.get("anchor_age"),
        "language": adm.get("language"),
        "race": adm.get("race") or (ed_stay.get("race") if ed_stay else None),
        "marital_status": adm.get("marital_status"),
        "insurance": adm.get("insurance"),
        "_time_deid": {
            "anchor_year": pat.get("anchor_year"),
            "anchor_year_group": pat.get("anchor_year_group"),
        },
        "_ed": {
            "arrival_transport": ed_stay.get("arrival_transport") if ed_stay else None,
            "disposition": ed_stay.get("disposition") if ed_stay else None,
        },
    }

    return meta, demographics, {"_admission_row": adm, "_patient_row": pat, "_edstays_row": ed_stay}


def extract_labs(conn: duckdb.DuckDBPyConnection, hadm_id: int, admittime: datetime, t_end: datetime,
                 item_map: Dict[int, Dict[str, Any]], max_labs: int = 3000) -> List[Dict[str, Any]]:
    lab = maybe_table(conn, "hosp.labevents")
    if not lab:
        return []
    sample = rows(conn, f"SELECT * FROM {lab} LIMIT 1")
    cols = set(sample[0].keys()) if sample else set()

    val_col = "value" if "value" in cols else ("valuenum" if "valuenum" in cols else None)
    if not val_col:
        return []

    sel_cols = [c for c in [
        "subject_id", "hadm_id", "specimen_id", "itemid", "charttime", "storetime",
        val_col, "valueuom", "flag", "priority"
    ] if c in cols]

    sql = f"""
      SELECT {', '.join(sel_cols)}
      FROM {lab}
      WHERE hadm_id = ?
        AND charttime IS NOT NULL
        AND charttime >= ?
        AND charttime < ?
      ORDER BY charttime
      LIMIT {int(max_labs)}
    """
    lr = rows(conn, sql, (hadm_id, admittime, t_end))

    out: List[Dict[str, Any]] = []
    for r in lr:
        itemid = r.get("itemid")
        try:
            itemid_i = int(itemid) if itemid is not None else None
        except Exception:
            itemid_i = None
        meta = item_map.get(itemid_i, {}) if itemid_i is not None else {}
        value = r.get(val_col)
        if not is_valid_value(value):
            continue
        out.append({
            "label": meta.get("label"),
            "fluid": meta.get("fluid"),
            "category": meta.get("category"),
            "charttime": r.get("charttime"),
            "value": str(value).strip(),
            "valueuom": r.get("valueuom"),
            "flag": r.get("flag"),
        })
    return out


def extract_microbiology(conn: duckdb.DuckDBPyConnection, hadm_id: int, admittime: datetime, t_end: datetime,
                         max_rows: int = 3000) -> List[Dict[str, Any]]:
    micro = maybe_table(conn, "hosp.microbiologyevents")
    if not micro:
        return []
    mrows = rows(
        conn,
        f"""
        SELECT *
        FROM {micro}
        WHERE hadm_id = ?
          AND charttime IS NOT NULL
          AND charttime >= ?
          AND charttime < ?
        ORDER BY charttime
        LIMIT {int(max_rows)}
        """,
        (hadm_id, admittime, t_end),
    )

    out: List[Dict[str, Any]] = []
    for r in mrows:
        rec = {
            "charttime": r.get("charttime"),
            "spec_type_desc": r.get("spec_type_desc"),
            "test_name": r.get("test_name"),
            "org_name": r.get("org_name"),
            "ab_name": r.get("ab_name"),
            "interpretation": r.get("interpretation"),
            "comments": r.get("comments"),
        }
        if not is_valid_value(rec.get("charttime")) and not any(is_valid_value(v) for v in rec.values()):
            continue
        out.append(rec)
    return out


def extract_transfers(conn: duckdb.DuckDBPyConnection, hadm_id: int, t_start: datetime, t_end: datetime,
                      max_rows: int = 2000) -> List[Dict[str, Any]]:
    """Extract pre-treatment unit movement (hosp.transfers)."""
    tb = maybe_table(conn, "hosp.transfers")
    if not tb:
        return []
    cols = set(get_cols(conn, tb))
    sel_cols = [c for c in [
        "stay_id",
        "eventtype", "careunit", "intime", "outtime", "los",
    ] if c in cols]
    if not sel_cols:
        sel_cols = ["*"]
    time_col = "intime" if "intime" in cols else None
    where = ["hadm_id = ?"]
    params: List[Any] = [hadm_id]
    if time_col:
        where.append(f"{time_col} IS NOT NULL AND {time_col} >= ? AND {time_col} < ?")
        params.extend([t_start, t_end])
    where_sql = " AND ".join(where)
    order_sql = f"ORDER BY {time_col} NULLS LAST" if time_col else ""
    sql = f"""
    SELECT {', '.join(sel_cols)}
    FROM {tb}
    WHERE {where_sql}
    {order_sql}
    LIMIT {int(max_rows)}
    """
    return rows(conn, sql, tuple(params))


def extract_services(conn: duckdb.DuckDBPyConnection, hadm_id: int, t_start: datetime, t_end: datetime,
                     max_rows: int = 2000) -> List[Dict[str, Any]]:
    """Extract pre-treatment service line changes (hosp.services)."""
    tb = maybe_table(conn, "hosp.services")
    if not tb:
        return []
    cols = set(get_cols(conn, tb))
    sel_cols = [c for c in ["transfertime", "prev_service", "curr_service"] if c in cols]
    if not sel_cols:
        sel_cols = ["*"]
    where = ["hadm_id = ?"]
    params: List[Any] = [hadm_id]
    if "transfertime" in cols:
        where.append("transfertime IS NOT NULL AND transfertime >= ? AND transfertime < ?")
        params.extend([t_start, t_end])
    where_sql = " AND ".join(where)
    order_sql = "ORDER BY transfertime NULLS LAST" if "transfertime" in cols else ""
    sql = f"""
    SELECT {', '.join(sel_cols)}
    FROM {tb}
    WHERE {where_sql}
    {order_sql}
    LIMIT {int(max_rows)}
    """
    return rows(conn, sql, tuple(params))


def extract_omr(conn: duckdb.DuckDBPyConnection, subject_id: int, admittime: datetime, t_end: datetime,
                max_rows: int = 3000) -> List[Dict[str, Any]]:
    """Extract baseline/outpatient OMR values before treatment.

    Prefer charttime if available; otherwise use chartdate <= DATE(admittime).
    """
    tb = maybe_table(conn, "hosp.omr")
    if not tb:
        return []
    cols = set(get_cols(conn, tb))
    sel_cols = [c for c in [
        "chartdate", "charttime",
        "result_name", "result_value", "result_valueuom",
    ] if c in cols]
    if not sel_cols:
        sel_cols = ["*"]

    where = ["subject_id = ?"]
    params: List[Any] = [subject_id]
    order_sql = ""

    if "charttime" in cols:
        where.append("charttime IS NOT NULL AND charttime <= ?")
        params.append(t_end)
        order_sql = "ORDER BY charttime NULLS LAST"
    elif "chartdate" in cols:
        where.append("chartdate IS NOT NULL AND chartdate <= ?")
        params.append(t_end.date())
        order_sql = "ORDER BY chartdate NULLS LAST"

    where_sql = " AND ".join(where)
    sql = f"""
    SELECT {', '.join(sel_cols)}
    FROM {tb}
    WHERE {where_sql}
    {order_sql}
    LIMIT {int(max_rows)}
    """
    return rows(conn, sql, tuple(params))


def extract_radiology_notes(conn: duckdb.DuckDBPyConnection, hadm_id: int, admittime: datetime, t_end: datetime,
                            max_notes: int = 50) -> List[Dict[str, Any]]:
    rad = maybe_table(conn, "note.radiology")
    if not rad:
        return []
    rads = rows(
        conn,
        f"""
        SELECT note_id, charttime, storetime, text
        FROM {rad}
        WHERE hadm_id = ?
          AND charttime IS NOT NULL
          AND charttime >= ?
          AND charttime < ?
        ORDER BY charttime, storetime
        LIMIT {int(max_notes)}
        """,
        (hadm_id, admittime, t_end),
    )
    out: List[Dict[str, Any]] = []
    for r in rads:
        txt = r.get("text")
        if not is_valid_value(txt):
            continue
        out.append({
            "note_id": r.get("note_id"),
            "charttime": r.get("charttime"),
            "storetime": r.get("storetime"),
            "text": txt,
        })
    return out


def extract_radiology_detail(conn: duckdb.DuckDBPyConnection, hadm_id: int, admittime: datetime, t_end: datetime,
                             max_rows: int = 2000) -> List[Dict[str, Any]]:
    """Extract radiology_detail rows with time filtering (prefer detail charttime, else join radiology)."""
    detail = maybe_table(conn, "note.radiology_detail")
    if not detail:
        return []

    det_cols = set(get_cols(conn, detail))
    rad = maybe_table(conn, "note.radiology")

    # Select useful columns if present; fall back to all columns.
    preferred = [
        "note_id", "charttime", "storetime",
        "field_name", "field_value", "field_ordinal",
        "section", "section_name", "section_title", "section_header", "subsection", "category",
        "text", "section_text", "section_content",
        "seq_num", "line_num", "line",
    ]
    sel = [f"d.{c}" for c in preferred if c in det_cols]
    if not sel:
        sel = ["d.*"]

    where = []
    params: List[Any] = []

    if "hadm_id" in det_cols:
        where.append("d.hadm_id = ?")
        params.append(hadm_id)
    elif "subject_id" in det_cols and has_table(conn, "hosp.admissions"):
        where.append("d.subject_id IN (SELECT subject_id FROM hosp.admissions WHERE hadm_id = ?)")
        params.append(hadm_id)

    time_col = "charttime" if "charttime" in det_cols else ("storetime" if "storetime" in det_cols else None)
    join = ""
    order_by = ""

    if time_col:
        where.append(f"d.{time_col} IS NOT NULL AND d.{time_col} >= ? AND d.{time_col} < ?")
        params.extend([admittime, t_end])
        order_by = f"ORDER BY d.{time_col}"
    else:
        if rad:
            rad_cols = set(get_cols(conn, rad))
            if "note_id" in det_cols and "note_id" in rad_cols and "charttime" in rad_cols:
                join = f"JOIN {rad} r ON d.note_id = r.note_id"
                sel.append("r.charttime")
                where.append("r.charttime IS NOT NULL AND r.charttime >= ? AND r.charttime < ?")
                params.extend([admittime, t_end])
                order_by = "ORDER BY r.charttime"

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
    SELECT {', '.join(sel)}
    FROM {detail} d
    {join}
    {where_sql}
    {order_by}
    LIMIT {int(max_rows)}
    """
    out = rows(conn, sql, tuple(params))
    for r in out:
        if isinstance(r, dict):
            r.pop("subject_id", None)
    return out


def extract_medications(conn: duckdb.DuckDBPyConnection, hadm_id: int, admittime: datetime, t_end: datetime,
                        max_meds: int = 2000) -> List[Dict[str, Any]]:
    """Prescriptions only (orders, not administrations), time-filtered to the diagnostic window."""
    out: List[Dict[str, Any]] = []

    pres = maybe_table(conn, "hosp.prescriptions")
    if pres:
        pcols = set(get_cols(conn, pres))
        want = [
            "drug", "drug_name_poe", "drug_name_generic",
            "prod_strength",
            "dose_val_rx", "dose_unit_rx", "dose_val", "dose_unit",
            "route",
            "starttime", "stoptime",
            "frequency",
        ]
        sel = [c for c in want if c in pcols]
        if not sel:
            sel = ["*"]
        order_col = pick_first_existing(list(pcols), ["starttime", "stoptime"])
        order_sql = f"ORDER BY {order_col} NULLS LAST" if order_col else ""

        # Keep only prescriptions that start within the diagnostic window.
        where_extra = ""
        params: List[Any] = [hadm_id]
        if "starttime" in pcols:
            where_extra = "AND starttime IS NOT NULL AND starttime >= ? AND starttime < ?"
            params.extend([admittime, t_end])

        pr = rows(
            conn,
            f"""
            SELECT {', '.join(sel)}
            FROM {pres}
            WHERE hadm_id = ?
            {where_extra}
            {order_sql}
            LIMIT {int(max_meds)}
            """,
            tuple(params),
        )
        for r in pr:
            out.append({
                "source": "prescriptions",
                "drug": r.get("drug") or r.get("drug_name_generic") or r.get("drug_name_poe"),
                "prod_strength": r.get("prod_strength"),
                "dose_val_rx": r.get("dose_val_rx") or r.get("dose_val"),
                "dose_unit_rx": r.get("dose_unit_rx") or r.get("dose_unit"),
                "route": r.get("route"),
                "starttime": r.get("starttime"),
                "stoptime": r.get("stoptime"),
            })

    return out


def extract_orders_and_procedures(conn: duckdb.DuckDBPyConnection, hadm_id: int, admittime: datetime, t_end: datetime,
                                 max_orders: int = 2000) -> List[Dict[str, Any]]:
    """Orders filtered to the diagnostic window.

    - POE: ordertime in [admittime, t_end)
    - procedures_icd: excluded (no time-of-day; cannot guarantee pre-treatment)
    """
    out: List[Dict[str, Any]] = []

    poe = maybe_table(conn, "hosp.poe")
    poe_detail = maybe_table(conn, "hosp.poe_detail")
    if poe:
        if poe_detail:
            q = f"""
            WITH p AS (
              SELECT *
              FROM {poe}
              WHERE hadm_id = ?
                AND ordertime IS NOT NULL
                AND ordertime >= ?
                AND ordertime < ?
              ORDER BY ordertime
              LIMIT {int(max_orders)}
            )
            SELECT p.*, d.field_name, d.field_value
            FROM p
            LEFT JOIN {poe_detail} d
              ON p.poe_id = d.poe_id
            ORDER BY p.ordertime
            """
            pr = rows(conn, q, (hadm_id, admittime, t_end))

            by_id: Dict[Any, Dict[str, Any]] = {}
            for r in pr:
                pid = r.get("poe_id")
                if pid is None:
                    continue
                if pid not in by_id:
                    by_id[pid] = {
                        "source": "poe",
                        "poe_id": pid,
                        "ordertime": r.get("ordertime"),
                        "order_type": r.get("order_type"),
                        "order_subtype": r.get("order_subtype"),
                        "order_name": r.get("order_name"),
                        "order_status": r.get("order_status") or r.get("status"),
                        "details": [],
                    }
                fn = r.get("field_name")
                fv = r.get("field_value")
                if is_valid_value(fn) or is_valid_value(fv):
                    by_id[pid]["details"].append({"field_name": fn, "field_value": fv})

            # infer order_name from details if missing
            name_fields = {"order name", "order", "name", "procedure", "test", "lab", "imaging", "study", "medication"}
            preferred = ["order name", "order", "procedure", "test", "study", "imaging", "medication", "lab"]
            for rec in by_id.values():
                if rec.get("order_name"):
                    continue
                details = rec.get("details") or []
                chosen = None
                for pf in preferred:
                    for d in details:
                        fn = (d.get("field_name") or "").strip().lower()
                        if fn == pf and (d.get("field_value") or "").strip():
                            chosen = d.get("field_value")
                            break
                    if chosen:
                        break
                if not chosen:
                    for d in details:
                        fn = (d.get("field_name") or "").strip().lower()
                        if any(k in fn for k in name_fields) and (d.get("field_value") or "").strip():
                            chosen = d.get("field_value")
                            break
                if chosen:
                    rec["order_name"] = chosen

            # Drop empty details to avoid noisy empty arrays.
            for rec in by_id.values():
                if not rec.get("details"):
                    rec.pop("details", None)

            out.extend(sorted(by_id.values(), key=lambda x: x.get("ordertime") or ""))
        else:
            pr = rows(
                conn,
                f"""
                SELECT *
                FROM {poe}
                WHERE hadm_id = ?
                  AND ordertime IS NOT NULL
                  AND ordertime >= ?
                  AND ordertime < ?
                ORDER BY ordertime
                LIMIT {int(max_orders)}
                """,
                (hadm_id, admittime, t_end),
            )
            for r in pr:
                out.append({
                    "source": "poe",
                    "poe_id": r.get("poe_id"),
                    "ordertime": r.get("ordertime"),
                    "order_type": r.get("order_type"),
                    "order_subtype": r.get("order_subtype"),
                    "order_name": r.get("order_name"),
                    "order_status": r.get("order_status") or r.get("status"),
                })

    # EXCLUDE procedures_icd explicitly (cannot ensure within [admittime, t_end) due to date-only).
    return out


def _is_medication_order_from_poe(order: Dict[str, Any]) -> bool:
    """Heuristic filter to detect medication-related orders inside POE."""
    ot = (order.get("order_type") or "").lower()
    osub = (order.get("order_subtype") or "").lower()
    oname = (order.get("order_name") or "").lower()
    if any(k in ot for k in ["med", "medication", "drug", "rx", "pharm"]):
        return True
    if any(k in osub for k in ["med", "medication", "drug", "rx", "pharm"]):
        return True
    if any(k in oname for k in ["med", "medication", "drug", "rx", "pharm"]):
        return True
    details = order.get("details") or []
    for d in details:
        fn = (d.get("field_name") or "").strip().lower()
        fv = (d.get("field_value") or "").strip().lower()
        if any(k in fn for k in ["med", "medication", "drug", "rx", "pharm"]) or any(k in fv for k in ["med", "medication", "drug", "rx", "pharm"]):
            return True
    return False


def build_actions_orders_general(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """General orders from POE (including medication orders)."""
    out: List[Dict[str, Any]] = []
    for o in orders or []:
        # Skip medication-type orders (they are usually empty/useless here).
        ot = (o.get("order_type") or "").strip().lower()
        if "med" in ot or "pharm" in ot or "rx" in ot:
            continue
        rec = {
            "source": "poe",
            "ordertime": o.get("ordertime"),
            "order_type": o.get("order_type"),
            "order_subtype": o.get("order_subtype"),
        }
        if o.get("details"):
            rec["details"] = o.get("details")
        out.append(rec)
    return out


def build_actions_medication_requests(
    prescriptions: List[Dict[str, Any]],
    poe_orders: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Medication action channel: prescriptions only."""
    out: List[Dict[str, Any]] = []
    for r in prescriptions or []:
        out.append({
            "source": "prescriptions",
            "drug": r.get("drug"),
            "prod_strength": r.get("prod_strength"),
            "dose_val_rx": r.get("dose_val_rx"),
            "dose_unit_rx": r.get("dose_unit_rx"),
            "route": r.get("route"),
            "starttime": r.get("starttime"),
            "stoptime": r.get("stoptime"),
        })
    return out


def extract_correct_diagnosis(conn: duckdb.DuckDBPyConnection, hadm_id: int, icd_title_map: Dict[Tuple[str, int], str]) -> Dict[str, Any]:
    dx = maybe_table(conn, "hosp.diagnoses_icd")
    if not dx:
        return {"icd_codes": [], "icd_titles": []}
    dx_rows = rows(
        conn,
        f"""
        SELECT icd_code, icd_version, seq_num
        FROM {dx}
        WHERE hadm_id = ?
        ORDER BY seq_num NULLS LAST
        """,
        (hadm_id,),
    )
    icd_codes: List[str] = []
    icd_titles: List[str] = []
    for r in dx_rows:
        code = r.get("icd_code")
        ver = r.get("icd_version")
        if code is None or ver is None:
            continue
        try:
            ver_i = int(ver)
        except Exception:
            continue
        icd_codes.append(f"{code} (v{ver_i})")
        icd_titles.append(icd_title_map.get((str(code), ver_i), str(code)))
    return {"icd_codes": icd_codes, "icd_titles": icd_titles}



def extract_principal_diagnosis(
    conn: duckdb.DuckDBPyConnection,
    hadm_id: int,
    icd_title_map: Dict[Tuple[str, int], str],
) -> Dict[str, Any]:
    """Extract *hospital principal diagnosis* from hosp.diagnoses_icd (seq_num = 1).

    Returns empty strings when unavailable. We keep this separate from Correct_Diagnosis
    (which may contain multiple ICD codes/titles).
    """
    dx = maybe_table(conn, "hosp.diagnoses_icd")
    if not dx:
        return {"icd_code": "", "icd_version": "", "icd_title": "", "seq_num": 1}

    pr_rows = rows(
        conn,
        f"""
        SELECT icd_code, icd_version, seq_num
        FROM {dx}
        WHERE hadm_id = ?
          AND try_cast(seq_num AS INTEGER) = 1
        LIMIT 1
        """,
        (hadm_id,),
    )
    if not pr_rows:
        return {"icd_code": "", "icd_version": "", "icd_title": "", "seq_num": 1}

    r = pr_rows[0]
    code = r.get("icd_code")
    ver = r.get("icd_version")
    if code is None or ver is None:
        return {"icd_code": "", "icd_version": "", "icd_title": "", "seq_num": 1}

    try:
        ver_i = int(ver)
    except Exception:
        return {"icd_code": str(code), "icd_version": "", "icd_title": "", "seq_num": 1}

    title = icd_title_map.get((str(code), ver_i), str(code))
    return {"icd_code": str(code), "icd_version": ver_i, "icd_title": title, "seq_num": 1}


# --------------------------
# Diagnostic ICD term anti-leakage (strong constraint)
# --------------------------

# Your requirement:
#   - Build a per-case "diagnostic ICD terms" set from *disease-only* ICD titles (filtering Z/V/R/E/... etc.).
#   - Remove these terms everywhere in the JSON EXCEPT inside OSCE_Examination.Correct_Diagnosis.
#   - Strategy: sentence-drop / replacement / blanking; if still failing validation, drop the case.

# ICD non-disease code prefixes (heuristic, matches ICD coding conventions)
# ICD-10: Z (factors influencing health status), R (symptoms/signs), V/W/X/Y (external causes), T (poisoning/injury)
# ICD-9:  V (supplementary), E (external causes)
_ICD10_NON_DISEASE_PREFIXES = tuple("ZRVWXYT")
_ICD9_NON_DISEASE_PREFIXES = tuple("VE")


def _is_disease_icd_code(icd_code: str, icd_version: int) -> bool:
    if not icd_code:
        return False
    c = str(icd_code).strip().upper()
    if not c:
        return False
    if icd_version == 10:
        return not c.startswith(_ICD10_NON_DISEASE_PREFIXES)
    if icd_version == 9:
        return not c.startswith(_ICD9_NON_DISEASE_PREFIXES)
    # unknown version: treat as disease (fail-open)
    return True


def extract_disease_only_icd_titles(
    conn: duckdb.DuckDBPyConnection,
    hadm_id: int,
    icd_title_map: Dict[Tuple[str, int], str],
) -> List[str]:
    """Return disease-only ICD titles for a hadm_id."""
    dx = maybe_table(conn, "hosp.diagnoses_icd")
    if not dx:
        return []
    dx_rows = rows(
        conn,
        f"""
        SELECT icd_code, icd_version
        FROM {dx}
        WHERE hadm_id = ?
        """,
        (hadm_id,),
    )
    titles: List[str] = []
    for r in dx_rows:
        code = r.get("icd_code")
        ver = r.get("icd_version")
        if code is None or ver is None:
            continue
        try:
            ver_i = int(ver)
        except Exception:
            continue
        if not _is_disease_icd_code(str(code), ver_i):
            continue
        title = icd_title_map.get((str(code), ver_i), str(code))
        if is_valid_value(title):
            titles.append(str(title))
    # de-dup while preserving order
    seen = set()
    out: List[str] = []
    for t in titles:
        k = t.strip().lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _norm_for_match(text: str, expand_abbrev: bool = True) -> str:
    """Lowercase and normalize to [a-z0-9 ] to make matching stable.

    When expand_abbrev=True, expands common abbreviations/phrases for ICD title matching.
    When False (for PMH/HPI), skips expansion to reduce false positives.
    """
    if text is None:
        return ""
    t = str(text).lower()
    for pat, repl in _PHRASE_EXPANSIONS.items():
        t = re.sub(pat, repl, t)
    if expand_abbrev:
        for pat, repl in _ABBREV_EXPANSIONS.items():
            t = re.sub(pat, repl, t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# --------------------------
# Diagnostic ICD title anti-leakage (token-set within window)
# --------------------------

# New leakage rule (per user specification):
#   Given a set of ICD *titles* (disease-only ICD titles for this admission), a field is considered
#   diagnostic leakage if within a *single sentence* there exists an ICD title such that **all tokens**
#   of that title appear within a bounded token window, irrespective of token order.
#   If leakage is detected (outside allowed prefixes), the entire field is replaced with "[REDACTED]".
#   No blanking/emptying is performed.

_TITLE_TOKEN_WINDOW = 30  # max tokens span for all title tokens to co-occur within one sentence
_SENT_SPLIT_RE = re.compile(r"[\n.!?]+")

# Common abbreviations/phrases that appear in notes but not in ICD titles.
_ABBREV_EXPANSIONS = {
    r"\bhfpef\b": "heart failure preserved ejection fraction",
    r"\bhfref\b": "heart failure reduced ejection fraction",
    r"\bchf\b": "heart failure",
    r"\bhtn\b": "hypertension",
    r"\bhld\b": "hyperlipidemia",
    r"\bdm\b": "diabetes mellitus",
    r"\bdka\b": "diabetic ketoacidosis",
    r"\bcad\b": "coronary artery disease",
    r"\bmi\b": "myocardial infarction",
    r"\bcva\b": "stroke",
    r"\bich\b": "intracerebral hemorrhage",
    r"\bsah\b": "subarachnoid hemorrhage",
    r"\bpe\b": "pulmonary embolism",
    r"\bdvt\b": "deep vein thrombosis",
    r"\bckd\b": "chronic kidney disease",
    r"\besrd\b": "end stage renal disease",
    r"\baki\b": "acute kidney injury",
    r"\bcopd\b": "chronic obstructive pulmonary disease",
    r"\bosa\b": "obstructive sleep apnea",
    r"\bohs\b": "obesity hypoventilation syndrome",
    r"\buti\b": "urinary tract infection",
    r"\bams\b": "altered mental status",
    r"\bgib\b": "gastrointestinal bleeding",
    r"\bbrbpr\b": "bright red blood per rectum",
}

_PHRASE_EXPANSIONS = {
    r"\bpontine hemorrhage\b": "intracerebral hemorrhage",
    r"\bbrainstem hemorrhage\b": "intracerebral hemorrhage",
    r"\baspiration pneumonia\b": "pneumonitis",
    r"\baspiration pneumonitis\b": "pneumonitis",
}

_TITLE_STOPWORDS = {
    "acute", "chronic", "other", "unspecified", "without", "with", "due", "to", "of", "and", "or",
    "not", "intractable", "status", "epilepticus", "history", "personal",
    "congestive", "diastolic", "systolic", "episode", "episodes", "complication",
    "reaction", "later", "procedure", "operation", "external", "abnormal",
    "inhalation", "food", "vomitus",
    "pain", "loss", "weakness",
}

# Sentence-level trigger cues for diagnostic/result statements.
_DIAG_TRIGGER_RE = re.compile(
    r"(?i)\b("
    r"diagnosed\s+with|found\s+to\s+have|consistent\s+with|suggestive\s+of|"
    r"impression|assessment|admitted\s+for|management\s+of|treated\s+for|"
    r"secondary\s+to|due\s+to"
    r")\b"
)

def _sentence_tokens(text: str, expand_abbrev: bool = True) -> List[str]:
    norm = _norm_for_match(text, expand_abbrev=expand_abbrev)
    return [t for t in norm.split(" ") if t]

def _title_to_token_set(title: str) -> List[str]:
    """Tokenize an ICD title into normalized tokens (order-free).

    We deliberately do *not* generate ngrams or partial-token patterns; the leakage rule requires that
    *all* tokens of the ICD title co-occur within a bounded window.
    """
    if not title:
        return []
    norm = _norm_for_match(title)
    if not norm:
        return []
    toks = [t for t in norm.split(" ") if t and t not in _TITLE_STOPWORDS]
    return toks


def build_diag_title_token_sets(disease_icd_titles: List[str]) -> List[Dict[str, Any]]:
    """Build per-title token sets for leakage checking.

    Returns a list of dicts: {"title": <original title>, "tokens": frozenset([...])}
    Deduplicates by token-set.
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for title in disease_icd_titles or []:
        toks = _title_to_token_set(title)
        if not toks:
            continue
        tokset = frozenset(toks)
        key = tuple(sorted(tokset))
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": str(title), "tokens": tokset})
    return out


def _sentences_to_token_lists(text: str, expand_abbrev: bool = True) -> List[List[str]]:
    """Split raw text into sentence-like segments and return token lists per segment."""
    if text is None:
        return []
    raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
    segs = [s.strip() for s in _SENT_SPLIT_RE.split(raw)]
    token_lists: List[List[str]] = []
    for seg in segs:
        if not seg:
            continue
        norm = _norm_for_match(seg, expand_abbrev=expand_abbrev)
        if not norm:
            continue
        toks = [t for t in norm.split(" ") if t]
        if toks:
            token_lists.append(toks)
    if not token_lists:
        norm = _norm_for_match(raw, expand_abbrev=expand_abbrev)
        toks = [t for t in norm.split(" ") if t]
        if toks:
            token_lists.append(toks)
    return token_lists


def _tokens_cooccur_within_window(sentence_tokens: List[str], title_tokens: frozenset, window: int) -> bool:
    """Return True if all title_tokens appear within any sliding window over sentence_tokens."""
    if not sentence_tokens or not title_tokens:
        return False
    if len(title_tokens) == 1:
        # single-token title: presence anywhere in the sentence is sufficient under the new rule
        (tok,) = tuple(title_tokens)
        return tok in sentence_tokens

    # quick rejection
    if not title_tokens.issubset(set(sentence_tokens)):
        return False

    w = max(int(window), len(title_tokens))
    n = len(sentence_tokens)
    for i in range(0, n):
        win = sentence_tokens[i : i + w]
        if title_tokens.issubset(set(win)):
            return True
    return False


def _text_has_title_token_leak(
    text: str,
    title_token_sets: List[Dict[str, Any]],
    window: int = _TITLE_TOKEN_WINDOW,
    expand_abbrev: bool = True,
) -> bool:
    """Check whether text contains any ICD title's full token set within one sentence/window."""
    if not text or not title_token_sets:
        return False

    for sent_tokens in _sentences_to_token_lists(text, expand_abbrev=expand_abbrev):
        if not sent_tokens:
            continue
        # For efficiency, compute set once
        st_set = set(sent_tokens)
        for entry in title_token_sets:
            tokset = entry.get("tokens")
            if not tokset:
                continue
            if not tokset.issubset(st_set):
                continue
            if _tokens_cooccur_within_window(sent_tokens, tokset, window=window):
                return True
    return False


def _text_has_triggered_title_leak(
    text: str, title_token_sets: List[Dict[str, Any]], expand_abbrev: bool = True
) -> bool:
    """Looser rule: if a sentence has a diagnostic trigger + title token overlap."""
    if not text or not title_token_sets:
        return False
    raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
    for seg in _SENT_SPLIT_RE.split(raw):
        s = seg.strip()
        if not s or not _DIAG_TRIGGER_RE.search(s):
            continue
        tokens = _sentence_tokens(s, expand_abbrev=expand_abbrev)
        if not tokens:
            continue
        st_set = set(tokens)
        for entry in title_token_sets:
            tokset = entry.get("tokens")
            if not tokset:
                continue
            inter = tokset.intersection(st_set)
            if len(tokset) == 1:
                if inter:
                    return True
            else:
                if len(inter) >= 3:
                    return True
    return False


def _sanitize_string_by_title_tokens(text: str, title_token_sets: List[Dict[str, Any]]) -> Optional[str]:
    """Replace the entire string with '[REDACTED]' if it satisfies the new leakage rule."""
    if not is_valid_value(text):
        return None
    s = str(text)
    if not title_token_sets:
        return s
    if _text_has_title_token_leak(s, title_token_sets) or _text_has_triggered_title_leak(s, title_token_sets):
        return "[REDACTED]"
    return s


def _drop_leaking_sentences(
    text: Optional[str],
    title_token_sets: List[Dict[str, Any]],
    triggered_only: bool = False,
    expand_abbrev: bool = True,
) -> Optional[str]:
    """Remove only sentences that leak Correct_Diagnosis titles; keep the rest."""
    out, _ = _drop_leaking_sentences_with_report(
        text, title_token_sets, triggered_only=triggered_only, expand_abbrev=expand_abbrev
    )
    return out


def _drop_leaking_sentences_with_report(
    text: Optional[str],
    title_token_sets: List[Dict[str, Any]],
    triggered_only: bool = False,
    expand_abbrev: bool = True,
) -> Tuple[Optional[str], List[str]]:
    """Like _drop_leaking_sentences but also returns list of dropped sentence snippets.

    When triggered_only=True (for PMH), only drop sentences with diagnostic trigger phrases.
    When expand_abbrev=False (for PMH/HPI), skip abbreviation expansion to reduce FP.
    """
    if not is_valid_value(text):
        return None, []
    if not title_token_sets:
        return str(text), []
    raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
    segs = [s.strip() for s in _SENT_SPLIT_RE.split(raw)]
    kept: List[str] = []
    dropped: List[str] = []
    for seg in segs:
        if not seg:
            continue
        leak = (
            _text_has_triggered_title_leak(seg, title_token_sets, expand_abbrev=expand_abbrev)
            if triggered_only
            else (
                _text_has_title_token_leak(seg, title_token_sets, expand_abbrev=expand_abbrev)
                or _text_has_triggered_title_leak(seg, title_token_sets, expand_abbrev=expand_abbrev)
            )
        )
        if leak:
            dropped.append(seg[:300])
            continue
        kept.append(seg)
    result = "\n".join(kept).strip() if kept else None
    return result, dropped


def sanitize_case_remove_diag_terms(case_obj: Any, title_token_sets: List[Dict[str, Any]], allow_prefixes: List[Tuple[str, ...]]) -> Any:
    """Recursively sanitize all strings in a JSON-like object, excluding allow_prefixes.

    Under the new rule, any leaking string is replaced with '[REDACTED]'.
    """

    def is_allowed(path: Tuple[str, ...]) -> bool:
        for pfx in allow_prefixes:
            if len(path) >= len(pfx) and path[: len(pfx)] == pfx:
                return True
        return False

    def rec(node: Any, path: Tuple[str, ...]) -> Any:
        if is_allowed(path):
            return node
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                out[k] = rec(v, path + (str(k),))
            return out
        if isinstance(node, list):
            return [rec(v, path + ("[]",)) for v in node]
        if isinstance(node, str):
            return _sanitize_string_by_title_tokens(node, title_token_sets)
        return node

    return rec(case_obj, tuple())


def _collect_redacted_paths(
    before: Any, after: Any, path: List[str], out: List[Dict[str, Any]]
) -> None:
    """Compare before/after; when after has [REDACTED] and before had different content, append to out."""
    if isinstance(after, dict) and isinstance(before, dict):
        for k in after:
            if k in before:
                _collect_redacted_paths(before[k], after[k], path + [str(k)], out)
    elif isinstance(after, list) and isinstance(before, list):
        for i, (bv, av) in enumerate(zip(before, after)):
            _collect_redacted_paths(bv, av, path + [f"[{i}]"], out)
    elif isinstance(after, str) and isinstance(before, str):
        if after == "[REDACTED]" and before != "[REDACTED]":
            out.append({"path": path, "original_snippet": before[:400].replace("\n", " ")})


def validate_no_diag_terms_outside_correct(case_obj: Any, title_token_sets: List[Dict[str, Any]], allow_prefixes: List[Tuple[str, ...]]) -> List[Tuple[Tuple[str, ...], str]]:
    """Return list of (path, offending_text_sample). Empty list => pass.

    Offense is defined by the same token-set co-occurrence leakage rule.
    """

    def is_allowed(path: Tuple[str, ...]) -> bool:
        for pfx in allow_prefixes:
            if len(path) >= len(pfx) and path[: len(pfx)] == pfx:
                return True
        return False

    offenders: List[Tuple[Tuple[str, ...], str]] = []

    def walk(node: Any, path: Tuple[str, ...]):
        if is_allowed(path):
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, path + (str(k),))
        elif isinstance(node, list):
            for v in node:
                walk(v, path + ("[]",))
        elif isinstance(node, str):
            if _text_has_title_token_leak(node, title_token_sets) or _text_has_triggered_title_leak(node, title_token_sets):
                sample = node.strip().replace("\n", " ")
                offenders.append((path, sample[:200]))

    walk(case_obj, tuple())
    return offenders


# --------------------------
# CXR extraction (filtered to diagnostic window)
# --------------------------


def extract_cxr_for_hadm(conn, hadm_id, time_start, time_end, max_studies=10,
                         *, cxr_conn):
    """Select report-bearing studies using the historical diagnostic interval.

    Uses corrected timestamps produced by initialize_cxr_index.py, not legacy
    image indices. Report completion time is unavailable, not imputed. A study
    qualifies if any of its metadata rows is inside [time_start, time_end).
    The newest max_studies are retained, returned chronologically (latest last).
    """
    if max_studies < 1:
        raise ValueError("max_studies must be positive")
    subject = conn.execute("SELECT subject_id FROM hosp.admissions WHERE hadm_id=?",
                           [hadm_id]).fetchone()
    if subject is None:
        raise ValueError("Admission not found")
    rows = cxr_conn.execute("""
        SELECT m.study_id, MAX(m.study_ts) AS study_ts, r.report_text, r.report_source
        FROM cxr.metadata_ts2 m JOIN cxr.reports r USING(study_id)
        WHERE m.subject_id=? AND m.study_ts >= ? AND m.study_ts < ?
        GROUP BY m.study_id, r.report_text, r.report_source
        ORDER BY study_ts DESC, m.study_id DESC LIMIT ?
    """, [subject[0], time_start, time_end, max_studies]).fetchall()
    studies = [{"study_id": sid, "study_ts": dt_to_str(ts),
                "report_text": text, "report_source": source,
                "report_charttime": None, "report_storetime": None,
                "time_basis": "study_acquisition"}
               for sid, ts, text, source in reversed(rows) if nonempty_report(text)]
    return {"_present": bool(studies), "mode": "reports",
            "window": {"start": str(time_start), "end": str(time_end)},
            "time_basis": "study_acquisition",
            "report_availability_verified": False, "studies": studies}


def build_osce_case(
    conn: duckdb.DuckDBPyConnection,
    hadm_id: int,
    diagnostic_hours: float = 2.0,
    max_labs: int = 3000,
    max_meds: int = 2000,
    max_orders: int = 2000,
    max_notes: int = 50,
    # CXR
    cxr_max_studies: int = 10,
    cxr_conn: Optional[duckdb.DuckDBPyConnection] = None,
    # Knowledge
    knowledge_json: Optional[str] = None,
    attach_principal_knowledge: bool = True,
    knowledge_max_items: int = 3,
    leakage_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    icd_title_map = build_icd_title_map(conn)
    lab_item_map = build_lab_item_map(conn)

    # Compute diagnostic window and eligibility (Criterion A)
    adm_row, t_start, t_end, t0_med, ed_stay, ed_schema = compute_diag_window_or_raise(conn, hadm_id, diagnostic_hours)

    # Unified diagnostic evidence window (align with au_with_cxr_reports):
    # When ED exists: [ed_intime, t0_med) for all evidence.
    # When hosp-only: [admittime, t0_med).
    # t0_med后的 imaging/CXR 剔除 (exclude post-t0_med).
    ed_intime = None
    if ed_stay is not None:
        ei = ed_stay.get("intime")
        if ei is not None:
            ed_intime = ei if isinstance(ei, datetime) else datetime.fromisoformat(str(ei))
    diag_start = ed_intime if ed_intime is not None else t_start
    diag_end = t0_med
    img_cxr_start = diag_start
    if diag_end is None:
        raise ValueError('t0_med is NULL')
    if diag_start is not None and diag_end <= diag_start:
        raise ValueError(f'Invalid diagnostic window: diag_end <= diag_start (diag_start={diag_start}, diag_end={diag_end})')

    # Meta + demographics (reflect unified window)
    meta, demographics, _debug_rows = extract_meta_and_demographics(conn, hadm_id, ed_stay=ed_stay, t_start=diag_start, t_end=diag_end)

    # ED records: extract when ed_stay exists; omit for hosp-only (align with au_with_cxr_reports).
    ed_records = None
    if ed_stay is not None and ed_schema is not None:
        ed_records = extract_ed_tables_for_stay(conn, ed_schema=ed_schema, stay_id=int(ed_stay["stay_id"]))

    # --------------------------
    # Fill patient-visible fields (per your priority strategy)
    # --------------------------
    # 1) Chief Complaint: ED triage chiefcomplaint; fallback to discharge summary "Chief Complaint" section.
    # 2) HPI/PMH/Social/ROS/Physical Exam/Brief Hospital Course: from discharge summary parsing (best-effort).
    # 3) Symptoms: derived from chief + hpi (best-effort).
    # 4) Vitals: prefer ED triage vitals; fallback to note regex.

    ds_text, ds_meta = extract_discharge_summary_text(conn, hadm_id)
    ds_parsed = parse_history_from_discharge_summary(ds_text)
    # Apply aggressive anti-leakage sanitization to any discharge-derived fields.
    ds_parsed = sanitize_discharge_parsed(ds_parsed)

    triage_row = ed_records.get("triage") if isinstance(ed_records, dict) else None
    chief_ed = triage_row.get("chiefcomplaint") if isinstance(triage_row, dict) else None
    chief = chief_ed if is_valid_value(chief_ed) else ds_parsed.get("chief")
    hpi = ds_parsed.get("hpi")
    pmh = ds_parsed.get("pmh")
    social = ds_parsed.get("social")
    phys_exam = ds_parsed.get("phys_exam_text")
    brief_course = ds_parsed.get("brief_course")

    primary_symptom, secondary_symptoms = _derive_symptoms(chief, hpi)

    vitals_ed = _vitals_from_ed_triage(triage_row if isinstance(triage_row, dict) else None)
    vitals_note = ds_parsed.get("vitals") or {"heart_rate": None, "bp": None, "resp_rate": None, "temp": None, "spo2": None}
    vitals = _merge_vitals(vitals_ed, vitals_note)

    phys = {
        "vitals": vitals,
        "general_exam": phys_exam,
    }

    admittime = adm_row.get("admittime")
    if admittime is not None and not isinstance(admittime, datetime):
        admittime = datetime.fromisoformat(str(admittime))

    labs = extract_labs(conn, hadm_id, diag_start, diag_end, lab_item_map, max_labs=max_labs)
    micro = extract_microbiology(conn, hadm_id, diag_start, diag_end, max_rows=max_labs)
    transfers = extract_transfers(conn, hadm_id, diag_start, diag_end, max_rows=2000)
    services = extract_services(conn, hadm_id, diag_start, diag_end, max_rows=2000)
    omr = extract_omr(conn, subject_id=meta.get("subject_id"), admittime=admittime, t_end=diag_end, max_rows=3000)
    rad_notes = extract_radiology_notes(conn, hadm_id, img_cxr_start, diag_end, max_notes=max_notes)
    rad_detail = extract_radiology_detail(conn, hadm_id, img_cxr_start, diag_end, max_rows=max_notes * 20)
    # Attach radiology_detail rows to their parent radiology note via note_id.
    rad_detail_by_note_id: Dict[Any, List[Dict[str, Any]]] = {}
    rad_detail_unmatched: List[Dict[str, Any]] = []
    for d in rad_detail:
        nid = d.get("note_id")
        if nid is None:
            rad_detail_unmatched.append(d)
            continue
        rad_detail_by_note_id.setdefault(nid, []).append(d)
    for rn in rad_notes:
        nid = rn.get("note_id")
        rn["details"] = rad_detail_by_note_id.get(nid, [])
    meds = extract_medications(conn, hadm_id, diag_start, diag_end, max_meds=max_meds)
    orders = extract_orders_and_procedures(conn, hadm_id, diag_start, diag_end, max_orders=max_orders)
    # CXR (unified window): [ed_intime, t0_med) when ED exists, else [admittime, t0_med)
    cxr_start = img_cxr_start
    cxr_end = diag_end
    if cxr_conn is None:
        cxr = {"_present": False, "mode": "disabled", "studies": [],
               "window": {"start": str(cxr_start), "end": str(cxr_end)}}
    else:
        # A requested report route must fail loudly, never become "no CXR".
        cxr = extract_cxr_for_hadm(
            conn, hadm_id, cxr_start, cxr_end, max_studies=cxr_max_studies,
            cxr_conn=cxr_conn)
    cxr["_audit"] = {"t_start": str(diag_start),
                     "admittime": str(meta.get("admittime")), "t0_med": str(t0_med)}

    correct = extract_correct_diagnosis(conn, hadm_id, icd_title_map)
    principal = extract_principal_diagnosis(conn, hadm_id, icd_title_map)

    # For HPI/PMH, Imaging notes, and CXR report_text, drop only leaking sentences
    # based on Correct_Diagnosis ICD titles.
    correct_titles = correct.get("icd_titles") if isinstance(correct, dict) else []
    correct_title_token_sets = build_diag_title_token_sets(correct_titles or [])
    if leakage_report is not None:
        leakage_report["sentence_dropped"] = leakage_report.get("sentence_dropped", [])

    def _do_drop(
        field_name: str,
        val: Optional[str],
        triggered_only: bool = False,
        expand_abbrev: bool = True,
    ) -> Optional[str]:
        if leakage_report is not None and val:
            out, dropped = _drop_leaking_sentences_with_report(
                val, correct_title_token_sets, triggered_only=triggered_only, expand_abbrev=expand_abbrev
            )
            for d in dropped:
                leakage_report["sentence_dropped"].append({"field": field_name, "dropped_snippet": d})
            return out
        return _drop_leaking_sentences(
            val, correct_title_token_sets, triggered_only=triggered_only, expand_abbrev=expand_abbrev
        )

    hpi = _do_drop("HPI", hpi, expand_abbrev=False)
    pmh = _do_drop("PMH", pmh, triggered_only=True, expand_abbrev=False)
    phys_exam = _do_drop("general_exam", phys_exam)
    phys["general_exam"] = phys_exam
    if isinstance(rad_notes, list):
        for i, rn in enumerate(rad_notes):
            if isinstance(rn, dict) and is_valid_value(rn.get("text")):
                rn["text"] = _do_drop(f"Imaging[{i}].text", rn.get("text"))
    if isinstance(cxr, dict) and isinstance(cxr.get("studies"), list):
        for i, st in enumerate(cxr["studies"]):
            if isinstance(st, dict) and is_valid_value(st.get("report_text")):
                st["report_text"] = _do_drop(f"CXR_studies[{i}].report_text", st.get("report_text"))

    if isinstance(cxr, dict):
        # Redaction can empty a report. Do not count it as an available report.
        cxr["studies"] = [st for st in cxr.get("studies", [])
                          if nonempty_report(st.get("report_text"))]
        cxr["_present"] = bool(cxr["studies"])

    # --------------------------
    # Strong anti-leakage: remove disease-only ICD terms from all fields except Correct_Diagnosis
    # --------------------------
    disease_titles = extract_disease_only_icd_titles(conn, hadm_id, icd_title_map)
    diag_title_token_sets = build_diag_title_token_sets(disease_titles)

    patient_actor = {
        "Demographics": {
            "gender": demographics.get("gender"),
            "age": demographics.get("age"),
            "language": demographics.get("language"),
            "race": demographics.get("race"),
            "marital_status": demographics.get("marital_status"),
            "insurance": demographics.get("insurance"),
        },
        "History": {
            "HPI": hpi,
            "Past_Medical_History": pmh,
            "Social_History": social,
        },
        "Symptoms": {
            "Chief_Complaint": chief,
            "Primary_Symptom": primary_symptom,
            "Secondary_Symptoms": secondary_symptoms,
        },
    }

    objective_for_doctor = _build_objective_for_doctor(
        chief=chief,
        primary_symptom=primary_symptom,
        secondary_symptoms=secondary_symptoms,
        hpi=hpi,
        demographics=demographics,
    )

    actions = {
        "orders_general": build_actions_orders_general(orders),
        "medication_requests": build_actions_medication_requests(meds, orders),
    }

    # Test_Results: add ED only when ed_records exists (align with au_with_cxr_reports).
    test_results: Dict[str, Any] = {
        "Labs": labs,
        "Microbiology": micro,
        "Imaging": rad_notes,
        "CXR": cxr,
        "Hosp": {
            "Transfers": transfers,
            "Services": services,
            "OMR": omr,
        },
    }
    if ed_records is not None:
        test_results["ED"] = ed_records

    diagnostic_filter: Dict[str, Any] = {
        "criterion": "A",
        "t_start": str(diag_start),
        "admittime": str(meta.get("admittime")),
        "t_end": str(t_end),
        "diag_start": str(diag_start),
        "diag_end": str(diag_end),
        "t0_med": str(t0_med),
        "excluded_proc_same_day": False,
    }
    if meta.get("ed_intime") is not None:
        diagnostic_filter["ed_intime"] = str(meta.get("ed_intime"))

    osce = {
        "OSCE_Examination": {
            "Meta": meta,
            "Objective_for_Doctor": objective_for_doctor,
            "Patient_Actor": patient_actor,
            "Physical_Examination_Findings": phys,
            "actions": actions,
            "Test_Results": test_results,
            "Correct_Diagnosis": correct,
            "Principal_Diagnosis": principal,
            "knowledge": {
                "principal_diagnosis": lookup_principal_knowledge(
                    principal,
                    build_knowledge_indices(knowledge_json) if (attach_principal_knowledge and is_valid_value(knowledge_json)) else {"exact_9cm": {}, "exact_10cm": {}, "rollup_9cm": {}, "rollup_10": {}},
                    max_items=knowledge_max_items,
                )
            },
            "_debug": {
                "discharge_meta": ds_meta,
                "diagnostic_filter": diagnostic_filter,
                "counts": {
                    "labs": len(labs),
                    "micro": len(micro),
                    "radiology_notes": len(rad_notes),
                    "radiology_detail": len(rad_detail),
                    "radiology_detail_unmatched": len(rad_detail_unmatched),
                    "cxr_studies": len(cxr.get("studies", [])) if isinstance(cxr, dict) else 0,
                    "medications": len(meds),
                    "actions_orders_general": len(actions.get("orders_general", [])),
                    "actions_medication_requests": len(actions.get("medication_requests", [])),
                    "icd_codes": len(correct.get("icd_codes", [])),
                    "principal_diagnosis": 1 if (isinstance(principal, dict) and is_valid_value(principal.get("icd_code"))) else 0,
                    "ed_vitals": len(ed_records.get("vitalsign", [])) if isinstance(ed_records, dict) else 0,
                    "ed_dx": len(ed_records.get("diagnosis", [])) if isinstance(ed_records, dict) else 0,
                    "ed_medrecon": len(ed_records.get("medrecon", [])) if isinstance(ed_records, dict) else 0,
                    "ed_pyxis": len(ed_records.get("pyxis", [])) if isinstance(ed_records, dict) else 0,
                },
            },
        }
    }

    # Apply post-hoc sanitization across the entire case object.
    # Leakage is defined by full ICD-title token co-occurrence (order-free) within a bounded window in a single sentence.
    allow = [
        ("OSCE_Examination", "Correct_Diagnosis"),
        ("OSCE_Examination", "Principal_Diagnosis"),
        ("OSCE_Examination", "knowledge"),
        ("OSCE_Examination", "Patient_Actor", "History", "HPI"),
        ("OSCE_Examination", "Patient_Actor", "History", "Past_Medical_History"),
        ("OSCE_Examination", "Patient_Actor", "Symptoms"),
        ("OSCE_Examination", "Objective_for_Doctor"),
        ("OSCE_Examination", "Physical_Examination_Findings", "general_exam"),
        ("OSCE_Examination", "Test_Results", "Imaging"),
        ("OSCE_Examination", "Test_Results", "CXR"),
    ]
    osce_before = copy.deepcopy(osce) if leakage_report is not None else None
    osce = sanitize_case_remove_diag_terms(osce, diag_title_token_sets, allow_prefixes=allow)

    offenders = validate_no_diag_terms_outside_correct(osce, diag_title_token_sets, allow_prefixes=allow)
    if offenders:
        if leakage_report is not None:
            leakage_report["validation_failed"] = True
            leakage_report["offenders"] = [
                {"path": list(p), "sample": s} for p, s in offenders
            ]
            return None
        raise ValueError(f"diagnostic ICD title leakage remains after redaction: {len(offenders)} fields")

    if leakage_report is not None and osce_before is not None:
        _collect_redacted_paths(osce_before, osce, [], leakage_report.setdefault("field_redacted", []))

    return osce


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="DuckDB file, e.g., mimic_hosp_note.db")
    ap.add_argument("--hadm_id", type=int, required=True, help="Hospital admission id")
    ap.add_argument("--out", required=True, help="New output JSON path; existing files are refused")

    ap.add_argument("--diagnostic_hours", type=float, default=2.0, help="Legacy compatibility argument; endpoint is t0_med, NOT a fixed 2-hour window")

    ap.add_argument("--knowledge_json", default=None, help="Optional authorized knowledge JSON; not bundled")
    ap.add_argument("--attach_principal_knowledge", type=int, default=1, help="Attach Principal_Diagnosis knowledge from knowledge.json (1=yes, 0=no)")
    ap.add_argument("--knowledge_max_items", type=int, default=3, help="Max number of knowledge entries to attach for the principal diagnosis")

    ap.add_argument("--max_labs", type=int, default=3000)
    ap.add_argument("--max_meds", type=int, default=2000)
    ap.add_argument("--max_orders", type=int, default=2000)
    ap.add_argument("--max_notes", type=int, default=50)

    ap.add_argument("--cxr_max_studies", type=int, default=10)
    cxr_args = ap.add_mutually_exclusive_group(required=True)
    cxr_args.add_argument("--cxr-db", help="Validated report-only DB; may be separate from --db")
    cxr_args.add_argument("--without-cxr-reports", action="store_true",
                          help="Explicitly omit the CXR-specific report route (ordinary NOTE reports remain)")


    args = ap.parse_args()

    out_path = args.out
    if os.path.lexists(out_path):
        ap.error("--out already exists; choose a new file")
    for path in (args.knowledge_json,):
        if path and not os.path.isfile(path):
            ap.error(f"Optional input file does not exist: {path}")

    if args.cxr_max_studies < 1:
        ap.error("--cxr_max_studies must be positive")
    conn = duckdb.connect(args.db, read_only=True)
    cxr_conn = None
    try:
        cxr_conn = open_report_database(args.cxr_db) if args.cxr_db else None
        case = build_osce_case(
            conn,
            hadm_id=args.hadm_id,
            diagnostic_hours=float(args.diagnostic_hours),
            max_labs=args.max_labs,
            max_meds=args.max_meds,
            max_orders=args.max_orders,
            max_notes=args.max_notes,
            cxr_max_studies=args.cxr_max_studies,
            cxr_conn=cxr_conn,
            knowledge_json=args.knowledge_json,
            attach_principal_knowledge=bool(args.attach_principal_knowledge),
            knowledge_max_items=args.knowledge_max_items,
        )
    except Exception as e:
        # Make failure explicit for batch scripts.
        raise SystemExit(f"SKIP hadm_id={args.hadm_id}: {e}")
    finally:
        if cxr_conn is not None:
            cxr_conn.close()
        conn.close()

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "x", encoding="utf-8") as f:
        json.dump(case, f, ensure_ascii=False, indent=2, default=str)
        f.write("\n")

    print("WROTE:", out_path)


if __name__ == "__main__":
    main()
