# NeoScrapper V2.1 — The Sovereign Data Harvester

General-purpose, instruction-driven extraction engine. Fetch any web page, extract any data, structured or free-form.

**Author:** Prabhakar Chandra (Dual-Core Architect)

## Quick Start

```powershell
# Install dependencies
pip install requests ollama crawl4ai python-dotenv
ollama pull qwen2.5:3b

# Set API keys in .env
# JINA_API_KEY=your_key
# GEMINI_API_KEY=your_key

# Extract data from any URL
python neoscrapper.py --url "https://truefoundry.com" --instruction "Get company name and tech stack" --engine cloud
```

## Usage Modes

### General Mode (any instruction, free-form output)
```powershell
python neoscrapper.py --url "https://github.com/about" --instruction "Summarize this company"
```

### Structured Mode (with schema, guaranteed JSON fields)
```powershell
python neoscrapper.py --url "https://example.com" --instruction "Get details" --schema schemas/lead_gen.json
```

### Batch Mode (multiple URLs)
```powershell
python neoscrapper.py --batch urls.txt --instruction "Get tech stack" --engine cloud
```

### Query History
```powershell
python neoscrapper.py --search github        # Search past extractions by keyword
python neoscrapper.py --last 10              # Show last 10 extractions
python neoscrapper.py --budget               # Show today's API budget usage
python neoscrapper.py --review batch_013     # Review low-confidence items
```

## Architecture

```
URL → Fetcher (Jina → Crawl4AI → curl) → Scrubber → Extractor → Scorer → Storage
                                                        ↓
                                              Cloud (Gemini 2.5 Flash)
                                              Local (Qwen 3B via Ollama)
                                              Hybrid (local pre-filter → cloud)
```

| Component | Role |
|---|---|
| **Fetcher** | Cascading: Jina Reader API → Crawl4AI (SPA) → curl (fallback) |
| **Scrubber** | Token budget enforcement (30K cloud, 6K local), HTML cleanup |
| **Extractor** | Prompt compiler + LLM routing. General mode (free-text) or structured (JSON) |
| **Scorer** | Confidence scoring (completeness × 0.5 + content × 0.3 + format × 0.2) |
| **Registry** | SQLite index: dedup, budget tracking, search, history |
| **Storage** | Dual-write: JSON files (`results/`) + SQLite (`neoscrapper.db`) |

## Pre-Built Schemas

| Schema | Fields |
|---|---|
| `schemas/job_hunt.json` | company, tech_stack, open_roles, salary_range, company_size, funding |
| `schemas/lead_gen.json` | company_name, industry, size, location, key_people, contact_email |
| `schemas/product_intel.json` | product_name, price, rating, review_count, seller, availability |

## Safety Features

- **Budget Guard:** Auto-fallback to local engine at 500 Gemini calls/day
- **API Retry:** 3 attempts on 429 with `retryDelay` parsing
- **CPU Thermal Guard:** Pauses 60s if CPU > 85°C (every 10 URLs)
- **Cross-batch Dedup:** Warns on re-extraction of previously scraped URLs
- **Never-stop:** Batch mode logs errors and continues, never crashes mid-batch

## Dependencies

| Package | Purpose |
|---|---|
| `requests` | Jina API + Gemini API |
| `ollama` | Local LLM (Qwen 3B) |
| `crawl4ai` | SPA/JavaScript page fallback |
| `python-dotenv` | `.env` credential loading |
| `psutil` | CPU thermal monitoring |
