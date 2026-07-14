CREATE DATABASE hindsight_canary_20b;
CREATE DATABASE hindsight_canary_120b;

\connect hindsight_canary_20b
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

\connect hindsight_canary_120b
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
