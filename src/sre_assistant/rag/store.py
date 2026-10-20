"""pgvector + Postgres full-text store.

Tables
  documents      one row per KB document + all metadata (filterable)
  parents        `##` sections returned to the LLM          (per collection)
  chunks         small child chunks: embedding + tsvector    (per collection)
  ingest_state   fingerprint per (collection, doc) for incremental re-ingestion
  lexeme_df      document frequency of every indexed term    (keyword query pruning)

A "collection" lets several chunking strategies (parent_child, naive) live side by side for A/B tests.
"""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import psycopg
from pgvector.psycopg import register_vector
from psycopg import sql
from psycopg.rows import dict_row

from sre_assistant.corpus.schema import Document
from sre_assistant.rag.chunking import Chunked

# Columns of `documents` that callers may filter on. Anything else is rejected: filter keys are
# turned into SQL identifiers, so an allowlist is what prevents SQL injection here.
FILTERABLE = frozenset(
    {"service", "doc_type", "environment", "infrastructure", "language", "error_type", "scenario", "severity"}
)

# Index both "sqlalchemy.exc.TimeoutError" as one token AND its parts (sqlalchemy / exc / TimeoutError).
_TSV_EXPR = "to_tsvector('english', {x} || ' ' || regexp_replace({x}, '[._/:=-]+', ' ', 'g'))"

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    doc_id         text PRIMARY KEY,
    doc_type       text NOT NULL,
    title          text NOT NULL,
    scenario       text NOT NULL,
    service        text NOT NULL,
    environment    text NOT NULL,
    language       text NOT NULL,
    infrastructure text NOT NULL,
    error_type     text NOT NULL,
    error_codes    text[] NOT NULL,
    severity       text NOT NULL,
    created        date NOT NULL,
    tags           text[] NOT NULL,
    path           text NOT NULL,
    content_hash   text NOT NULL
);
CREATE INDEX IF NOT EXISTS documents_service_idx ON documents (service);

CREATE TABLE IF NOT EXISTS parents (
    collection  text NOT NULL,
    parent_id   text NOT NULL,
    doc_id      text NOT NULL REFERENCES documents ON DELETE CASCADE,
    section     text NOT NULL,
    content     text NOT NULL,
    token_count int  NOT NULL,
    PRIMARY KEY (collection, parent_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    collection      text NOT NULL,
    chunk_id        text NOT NULL,
    parent_id       text NOT NULL,
    doc_id          text NOT NULL REFERENCES documents ON DELETE CASCADE,
    section         text NOT NULL,
    chunk_index     int  NOT NULL,
    content         text NOT NULL,
    token_count     int  NOT NULL,
    embedding       vector({dim}) NOT NULL,
    embedding_model text NOT NULL,
    tsv tsvector GENERATED ALWAYS AS ({tsv}) STORED,
    PRIMARY KEY (collection, chunk_id),
    FOREIGN KEY (collection, parent_id) REFERENCES parents ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin ON chunks USING gin (tsv);

CREATE TABLE IF NOT EXISTS ingest_state (
    collection  text NOT NULL,
    doc_id      text NOT NULL REFERENCES documents ON DELETE CASCADE,
    fingerprint text NOT NULL,
    PRIMARY KEY (collection, doc_id)
);

CREATE TABLE IF NOT EXISTS lexeme_df (
    collection text NOT NULL,
    lexeme     text NOT NULL,
    df         int  NOT NULL,
    PRIMARY KEY (collection, lexeme)
);
CREATE TABLE IF NOT EXISTS collection_stats (
    collection   text PRIMARY KEY,
    n_chunks     int NOT NULL,
    refreshed_at timestamptz NOT NULL DEFAULT now()
);
"""


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: str
    parent_id: str
    section: str
    content: str
    score: float


def content_hash(doc: Document) -> str:
    return hashlib.sha256((doc.metadata.model_dump_json() + doc.body).encode()).hexdigest()


class VectorStore:
    def __init__(self, conninfo: str, dim: int):
        self.conninfo = conninfo
        self.dim = dim
        self._conn: psycopg.Connection | None = None

    # ---------- connection & schema ----------
    @property
    def conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.conninfo, autocommit=True, row_factory=dict_row)
            self._conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            register_vector(self._conn)
            # pgvector 0.8: keep scanning the HNSW graph until enough rows pass the WHERE filters,
            # otherwise filtered ANN queries can silently return fewer than k results.
            self._conn.execute("SET hnsw.iterative_scan = strict_order")
            self._conn.execute("SET hnsw.ef_search = 100")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def init_schema(self, reset: bool = False) -> None:
        with self.conn.transaction():
            if reset:
                self.conn.execute(
                    "DROP TABLE IF EXISTS collection_stats, lexeme_df, ingest_state, chunks, parents, documents"
                )
            self.conn.execute(SCHEMA.format(dim=self.dim, tsv=_TSV_EXPR.format(x="content")))
        self.check_embedding_dim()

    def check_embedding_dim(self) -> None:
        row = self.conn.execute(
            "SELECT atttypmod AS dim FROM pg_attribute WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
        ).fetchone()
        if row and row["dim"] != self.dim:
            raise RuntimeError(
                f"chunks.embedding is vector({row['dim']}) but the embedding model produces {self.dim} dims. "
                "Different models live in different vector spaces: re-ingest with --reset (or a new collection)."
            )

    # ---------- writes ----------
    def fingerprints(self, collection: str) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT doc_id, fingerprint FROM ingest_state WHERE collection = %s", (collection,)
        ).fetchall()
        return {r["doc_id"]: r["fingerprint"] for r in rows}

    def upsert_document(self, doc: Document) -> None:
        m = doc.metadata
        self.conn.execute(
            """
            INSERT INTO documents (doc_id, doc_type, title, scenario, service, environment, language,
                                   infrastructure, error_type, error_codes, severity, created, tags, path,
                                   content_hash)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (doc_id) DO UPDATE SET
                doc_type=EXCLUDED.doc_type, title=EXCLUDED.title, scenario=EXCLUDED.scenario,
                service=EXCLUDED.service, environment=EXCLUDED.environment, language=EXCLUDED.language,
                infrastructure=EXCLUDED.infrastructure, error_type=EXCLUDED.error_type,
                error_codes=EXCLUDED.error_codes, severity=EXCLUDED.severity, created=EXCLUDED.created,
                tags=EXCLUDED.tags, path=EXCLUDED.path, content_hash=EXCLUDED.content_hash
            """,
            (
                m.doc_id, m.doc_type.value, m.title, m.scenario, m.service, m.environment.value, m.language,
                m.infrastructure.value, m.error_type, m.error_codes, m.severity, m.created, m.tags, doc.path,
                content_hash(doc),
            ),
        )  # fmt: skip

    def replace_chunks(
        self,
        collection: str,
        doc_id: str,
        chunked: Chunked,
        embeddings: Sequence[Sequence[float]],
        embedding_model: str,
        fingerprint: str,
    ) -> None:
        """Atomically swap all parents/chunks of one document in one collection."""
        if len(embeddings) != len(chunked.children):
            raise ValueError("one embedding per child chunk required")
        with self.conn.transaction():
            self.conn.execute("DELETE FROM parents WHERE collection = %s AND doc_id = %s", (collection, doc_id))
            with self.conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO parents VALUES (%s,%s,%s,%s,%s,%s)",
                    [(collection, p.parent_id, p.doc_id, p.section, p.content, p.token_count) for p in chunked.parents],
                )
                cur.executemany(
                    "INSERT INTO chunks (collection, chunk_id, parent_id, doc_id, section, chunk_index, content,"
                    " token_count, embedding, embedding_model) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s)",
                    [
                        (collection, c.chunk_id, c.parent_id, c.doc_id, c.section, c.index, c.content,
                         c.token_count, list(e), embedding_model)
                        for c, e in zip(chunked.children, embeddings, strict=True)
                    ],
                )  # fmt: skip
            self.conn.execute(
                "INSERT INTO ingest_state VALUES (%s,%s,%s) "
                "ON CONFLICT (collection, doc_id) DO UPDATE SET fingerprint = EXCLUDED.fingerprint",
                (collection, doc_id, fingerprint),
            )

    def delete_documents_not_in(self, keep_doc_ids: Iterable[str]) -> list[str]:
        rows = self.conn.execute(
            "DELETE FROM documents WHERE NOT (doc_id = ANY(%s)) RETURNING doc_id", (list(keep_doc_ids),)
        ).fetchall()
        return [r["doc_id"] for r in rows]

    def refresh_lexeme_stats(self, collection: str) -> int:
        with self.conn.transaction():
            self.conn.execute("DELETE FROM lexeme_df WHERE collection = %s", (collection,))
            self.conn.execute(
                "INSERT INTO lexeme_df (collection, lexeme, df) "
                "SELECT %s, word, ndoc FROM ts_stat(format('SELECT tsv FROM chunks WHERE collection = %%L', %s::text))",
                (collection, collection),
            )
            n = self.conn.execute("SELECT count(*) AS n FROM chunks WHERE collection = %s", (collection,)).fetchone()
            self.conn.execute(
                "INSERT INTO collection_stats (collection, n_chunks) VALUES (%s, %s) ON CONFLICT (collection) "
                "DO UPDATE SET n_chunks = EXCLUDED.n_chunks, refreshed_at = now()",
                (collection, n["n"]),
            )
        return n["n"]

    # ---------- reads ----------
    @staticmethod
    def _filter_sql(filters: dict[str, str | list[str]] | None) -> tuple[sql.Composable, list]:
        if not filters:
            return sql.SQL(""), []
        unknown = set(filters) - FILTERABLE
        if unknown:
            raise ValueError(f"unsupported filter(s): {sorted(unknown)}; allowed: {sorted(FILTERABLE)}")
        clauses, params = [], []
        for key, value in sorted(filters.items()):
            clauses.append(sql.SQL(" AND d.{} = ANY(%s)").format(sql.Identifier(key)))
            params.append([value] if isinstance(value, str) else list(value))
        return sql.Composed(clauses), params

    def vector_search(
        self, collection: str, query_vec: Sequence[float], k: int, filters: dict | None = None
    ) -> list[ChunkHit]:
        where, fparams = self._filter_sql(filters)
        q = sql.SQL(
            "SELECT c.chunk_id, c.parent_id, c.section, c.content, 1 - (c.embedding <=> %s::vector) AS score "
            "FROM chunks c JOIN documents d USING (doc_id) WHERE c.collection = %s{} "
            "ORDER BY c.embedding <=> %s::vector LIMIT %s"
        ).format(where)
        vec = list(query_vec)
        rows = self.conn.execute(q, [vec, collection, *fparams, vec, k]).fetchall()
        return [ChunkHit(r["chunk_id"], r["parent_id"], r["section"], r["content"], float(r["score"])) for r in rows]

    def keyword_search(
        self, collection: str, text: str, k: int, max_df: float, filters: dict | None = None
    ) -> list[ChunkHit]:
        """Full-text search with an OR query of the *informative* query terms.

        Postgres ts_rank has no IDF (unlike BM25), so generic terms such as "error", "level" or "service"
        would dominate. We approximate IDF by dropping terms that occur in more than `max_df` of chunks and
        keeping the rarest 32 terms of the query.
        """
        where, fparams = self._filter_sql(filters)
        q = sql.SQL(
            """
            WITH terms AS (
                SELECT t.lexeme
                FROM unnest(tsvector_to_array({tsv_query})) AS t(lexeme)
                JOIN lexeme_df s ON s.collection = %s AND s.lexeme = t.lexeme
                WHERE s.df <= %s * (SELECT n_chunks FROM collection_stats WHERE collection = %s)
                ORDER BY s.df, t.lexeme
                LIMIT 32
            ), q AS (
                SELECT to_tsquery('simple', string_agg(quote_literal(lexeme), ' | ')) AS query FROM terms
            )
            SELECT c.chunk_id, c.parent_id, c.section, c.content, ts_rank_cd(c.tsv, q.query, 32) AS score
            FROM chunks c JOIN documents d USING (doc_id), q
            WHERE c.collection = %s AND q.query IS NOT NULL AND c.tsv @@ q.query{where}
            ORDER BY score DESC, c.chunk_id
            LIMIT %s
            """
        ).format(tsv_query=sql.SQL(_TSV_EXPR.format(x="%s::text")), where=where)
        params = [text, text, collection, max_df, collection, collection, *fparams, k]
        rows = self.conn.execute(q, params).fetchall()
        return [ChunkHit(r["chunk_id"], r["parent_id"], r["section"], r["content"], float(r["score"])) for r in rows]

    def get_parents(self, collection: str, parent_ids: Sequence[str]) -> dict[str, dict]:
        rows = self.conn.execute(
            """
            SELECT p.parent_id, p.section, p.content, p.token_count, d.*
            FROM parents p JOIN documents d USING (doc_id)
            WHERE p.collection = %s AND p.parent_id = ANY(%s)
            """,
            (collection, list(parent_ids)),
        ).fetchall()
        return {r["parent_id"]: r for r in rows}

    def get_document_tokens(self, collection: str, doc_ids: Sequence[str]) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT doc_id, sum(token_count) AS n FROM parents WHERE collection = %s AND doc_id = ANY(%s) "
            "GROUP BY doc_id",
            (collection, list(doc_ids)),
        ).fetchall()
        return {r["doc_id"]: int(r["n"]) for r in rows}

    def stats(self) -> list[dict]:
        return self.conn.execute(
            """
            SELECT c.collection, count(DISTINCT c.doc_id) AS docs, count(DISTINCT c.parent_id) AS parents,
                   count(*) AS chunks, round(avg(c.token_count))::int AS avg_chunk_tokens,
                   min(c.embedding_model) AS embedding_model
            FROM chunks c GROUP BY c.collection ORDER BY c.collection
            """
        ).fetchall()
