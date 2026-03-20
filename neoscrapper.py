"""
NeoScrapper V2: The Sovereign Data Harvester
=============================================
General-purpose, instruction-driven extraction engine.
3-Tier Intelligence: Code → Local LLM → Gemini CLI

Author: Prabhakar Chandra (Dual-Core Architect)
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Load .env credentials
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass  # python-dotenv not installed, rely on system env vars

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "neoscrapper.db")
RESULTS_DIR = os.path.join(BASE_DIR, "results")
REVIEW_DIR = os.path.join(RESULTS_DIR, "_review")
SCHEMAS_DIR = os.path.join(BASE_DIR, "schemas")

JINA_API_KEY = os.environ.get("JINA_API_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
GEMINI_DAILY_BUDGET = 500
CONFIDENCE_THRESHOLD = 0.6

# UA rotation pool
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 OPR/109.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.6422.112 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("neoscrapper")


# ---------------------------------------------------------------------------
# SQLite Registry
# ---------------------------------------------------------------------------
class Registry:
    """SQLite index layer for querying, dedup, and budget tracking."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id       TEXT PRIMARY KEY,
                    created_at     TEXT NOT NULL,
                    instruction    TEXT NOT NULL,
                    engine         TEXT NOT NULL,
                    urls_total     INTEGER DEFAULT 0,
                    success        INTEGER DEFAULT 0,
                    review         INTEGER DEFAULT 0,
                    gemini_used    INTEGER DEFAULT 0,
                    jina_used      INTEGER DEFAULT 0,
                    local_used     INTEGER DEFAULT 0,
                    avg_confidence REAL DEFAULT 0.0,
                    duration_s     REAL DEFAULT 0.0
                );

                CREATE TABLE IF NOT EXISTS extractions (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id      TEXT REFERENCES batches(batch_id),
                    seq           INTEGER NOT NULL,
                    url           TEXT NOT NULL,
                    timestamp     TEXT NOT NULL,
                    engine        TEXT NOT NULL,
                    fetch_method  TEXT NOT NULL,
                    confidence    REAL DEFAULT 0.0,
                    needs_review  BOOLEAN DEFAULT 0,
                    instruction   TEXT,
                    data_json     TEXT,
                    diagnosis     TEXT,
                    file_path     TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS budget_log (
                    date          TEXT PRIMARY KEY,
                    gemini_cli    INTEGER DEFAULT 0,
                    gemini_api    INTEGER DEFAULT 0,
                    jina_calls    INTEGER DEFAULT 0,
                    local_calls   INTEGER DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_confidence ON extractions(confidence);
                CREATE INDEX IF NOT EXISTS idx_url ON extractions(url);
                CREATE INDEX IF NOT EXISTS idx_review ON extractions(needs_review);
            """)

    def is_duplicate(self, url: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT id FROM extractions WHERE url = ? LIMIT 1", (url,)
            ).fetchone()
            return row is not None

    def get_budget_today(self) -> dict:
        today = date.today().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT gemini_cli, gemini_api, jina_calls, local_calls FROM budget_log WHERE date = ?",
                (today,),
            ).fetchone()
            if row:
                return {"gemini_cli": row[0], "gemini_api": row[1], "jina": row[2], "local": row[3]}
            return {"gemini_cli": 0, "gemini_api": 0, "jina": 0, "local": 0}

    def increment_budget(self, engine: str, fetch_method: str):
        today = date.today().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO budget_log (date) VALUES (?) ON CONFLICT(date) DO NOTHING",
                (today,),
            )
            if engine == "cloud":
                conn.execute("UPDATE budget_log SET gemini_cli = gemini_cli + 1 WHERE date = ?", (today,))
            elif engine == "local":
                conn.execute("UPDATE budget_log SET local_calls = local_calls + 1 WHERE date = ?", (today,))
            if fetch_method == "jina":
                conn.execute("UPDATE budget_log SET jina_calls = jina_calls + 1 WHERE date = ?", (today,))
            conn.commit()

    def create_batch(self, batch_id: str, instruction: str, engine: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO batches (batch_id, created_at, instruction, engine) VALUES (?, ?, ?, ?)",
                (batch_id, datetime.now().isoformat(), instruction, engine),
            )
            conn.commit()

    def get_next_batch_id_from_db(self) -> str:
        """Derive next batch_id from SQLite (source of truth), not filesystem."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT MAX(CAST(SUBSTR(batch_id, 7) AS INTEGER)) FROM batches").fetchone()
            next_num = (row[0] or 0) + 1 if row and row[0] is not None else 1
        return f"batch_{next_num:03d}"

    def insert_extraction(self, batch_id: str, seq: int, url: str, engine: str,
                          fetch_method: str, confidence: float, needs_review: bool,
                          instruction: str, data_json: str, diagnosis: str, file_path: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO extractions
                   (batch_id, seq, url, timestamp, engine, fetch_method, confidence,
                    needs_review, instruction, data_json, diagnosis, file_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (batch_id, seq, url, datetime.now().isoformat(), engine, fetch_method,
                 confidence, needs_review, instruction, data_json, diagnosis, file_path),
            )
            conn.commit()

    def update_batch_stats(self, batch_id: str, urls_total: int, success: int,
                           review: int, gemini_used: int, jina_used: int,
                           local_used: int, avg_confidence: float, duration_s: float):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """UPDATE batches SET urls_total=?, success=?, review=?,
                   gemini_used=?, jina_used=?, local_used=?,
                   avg_confidence=?, duration_s=? WHERE batch_id=?""",
                (urls_total, success, review, gemini_used, jina_used,
                 local_used, avg_confidence, duration_s, batch_id),
            )
            conn.commit()

    def search(self, keyword: str) -> list:
        """Safe parameterized search across url, instruction, and data_json."""
        pattern = f"%{keyword}%"
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT * FROM extractions
                   WHERE url LIKE ? OR instruction LIKE ? OR data_json LIKE ?
                   ORDER BY id DESC LIMIT 50""",
                (pattern, pattern, pattern),
            ).fetchall()
            return [dict(r) for r in rows]

    def last(self, n: int = 5) -> list:
        """Return the N most recent extractions."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM extractions ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
            return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Fetcher (Cascading: Jina → Crawl4AI → curl)
# ---------------------------------------------------------------------------
@dataclass
class FetchResult:
    content: str = ""
    method: str = "none"
    status_code: int = 0
    content_length: int = 0
    error: str = ""


class Fetcher:
    """Cascading URL fetcher: Jina Reader → Crawl4AI → curl fallback."""

    def __init__(self):
        self._ua_index = 0

    def _next_ua(self) -> str:
        ua = USER_AGENTS[self._ua_index % len(USER_AGENTS)]
        self._ua_index += 1
        return ua

    def fetch(self, url: str) -> FetchResult:
        """Try Jina → Crawl4AI → curl. Returns first success."""
        # Try 1: Jina Reader API
        result = self._fetch_jina(url)
        if result.content_length > 500:
            return result

        logger.info(f"Jina returned {result.content_length} chars, trying Crawl4AI...")

        # Try 2: Crawl4AI (SPA fallback)
        result_c4a = self._fetch_crawl4ai(url)
        if result_c4a.content_length > 500:
            return result_c4a

        logger.info(f"Crawl4AI returned {result_c4a.content_length} chars, trying curl...")

        # Try 3: curl fallback
        return self._fetch_curl(url)

    def _fetch_jina(self, url: str) -> FetchResult:
        try:
            import requests
            headers = {"Accept": "text/markdown"}
            if JINA_API_KEY:
                headers["Authorization"] = f"Bearer {JINA_API_KEY}"

            resp = requests.get(f"https://r.jina.ai/{url}", headers=headers, timeout=15)
            content = resp.text if resp.status_code == 200 else ""
            return FetchResult(
                content=content,
                method="jina",
                status_code=resp.status_code,
                content_length=len(content),
            )
        except Exception as e:
            logger.warning(f"Jina fetch failed: {e}")
            return FetchResult(method="jina", error=str(e))

    def _fetch_crawl4ai(self, url: str) -> FetchResult:
        try:
            import asyncio
            from crawl4ai import AsyncWebCrawler, CacheMode
            from crawl4ai.extraction_strategy import NoExtractionStrategy

            async def _crawl():
                async with AsyncWebCrawler(verbose=False) as crawler:
                    result = await crawler.arun(
                        url=url,
                        cache_mode=CacheMode.BYPASS,
                        magic=True,
                        extraction_strategy=NoExtractionStrategy(),
                    )
                    if result.success:
                        md = result.markdown or ""
                        if len(md) > 500000:
                            md = md[:500000]
                        return md
                    return ""

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                content = loop.run_until_complete(_crawl())
            finally:
                loop.close()
            return FetchResult(
                content=content,
                method="crawl4ai",
                status_code=200 if content else 0,
                content_length=len(content),
            )
        except ImportError:
            logger.warning("crawl4ai not installed — skipping SPA fallback")
            return FetchResult(method="crawl4ai", error="crawl4ai not installed")
        except Exception as e:
            logger.warning(f"Crawl4AI failed: {e}")
            return FetchResult(method="crawl4ai", error=str(e))

    def _fetch_curl(self, url: str, retries: int = 2) -> FetchResult:
        ua = self._next_ua()
        for attempt in range(retries + 1):
            try:
                result = subprocess.run(
                    ["curl.exe", "-s", "-L", "-m", "20",
                     "-H", f"User-Agent: {ua}",
                     "-w", "\n%{http_code}", url],
                    capture_output=True, text=True, encoding="utf-8",
                )
                parts = result.stdout.rsplit("\n", 1)
                body = parts[0] if len(parts) > 1 else result.stdout
                code = int(parts[1]) if len(parts) > 1 else 0

                if code in (403, 429):
                    return FetchResult(content=body, method="curl",
                                      status_code=code, content_length=len(body),
                                      error=f"Blocked: HTTP {code}")
                if code >= 500 and attempt < retries:
                    time.sleep(2 ** (attempt + 1))
                    continue

                return FetchResult(
                    content=body, method="curl",
                    status_code=code, content_length=len(body),
                )
            except Exception as e:
                if attempt < retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                return FetchResult(method="curl", error=str(e))

        return FetchResult(method="curl", error="All retries exhausted")


# ---------------------------------------------------------------------------
# Scrubber (Token budget + HTML cleanup for curl path)
# ---------------------------------------------------------------------------
class Scrubber:
    """Clean content and enforce token budgets."""

    @staticmethod
    def scrub(content: str, fetch_method: str, engine: str = "cloud") -> str:
        # Jina/Crawl4AI already return clean markdown — just enforce budget
        if fetch_method in ("jina", "crawl4ai"):
            budget = 30000 if engine == "cloud" else 6000
            return content[:budget]

        # curl path: full HTML cleaning pipeline
        text = content
        # Strip script, style, nav, header, footer blocks
        for tag in ["script", "style", "nav", "header", "footer", "noscript"]:
            text = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", text, flags=re.DOTALL | re.IGNORECASE)
        # Strip remaining HTML tags
        text = re.sub(r"<[^>]+>", " ", text)
        # Collapse whitespace
        text = re.sub(r"\s+", " ", text).strip()

        budget = 30000 if engine == "cloud" else 6000
        return text[:int(budget)]


# ---------------------------------------------------------------------------
# Scorer & Diagnosis
# ---------------------------------------------------------------------------
@dataclass
class ScoreResult:
    confidence: float = 0.0
    needs_review: bool = True
    diagnosis: dict = field(default_factory=dict)


class Scorer:
    """Compute confidence and generate diagnosis for low-confidence results."""

    @staticmethod
    def score(extracted_data: dict, raw_content_length: int,
              json_parsed: bool, instruction_fields: list,
              fetch_result: FetchResult) -> ScoreResult:

        # Completeness: non-null fields / total fields
        if instruction_fields:
            non_null = sum(1 for f in instruction_fields if extracted_data.get(f) is not None)
            completeness = non_null / len(instruction_fields)
        else:
            completeness = 1.0 if extracted_data else 0.0

        # Content quality
        if raw_content_length > 5000:
            content_quality = 1.0
        elif raw_content_length > 2000:
            content_quality = 0.5
        else:
            content_quality = 0.1

        # Format validity
        format_score = 1.0 if json_parsed else 0.0

        confidence = (completeness * 0.5) + (content_quality * 0.3) + (format_score * 0.2)
        needs_review = confidence < CONFIDENCE_THRESHOLD

        # Diagnosis
        diagnosis = {}
        if needs_review:
            if raw_content_length < 2000:
                diagnosis = {"reason": "Minimal content fetched",
                             "suggestion": "Retry with Crawl4AI or verify URL",
                             "http_status": fetch_result.status_code,
                             "content_length": raw_content_length}
            elif fetch_result.status_code in (403, 429):
                diagnosis = {"reason": f"WAF blocked (HTTP {fetch_result.status_code})",
                             "suggestion": "Try different UA or add delay",
                             "http_status": fetch_result.status_code,
                             "content_length": raw_content_length}
            elif not json_parsed:
                diagnosis = {"reason": "LLM output not parseable as JSON",
                             "suggestion": "Manual review of raw output",
                             "http_status": fetch_result.status_code,
                             "content_length": raw_content_length}
            elif completeness < 0.5:
                diagnosis = {"reason": f"Only {int(completeness*100)}% fields populated",
                             "suggestion": "URL may not contain requested data",
                             "http_status": fetch_result.status_code,
                             "content_length": raw_content_length}

        return ScoreResult(confidence=float(round(confidence, 3)),
                           needs_review=needs_review,
                           diagnosis=diagnosis)


# ---------------------------------------------------------------------------
# Extractor (Prompt compiler + Engine routing)
# ---------------------------------------------------------------------------
class Extractor:
    """Compiles instructions into prompts and routes to local/cloud LLM."""

    @staticmethod
    def compile_prompt(instruction: str, content: str, fields: list = None) -> str:
        """Build prompt. Structured (JSON) if fields provided, general (free-text) otherwise."""
        if fields:
            # Structured mode: enforce JSON output
            field_spec = "Fields to extract: " + ", ".join(fields) + "\n"
            return f"""You are a precise data extraction engine.
Task: Extract data from the provided text according to the instruction.
Instruction: {instruction}
{field_spec}
Rules:
1. Output ONLY valid JSON. No markdown fences, no explanation, no preamble.
2. If a field cannot be found, set its value to null.
3. For lists of items, return a JSON array of objects.

Example:
Input: "Acme Corp uses React and Go. Series B, 150 employees."
Output: {{"company": "Acme Corp", "tech_stack": ["React", "Go"], "funding": "Series B", "size": 150}}

---
TEXT TO EXTRACT FROM:
{content}"""
        else:
            # General mode: let LLM decide format based on instruction
            return f"""You are a precise data extraction and analysis engine.
Task: {instruction}

Rules:
1. Follow the instruction precisely.
2. If the instruction asks for structured data (names, prices, lists), output valid JSON.
3. If the instruction asks for summaries, analysis, or comparisons, output clean text.
4. No markdown fences. No preamble. Just the answer.

---
TEXT TO PROCESS:
{content}"""

    @staticmethod
    def extract_local(prompt: str) -> str:
        """Route to Ollama (Qwen2.5-3B)."""
        try:
            import ollama
            response = ollama.generate(model="qwen2.5:3b", prompt=prompt)
            return response.get("response", "")
        except ImportError:
            # Fallback: HTTP request to Ollama API
            import requests
            resp = requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": "qwen2.5:3b", "prompt": prompt, "stream": False},
                timeout=120,
            )
            return resp.json().get("response", "")

    @staticmethod
    def extract_cloud(prompt: str) -> str:
        """Route to Gemini Cloud: Direct API first, then CLI fallback."""
        if GEMINI_API_KEY:
            return Extractor._extract_via_api(prompt)
        return Extractor._extract_via_cli(prompt)

    @staticmethod
    def _extract_via_api(prompt: str) -> str:
        """Direct call to Google Gemini API with retry on 429."""
        import requests
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
        headers = {'Content-Type': 'application/json'}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.1}
        }
        max_retries = 3
        for attempt in range(max_retries):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=90)
                if resp.status_code == 200:
                    res = resp.json()
                    return res.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', "")
                elif resp.status_code == 429:
                    # Rate limited — honor retryDelay if present
                    retry_delay = 5  # default
                    try:
                        error_data = resp.json()
                        for detail in error_data.get('error', {}).get('details', []):
                            if 'retryDelay' in detail:
                                delay_str = detail['retryDelay'].rstrip('s')
                                retry_delay = int(float(delay_str)) + 1
                    except Exception:
                        pass
                    if attempt < max_retries - 1:
                        logger.warning(f"Gemini API rate limited (429). Retrying in {retry_delay}s... (attempt {attempt+1}/{max_retries})")
                        time.sleep(retry_delay)
                        continue
                    logger.error(f"Gemini API rate limited after {max_retries} retries.")
                    return ""
                else:
                    logger.error(f"Gemini API failed (HTTP {resp.status_code}): {resp.text[:500]}")
                    return ""
            except Exception as e:
                logger.error(f"Gemini API request error: {e}")
                return ""
        return ""

    @staticmethod
    def _extract_via_cli(prompt: str) -> str:
        """Route to Gemini CLI via subprocess (fallback)."""
        # Absolute path to avoid [WinError 2] and bypass cmd.exe 8k limit via shell=False
        gemini_path = os.path.join(os.environ.get("APPDATA", ""), r"npm\gemini.cmd")
        if not os.path.exists(gemini_path):
            gemini_path = "gemini" # Fallback to PATH

        try:
            result = subprocess.run(
                [gemini_path, "-p", prompt],
                capture_output=True, text=True, encoding="utf-8", 
                timeout=60, shell=False
            )
            if result.returncode != 0:
                logger.error(f"Gemini CLI failed (code {result.returncode}): {result.stderr}")
            return result.stdout.strip()
        except Exception as e:
            logger.error(f"Gemini CLI execution error: {e}")
            return ""

    @staticmethod
    def parse_json(raw: str) -> tuple[dict | list | None, bool]:
        """Parse LLM output as JSON, stripping markdown fences if present."""
        # Try direct parse
        try:
            return json.loads(raw), True
        except json.JSONDecodeError:
            pass
        # Strip markdown fences
        cleaned = re.sub(r"^```(?:json)?\n?", "", raw, flags=re.MULTILINE | re.IGNORECASE)
        cleaned = re.sub(r"\n?```$", "", cleaned, flags=re.MULTILINE).strip()
        try:
            return json.loads(cleaned), True
        except json.JSONDecodeError:
            return None, False

    def extract(self, instruction: str, content: str, engine: str = "cloud",
                fields: list = None, registry: "Registry" = None) -> tuple[dict, bool]:
        """Full extraction pipeline. Structured (JSON) if fields provided, general otherwise."""
        structured_mode = fields is not None and len(fields) > 0
        prompt = self.compile_prompt(instruction, content, fields)

        for attempt in range(2):
            if engine == "local":
                raw = self.extract_local(prompt)
            else:
                raw = self.extract_cloud(prompt)

            if structured_mode:
                # Structured mode: parse as JSON
                data, parsed = self.parse_json(raw)
                if parsed and data is not None:
                    return (data if isinstance(data, dict) else {"results": data}), True
                # Retry with stricter prompt
                if attempt == 0:
                    prompt += "\n\nCRITICAL: Output ONLY a raw JSON object. No markdown. No explanation."
                    logger.info("JSON parse failed, retrying with stricter prompt...")
            else:
                # General mode: try JSON first, fallback to free-text
                data, parsed = self.parse_json(raw)
                if parsed and data is not None:
                    return (data if isinstance(data, dict) else {"results": data}), True
                # Not JSON — that's fine in general mode, wrap the text
                if raw.strip():
                    return {"response": raw.strip()}, True
                if attempt == 0:
                    logger.info("Empty response, retrying...")

        # Return raw on failure
        return {"_raw_output": raw[:2000] if raw else ""}, False


# ---------------------------------------------------------------------------
# Storage (File writer)
# ---------------------------------------------------------------------------
class Storage:
    """Write extraction results to sequenced batch folders."""

    @staticmethod
    def get_next_batch_id() -> str:
        """Fallback: derive from filesystem. Prefer Registry.get_next_batch_id_from_db()."""
        os.makedirs(RESULTS_DIR, exist_ok=True)
        existing = [d for d in os.listdir(RESULTS_DIR)
                    if os.path.isdir(os.path.join(RESULTS_DIR, d)) and d.startswith("batch_")]
        if not existing:
            return "batch_001"
        nums = [int(d.split("_")[1]) for d in existing if d.split("_")[1].isdigit()]
        return f"batch_{max(nums) + 1:03d}"

    @staticmethod
    def domain_slug(url: str) -> str:
        parsed = urlparse(url)
        return parsed.netloc.replace(".", "_").replace(":", "_") if parsed.netloc else "unknown"

    @staticmethod
    def save_extraction(batch_dir: str, seq: int, url: str, result: dict) -> str:
        os.makedirs(batch_dir, exist_ok=True)
        slug = Storage.domain_slug(url)
        filename = f"{seq:03d}_{slug}.json"
        filepath = os.path.join(batch_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        return filepath

    @staticmethod
    def save_to_review(batch_id: str, seq: int, url: str, result: dict) -> str:
        os.makedirs(REVIEW_DIR, exist_ok=True)
        slug = Storage.domain_slug(url)
        filename = f"{batch_id}_{seq:03d}_{slug}.json"
        filepath = os.path.join(REVIEW_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        return filepath

    @staticmethod
    def save_manifest(batch_dir: str, manifest: dict):
        filepath = os.path.join(batch_dir, "_manifest.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# BatchRunner (Orchestrator)
# ---------------------------------------------------------------------------
class BatchRunner:
    """Orchestrates batch extraction: fetch → scrub → extract → score → save."""

    def __init__(self, engine: str = "cloud"):
        self.fetcher = Fetcher()
        self.scrubber = Scrubber()
        self.extractor = Extractor()
        self.scorer = Scorer()
        self.storage = Storage()
        self.registry = Registry()
        self.engine = engine

    def _check_budget_guard(self) -> str:
        """If Gemini budget exhausted, downgrade to local."""
        if self.engine in ("cloud", "hybrid"):
            budget = self.registry.get_budget_today()
            if budget["gemini_cli"] >= GEMINI_DAILY_BUDGET:
                logger.warning(f"Gemini budget exhausted ({budget['gemini_cli']}/{GEMINI_DAILY_BUDGET}). Falling back to local.")
                return "local"
        return self.engine

    def run_single(self, url: str, instruction: str, batch_id: str,
                   seq: int, batch_dir: str, fields: list = None) -> dict:
        """Process a single URL through the full pipeline."""
        engine = self._check_budget_guard()
        start = time.time()

        # 1. Fetch
        fetch_result = self.fetcher.fetch(url)

        # 2. Scrub
        cleaned = self.scrubber.scrub(fetch_result.content, fetch_result.method, engine)

        # 3. Extract
        if engine == "hybrid":
            # Local pre-classification
            pre_prompt = f"Is this text relevant to: '{instruction}'? Answer ONLY 'yes' or 'no'.\n\nText: {cleaned[:1000]}"
            try:
                pre_answer = self.extractor.extract_local(pre_prompt).strip().lower()
            except Exception:
                pre_answer = "yes"
            actual_engine = "cloud" if "yes" in pre_answer else "local"
        else:
            actual_engine = engine

        data, json_ok = self.extractor.extract(instruction, cleaned, actual_engine, fields, self.registry)
        self.registry.increment_budget(actual_engine, fetch_result.method)

        # 4. Score
        score = self.scorer.score(data, fetch_result.content_length, json_ok,
                                  fields or [], fetch_result)

        # 5. Build result
        result = {
            "seq": seq,
            "url": url,
            "timestamp": datetime.now().isoformat(),
            "engine": actual_engine,
            "fetch_method": fetch_result.method,
            "confidence": score.confidence,
            "needs_review": score.needs_review,
            "instruction": instruction,
            "data": data,
            "latency_s": round(time.time() - start, 2),
        }
        if score.diagnosis:
            result["diagnosis"] = score.diagnosis

        # 6. Save to file
        filepath = self.storage.save_extraction(batch_dir, seq, url, result)
        if score.needs_review:
            self.storage.save_to_review(batch_id, seq, url, result)

        # 7. Save to SQLite
        self.registry.insert_extraction(
            batch_id, seq, url, actual_engine, fetch_result.method,
            score.confidence, score.needs_review, instruction,
            json.dumps(data), json.dumps(score.diagnosis), filepath,
        )

        return result

    def run_batch(self, urls: list[str], instruction: str, fields: list = None):
        """Process a list of URLs sequentially with never-stop logic."""
        # Use SQLite as source of truth for batch_id (prevents orphan collisions)
        batch_id = self.registry.get_next_batch_id_from_db()
        batch_dir = os.path.join(RESULTS_DIR, batch_id)
        os.makedirs(batch_dir, exist_ok=True)
        self.registry.create_batch(batch_id, instruction, self.engine)

        # Pre-flight budget display
        budget = self.registry.get_budget_today()
        est_calls = int(len(urls) * 1.5)  # ~50% retry rate estimate
        print(f"[Pre-flight] {len(urls)} URLs | Budget: {budget['gemini_cli']}/{GEMINI_DAILY_BUDGET} used today | Est. API calls: ~{est_calls}")

        stats_success: int = 0
        stats_review: int = 0
        stats_gemini: int = 0
        stats_jina: int = 0
        stats_local: int = 0
        stats_confidences: list[float] = []
        start_time = time.time()

        for seq, url in enumerate(urls, 1):
            url = url.strip()
            if not url:
                continue

            # Dedup check
            if self.registry.is_duplicate(url):
                logger.info(f"[{seq}/{len(urls)}] ⊘ {url} (already scraped, skipping)")
                continue

            try:
                result = self.run_single(url, instruction, batch_id, seq, batch_dir, fields)
                conf = result["confidence"]
                stats_confidences.append(conf)

                if result["needs_review"]:
                    stats_review += 1
                    status = "⚠"
                else:
                    stats_success += 1
                    status = "✓"

                if result["engine"] == "cloud":
                    stats_gemini += 1
                else:
                    stats_local += 1
                if result["fetch_method"] == "jina":
                    stats_jina += 1

                budget = self.registry.get_budget_today()
                print(f"[{seq}/{len(urls)}] {status} {urlparse(url).netloc} "
                      f"({conf:.2f}) | ⚠ {stats_review} review | "
                      f"Budget: {budget['gemini_cli']}/{GEMINI_DAILY_BUDGET} Gemini")

            except Exception as e:
                logger.error(f"[{seq}/{len(urls)}] ✗ {url}: {e}")
                print(f"[{seq}/{len(urls)}] ✗ {urlparse(url).netloc} (CRASH: {e})")
                stats_review += 1

            # CPU Thermal Guard (every 10 URLs)
            if seq % 10 == 0:
                try:
                    import psutil
                    # On Windows, psutil.sensors_temperatures() may not be available.
                    # Fallback to WMI if needed.
                    cpu_temp = 0
                    if hasattr(psutil, "sensors_temperatures"):
                        temps = psutil.sensors_temperatures()
                        if "coretemp" in temps:
                            cpu_temp = max(t.current for t in temps["coretemp"])
                    
                    if cpu_temp == 0:
                        # Windows WMI fallback
                        import subprocess
                        wmi_cmd = "wmic /namespace:\\\\root\\wmi PATH MSAcpi_ThermalZoneTemperature get CurrentTemperature"
                        wmi_out = subprocess.check_output(wmi_cmd, shell=True).decode().split()
                        if len(wmi_out) > 1:
                            # Temp is in deci-Kelvin
                            cpu_temp = (int(wmi_out[1]) / 10.0) - 273.15
                    
                    if cpu_temp > 85:
                        logger.warning(f"CPU Thermal Guard: Temperature {cpu_temp:.1f}°C > 85°C. Pausing for 60s...")
                        time.sleep(60)
                except Exception as e:
                    logger.debug(f"Thermal check failed: {e}")

            # Rate limiter
            time.sleep(3)

        # Write manifest
        duration = time.time() - start_time
        avg_conf = sum(stats_confidences) / len(stats_confidences) if stats_confidences else 0
        manifest = {
            "batch_id": batch_id,
            "created": datetime.now().isoformat(),
            "instruction": instruction,
            "engine": self.engine,
            "urls_total": len(urls),
            "success_count": stats_success,
            "review_count": stats_review,
            "gemini_requests_used": stats_gemini,
            "jina_requests_used": stats_jina,
            "local_requests_used": stats_local,
            "avg_confidence": round(avg_conf, 3),
            "duration_seconds": round(duration, 1),
        }
        self.storage.save_manifest(batch_dir, manifest)
        self.registry.update_batch_stats(
            batch_id, len(urls), stats_success, stats_review,
            stats_gemini, stats_jina, stats_local, avg_conf, duration,
        )

        print(f"\n{'='*60}")
        print(f"Batch {batch_id} complete: {stats_success} success, "
              f"{stats_review} review, avg confidence {avg_conf:.2f}")
        print(f"Duration: {duration:.1f}s | Results: {batch_dir}")
        print(f"{'='*60}")
        return batch_id


# ---------------------------------------------------------------------------
# CLI Interface
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="NeoScrapper V2: The Sovereign Data Harvester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # General mode — any instruction, free-text or JSON output
  python neoscrapper.py --url https://github.com/about --instruction "Summarize this company"

  # Structured mode — with schema for guaranteed JSON fields
  python neoscrapper.py --url https://truefoundry.com --instruction "Get details" --schema schemas/lead_gen.json

  # Batch mode
  python neoscrapper.py --batch urls.txt --instruction "Get tech stack" --engine cloud

  # Search past extractions
  python neoscrapper.py --search github

  # Show last 10 extractions
  python neoscrapper.py --last 10

  # Check budget
  python neoscrapper.py --budget
        """,
    )
    parser.add_argument("--url", help="Single URL to scrape")
    parser.add_argument("--batch", help="Path to .txt file with one URL per line")
    parser.add_argument("--instruction", help="What to extract or analyze (plain English)")
    parser.add_argument("--engine", choices=["local", "cloud", "hybrid"], default="cloud")
    parser.add_argument("--schema", help="Path to pre-built schema JSON for structured extraction")
    parser.add_argument("--budget", action="store_true", help="Show today's API budget usage")
    parser.add_argument("--search", help="Search past extractions by keyword (url, instruction, or data)")
    parser.add_argument("--last", type=int, metavar="N", help="Show the last N extractions")
    parser.add_argument("--review", help="Show review items for a batch ID")

    args = parser.parse_args()

    # Budget check
    if args.budget:
        reg = Registry()
        b = reg.get_budget_today()
        print(f"Today's Budget: Gemini CLI {b['gemini_cli']}/{GEMINI_DAILY_BUDGET} | "
              f"Local {b['local']} | Jina {b['jina']}")
        return

    # Search past results (safe, parameterized)
    if args.search:
        reg = Registry()
        results = reg.search(args.search)
        if not results:
            print(f"No results matching '{args.search}'.")
            return
        for r in results:
            print(f"  [{r['confidence']:.2f}] {r['url']}")
            print(f"           {r['instruction']} → {r['file_path']}")
        print(f"\n{len(results)} results found.")
        return

    # Show last N extractions
    if args.last:
        reg = Registry()
        results = reg.last(args.last)
        if not results:
            print("No extractions found.")
            return
        for r in results:
            print(f"  [{r['confidence']:.2f}] {r['url']}")
            print(f"           {r['instruction']} | {r['engine']} | {r['timestamp']}")
        print(f"\nShowing last {len(results)} extractions.")
        return

    # Review mode
    if args.review:
        review_files = [f for f in os.listdir(REVIEW_DIR)
                        if f.startswith(args.review)] if os.path.exists(REVIEW_DIR) else []
        if not review_files:
            print(f"No review items for {args.review}")
            return
        for f in review_files:
            with open(os.path.join(REVIEW_DIR, f), "r") as fh:
                data = json.load(fh)
                print(f"\n⚠ {data['url']} (confidence: {data['confidence']})")
                if "diagnosis" in data:
                    print(f"  Reason: {data['diagnosis'].get('reason', 'N/A')}")
                    print(f"  Suggestion: {data['diagnosis'].get('suggestion', 'N/A')}")
        return

    # Extraction mode
    if not args.instruction:
        parser.error("--instruction is required for extraction")

    # Load schema fields if provided
    fields = None
    if args.schema:
        with open(args.schema, "r") as f:
            schema = json.load(f)
            fields = schema.get("fields", [])

    runner = BatchRunner(engine=args.engine)

    if args.url:
        # Cross-batch dedup warning
        if runner.registry.is_duplicate(args.url):
            logger.info(f"Note: {args.url} was previously extracted. Re-extracting.")

        batch_id = runner.registry.get_next_batch_id_from_db()
        batch_dir = os.path.join(RESULTS_DIR, batch_id)
        runner.registry.create_batch(batch_id, args.instruction, args.engine)
        result = runner.run_single(args.url, args.instruction, batch_id, 1, batch_dir, fields)
        print(json.dumps(result, indent=2))

    elif args.batch:
        with open(args.batch, "r") as f:
            urls = [line.strip() for line in f if line.strip()]
        runner.run_batch(urls, args.instruction, fields)

    else:
        parser.error("Provide --url or --batch")


if __name__ == "__main__":
    main()
