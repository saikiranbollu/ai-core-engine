#!/usr/bin/env python3
"""Standalone Datasheet Parser.

Takes a datasheet PDF as input and produces device_properties.json for all devices.
All logic is self-contained - no imports from section_Extraction, multiagent_latest,
token_scraper, or splitline_flag_audit.

Usage:
    python DS_parser.py --pdf path/to/datasheet.pdf --all-devices
    python DS_parser.py --pdf path/to/datasheet.pdf --devices DEV1,DEV2
    python DS_parser.py --pdf path/to/datasheet.pdf --all-devices --skip-pins
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import ssl
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

# Force UTF-8 on Windows
if os.name == 'nt':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace', line_buffering=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace', line_buffering=True)
    except Exception:
        pass

# ── External pip dependencies ─────────────────────────────────────────────────
import fitz  # PyMuPDF
import PyPDF2
import httpx
import litellm
import requests
import nest_asyncio
nest_asyncio.apply()
from crewai import Agent, Task, Crew, Process, LLM

try:
    import certifi
except ImportError:
    certifi = None

# ── Path Setup ────────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
EXT_ROOT = os.path.dirname(HERE)
AUTH_DIR = os.path.join(EXT_ROOT, 'auth')
_CREDENTIALS_FILE = os.path.join(AUTH_DIR, 'credentials')
_CA_BUNDLE = os.path.join(AUTH_DIR, 'ca-bundle.crt')
_MERGED_CA = os.path.join(AUTH_DIR, 'merged-ca.pem')

_INSECURE = os.environ.get('MULTIAGENT_INSECURE') == '1'
os.environ['OTEL_SDK_DISABLED'] = 'true'

# When running in insecure mode, disable SSL verification globally at the Python ssl
# module level. This is needed because CrewAI creates its own httpx/openai clients
# that bypass litellm.ssl_verify settings.
if _INSECURE:
    import ssl
    ssl._create_default_https_context = ssl._create_unverified_context
    # Monkey-patch ssl.create_default_context so httpx/openai clients get unverified ctx
    _orig_create_default_context = ssl.create_default_context
    def _patched_create_default_context(*args, **kwargs):
        ctx = _orig_create_default_context(*args, **kwargs)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ssl.create_default_context = _patched_create_default_context
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Constants ─────────────────────────────────────────────────────────────────
DEFAULT_PIN_HEADING = "Pin definition and functions"
DEFAULT_FALLBACK_SECTION = 3
DEFAULT_OUTPUT_FILE = 'device_properties.json'

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1: TOKEN SCRAPER (inlined from packages/token_scraper.py)
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_credentials_path(override: str | None = None) -> str:
    if override and os.path.isfile(override):
        return override
    if os.path.isfile(_CREDENTIALS_FILE):
        return _CREDENTIALS_FILE
    env_path = os.environ.get("CREDENTIALS_FILE")
    if env_path and os.path.isfile(env_path):
        return env_path
    return _CREDENTIALS_FILE


def _resolve_ca_bundle() -> Optional[str]:
    if os.path.isfile(_CA_BUNDLE):
        return _CA_BUNDLE
    env_bundle = os.environ.get("REQUESTS_CA_BUNDLE")
    if env_bundle and os.path.isfile(env_bundle):
        return env_bundle
    return None


def _atomic_write(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _build_credentials_from_env() -> dict | None:
    base64_payload = os.environ.get("BASIC_AUTH_BASE64")
    user = os.environ.get("BASIC_AUTH_USERNAME")
    pwd = os.environ.get("BASIC_AUTH_PASSWORD")
    if base64_payload:
        encoded = base64_payload.strip()
    elif user and pwd:
        encoded = base64.b64encode(f"{user}:{pwd}".encode("ascii")).decode("ascii")
    else:
        return None
    return {
        "accept": "application/json",
        "Authorization": f"Basic {encoded}",
        "Cache-Control": "no-cache, max-age=0",
        "Pragma": "no-cache",
    }


def _load_credentials_file(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and 'Authorization' in data:
            return data
        return None
    except Exception:
        return None


def _ensure_credentials(resolved_path: str) -> dict | None:
    creds = _load_credentials_file(resolved_path)
    if creds:
        return creds
    creds = _build_credentials_from_env()
    if creds:
        try:
            os.makedirs(os.path.dirname(resolved_path), exist_ok=True)
            _atomic_write(resolved_path, creds)
        except OSError:
            pass
        return creds
    return None


def token(site: str, credentials_override: str | None = None):
    ca_path = _resolve_ca_bundle()
    if not ca_path and not _INSECURE:
        print(json.dumps({"TokenError": {"error": "Missing CA bundle", "path": _CA_BUNDLE}}))
        return None
    if ca_path:
        os.environ['REQUESTS_CA_BUNDLE'] = ca_path
    cred_path = _resolve_credentials_path(credentials_override)
    credentials = _ensure_credentials(cred_path)
    if not credentials:
        print(json.dumps({"TokenError": {"error": "Missing credentials", "path": cred_path}}))
        return None
    if site.endswith('/v1'):
        url = site[:-3] + "/auth/token"
    else:
        url = site.rstrip('/') + "/auth/token"
    try:
        session = requests.Session()
        resp = session.get(url, headers=credentials, timeout=30, verify=(not _INSECURE))
    except requests.RequestException as e:
        print(f"Request error: {e}")
        return None
    if resp.status_code == 200:
        return resp.text
    else:
        print(f"Authentication failed (status {resp.status_code}).")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2: TLS & LLM INITIALIZATION
# ══════════════════════════════════════════════════════════════════════════════

def _prepare_merged_ca():
    if _INSECURE:
        return None
    if not os.path.isfile(_CA_BUNDLE):
        return None
    if certifi:
        try:
            with open(_CA_BUNDLE, 'rb') as f1, open(certifi.where(), 'rb') as f2, open(_MERGED_CA, 'wb') as out:
                out.write(f1.read())
                out.write(b"\n")
                out.write(f2.read())
            return _MERGED_CA
        except Exception:
            return _CA_BUNDLE
    return _CA_BUNDLE


def _init_llm_session():
    ca_candidate = _prepare_merged_ca() or _CA_BUNDLE
    if not os.path.isfile(_CA_BUNDLE) and not _INSECURE:
        print(json.dumps({"DSParserError": {"error": "Missing ca-bundle.crt", "path": _CA_BUNDLE}}))
        sys.exit(2)
    if not os.path.isfile(_CREDENTIALS_FILE):
        print(json.dumps({"DSParserError": {"error": "Missing credentials file", "path": _CREDENTIALS_FILE}}))
        sys.exit(3)
    if _INSECURE:
        print(json.dumps({"DSParserWarn": {"warning": "TLS verification disabled"}}))
    if not _INSECURE:
        os.environ['REQUESTS_CA_BUNDLE'] = ca_candidate
        os.environ['CURL_CA_BUNDLE'] = ca_candidate
        os.environ['SSL_CERT_FILE'] = ca_candidate
    print(f"[DS_parser] Using CA bundle: {ca_candidate if not _INSECURE else 'INSECURE:NO_VERIFY'}")
    print(f"[DS_parser] Credentials file present: {_CREDENTIALS_FILE}")
    transport = httpx.HTTPTransport(retries=3)
    total_timeout = float(os.environ.get('MULTIAGENT_HTTP_TIMEOUT', '30'))
    connect_timeout = float(os.environ.get('MULTIAGENT_CONNECT_TIMEOUT', '10'))
    litellm.client_session = httpx.Client(
        verify=False if _INSECURE else ca_candidate,
        timeout=httpx.Timeout(total_timeout, connect=connect_timeout),
        transport=transport,
        trust_env=True
    )
    if _INSECURE:
        litellm.ssl_verify = False


def _ping_llm(base_url: str, headers: Dict[str, str]):
    try:
        client = litellm.client_session or httpx.Client(verify=False if _INSECURE else (_prepare_merged_ca() or _CA_BUNDLE), trust_env=True)
        resp = client.get(base_url.rstrip('/'))
        if resp.status_code >= 400:
            print(json.dumps({"DSParserError": {"error": "LLM ping failed", "status": resp.status_code}}))
        else:
            print(json.dumps({"DSParserInfo": {"message": "LLM endpoint reachable", "status": resp.status_code}}))
    except Exception as e:
        print(json.dumps({"DSParserError": {"error": "Connectivity check failed", "details": str(e)}}))


# Initialize LLM
_init_llm_session()

url = 'https://gpt4ifx.icp.infineon.com'
key = token(url, credentials_override=_CREDENTIALS_FILE)
if not key:
    print(json.dumps({"DSParserTokenError": {"error": "Failed to acquire bearer token"}}))
    sys.exit(4)

header = {'Authorization': f"Bearer {key}"}

llm_model = LLM(
    model='openai/gpt-5',
    base_url=url,
    api_key=key,
    default_headers=header,
)

_ping_llm(url, header)

# Crew retry config
_CREW_LOCK = threading.Lock()
RETRIES = int(os.environ.get("MULTIAGENT_CREW_RETRIES", "3"))
INITIAL_BACKOFF = float(os.environ.get("MULTIAGENT_RETRY_BACKOFF", "2.0"))
BACKOFF_FACTOR = float(os.environ.get("MULTIAGENT_BACKOFF_FACTOR", "1.6"))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3: SECTION EXTRACTION (inlined from section_Extraction.py)
# ══════════════════════════════════════════════════════════════════════════════

def parse_toc(doc):
    return [
        {'level': lvl, 'title': title, 'page': page}
        for (lvl, title, page) in doc.get_toc()
    ]


def find_section_range(doc, toc, section_num=3):
    start, end_excl = None, None
    for i, e in enumerate(toc):
        if re.match(rf'^\s*{section_num}(\b|[.\s])', e['title']):
            start = e['page']
            for j in range(i + 1, len(toc)):
                m = re.match(r'^\s*(\d+)(\b|[.\s])', toc[j]['title'])
                if m and int(m.group(1)) > section_num and toc[j]['level'] <= e['level']:
                    end_excl = toc[j]['page']
                    break
            break
    if start is None:
        return None, None
    if end_excl is None:
        end_excl = doc.page_count + 1
    return start, end_excl


def find_heading_range(doc, toc, heading_phrase):
    hp = heading_phrase.lower().strip()
    for i, entry in enumerate(toc):
        title_lower = entry['title'].lower()
        if hp in title_lower:
            start = entry['page']
            heading_level = entry['level']
            end_excl = None
            for j in range(i + 1, len(toc)):
                nxt = toc[j]
                if nxt['level'] <= heading_level:
                    end_excl = nxt['page']
                    break
            if end_excl is None:
                end_excl = doc.page_count + 1
            return start, end_excl, heading_level
    return None, None, None


def get_subsections_in_range(toc, start_page, end_page_excl, min_level):
    subs = [e for e in toc if start_page <= e['page'] < end_page_excl and e['level'] > min_level]
    subs.sort(key=lambda x: x['page'])
    return subs


def extract_device_name_from_title(title):
    m = re.search(r'\b([A-Z]{2,}[0-9]{2,}(?:_[A-Z]{2,})?)\b', title)
    return m.group(1) if m else None


def extract_device_sections(pdf_path, heading_phrase="Pin definition and functions", fallback_section_num=3) -> Dict[str, Dict[str, int]]:
    doc = fitz.open(pdf_path)
    try:
        toc = parse_toc(doc)
        if not toc:
            raise RuntimeError("PDF has no table of contents.")
        h_start, h_end_excl, h_level = find_heading_range(doc, toc, heading_phrase)
        heading_label = heading_phrase
        if h_start is None:
            h_start, h_end_excl = find_section_range(doc, toc, section_num=fallback_section_num)
            if h_start is None:
                raise RuntimeError(f"Neither heading '{heading_phrase}' nor section {fallback_section_num} found.")
            h_level = min(e['level'] for e in toc if re.match(rf'^\s*{fallback_section_num}(\b|[.\s])', e['title']))
            heading_label = f"Section{fallback_section_num}"
        subsections = get_subsections_in_range(toc, h_start, h_end_excl, h_level)
        if not subsections:
            return {heading_label: {"start_page": h_start, "end_page": h_end_excl}}
        result: Dict[str, Dict[str, int]] = {}
        for idx, sub in enumerate(subsections):
            next_start = (subsections[idx + 1]['page'] if idx + 1 < len(subsections) else h_end_excl)
            device_name = extract_device_name_from_title(sub['title'])
            if not device_name:
                continue
            rng = result.get(device_name)
            if rng is None:
                result[device_name] = {"start_page": sub['page'], "end_page": next_start}
            else:
                if sub['page'] < rng['start_page']:
                    rng['start_page'] = sub['page']
                if next_start > rng['end_page']:
                    rng['end_page'] = next_start
        if not result:
            return {heading_label: {"start_page": h_start, "end_page": h_end_excl}}
        return result
    finally:
        doc.close()


def get_device_page_ranges(pdf_path: str, heading_phrase: str = DEFAULT_PIN_HEADING, fallback_section: int = DEFAULT_FALLBACK_SECTION) -> Dict[str, Tuple[int, int]]:
    try:
        mapping = extract_device_sections(pdf_path, heading_phrase=heading_phrase, fallback_section_num=fallback_section)
        return {dev: (info['start_page'], info['end_page']) for dev, info in mapping.items()}
    except Exception as e:
        print(f"[DS_parser] Warning: device page range extraction failed: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4: SPLIT-LINE FLAG AUDIT (inlined from splitline_flag_audit.py)
# ══════════════════════════════════════════════════════════════════════════════

MODE_TOKEN_RE = re.compile(r"^[A-Z0-9_]{4,}$")
SPLIT_CANDIDATE_RE = re.compile(r"([A-Z0-9_]{4,})\n([A-Z0-9]{1,3})(?=[A-Z_])")


def _safe_json_loads(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _uppercase_mode(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    t = value.strip().upper()
    if not t or "_" not in t:
        return None
    if not MODE_TOKEN_RE.match(t):
        return None
    return t


def _extract_modes_audit(collab_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    vs = collab_obj.get("verification_summary", {}) if isinstance(collab_obj, dict) else {}
    if not isinstance(vs, dict):
        return []
    extracted: List[Dict[str, Any]] = []
    for item in (vs.get("input_modes_verified") or []):
        if not isinstance(item, dict):
            continue
        mn = _uppercase_mode(item.get("mode"))
        if mn:
            extracted.append({"mode_name": mn, "mode_type": "input", "mode_slot": None, "found_on_page": item.get("found_on_page")})
    for item in (vs.get("output_modes_verified") or []):
        if not isinstance(item, dict):
            continue
        mn = _uppercase_mode(item.get("mode_name"))
        if mn:
            extracted.append({"mode_name": mn, "mode_type": "output", "mode_slot": item.get("mode"), "found_on_page": item.get("found_on_page")})
    return extracted


def _make_snippet(text: str, start: int, end: int, window: int = 48) -> str:
    s = max(0, start - window)
    e = min(len(text), end + window)
    return text[s:e].replace("\n", "\\n")


def _find_split_candidates(page_text_upper: str):
    candidates = []
    for match in SPLIT_CANDIDATE_RE.finditer(page_text_upper):
        prefix = match.group(1)
        suffix = match.group(2)
        candidates.append({
            "prefix": prefix, "suffix": suffix, "candidate": prefix + suffix,
            "start": match.start(), "end": match.end(),
            "evidence": _make_snippet(page_text_upper, match.start(), match.end()),
        })
    return candidates


def _evaluate_mode_against_page(mode_name: str, page_text: str):
    page_upper = (page_text or "").upper()
    if not page_upper:
        return None, None, None
    in_original = mode_name in page_upper
    in_spaced = mode_name in page_upper.replace("\n", " ")
    in_compact = mode_name in page_upper.replace("\n", "")
    if not in_compact:
        return None, None, None
    candidates = _find_split_candidates(page_upper)
    mode_candidates = [c for c in candidates if c["candidate"] == mode_name]
    if in_compact and not in_original and not in_spaced:
        if mode_candidates:
            best = mode_candidates[0]
            if best["prefix"] in page_upper:
                return "High", "Mode exists only after newline-compaction; stitched from split line.", best["evidence"]
            return "Medium", "Mode exists only after newline-compaction; split-line stitch pattern.", best["evidence"]
        trimmed = mode_name[:-1]
        if len(trimmed) >= 4 and "_" in trimmed and trimmed in page_upper.replace("\n", " "):
            return "Medium", "Mode matched only after compaction; shorter token exists.", _make_snippet(page_upper, max(page_upper.find(trimmed), 0), max(page_upper.find(trimmed), 0) + len(trimmed))
        return "Low", "Mode matched only after newline-compaction.", None
    return None, None, None


def _scan_pin_file(file_path: Path) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    collab_obj = _safe_json_loads(payload.get("collaborative_analysis_result"))
    if not collab_obj:
        return []
    modes = _extract_modes_audit(collab_obj)
    raw_pages = payload.get("raw_page_data", [])
    if not isinstance(raw_pages, list) or not raw_pages:
        return []
    file_flags: List[Dict[str, Any]] = []
    for mode in modes:
        mode_name = mode["mode_name"]
        for page_blob in raw_pages:
            if not isinstance(page_blob, dict):
                continue
            severity, reason, evidence = _evaluate_mode_against_page(mode_name, page_blob.get("text", ""))
            if not severity:
                continue
            file_flags.append({
                "file": str(file_path).replace("\\", "/"),
                "pin_id": collab_obj.get("pin_id") or payload.get("pin_id"),
                "page": page_blob.get("page"),
                "severity": severity,
                "mode_name": mode_name,
                "mode_type": mode.get("mode_type"),
                "mode_slot": mode.get("mode_slot"),
                "reason": reason,
                "evidence": evidence,
            })
    unique_keys = set()
    deduped: List[Dict[str, Any]] = []
    for entry in file_flags:
        k = (entry["pin_id"], entry["page"], entry["mode_name"], entry["severity"], entry["reason"])
        if k not in unique_keys:
            unique_keys.add(k)
            deduped.append(entry)
    return deduped


def run_splitline_audit(scan_root: Path, report_path: Path) -> Dict[str, Any]:
    pin_files = sorted(scan_root.glob("**/pins/P*.json"))
    flags: List[Dict[str, Any]] = []
    for pf in pin_files:
        flags.extend(_scan_pin_file(pf))
    severity_counts = Counter(f["severity"] for f in flags)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scan_root": str(scan_root).replace("\\", "/"),
        "total_pin_files_scanned": len(pin_files),
        "total_flags": len(flags),
        "severity_counts": {"High": severity_counts.get("High", 0), "Medium": severity_counts.get("Medium", 0), "Low": severity_counts.get("Low", 0)},
        "flags": flags,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5: PORT PIN EXTRACTOR (inlined from multiagent_latest.py)
# ══════════════════════════════════════════════════════════════════════════════

class PortPinExtractor:
    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path
        self.pin_pattern = re.compile(r'P(\d{2})\.(\d{1,2})', re.IGNORECASE)

    def extract_text_by_page(self) -> List[str]:
        with open(self.pdf_path, 'rb') as file:
            pdf_reader = PyPDF2.PdfReader(file)
            return [page.extract_text() for page in pdf_reader.pages]

    def extract_single_page_text(self, page_index: int) -> str:
        with open(self.pdf_path, 'rb') as file:
            pdf_reader = PyPDF2.PdfReader(file)
            if 0 <= page_index < len(pdf_reader.pages):
                return pdf_reader.pages[page_index].extract_text()
            return ""

    def extract_pin_specific_content(self, page_text: str, target_pin_id: str) -> str:
        if not page_text or not target_pin_id:
            return ""
        lines = page_text.split('\n')
        target_content = []
        current_section = []
        in_target_section = False
        pin_pattern = r'P(\d{2})\.(\d{1,2})'
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                if in_target_section:
                    current_section.append(line)
                continue
            pin_matches = re.findall(pin_pattern, line_stripped)
            if pin_matches:
                if in_target_section and current_section:
                    target_content.extend(current_section)
                    current_section = []
                if target_pin_id in line_stripped:
                    in_target_section = True
                    current_section = [line]
                else:
                    in_target_section = False
                    current_section = []
            else:
                if in_target_section:
                    current_section.append(line)
        if in_target_section and current_section:
            target_content.extend(current_section)
        if not target_content:
            return ""
        combined_content = '\n'.join(target_content)
        cleaned_lines = []
        prev_empty = False
        for line in combined_content.split('\n'):
            if line.strip():
                cleaned_lines.append(line)
                prev_empty = False
            elif not prev_empty:
                cleaned_lines.append(line)
                prev_empty = True
        return '\n'.join(cleaned_lines).strip()

    def find_port_pins(self, pages_text: List[str]) -> List[Tuple[int, str, int, int]]:
        found_pins = []
        seen_pins = set()
        for page_num, text in enumerate(pages_text):
            matches = self.pin_pattern.findall(text)
            # Skip diagram/overview pages that list many pins without table data
            unique_on_page = set()
            for match in matches:
                port_str, pin_str = match[0], match[1]
                port_num, pin_num = int(port_str), int(pin_str)
                if 0 <= port_num <= 40 and 0 <= pin_num <= 15:
                    unique_on_page.add(f"P{port_str}.{pin_str}")
            if len(unique_on_page) > 20:
                continue  # ball-out diagram page — skip
            for match in matches:
                port_str, pin_str = match[0], match[1]
                port_num, pin_num = int(port_str), int(pin_str)
                if 0 <= port_num <= 40 and 0 <= pin_num <= 15:
                    pin_id = f"P{port_str}.{pin_str}"
                    if pin_id not in seen_pins:
                        found_pins.append((page_num, pin_id, port_num, pin_num))
                        seen_pins.add(pin_id)
        return found_pins


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6: PIN MODE ANALYZER (inlined from multiagent_latest.py)
# ══════════════════════════════════════════════════════════════════════════════

class PinModeAnalyzer:
    def __init__(self):
        self.input_mode_agent = Agent(
            role="Pin Input Mode Analyzer",
            goal="Analyze pin documentation text and identify all available input modes for the specified port pin",
            backstory="""You are an expert in microcontroller pin configuration and digital electronics. 
            You specialize in analyzing technical documentation text to identify input modes.
            Look for:
            - Pin configuration tables with 'I' or 'AI in available modes Control column
            - You have to return all the input mode names (NOT the function of those modes)
            - Do not just give input mode type but all the actual names
            - To verify if it is a valid name, check whether it has atleast 1 '_' in the name
            - Make sure to take into account that some mode names might be split across line breaks, usually characterised by 1-3 stray charactors
            - Append these stray characters to the mode name correctly for example: AUDIO0_TDM0_TXFSYNCI\\nA would be AUDIO0_TDM0_TXFSYNCIA and AUDIO0_TDM1_RXFSYNC\\nIB would be AUDIO0_TDM1_RXFSYNCIB""",
            llm=llm_model,
            multimodal=False,
            verbose=False
        )
        self.output_mode_agent = Agent(
            role="Pin Output Mode Analyzer",
            goal="Analyze pin documentation text and identify all available output modes for the specified port pin",
            backstory="""You are an expert in microcontroller pin configuration and digital electronics.
            You specialize in analyzing technical documentation text to identify output modes.
            Look for:
            - Pin configuration tables with 'Ox' or 'O' or "O#x"  in available modes Control column
            - You have to return all the output mode names (NOT the function of those modes)
            - Do not just give output mode type but all the actual names
            - To verify if it is a valid name, check whether it has atleast 1 '_' in the name
            - Make sure to take into account that some mode names might be split across line breaks, usually characterised by 1-3 stray charactors
            - Append these stray characters to the mode name correctly for example: AUDIO0_TDM0_RXFSYNC\\nO would be AUDIO0_TDM0_RXFSYNCO""",
            llm=llm_model,
            multimodal=False,
            verbose=False
        )
        self.verification_agent = Agent(
            role="Pin Mode Verification Specialist",
            goal="Verify and validate pin modes against extracted text to ensure accuracy",
            backstory="""You are a meticulous technical documentation validator with expertise in cross-referencing 
            information from multiple sources. You specialize in:
            - Verifying mode names against extracted text from documentation pages
            - Cross-checking pin configurations for accuracy
            - Eliminating false positives and ensuring mode names are actually valid for the specific pin
            - Providing final validated lists with confidence ratings""",
            llm=llm_model,
            verbose=False
        )

    def create_input_analysis_task(self, pin_id: str, text_content: str, page_numbers: List[int]) -> Task:
        return Task(
            description=f"""Analyze the pin documentation text for pin {pin_id} and identify ALL available input modes.
            
            PIN-SPECIFIC TEXT CONTENT FROM PAGES {page_numbers}:
            {text_content}
            
            Look for:
            1. Pin configuration tables with 'I' in the Control/Available Modes column
            2. Extract the specific mode names (not just descriptions or functions)
            3. Focus on entries that have at least one underscore '_' in the name
            4. Return actual mode names like 'GTM_CDTM1_DTM0_5', 'ASCLIN3_ATX', etc.
            5. For each mode found, note which page number it was found on
            
            Examine all tables, specifications, and pin mapping information in the text.
            Be thorough and extract all input-related mode names.""",
            expected_output="""Return a structured JSON output with the following format:
            {
                "pin_id": "pin_identifier",
                "input_modes": [
                    {
                        "mode_name": "specific_mode_name",
                        "found_on_page": page_number
                    }
                ]
            }""",
            agent=self.input_mode_agent
        )

    def create_output_analysis_task(self, pin_id: str, text_content: str, page_numbers: List[int]) -> Task:
        return Task(
            description=f"""Analyze the pin documentation text for pin {pin_id} and identify ALL available output modes.
            
            PIN-SPECIFIC TEXT CONTENT FROM PAGES {page_numbers}:
            {text_content}
            
            Look for:
            1. Pin configuration tables with 'Ox' or 'O' or "O#x"  in available modes Control column
            2. Extract the specific mode names (not just descriptions or functions)
            3. Focus on entries that have at least one underscore '_' in the name
            4. Return actual mode names like 'GTM_CDTM1_DTM0_5', 'ASCLIN3_ATX', etc.
            5. For each mode found, note which page number it was found on
            
            Examine all tables, specifications, and pin mapping information in the text.
            Be thorough and extract all output-related mode names.""",
            expected_output="""Return a structured JSON output with the following format:
            {
                "pin_id": "pin_identifier", 
                "output_modes": [
                    {
                        "mode":"Ox"
                        "mode_name": "specific_mode_name",
                        "found_on_page": page_number
                    }
                ]
            }""",
            agent=self.output_mode_agent
        )

    def create_verification_task(self, pin_id: str, input_modes: str, output_modes: str, text_content: str, page_numbers: List[int]) -> Task:
        return Task(
            description=f"""Verify and validate the pin modes identified by the Input Mode Analyzer and Output Mode Analyzer for {pin_id}.
            
            Your role is to cross-reference the findings from your fellow agents against the extracted text content.
            
            TEXT CONTENT FROM PAGES {page_numbers}:
            {text_content}
            
            Your verification tasks:
            1. Review the input modes identified by the Pin Input Mode Analyzer
            2. Review the output modes identified by the Pin Output Mode Analyzer which  are in Pin configuration tables with 'Ox' or 'O' or "O#x"  in available modes Control column  
            3. Cross-reference each identified mode name against the text content above
            4. Verify that each mode is actually available for pin {pin_id}
            5. Check for any missed modes in the text that weren't identified
            6. Remove any false positives
            7. Report the page number where each verified mode was found
            8. Find the buffer types.All buffer types will be mentioned with "\\" together.
            9. Buffer Types will be a subset of the following: Fast, Slow, HSFast, ES/ESx, PUy, VDD<something>, LVDS_Rx, LVDS_Tx . Where x and y are numbers.
            10. 1 pin cannot have both Fast and Slow buffer types.
            11. HSFAST and Fast are different buffer types.
            12. ES/ESx are not slow type buffer.
            13. Each buffer type is separated by /, if not it is just a line break. For example: / VDDEXTH\\nS / would be VDDEXTHS and not VDDEXTH and S.
            14. Make sure to take into account that some mode names might be split across line breaks, usually characterised by 1-3 stray charactors
            15. Append these stray characters to the mode name correctly for example: AUDIO0_TDM0_RXFSYNC\\nO would be AUDIO0_TDM0_RXFSYNCO
            
            Return a comprehensive JSON object with this structure:
            {{
                "pin_id": "{pin_id}",
                "verification_summary": {{
                    "buffer type": ["Fast etc."],
                    "input_modes_verified": [
                        {{"mode": "MODE_NAME", "found_on_page": page_number}}
                    ],
                    "output_modes_verified": [
                        {{"mode": "MODE","mode_name":"Mode_Name", "found_on_page": page_number}}
                    ],
                    "additional_modes_found_in_text": [],
                    "false_positives_removed": [],
                    "verification_notes": "Any important observations about the analysis"
                }}
            }}
            
            Be thorough and provide the most accurate final assessment using text-based analysis only.""",
            expected_output="Comprehensive JSON object with verified pin modes, confidence levels, and detailed verification results",
            agent=self.verification_agent
        )


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7: PIN PROCESSING (inlined from multiagent_latest.py)
# ══════════════════════════════════════════════════════════════════════════════

def _mp_pin_worker(args):
    """Multiprocessing worker — runs in a separate OS process with its own CrewAI state."""
    pin_data, pdf_path, pins_dir, total_pages = args
    page_num, pin_id, port_num, pin_num = pin_data
    worker_prefix = f"[worker-{os.getpid()}] "

    try:
        extractor = PortPinExtractor(pdf_path)
        analyzer = PinModeAnalyzer()

        pages_to_combine = list(range(max(0, page_num - 1), min(total_pages, page_num + 5)))
        combined_text = ""
        raw_page_data = []
        for page_idx in pages_to_combine:
            page_text = extractor.extract_single_page_text(page_idx)
            combined_text += f"\n--- Page {page_idx + 1} ---\n{page_text}\n"
            raw_page_data.append({"page": page_idx + 1, "text": page_text})

        pin_specific_text = extractor.extract_pin_specific_content(combined_text, pin_id)
        if pin_specific_text:
            text_content = f"Pin {pin_id} specific content (from pages {[p + 1 for p in pages_to_combine]}):\n{pin_specific_text}"
        else:
            text_content = f"Combined text from pages {[p + 1 for p in pages_to_combine]} (WARNING: may contain multiple pins):\n{combined_text}"

        if not text_content.strip():
            print(f"{worker_prefix}No text content found for pin {pin_id}")
            return None

        input_task = analyzer.create_input_analysis_task(pin_id, text_content, pages_to_combine)
        output_task = analyzer.create_output_analysis_task(pin_id, text_content, pages_to_combine)
        verification_task = analyzer.create_verification_task(
            pin_id,
            "Input modes will be provided by input_mode_agent",
            "Output modes will be provided by output_mode_agent",
            text_content,
            pages_to_combine
        )

        retry_delays = [10, 30, 60]
        max_attempts = len(retry_delays) + 1
        for attempt in range(1, max_attempts + 1):
            crew = Crew(
                agents=[analyzer.input_mode_agent, analyzer.output_mode_agent, analyzer.verification_agent],
                tasks=[input_task, output_task, verification_task],
                process=Process.sequential,
                verbose=False
            )
            try:
                result = crew.kickoff()
                combined_result = str(result)
                pin_result = {
                    "page_found": page_num + 1,
                    "port_number": port_num,
                    "pin_number": pin_num,
                    "collaborative_analysis_result": combined_result,
                    "pages_analyzed": [p + 1 for p in pages_to_combine],
                    "raw_page_data": raw_page_data,
                    "analysis_type": "text_only",
                    "processing_timestamp": datetime.now().isoformat(),
                    "pdf_source": os.path.basename(pdf_path),
                    "status": "completed",
                    "thread_name": f"process-{os.getpid()}"
                }
                pin_filename = f"{pin_id.replace('.', '_')}.json"
                pin_filepath = os.path.join(pins_dir, pin_filename)
                with open(pin_filepath, 'w') as pin_file:
                    json.dump(pin_result, pin_file, indent=2)
                print(f"Completed: {pin_id}")
                return pin_result
            except Exception as crew_error:
                msg = str(crew_error).lower()
                print(f"{worker_prefix}Error for {pin_id} (attempt {attempt}): {crew_error}")
                if "database is locked" in msg or "rate" in msg:
                    if attempt <= len(retry_delays):
                        time.sleep(retry_delays[attempt - 1])
                        continue
                raise crew_error

    except Exception as e:
        print(f"{worker_prefix}Error processing pin {pin_id}: {str(e)}")
        error_result = {
            "page_found": page_num + 1,
            "port_number": port_num,
            "pin_number": pin_num,
            "error": str(e),
            "analysis_type": "text_only",
            "processing_timestamp": datetime.now().isoformat(),
            "pdf_source": os.path.basename(pdf_path),
            "status": "failed",
            "thread_name": f"process-{os.getpid()}"
        }
        pin_filename = f"{pin_id.replace('.', '_')}_error.json"
        pin_filepath = os.path.join(pins_dir, pin_filename)
        try:
            with open(pin_filepath, 'w') as pin_file:
                json.dump(error_result, pin_file, indent=2)
        except Exception:
            pass
        return error_result


def process_single_pin(pin_data: tuple, all_pages_text: List[str], pdf_path: str, agent_output_dir: str,
                       extractor: PortPinExtractor, analyzer: PinModeAnalyzer, thread_id: int = None) -> dict:
    page_num, pin_id, port_num, pin_num = pin_data
    current_thread = threading.current_thread()
    thread_name = current_thread.name if thread_id is None else f"Thread-{thread_id}"
    thread_prefix = f"[{thread_name}] "

    pages_to_combine = list(range(max(0, page_num - 1), min(len(all_pages_text), page_num + 5)))
    combined_text = ""
    raw_page_data = []
    for page_idx in pages_to_combine:
        page_text = extractor.extract_single_page_text(page_idx)
        combined_text += f"\n--- Page {page_idx + 1} ---\n{page_text}\n"
        raw_page_data.append({"page": page_idx + 1, "text": page_text})

    pin_specific_text = extractor.extract_pin_specific_content(combined_text, pin_id)
    if pin_specific_text:
        text_content = f"Pin {pin_id} specific content (from pages {[p + 1 for p in pages_to_combine]}):\n{pin_specific_text}"
    else:
        text_content = f"Combined text from pages {[p + 1 for p in pages_to_combine]} (WARNING: may contain multiple pins):\n{combined_text}"

    if not text_content.strip():
        print(f"{thread_prefix}No text content found for pin {pin_id}")
        return None

    try:
        input_task = analyzer.create_input_analysis_task(pin_id, text_content, pages_to_combine)
        output_task = analyzer.create_output_analysis_task(pin_id, text_content, pages_to_combine)
        verification_task = analyzer.create_verification_task(
            pin_id,
            "Input modes will be provided by input_mode_agent",
            "Output modes will be provided by output_mode_agent",
            text_content,
            pages_to_combine
        )

        retry_delays = [10, 30, 60]
        max_attempts = len(retry_delays) + 1
        for attempt in range(1, max_attempts + 1):
            crew = Crew(
                agents=[analyzer.input_mode_agent, analyzer.output_mode_agent, analyzer.verification_agent],
                tasks=[input_task, output_task, verification_task],
                process=Process.sequential,
                verbose=False
            )
            try:
                result = crew.kickoff()
                combined_result = str(result)
                pin_result = {
                    "page_found": page_num + 1,
                    "port_number": port_num,
                    "pin_number": pin_num,
                    "collaborative_analysis_result": combined_result,
                    "pages_analyzed": [p + 1 for p in pages_to_combine],
                    "raw_page_data": raw_page_data,
                    "analysis_type": "text_only",
                    "processing_timestamp": datetime.now().isoformat(),
                    "pdf_source": os.path.basename(pdf_path),
                    "status": "completed",
                    "thread_name": thread_name
                }
                pin_filename = f"{pin_id.replace('.', '_')}.json"
                pin_filepath = os.path.join(agent_output_dir, pin_filename)
                with threading.Lock():
                    try:
                        with open(pin_filepath, 'w') as pin_file:
                            json.dump(pin_result, pin_file, indent=2)
                    except Exception as save_error:
                        print(f"{thread_prefix}Failed to save {pin_id}: {save_error}")
                return pin_result
            except Exception as crew_error:
                msg = str(crew_error).lower()
                print(f"{thread_prefix}Error for {pin_id}: {crew_error}")
                if "database is locked" in msg:
                    if attempt <= len(retry_delays):
                        wait_s = retry_delays[attempt - 1]
                        print(f"{thread_prefix}Retrying in {wait_s}s (attempt {attempt}/{max_attempts})...")
                        time.sleep(wait_s)
                        continue
                raise crew_error

    except Exception as e:
        print(f"{thread_prefix}Error processing pin {pin_id}: {str(e)}")
        error_result = {
            "page_found": page_num + 1,
            "port_number": port_num,
            "pin_number": pin_num,
            "error": str(e),
            "analysis_type": "text_only",
            "processing_timestamp": datetime.now().isoformat(),
            "pdf_source": os.path.basename(pdf_path),
            "status": "failed",
            "thread_name": thread_name
        }
        pin_filename = f"{pin_id.replace('.', '_')}_error.json"
        pin_filepath = os.path.join(agent_output_dir, pin_filename)
        with threading.Lock():
            try:
                with open(pin_filepath, 'w') as pin_file:
                    json.dump(error_result, pin_file, indent=2)
            except Exception:
                pass
        return error_result


import asyncio

async def process_single_pin_async(pin_data: tuple, all_pages_text: List[str], pdf_path: str, agent_output_dir: str,
                                   extractor: PortPinExtractor, analyzer: PinModeAnalyzer, semaphore: asyncio.Semaphore) -> dict:
    """Async version of process_single_pin using crew.kickoff_async() for safe concurrency."""
    async with semaphore:
        page_num, pin_id, port_num, pin_num = pin_data
        worker_prefix = f"[async-{pin_id}] "

        pages_to_combine = list(range(max(0, page_num - 1), min(len(all_pages_text), page_num + 5)))
        combined_text = ""
        raw_page_data = []
        for page_idx in pages_to_combine:
            page_text = extractor.extract_single_page_text(page_idx)
            combined_text += f"\n--- Page {page_idx + 1} ---\n{page_text}\n"
            raw_page_data.append({"page": page_idx + 1, "text": page_text})

        pin_specific_text = extractor.extract_pin_specific_content(combined_text, pin_id)
        if pin_specific_text:
            text_content = f"Pin {pin_id} specific content (from pages {[p + 1 for p in pages_to_combine]}):\n{pin_specific_text}"
        else:
            text_content = f"Combined text from pages {[p + 1 for p in pages_to_combine]} (WARNING: may contain multiple pins):\n{combined_text}"

        if not text_content.strip():
            print(f"{worker_prefix}No text content found for pin {pin_id}")
            return None

        try:
            input_task = analyzer.create_input_analysis_task(pin_id, text_content, pages_to_combine)
            output_task = analyzer.create_output_analysis_task(pin_id, text_content, pages_to_combine)
            verification_task = analyzer.create_verification_task(
                pin_id,
                "Input modes will be provided by input_mode_agent",
                "Output modes will be provided by output_mode_agent",
                text_content,
                pages_to_combine
            )

            retry_delays = [10, 30, 60]
            max_attempts = len(retry_delays) + 1
            for attempt in range(1, max_attempts + 1):
                crew = Crew(
                    agents=[analyzer.input_mode_agent, analyzer.output_mode_agent, analyzer.verification_agent],
                    tasks=[input_task, output_task, verification_task],
                    process=Process.sequential,
                    verbose=False
                )
                try:
                    result = await crew.kickoff_async()
                    combined_result = str(result)
                    pin_result = {
                        "page_found": page_num + 1,
                        "port_number": port_num,
                        "pin_number": pin_num,
                        "collaborative_analysis_result": combined_result,
                        "pages_analyzed": [p + 1 for p in pages_to_combine],
                        "raw_page_data": raw_page_data,
                        "analysis_type": "text_only",
                        "processing_timestamp": datetime.now().isoformat(),
                        "pdf_source": os.path.basename(pdf_path),
                        "status": "completed",
                        "thread_name": "async"
                    }
                    pin_filename = f"{pin_id.replace('.', '_')}.json"
                    pin_filepath = os.path.join(agent_output_dir, pin_filename)
                    with open(pin_filepath, 'w') as pin_file:
                        json.dump(pin_result, pin_file, indent=2)
                    print(f"Completed: {pin_id}")
                    return pin_result
                except Exception as crew_error:
                    msg = str(crew_error).lower()
                    print(f"{worker_prefix}Error for {pin_id}: {crew_error}")
                    if "database is locked" in msg or "rate" in msg:
                        if attempt <= len(retry_delays):
                            wait_s = retry_delays[attempt - 1]
                            print(f"{worker_prefix}Retrying in {wait_s}s (attempt {attempt}/{max_attempts})...")
                            await asyncio.sleep(wait_s)
                            continue
                    raise crew_error

        except Exception as e:
            print(f"{worker_prefix}Error processing pin {pin_id}: {str(e)}")
            error_result = {
                "page_found": page_num + 1,
                "port_number": port_num,
                "pin_number": pin_num,
                "error": str(e),
                "analysis_type": "text_only",
                "processing_timestamp": datetime.now().isoformat(),
                "pdf_source": os.path.basename(pdf_path),
                "status": "failed",
                "thread_name": "async"
            }
            pin_filename = f"{pin_id.replace('.', '_')}_error.json"
            pin_filepath = os.path.join(agent_output_dir, pin_filename)
            try:
                with open(pin_filepath, 'w') as pin_file:
                    json.dump(error_result, pin_file, indent=2)
            except Exception:
                pass
            return error_result


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8: PDF PROCESSING ORCHESTRATION
# ══════════════════════════════════════════════════════════════════════════════

def build_gpio_properties_summary(pin_results: Dict[str, Dict[str, Any]], pdf_path: str, device: str, page_range, heading_phrase: str, fallback_section: int) -> Dict[str, Any]:
    aggregated = []
    total_input_modes = 0
    total_output_modes = 0
    for pin_id, data in pin_results.items():
        coll_str = data.get('collaborative_analysis_result', '{}')
        try:
            coll_obj = json.loads(coll_str) if isinstance(coll_str, str) else coll_str
        except json.JSONDecodeError:
            continue
        vs = coll_obj.get('verification_summary', {})
        aggregated.append({
            'pin_id': coll_obj.get('pin_id', pin_id),
            'verification_summary': vs,
            'metadata': {
                'page_found': data.get('page_found'),
                'port_number': data.get('port_number'),
                'pin_number': data.get('pin_number'),
                'pages_analyzed': data.get('pages_analyzed'),
                'status': data.get('status')
            }
        })
        total_input_modes += len(vs.get('input_modes_verified', []) or [])
        total_output_modes += len(vs.get('output_modes_verified', []) or [])

    aggregated.sort(key=lambda x: ((x.get('metadata', {}) or {}).get('port_number', 999), (x.get('metadata', {}) or {}).get('pin_number', 999)))

    return {
        'device': device,
        'pdf_source': os.path.basename(pdf_path),
        'heading_phrase': heading_phrase,
        'fallback_section': fallback_section,
        'total_pins': len(pin_results),
        'verification_summaries': aggregated,
        'stats': {
            'total_input_modes': total_input_modes,
            'total_output_modes': total_output_modes,
            'generated_at': datetime.now().isoformat(),
            'page_range': f"{page_range[0]}-{page_range[1]}" if page_range else 'all'
        }
    }


def process_pdf_for_pin_modes(pdf_path: str, output_root: str = "output", page_range: tuple = None, device: str = None,
                              heading_phrase: str = DEFAULT_PIN_HEADING, fallback_section: int = DEFAULT_FALLBACK_SECTION) -> Dict[str, Any]:
    # Device-based page range override
    if device:
        device_ranges = get_device_page_ranges(pdf_path, heading_phrase=heading_phrase, fallback_section=fallback_section)
        if device not in device_ranges:
            print(f"Device '{device}' not found. Available: {list(device_ranges.keys())}")
        else:
            pr = device_ranges[device]
            page_range = (pr[0], pr[1] - 1)
            print(f"Using device '{device}' pages {page_range[0]} to {page_range[1]}")

    print(f"Processing PDF: {pdf_path}")

    device_dir_name = device if device else "_ALL_"
    device_base_dir = os.path.join(output_root, device_dir_name)
    pins_dir = os.path.join(device_base_dir, "pins")
    os.makedirs(pins_dir, exist_ok=True)
    print(f"Results under: {device_base_dir}")

    extractor = PortPinExtractor(pdf_path)
    print("Extracting text from PDF pages...")
    all_pages_text = extractor.extract_text_by_page()

    if page_range:
        start_page, end_page = page_range
        start_idx = max(0, start_page - 1)
        end_idx = min(len(all_pages_text), end_page)
        pages_text = all_pages_text[start_idx:end_idx]
        page_offset = start_idx
        print(f"Processing pages {start_page} to {end_page} ({len(pages_text)} pages)")
    else:
        pages_text = all_pages_text
        page_offset = 0
        print(f"Extracted {len(pages_text)} pages")

    # Device heading trimming
    if device:
        device_heading_pattern = re.compile(rf"{re.escape(device)}\s+package\s+variant\s+pin\s+configuration", re.IGNORECASE)
        heading_found = False
        search_pages = min(5, len(pages_text))
        for idx in range(search_pages):
            txt = pages_text[idx]
            if not txt:
                continue
            m = device_heading_pattern.search(txt)
            if m:
                heading_found = True
                for i in range(idx):
                    pages_text[i] = ""
                pages_text[idx] = pages_text[idx][m.end():]
                print(f"Trimmed device prefix for '{device}'")
                break
        if not heading_found:
            print(f"Did not find device heading for '{device}'; no trim.")

    print("Searching for Port Pin patterns...")
    found_pins = extractor.find_port_pins(pages_text)

    if page_offset > 0:
        found_pins = [(page_num + page_offset, pin_id, port_num, pin_num)
                      for page_num, pin_id, port_num, pin_num in found_pins]

    print(f"Found {len(found_pins)} unique port pins")

    if not found_pins:
        print("No valid port pins found.")
        return {}

    all_results = {}

    print(f"\n{'='*60}")
    _max_workers = int(os.environ.get("DS_PARSER_WORKERS", "3"))
    print(f"Starting multiprocessing with {_max_workers} worker processes")
    print(f"{'='*60}")

    total_pages = len(all_pages_text)
    mp_args = [(pin_data, pdf_path, pins_dir, total_pages) for pin_data in found_pins]

    from multiprocessing import get_context
    mp_ctx = get_context('spawn')
    with mp_ctx.Pool(processes=_max_workers) as pool:
        results_list = pool.map(_mp_pin_worker, mp_args)

    for pin_data, result in zip(found_pins, results_list):
        pin_id = pin_data[1]
        if result:
            all_results[pin_id] = result

    print(f"\nAll pins completed. Processed {len(all_results)} pins.")

    # Rerun phase for error pins
    try:
        error_files = [f for f in os.listdir(pins_dir) if f.endswith('_error.json')]
    except Exception:
        error_files = []

    if error_files:
        print(f"\nRerunning {len(error_files)} errored pin(s)...")
        rerun_pin_data = []
        for fname in error_files:
            fpath = os.path.join(pins_dir, fname)
            try:
                with open(fpath, 'r') as ef:
                    ed = json.load(ef)
                base = fname[:-len('_error.json')]
                pin_id = base.replace('_', '.')
                page_found = ed.get('page_found')
                port_number = ed.get('port_number')
                pin_number = ed.get('pin_number')
                if isinstance(page_found, int) and port_number is not None and pin_number is not None:
                    rerun_pin_data.append((page_found - 1, pin_id, int(port_number), int(pin_number)))
            except Exception:
                pass

        if rerun_pin_data:
            time.sleep(5)
            workers = max(1, min(_max_workers, len(rerun_pin_data)))
            rerun_args = [(pd, pdf_path, pins_dir, total_pages) for pd in rerun_pin_data]
            # Remove old error files before rerun
            for fname in error_files:
                try:
                    os.remove(os.path.join(pins_dir, fname))
                except Exception:
                    pass
            with mp_ctx.Pool(processes=workers) as pool:
                rerun_results = pool.map(_mp_pin_worker, rerun_args)
            for pd, result in zip(rerun_pin_data, rerun_results):
                pin_id = pd[1]
                if result and result.get('status') == 'completed':
                    all_results[pin_id] = result
                    print(f"Rerun succeeded: {pin_id}")
                else:
                    print(f"Rerun failed for {pin_id}")

    # Build GPIO properties
    gpio_properties = build_gpio_properties_summary(all_results, pdf_path, device, page_range, heading_phrase, fallback_section)
    gpio_path = os.path.join(device_base_dir, "GPIO_properties.json")
    with open(gpio_path, 'w') as f:
        json.dump(gpio_properties, f, indent=2)
    print(f"Saved GPIO_properties.json to {gpio_path}")

    # Split-line flag audit
    try:
        audit_scan_root = Path(device_base_dir)
        audit_report_path = audit_scan_root / "splitline_flags_report.json"
        audit_report = run_splitline_audit(audit_scan_root, audit_report_path)
        print(f"Split-line audit: {audit_report['total_flags']} flag(s)")
    except Exception as audit_err:
        print(f"Warning: split-line audit failed: {audit_err}")

    print(f"\nDevice {device_dir_name} processing complete. Pins: {len(all_results)}")
    return all_results


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9: MAIN ORCHESTRATOR - DEVICE PROPERTIES BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def load_existing_gpio_properties(device: str, output_root: str) -> Optional[Dict]:
    gpio_path = os.path.join(output_root, device, 'GPIO_properties.json')
    if os.path.isfile(gpio_path):
        with open(gpio_path, 'r') as f:
            return json.load(f)
    return None


def load_feature_set(device: str, output_root: str) -> Optional[Dict]:
    fs_path = os.path.join(output_root, device, 'feature_set.json')
    if os.path.isfile(fs_path):
        with open(fs_path, 'r') as f:
            return json.load(f)
    return None


def load_um_inputs(device: str, output_root: str) -> Optional[Dict]:
    um_path = os.path.join(output_root, device, 'um_inputs.json')
    if os.path.isfile(um_path):
        with open(um_path, 'r') as f:
            return json.load(f)
    return None


def collect_device_metadata(device: str, pdf_path: str, page_range: Tuple[int, int]) -> Dict[str, Any]:
    doc = fitz.open(pdf_path)
    try:
        start_idx = max(0, page_range[0] - 1)
        end_idx = min(doc.page_count, page_range[1])
        return {
            'device': device,
            'page_range': {'start': page_range[0], 'end': page_range[1] - 1},
            'total_pages': end_idx - start_idx,
            'pdf_source': os.path.basename(pdf_path),
        }
    finally:
        doc.close()


def build_device_properties(
    pdf_path: str,
    devices: Dict[str, Tuple[int, int]],
    output_root: str,
    skip_pins: bool = False,
    include_feature_set: bool = False,
    heading_phrase: str = DEFAULT_PIN_HEADING,
    fallback_section: int = DEFAULT_FALLBACK_SECTION,
) -> Dict[str, Any]:
    result = {
        'datasheet': os.path.basename(pdf_path),
        'pdf_path': os.path.abspath(pdf_path),
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'heading_phrase': heading_phrase,
        'fallback_section': fallback_section,
        'device_count': len(devices),
        'devices': {},
    }

    total_pins_all = 0
    total_input_modes_all = 0
    total_output_modes_all = 0

    for idx, (device, page_range) in enumerate(devices.items(), 1):
        print(f"\n{'='*60}")
        print(f"[{idx}/{len(devices)}] Processing device: {device} (pages {page_range[0]}-{page_range[1]-1})")
        print(f"{'='*60}")

        device_entry: Dict[str, Any] = {}

        try:
            device_entry['metadata'] = collect_device_metadata(device, pdf_path, page_range)
        except Exception as e:
            device_entry['metadata'] = {'device': device, 'page_range': {'start': page_range[0], 'end': page_range[1] - 1}, 'pdf_source': os.path.basename(pdf_path)}

        if skip_pins:
            print(f"  [SKIP] Pin analysis disabled")
            device_entry['pins'] = None
            device_entry['pin_analysis'] = {'status': 'skipped'}
            existing_gpio = load_existing_gpio_properties(device, output_root)
            if existing_gpio:
                device_entry['existing_gpio_properties'] = existing_gpio
        else:
            existing_gpio = load_existing_gpio_properties(device, output_root)
            if existing_gpio:
                print(f"  [CACHED] Using existing GPIO_properties.json")
                device_entry['pin_analysis'] = existing_gpio
            else:
                print(f"  [RUN] Starting multiagent pin analysis...")
                try:
                    pin_results = process_pdf_for_pin_modes(
                        pdf_path, output_root=output_root, page_range=None, device=device,
                        heading_phrase=heading_phrase, fallback_section=fallback_section,
                    )
                    time.sleep(1)
                    existing_gpio = load_existing_gpio_properties(device, output_root)
                    if existing_gpio:
                        device_entry['pin_analysis'] = existing_gpio
                    else:
                        device_entry['pin_analysis'] = {'status': 'completed', 'pin_count': len(pin_results) if pin_results else 0}
                except Exception as e:
                    device_entry['pin_analysis'] = {'status': 'error', 'error': str(e)}
                    print(f"  [ERROR] Pin analysis failed: {e}")

            pa = device_entry.get('pin_analysis', {})
            if pa and isinstance(pa, dict):
                vs_list = pa.get('verification_summaries', [])
                pin_count = len(vs_list)
                total_pins_all += pin_count
                for vs in vs_list:
                    vs_data = vs.get('verification_summary', {}) if 'verification_summary' in vs else vs
                    total_input_modes_all += len(vs_data.get('input_modes_verified', []))
                    total_output_modes_all += len(vs_data.get('output_modes_verified', []))
                device_entry['pin_count'] = pin_count

        if include_feature_set:
            fs = load_feature_set(device, output_root)
            if fs:
                device_entry['feature_set'] = fs

        um = load_um_inputs(device, output_root)
        if um:
            device_entry['um_inputs'] = um

        audit_path = os.path.join(output_root, device, 'splitline_flags_report.json')
        if os.path.isfile(audit_path):
            with open(audit_path, 'r') as f:
                device_entry['splitline_audit'] = json.load(f)

        result['devices'][device] = device_entry

    result['statistics'] = {
        'total_devices': len(devices),
        'total_pins': total_pins_all,
        'total_input_modes': total_input_modes_all,
        'total_output_modes': total_output_modes_all,
    }
    return result


def interactive_device_selection(devices: List[str]) -> List[str]:
    print("\nDetected devices:")
    for i, d in enumerate(devices, 1):
        print(f"  {i}. {d}")
    selection = input("\nEnter device numbers (comma-separated) or 'all': ").strip().lower()
    if selection == 'all':
        return devices
    try:
        indices = [int(x.strip()) for x in selection.split(',') if x.strip()]
        return [devices[i - 1] for i in indices if 1 <= i <= len(devices)]
    except (ValueError, IndexError):
        print("Invalid selection. Processing all.")
        return devices


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description='Standalone DS parser -> device_properties.json')
    p.add_argument('--pdf', required=True, help='Path to the datasheet PDF.')
    p.add_argument('--devices', help='Comma-separated device names.')
    p.add_argument('--all-devices', action='store_true', help='Process all detected devices.')
    p.add_argument('--heading', default=DEFAULT_PIN_HEADING, help='Heading phrase for device extraction.')
    p.add_argument('--fallback', type=int, default=DEFAULT_FALLBACK_SECTION, help='Fallback numeric section.')
    p.add_argument('--output-root', default=HERE, help='Root directory for output.')
    p.add_argument('--output-file', default=DEFAULT_OUTPUT_FILE, help='Output JSON filename.')
    p.add_argument('--skip-pins', action='store_true', help='Skip pin-level analysis.')
    p.add_argument('--include-feature-set', action='store_true', help='Include feature_set.json per device.')
    return p.parse_args()


def main():
    args = parse_args()

    if not os.path.isfile(args.pdf):
        print(f"[DS_parser] Error: PDF not found: {args.pdf}")
        sys.exit(1)

    print("=" * 60)
    print("  DS_parser - Standalone Device Properties Generator")
    print("=" * 60)
    print(f"  PDF: {args.pdf}")
    print(f"  Output root: {args.output_root}")
    print(f"  Output file: {args.output_file}")
    print(f"  Skip pins: {args.skip_pins}")
    print(f"  Heading: {args.heading}")
    print("=" * 60)

    os.makedirs(args.output_root, exist_ok=True)

    print("\n[DS_parser] Discovering devices in datasheet...")
    device_mapping = get_device_page_ranges(args.pdf, heading_phrase=args.heading, fallback_section=args.fallback)

    if not device_mapping:
        print("[DS_parser] No devices detected. Processing entire document.")
        device_mapping = {'_FULL_DOCUMENT_': (1, 99999)}

    devices_sorted = sorted(device_mapping.keys(), key=lambda d: device_mapping[d][0])
    print(f"\n[DS_parser] Found {len(devices_sorted)} device(s): {', '.join(devices_sorted)}")

    if args.devices:
        target_devices = [d.strip() for d in args.devices.split(',') if d.strip()]
        target_devices = [d for d in target_devices if d in device_mapping]
        if not target_devices:
            print("[DS_parser] Error: No valid devices specified.")
            sys.exit(1)
    elif args.all_devices:
        target_devices = devices_sorted
    else:
        target_devices = interactive_device_selection(devices_sorted)

    filtered_mapping = {d: device_mapping[d] for d in target_devices}

    print(f"\n[DS_parser] Building device properties for {len(filtered_mapping)} device(s)...")
    start_time = time.time()

    device_props = build_device_properties(
        pdf_path=args.pdf,
        devices=filtered_mapping,
        output_root=args.output_root,
        skip_pins=args.skip_pins,
        include_feature_set=args.include_feature_set,
        heading_phrase=args.heading,
        fallback_section=args.fallback,
    )

    elapsed = time.time() - start_time

    output_path = os.path.join(args.output_root, args.output_file)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(device_props, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"[DS_parser] Complete!")
    print(f"  Devices processed: {device_props['device_count']}")
    print(f"  Total pins: {device_props['statistics']['total_pins']}")
    print(f"  Total input modes: {device_props['statistics']['total_input_modes']}")
    print(f"  Total output modes: {device_props['statistics']['total_output_modes']}")
    print(f"  Elapsed time: {elapsed:.1f}s")
    print(f"  Output: {output_path}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
