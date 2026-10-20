-- Approximate nearest-neighbour index for vector search.
-- Cosine distance, matching the <=> operator in app/rag/search.py.
--
-- With RLS, the index finds the nearest chunks across ALL rows first, and the
-- tenant/role policies filter them afterwards. An employee can then get fewer
-- than k results. search.py turns on pgvector's iterative index scans so the
-- index keeps going until it finds enough rows the user is allowed to see.
CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
