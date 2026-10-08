-- Extraction attempts vs. invalid model outputs (docs/contracts/100 §3: an unavailable LLM — no key,
-- 402 credits, timeout — keeps an item pending_extraction and retries it later; only *invalid
-- outputs* may eventually fail it). `attempts` keeps counting every try (it drives the backoff);
-- `bad_outputs` counts only invalid model answers and is what SIGNALS_EXTRACTION_MAX_ATTEMPTS caps.
ALTER TABLE news_item ADD COLUMN IF NOT EXISTS bad_outputs integer NOT NULL DEFAULT 0;
