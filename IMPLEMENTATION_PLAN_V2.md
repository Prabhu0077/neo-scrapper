# NeoScrapper V2.1: Project Blueprint

> Finalized blueprint reflecting the current state of the engine as of 2026-03-20.
> All phases through V2.1 hardening are complete and verified.

---

## Goal

A general-purpose, instruction-driven data extraction engine. Pass any URL, any instruction, get structured or free-form output. Never stops on failure. All results persist in JSON files AND a SQLite index.

---

## Resource Budget

| Resource | Daily Capacity | NeoScrapper Allocation |
|---|---|---|
| Gemini 2.5 Flash API | ~1,500 RPD | **500 req/day** (budget-guarded) |
| Jina Reader API | ~33,000 pages/day | **Primary fetcher** (free, 500 RPM) |
| Local Qwen2.5-3B | Unlimited | Fallback when offline/quota-exhausted |
| Crawl4AI | Unlimited (local) | SPA fallback |

---

## 3-Tier Intelligence Architecture

```
┌──────────────────────────────────────────────────────────┐
│  TIER 1: CODE (Zero Cost, Unlimited)                     │
│  Jina Reader fetching, regex discovery, token budgeting, │
│  confidence scoring, JSON validation, SQLite indexing    │
├──────────────────────────────────────────────────────────┤
│  TIER 2: LOCAL LLM — Qwen2.5-3B via Ollama              │
│  Offline fallback, pre-classification (hybrid mode)      │
│  ~87s/URL on CPU, ~15s with GPU offload                  │
├──────────────────────────────────────────────────────────┤
│  TIER 3: GEMINI 2.5 FLASH (Direct API, 500 req/day)     │
│  Primary cloud extraction. 10-14s/URL, confidence 1.0.   │
│  Auto-retry on 429 with retryDelay parsing (3 attempts). │
└──────────────────────────────────────────────────────────┘
```

---

## Implemented Components (by Phase)

### Phase 1: Fetcher — DONE ✅

[neoscrapper.py](file:///d:/P-Projects/neo_scrapper/neoscrapper.py) — `Fetcher` class (L221–L351)

```
URL arrives
  ├─ Try 1: Jina Reader API (r.jina.ai/{url})
  │         Returns clean markdown. 500+ chars = success.
  ├─ Try 2: Crawl4AI (local headless browser)
  │         Handles JavaScript SPAs.
  ├─ Try 3: curl.exe fallback
  │         Raw HTML with UA rotation (10 agents).
  └─ Tag result with fetch_method: "jina" | "crawl4ai" | "curl"
```

- Exponential backoff: 2s → 4s (max 2 retries) on 5xx
- HTTP 403/429: tagged `blocked`, routed to diagnosis

---

### Phase 2: Scrubber — DONE ✅

`Scrubber` class (L357–L386)

- **Jina/Crawl4AI path:** Token budget only (30K cloud, 6K local)
- **curl path:** Strip `<script>`, `<style>`, `<nav>`, `<header>`, `<footer>`, collapse whitespace, then budget

---

### Phase 3: Extractor — DONE ✅ (V2.1: Generalized)

`Extractor` class (L452–L635)

**Dual-mode prompt compiler:**
- **Structured mode** (when `--schema` or fields provided): enforces JSON, validates with `parse_json()`, retries with stricter prompt
- **General mode** (no schema): LLM decides output format based on instruction. Free-text wrapped in `{"response": "..."}` if not JSON.

**Engine router:**
- `--engine cloud` → Direct Gemini 2.5 Flash API with retry on 429
- `--engine local` → Ollama HTTP API (qwen2.5:3b)
- `--engine hybrid` → Local pre-classifies, cloud extracts if relevant

**Cloud API integration:**
- Direct HTTP to `generativelanguage.googleapis.com` (bypasses Windows CLI 8k limit)
- 3-attempt retry on HTTP 429 with `retryDelay` parsing from error response
- CLI subprocess fallback (`gemini.cmd`) if API key absent

---

### Phase 4: Scorer & Diagnosis — DONE ✅

`Scorer` class (L390–L450)

```
confidence = (completeness × 0.5) + (content_quality × 0.3) + (format × 0.2)
needs_review = confidence < 0.6
```

Diagnosis engine auto-triggers with structured reason/suggestion.

---

### Phase 5: SQLite Registry + File Storage — DONE ✅

`Registry` class (L73–L227) + `Storage` class (L639–L684)

**Schema:**
```sql
batches     (batch_id PK, created_at, instruction, engine, stats...)
extractions (id AUTOINCREMENT, batch_id FK, url, confidence, data_json, file_path...)
budget_log  (date PK, gemini_cli, jina_calls, local_calls)
```

- **Dual-write:** JSON file + SQLite row for every extraction
- **batch_id from SQLite** (V2.1 fix): `MAX(CAST(SUBSTR(batch_id,7) AS INTEGER))` — no filesystem/DB desync
- **INSERT OR REPLACE** on batches: handles orphaned entries from crashed runs
- **Cross-batch dedup:** `is_duplicate()` checks all extractions, warns in single-URL mode

---

### Phase 6: Orchestrator — DONE ✅

`BatchRunner` class (L688–L890)

- Sequential processing with 3s rate limiter
- **Never-stop logic:** catches all exceptions, logs diagnosis, moves to next URL
- **Pre-flight budget display:** `[Pre-flight] 6 URLs | Budget: 2/500 used today | Est. API calls: ~9`
- **Budget guard:** auto-fallback to local at 500 Gemini calls/day
- **CPU thermal guard:** every 10 URLs, checks via psutil/WMI, pauses 60s if > 85°C

---

### Phase 7: Schemas — DONE ✅

[schemas/](file:///d:/P-Projects/neo_scrapper/schemas/)

| File | Fields |
|---|---|
| `job_hunt.json` | company, tech_stack, open_roles, salary_range, company_size, funding |
| `lead_gen.json` | company_name, industry, size, location, key_people, contact_email |
| `product_intel.json` | product_name, price, rating, review_count, seller, availability |

---

### Phase 8: CLI Interface — DONE ✅ (V2.1: Upgraded)

```
--url URL              Single URL extraction
--batch FILE           Batch file (one URL per line)
--instruction TEXT     What to extract or analyze (plain English)
--engine {local,cloud,hybrid}  Default: cloud
--schema FILE          Schema JSON for structured extraction
--search KEYWORD       Search past extractions (safe, parameterized)
--last N               Show last N extractions
--budget               Show today's API budget usage
--review BATCH_ID      Review low-confidence items
```

---

## File Structure

```
d:\P-Projects\neo_scrapper\
  neoscrapper.py          ← Core engine (1018 lines, single file)
  neoscrapper.db          ← SQLite registry
  .env                    ← API keys (JINA_API_KEY, GEMINI_API_KEY)
  README.md               ← Project documentation
  schemas/                ← Pre-built extraction schemas
  results/
    batch_001/            ← JSON extraction results
      001_truefoundry_com.json
      _manifest.json
    _review/              ← Low-confidence items
  _reference/             ← V1 code preserved for reference
```

---

## Dependencies

| Package | Purpose | Install |
|---|---|---|
| `requests` | Jina API + Gemini API | `pip install requests` |
| `ollama` | Local LLM (Qwen 3B) | `pip install ollama` |
| `crawl4ai` | SPA fallback | `pip install crawl4ai` |
| `python-dotenv` | `.env` credential loading | `pip install python-dotenv` |
| `psutil` | CPU thermal monitoring | `pip install psutil` |
| `sqlite3` | Registry/index | **Built-in** |
| Ollama runtime | Model host | `winget install ollama` |
| Qwen2.5-3B | Local model | `ollama pull qwen2.5:3b` |

---

## Verified Test Results

| Test | Mode | Latency | Confidence | Engine |
|---|---|---|---|---|
| TrueFoundry (tech stack) | Cloud | **10.9s** | 1.0 | Gemini 2.5 Flash |
| GitHub (summarize) | General | **6.85s** | 1.0 | Gemini 2.5 Flash |
| TrueFoundry (lead_gen schema) | Structured | **13.7s** | 0.75 | Gemini 2.5 Flash |
| TrueFoundry (local) | Structured | 87s | 1.0 | Qwen 3B (CPU) |

---

## Future Scope (V3 Roadmap)

### 1. Headless SPA Interception
- **Goal:** Bypass limitations of static HTML scraping for client-side rendered targets (Next.js/React apps).
- **Approach:** Integrate a lightweight headless C++ engine or intercept raw `/api/v1` data streams to capture dynamically rendered content.

### 2. Zero-Blocking I/O (Concurrency)
- **Goal:** Scrape 10–50 pages simultaneously, reducing per-URL latency from ~10s to <2s.
- **Approach:** Re-architect the network layer with async execution or parallelized curl threads.

### 3. WAF Evading "Header Masquerading"
- **Goal:** Guarantee consistent ingress past enterprise WAFs (Cloudflare, Imperva).
- **Approach:** Dynamically inject rotating User-Agents and TLS fingerprints to match standard consumer browsers.

### 4. Recursive Pagination Engine
- **Goal:** Break through depth limits to ingest entire enterprise boards (e.g., all 300 open roles at NVIDIA).
- **Approach:** Autonomous loop identifying `href` tags with pagination data (`?page=2`, `offset=50`), recursively fetching until a token threshold is met.

### 5. Local State Caching
- **Goal:** Eliminate redundant network I/O and LLM inference cycles.
- **Approach:** Hash the target domain before fetching. If a hash exists within a 24-hour decay window in `.knowledge/cache/`, return the cached JSON via memory-mapped I/O.

---

> **Publicity Note (Dual-Core Architect Narrative):**
> When pitching to VP of Engineering / CTOs, NeoScrapper demonstrates real IP:
> *"I architected NeoScrapper as a subsystem for NeoGravity — an agentic CLI tool that autonomously hunts across dynamic ATS systems, scrubs React bloat offline, and outputs strictly typed JSON without ever leaving the local control plane."*

