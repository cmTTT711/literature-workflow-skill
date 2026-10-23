PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS pdf_names(
 folder TEXT NOT NULL, paper_id TEXT NOT NULL,
 filename TEXT NOT NULL COLLATE NOCASE,
 PRIMARY KEY(folder,paper_id), UNIQUE(folder,filename)
);
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS search_runs(
 run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
 profile_json TEXT NOT NULL, status TEXT NOT NULL, report_json TEXT
);
CREATE TABLE IF NOT EXISTS provider_queries(
 query_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES search_runs,
 provider TEXT NOT NULL, user_query TEXT NOT NULL, executed_json TEXT,
 started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
 total INTEGER, records INTEGER NOT NULL DEFAULT 0, error TEXT, warnings_json TEXT
);
CREATE TABLE IF NOT EXISTS raw_pages(
 page_id TEXT PRIMARY KEY, query_id TEXT NOT NULL REFERENCES provider_queries,
 path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS papers(paper_id TEXT PRIMARY KEY, doi TEXT, paper_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS papers_doi ON papers(doi);
CREATE TABLE IF NOT EXISTS source_records(
 record_id TEXT PRIMARY KEY, query_id TEXT NOT NULL REFERENCES provider_queries,
 provider TEXT NOT NULL, raw_path TEXT NOT NULL, ordinal INTEGER NOT NULL,
 payload_json TEXT NOT NULL, paper_id TEXT REFERENCES papers, normalization_error TEXT
);
CREATE INDEX IF NOT EXISTS source_paper ON source_records(paper_id);
CREATE TABLE IF NOT EXISTS query_progress(
 query_id TEXT PRIMARY KEY REFERENCES provider_queries,
 cursor_json TEXT, page_size INTEGER NOT NULL, received INTEGER NOT NULL,
 updated_at TEXT NOT NULL, state TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_version VALUES(2);
CREATE TABLE IF NOT EXISTS resolution_runs(
 run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, report_json TEXT
);
CREATE TABLE IF NOT EXISTS resolver_evidence(
 evidence_id TEXT PRIMARY KEY, source TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
 sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access_resolution(
 paper_id TEXT PRIMARY KEY REFERENCES papers ON DELETE CASCADE,
 run_id TEXT REFERENCES resolution_runs, status TEXT NOT NULL,
 updated_at TEXT NOT NULL, result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access_candidates(
 candidate_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers ON DELETE CASCADE,
 priority INTEGER NOT NULL, eligible INTEGER NOT NULL, candidate_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS candidate_paper ON access_candidates(paper_id,priority);
CREATE TABLE IF NOT EXISTS acquisition_attempts(
 attempt_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers ON DELETE CASCADE,
 candidate_id TEXT NOT NULL REFERENCES access_candidates ON DELETE CASCADE,
 started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
 staging_path TEXT NOT NULL, result_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS attempt_paper ON acquisition_attempts(paper_id);
CREATE TABLE IF NOT EXISTS artifacts(
 artifact_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers ON DELETE CASCADE,
 attempt_id TEXT NOT NULL REFERENCES acquisition_attempts ON DELETE CASCADE,
 channel TEXT NOT NULL CHECK(channel IN ('program','agent')),
 status TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL, verification_json TEXT NOT NULL,
 UNIQUE(paper_id,sha256)
);
CREATE INDEX IF NOT EXISTS artifact_hash ON artifacts(sha256);
CREATE TABLE IF NOT EXISTS acquisition_events(
 event_id TEXT PRIMARY KEY, paper_id TEXT NOT NULL REFERENCES papers ON DELETE CASCADE,
 status TEXT NOT NULL, occurred_at TEXT NOT NULL, detail_json TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_version VALUES(3);
CREATE TABLE IF NOT EXISTS master_records(
 doi TEXT PRIMARY KEY, paper_id TEXT NOT NULL UNIQUE REFERENCES papers ON DELETE CASCADE,
 status TEXT NOT NULL CHECK(status IN ('PROGRAM_VERIFIED','AGENT_REQUIRED','REVIEW')),
 reason_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS master_exclusions(
 paper_id TEXT PRIMARY KEY REFERENCES papers ON DELETE CASCADE, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doi_metadata(
 doi TEXT PRIMARY KEY, payload_json TEXT, error_code TEXT, evidence_id TEXT
);
