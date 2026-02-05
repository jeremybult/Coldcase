from __future__ import annotations

import hashlib
import math
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import requests
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from pgvector.sqlalchemy import Vector

# ============================================================
# CONFIG
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://fcix:fcix@localhost:5432/fcix")
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))
NAMUS_BASE = os.getenv("NAMUS_BASE", "https://www.namus.gov")
NAMUS_RPS = float(os.getenv("NAMUS_RPS", "0.3"))
USER_AGENT = os.getenv("FCIX_UA", "FCIX/0.1 (contact: your-email@example.com)")

NAMUS_SEARCH_ENDPOINT = os.getenv(
    "NAMUS_SEARCH_ENDPOINT",
    "/api/public/search",
)
NAMUS_CASE_ENDPOINT = os.getenv(
    "NAMUS_CASE_ENDPOINT",
    "/api/public/case/{case_type}/{source_case_id}",
)
NAMUS_DOC_LIST_ENDPOINT = os.getenv(
    "NAMUS_DOC_LIST_ENDPOINT",
    "/api/public/case/{case_type}/{source_case_id}/documents",
)
NAMUS_DOC_DOWNLOAD_ENDPOINT = os.getenv(
    "NAMUS_DOC_DOWNLOAD_ENDPOINT",
    "/api/public/case/{case_type}/{source_case_id}/documents/{document_id}",
)

GEDMATCH_API_BASE = os.getenv("GEDMATCH_API_BASE")
FTDNA_API_BASE = os.getenv("FTDNA_API_BASE")
DNA_JUSTICE_API_BASE = os.getenv("DNA_JUSTICE_API_BASE")

WEB_SEARCH_PROVIDER = os.getenv("WEB_SEARCH_PROVIDER", "")
WEB_SEARCH_API_KEY = os.getenv("WEB_SEARCH_API_KEY", "")
WEB_SEARCH_API_BASE = os.getenv("WEB_SEARCH_API_BASE", "")


# ============================================================
# DB SETUP
# ============================================================

from sqlalchemy import create_engine

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ============================================================
# MODELS
# ============================================================

import enum


class CaseType(str, enum.Enum):
    MP = "MP"
    UHR = "UHR"


class CaseStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    IDENTIFIED = "identified"


class Visibility(str, enum.Enum):
    PUBLIC = "public"
    ORG_PRIVATE = "org_private"


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Case(Base):
    __tablename__ = "cases"

    case_uuid: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_case_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    case_type: Mapped[CaseType] = mapped_column(SAEnum(CaseType, name="case_type"), nullable=False)
    status: Mapped[CaseStatus] = mapped_column(
        SAEnum(CaseStatus, name="case_status"), nullable=False, default=CaseStatus.OPEN
    )
    visibility: Mapped[Visibility] = mapped_column(
        SAEnum(Visibility, name="visibility"), nullable=False, default=Visibility.PUBLIC
    )

    title: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    sex: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    age_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    age_max: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stature_min_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    stature_max_cm: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    last_seen_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    found_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    lat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lon: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    case_embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(EMBED_DIM), nullable=True)

    documents: Mapped[List["Document"]] = relationship(back_populates="case", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("source_system", "source_case_id", name="uq_cases_source"),
        Index("ix_cases_type_status", "case_type", "status"),
        Index("ix_cases_filters", "sex", "age_min", "age_max"),
    )


class Document(Base):
    __tablename__ = "documents"

    document_uuid: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    case_uuid: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cases.case_uuid", ondelete="CASCADE"), nullable=False
    )

    doc_type: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    source_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    raw_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    case: Mapped["Case"] = relationship(back_populates="documents")
    chunks: Mapped[List["Chunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_docs_case", "case_uuid"),
        UniqueConstraint("sha256", name="uq_documents_sha256"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    chunk_uuid: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    document_uuid: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("documents.document_uuid", ondelete="CASCADE"), nullable=False
    )
    case_uuid: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("cases.case_uuid", ondelete="CASCADE"), nullable=False
    )

    page_start: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    page_end: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    keyword_tokens: Mapped[Optional[Any]] = mapped_column(TSVECTOR, nullable=True)

    embedding: Mapped[Optional[List[float]]] = mapped_column(Vector(EMBED_DIM), nullable=True)
    extraction_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    quality_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    document: Mapped["Document"] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("ix_chunks_case", "case_uuid"),
        Index("ix_chunks_doc", "document_uuid"),
        Index("ix_chunks_tsv", "keyword_tokens", postgresql_using="gin"),
    )


class MatchRun(Base):
    __tablename__ = "match_runs"

    run_uuid: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    config_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    candidates: Mapped[List["MatchCandidate"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class MatchCandidate(Base):
    __tablename__ = "match_candidates"

    candidate_uuid: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    run_uuid: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("match_runs.run_uuid", ondelete="CASCADE"), nullable=False
    )

    case_uuid_left: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    case_uuid_right: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)

    hard_filter_pass: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    score_total: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    score_breakdown_json: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    top_evidence: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)

    run: Mapped["MatchRun"] = relationship(back_populates="candidates")

    __table_args__ = (
        UniqueConstraint("run_uuid", "case_uuid_left", "case_uuid_right", name="uq_match_pair"),
        Index("ix_match_score", "score_total"),
    )


# ============================================================
# PGVECTOR EXTENSION + INDEXES
# ============================================================


def init_db(db: Session) -> None:
    Base.metadata.create_all(bind=engine)

    db.execute(sql_text("CREATE EXTENSION IF NOT EXISTS vector;"))

    db.execute(
        sql_text(
            """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = 'ix_chunks_embedding_hnsw'
            ) THEN
                CREATE INDEX ix_chunks_embedding_hnsw
                ON chunks USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64);
            END IF;
        END $$;
    """
        )
    )

    db.execute(
        sql_text(
            """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = 'ix_cases_case_embedding_hnsw'
            ) THEN
                CREATE INDEX ix_cases_case_embedding_hnsw
                ON cases USING hnsw (case_embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64);
            END IF;
        END $$;
    """
        )
    )

    db.commit()


def cosine_similarity_query_chunks(
    db: Session,
    query_vec: List[float],
    case_type: Optional[CaseType] = None,
    limit: int = 20,
) -> List[dict]:
    if len(query_vec) != EMBED_DIM:
        raise ValueError(f"query_vec dim {len(query_vec)} != EMBED_DIM {EMBED_DIM}")

    stmt = (
        select(
            Chunk.chunk_uuid,
            Chunk.case_uuid,
            Chunk.document_uuid,
            Chunk.page_start,
            Chunk.page_end,
            Chunk.text,
            (Chunk.embedding.cosine_distance(query_vec)).label("distance"),
        )
        .where(Chunk.embedding.is_not(None))
        .order_by(sql_text("distance ASC"))
        .limit(limit)
    )

    if case_type is not None:
        stmt = stmt.join(Case, Case.case_uuid == Chunk.case_uuid).where(Case.case_type == case_type)

    rows = db.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


def cosine_similarity_query_cases(
    db: Session,
    query_vec: List[float],
    case_type: Optional[CaseType] = None,
    limit: int = 20,
) -> List[dict]:
    if len(query_vec) != EMBED_DIM:
        raise ValueError(f"query_vec dim {len(query_vec)} != EMBED_DIM {EMBED_DIM}")

    stmt = (
        select(
            Case.case_uuid,
            Case.case_type,
            Case.title,
            Case.source_system,
            Case.source_case_id,
            (Case.case_embedding.cosine_distance(query_vec)).label("distance"),
        )
        .where(Case.case_embedding.is_not(None))
        .order_by(sql_text("distance ASC"))
        .limit(limit)
    )
    if case_type is not None:
        stmt = stmt.where(Case.case_type == case_type)

    rows = db.execute(stmt).mappings().all()
    return [dict(r) for r in rows]


# ============================================================
# SIMPLE TEXT CHUNKING + DUMMY EMBEDDINGS
# ============================================================


def chunk_text(text: str, max_chars: int = 2500) -> List[str]:
    t = " ".join((text or "").split()).strip()
    if not t:
        return []
    out = []
    i = 0
    while i < len(t):
        out.append(t[i : i + max_chars])
        i += max_chars
    return out


def dummy_embed(texts: List[str]) -> List[List[float]]:
    vecs: List[List[float]] = []
    for s in texts:
        h = hashlib.sha256(s.encode("utf-8")).digest()
        v = []
        for i in range(EMBED_DIM):
            b = h[i % len(h)]
            v.append((b / 255.0) * 2.0 - 1.0)
        vecs.append(v)
    return vecs


def update_case_embedding(db: Session, case_uuid: uuid.UUID) -> None:
    rows = db.execute(
        select(Chunk.embedding).where(and_(Chunk.case_uuid == case_uuid, Chunk.embedding.is_not(None)))
    ).all()
    if not rows:
        return

    acc = [0.0] * EMBED_DIM
    n = 0
    for (emb,) in rows:
        if emb is None:
            continue
        n += 1
        for i in range(EMBED_DIM):
            acc[i] += float(emb[i])
    if n == 0:
        return
    avg = [x / n for x in acc]
    db.execute(
        sql_text("UPDATE cases SET case_embedding = :v WHERE case_uuid = :id"),
        {"v": avg, "id": str(case_uuid)},
    )


# ============================================================
# CONNECTORS
# ============================================================


@dataclass
class NamUsConnector:
    base: str = NAMUS_BASE
    rps: float = NAMUS_RPS
    session: requests.Session = requests.Session()

    def _sleep(self) -> None:
        if self.rps <= 0:
            return
        time.sleep(1.0 / self.rps)

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        self._sleep()
        url = self.base.rstrip("/") + "/" + path.lstrip("/")
        resp = self.session.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30,
        )
        if resp.status_code == 404:
            raise HTTPException(status_code=502, detail=f"NamUs endpoint not found: {url}")
        if resp.status_code in (401, 403):
            raise HTTPException(status_code=403, detail="Access denied by source. Use agency upload or approved access.")
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"NamUs error {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except Exception:
            return {"raw_text": resp.text}

    def search(self, case_type: CaseType, query: str, limit: int = 20) -> Any:
        params = {"q": query, "type": case_type.value, "limit": limit}
        return self._get(NAMUS_SEARCH_ENDPOINT, params=params)

    def fetch_case(self, case_type: CaseType, source_case_id: str) -> Any:
        path = NAMUS_CASE_ENDPOINT.format(case_type=case_type.value, source_case_id=source_case_id)
        return self._get(path)

    def list_documents(self, case_type: CaseType, source_case_id: str) -> Any:
        path = NAMUS_DOC_LIST_ENDPOINT.format(case_type=case_type.value, source_case_id=source_case_id)
        return self._get(path)

    def download_document(self, case_type: CaseType, source_case_id: str, document_id: str) -> Any:
        path = NAMUS_DOC_DOWNLOAD_ENDPOINT.format(
            case_type=case_type.value, source_case_id=source_case_id, document_id=document_id
        )
        return self._get(path)


@dataclass
class ExternalSearchConnector:
    base_url: Optional[str]
    source_name: str
    session: requests.Session = requests.Session()

    def search(self, query: str, filters: Dict[str, Any]) -> Dict[str, Any]:
        if not self.base_url:
            raise HTTPException(
                status_code=501,
                detail=f"{self.source_name} API base is not configured. Provide {self.source_name.upper()}_API_BASE.",
            )
        resp = self.session.get(
            self.base_url.rstrip("/") + "/search",
            params={"q": query, **filters},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=30,
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"{self.source_name} error {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except Exception:
            return {"raw_text": resp.text}


class WebSearchProvider:
    def __init__(self) -> None:
        self.provider = WEB_SEARCH_PROVIDER.lower().strip()
        self.api_key = WEB_SEARCH_API_KEY
        self.api_base = WEB_SEARCH_API_BASE

    def enabled(self) -> bool:
        return bool(self.provider and self.api_key and self.api_base)

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        if not self.enabled():
            raise HTTPException(
                status_code=501,
                detail="Web search provider is not configured. Set WEB_SEARCH_PROVIDER, WEB_SEARCH_API_BASE, WEB_SEARCH_API_KEY.",
            )
        params = {"q": query, "count": limit, "limit": limit}
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json", "Authorization": f"Bearer {self.api_key}"}
        resp = requests.get(self.api_base, params=params, headers=headers, timeout=30)
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=f"Web search error {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except Exception:
            return {"raw_text": resp.text}


# ============================================================
# INGEST HELPERS
# ============================================================


def upsert_case_from_payload(db: Session, payload: dict, case_type: CaseType, source_system: str = "namus") -> Case:
    source_case_id = str(payload.get("id") or payload.get("source_case_id") or payload.get("case_id") or "")
    if not source_case_id:
        raise HTTPException(status_code=400, detail="Case payload missing id/source_case_id")

    existing = db.execute(
        select(Case).where(and_(Case.source_system == source_system, Case.source_case_id == source_case_id))
    ).scalar_one_or_none()

    def parse_date(val: Any) -> Optional[date]:
        if not val:
            return None
        if isinstance(val, date):
            return val
        s = str(val).strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(s, fmt).date()
            except Exception:
                continue
        return None

    if existing is None:
        c = Case(
            source_system=source_system,
            source_case_id=source_case_id,
            case_type=case_type,
            status=CaseStatus.OPEN,
            visibility=Visibility.PUBLIC,
            title=payload.get("title"),
            sex=payload.get("sex"),
            age_min=payload.get("age_min"),
            age_max=payload.get("age_max"),
            stature_min_cm=payload.get("stature_min_cm"),
            stature_max_cm=payload.get("stature_max_cm"),
            last_seen_date=parse_date(payload.get("last_seen_date")),
            found_date=parse_date(payload.get("found_date")),
            lat=payload.get("lat"),
            lon=payload.get("lon"),
        )
        db.add(c)
        db.flush()
        return c

    existing.title = payload.get("title") or existing.title
    existing.sex = payload.get("sex") or existing.sex
    existing.age_min = payload.get("age_min") if payload.get("age_min") is not None else existing.age_min
    existing.age_max = payload.get("age_max") if payload.get("age_max") is not None else existing.age_max
    existing.stature_min_cm = (
        payload.get("stature_min_cm") if payload.get("stature_min_cm") is not None else existing.stature_min_cm
    )
    existing.stature_max_cm = (
        payload.get("stature_max_cm") if payload.get("stature_max_cm") is not None else existing.stature_max_cm
    )
    existing.lat = payload.get("lat") if payload.get("lat") is not None else existing.lat
    existing.lon = payload.get("lon") if payload.get("lon") is not None else existing.lon

    ls = payload.get("last_seen_date")
    fd = payload.get("found_date")
    if ls:
        existing.last_seen_date = parse_date(ls) or existing.last_seen_date
    if fd:
        existing.found_date = parse_date(fd) or existing.found_date

    return existing


def ingest_document_text(db: Session, case: Case, doc_type: str, source_url: Optional[str], text_body: str) -> Document:
    sha = hashlib.sha256((text_body or "").encode("utf-8")).hexdigest()

    existing = db.execute(select(Document).where(Document.sha256 == sha)).scalar_one_or_none()
    if existing is not None:
        return existing

    d = Document(case_uuid=case.case_uuid, doc_type=doc_type, source_url=source_url, sha256=sha, raw_text=text_body)
    db.add(d)
    db.flush()

    chunks = chunk_text(text_body)
    embeds = dummy_embed(chunks)

    for t, e in zip(chunks, embeds):
        ch = Chunk(
            document_uuid=d.document_uuid,
            case_uuid=case.case_uuid,
            text=t,
            embedding=e,
            extraction_json=None,
            quality_score=1.0,
        )
        db.add(ch)

    db.flush()
    update_case_embedding(db, case.case_uuid)
    return d


# ============================================================
# SEARCH + MATCH LOGIC
# ============================================================


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2) + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * (
        math.sin(dlon / 2) ** 2
    )
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def match_candidates(
    db: Session,
    seed: Case,
    max_km: float = 500.0,
    age_pad: int = 2,
    stature_pad_cm: float = 3.0,
    limit: int = 50,
) -> List[dict]:
    if seed.case_type == CaseType.MP:
        target_type = CaseType.UHR
        seed_date = seed.last_seen_date
        date_field = Case.found_date
        date_cmp = ">= "
    else:
        target_type = CaseType.MP
        seed_date = seed.found_date
        date_field = Case.last_seen_date
        date_cmp = "<= "

    base = select(Case).where(Case.case_type == target_type)

    if seed.sex:
        base = base.where(or_(Case.sex.is_(None), Case.sex == seed.sex))

    if seed.age_min is not None and seed.age_max is not None:
        base = base.where(
            or_(
                Case.age_min.is_(None),
                Case.age_max.is_(None),
                and_(
                    (Case.age_min - age_pad) <= (seed.age_max + age_pad),
                    (seed.age_min - age_pad) <= (Case.age_max + age_pad),
                ),
            )
        )

    if seed.stature_min_cm is not None and seed.stature_max_cm is not None:
        base = base.where(
            or_(
                Case.stature_min_cm.is_(None),
                Case.stature_max_cm.is_(None),
                and_(
                    (Case.stature_min_cm - stature_pad_cm) <= (seed.stature_max_cm + stature_pad_cm),
                    (seed.stature_min_cm - stature_pad_cm) <= (Case.stature_max_cm + stature_pad_cm),
                ),
            )
        )

    if seed_date is not None:
        if date_cmp.strip() == ">=":
            base = base.where(or_(date_field.is_(None), date_field >= seed_date))
        else:
            base = base.where(or_(date_field.is_(None), date_field <= seed_date))

    candidates: List[Case] = db.execute(base.limit(limit * 5)).scalars().all()

    out = []
    for c in candidates:
        geo_ok = True
        km = None
        if seed.lat is not None and seed.lon is not None and c.lat is not None and c.lon is not None:
            km = haversine_km(seed.lat, seed.lon, c.lat, c.lon)
            geo_ok = km <= max_km

        if not geo_ok:
            continue

        dist = None
        if seed.case_embedding is not None and c.case_embedding is not None:
            res = db.execute(
                select((Case.case_embedding.cosine_distance(seed.case_embedding)).label("d")).where(
                    Case.case_uuid == c.case_uuid
                )
            ).scalar_one_or_none()
            dist = float(res) if res is not None else None

        score = 0.0
        breakdown = {}

        if dist is not None:
            sim = max(0.0, 1.0 - dist)
            score += sim * 70.0
            breakdown["semantic_sim"] = sim * 70.0
        else:
            breakdown["semantic_sim"] = 0.0

        if c.age_min is not None and c.age_max is not None:
            score += 5.0
            breakdown["age_present"] = 5.0
        if c.stature_min_cm is not None and c.stature_max_cm is not None:
            score += 5.0
            breakdown["stature_present"] = 5.0
        if km is not None:
            proximity = max(0.0, 1.0 - (km / max_km))
            score += proximity * 20.0
            breakdown["geo_proximity"] = proximity * 20.0
        else:
            breakdown["geo_proximity"] = 0.0

        out.append(
            {
                "seed_case_uuid": str(seed.case_uuid),
                "candidate_case_uuid": str(c.case_uuid),
                "candidate_source": {"system": c.source_system, "id": c.source_case_id},
                "candidate_title": c.title,
                "candidate_type": c.case_type.value,
                "geo_km": km,
                "semantic_distance": dist,
                "score_total": score,
                "score_breakdown": breakdown,
            }
        )

    out.sort(key=lambda x: x["score_total"], reverse=True)
    return out[:limit]


# ============================================================
# FASTAPI SCHEMAS
# ============================================================


class InitResponse(BaseModel):
    ok: bool
    embed_dim: int


class NamUsSearchRequest(BaseModel):
    case_type: CaseType
    query: str
    limit: int = 20


class NamUsIngestRequest(BaseModel):
    case_type: CaseType
    source_case_id: str
    ingest_documents: bool = True


class NamUsBatchIngestRequest(BaseModel):
    case_type: CaseType
    source_case_ids: List[str]
    ingest_documents: bool = True


class SearchRequest(BaseModel):
    keyword: Optional[str] = None
    case_type: Optional[CaseType] = None
    sex: Optional[str] = None
    age_min: Optional[int] = None
    age_max: Optional[int] = None
    semantic_query_text: Optional[str] = None
    limit: int = 20


class MatchRequest(BaseModel):
    max_km: float = 500.0
    age_pad: int = 2
    stature_pad_cm: float = 3.0
    limit: int = 25


class ExternalSearchRequest(BaseModel):
    source: str = Field(description="One of: gedmatch, ftdna, dnajustice")
    query: str
    filters: Dict[str, Any] = Field(default_factory=dict)


class LeadSearchRequest(BaseModel):
    case_uuid: str
    min_score: float = 65.0
    include_web_search: bool = True
    web_results_limit: int = 5
    max_km: float = 500.0
    age_pad: int = 2
    stature_pad_cm: float = 3.0
    limit: int = 25


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(title="FCIX API", version="0.2")


@app.on_event("startup")
def _startup() -> None:
    db = SessionLocal()
    try:
        init_db(db)
    finally:
        db.close()


@app.get("/init", response_model=InitResponse)
def init_check() -> InitResponse:
    return InitResponse(ok=True, embed_dim=EMBED_DIM)


@app.post("/ingest/namus/search")
def ingest_namus_search(req: NamUsSearchRequest) -> dict:
    c = NamUsConnector()
    data = c.search(case_type=req.case_type, query=req.query, limit=req.limit)
    return {"source": "namus", "case_type": req.case_type.value, "raw": data}


@app.post("/ingest/namus/case")
def ingest_namus_case(req: NamUsIngestRequest, db: Session = Depends(get_db)) -> dict:
    c = NamUsConnector()
    case_payload = c.fetch_case(req.case_type, req.source_case_id)

    case = upsert_case_from_payload(db, case_payload, case_type=req.case_type, source_system="namus")
    ingested_docs = 0

    if req.ingest_documents:
        doc_list = c.list_documents(req.case_type, req.source_case_id)
        docs = doc_list.get("documents") if isinstance(doc_list, dict) else doc_list
        if not isinstance(docs, list):
            docs = []

        for dmeta in docs:
            doc_id = str(dmeta.get("id") or dmeta.get("document_id") or "")
            if not doc_id:
                continue
            raw_doc = c.download_document(req.case_type, req.source_case_id, doc_id)

            text_body = ""
            if isinstance(raw_doc, dict):
                text_body = str(raw_doc.get("text") or raw_doc.get("raw_text") or "")
            else:
                text_body = str(raw_doc)

            if not text_body.strip():
                continue

            ingest_document_text(
                db=db,
                case=case,
                doc_type=str(dmeta.get("type") or dmeta.get("doc_type") or "other"),
                source_url=str(dmeta.get("url") or ""),
                text_body=text_body,
            )
            ingested_docs += 1

    db.commit()
    return {
        "ok": True,
        "case_uuid": str(case.case_uuid),
        "source_case_id": case.source_case_id,
        "case_type": case.case_type.value,
        "documents_ingested": ingested_docs,
        "note": "If documents_ingested is 0, doc access may be restricted or endpoint templates need configuration.",
    }


@app.post("/ingest/namus/batch")
def ingest_namus_batch(req: NamUsBatchIngestRequest, db: Session = Depends(get_db)) -> dict:
    ok = 0
    failed: List[dict] = []
    for cid in req.source_case_ids:
        try:
            ingest_namus_case(
                NamUsIngestRequest(
                    case_type=req.case_type, source_case_id=cid, ingest_documents=req.ingest_documents
                ),
                db=db,
            )
            ok += 1
        except Exception as e:
            failed.append({"source_case_id": cid, "error": str(e)[:200]})
            db.rollback()
    db.commit()
    return {"ok": True, "ingested": ok, "failed": failed[:50]}


@app.post("/search")
def search(req: SearchRequest, db: Session = Depends(get_db)) -> dict:
    results: Dict[str, Any] = {"keyword": [], "semantic_cases": [], "semantic_chunks": []}

    if req.keyword:
        stmt = sql_text(
            """
            SELECT
                c.case_uuid,
                ca.case_type,
                ca.title,
                c.document_uuid,
                c.chunk_uuid,
                c.page_start,
                c.page_end,
                substring(c.text, 1, 400) AS excerpt,
                ts_rank_cd(to_tsvector('english', c.text), plainto_tsquery('english', :q)) AS rank
            FROM chunks c
            JOIN cases ca ON ca.case_uuid = c.case_uuid
            WHERE to_tsvector('english', c.text) @@ plainto_tsquery('english', :q)
        """
        )
        params = {"q": req.keyword}
        if req.case_type:
            stmt = sql_text(stmt.text + " AND ca.case_type = :ct")
            params["ct"] = req.case_type.value
        if req.sex:
            stmt = sql_text(stmt.text + " AND (ca.sex IS NULL OR ca.sex = :sx)")
            params["sx"] = req.sex

        stmt = sql_text(stmt.text + " ORDER BY rank DESC LIMIT :lim")
        params["lim"] = req.limit

        rows = db.execute(stmt, params).mappings().all()
        results["keyword"] = [dict(r) for r in rows]

    if req.semantic_query_text:
        qvec = dummy_embed([req.semantic_query_text])[0]
        results["semantic_cases"] = cosine_similarity_query_cases(db, qvec, case_type=req.case_type, limit=req.limit)
        results["semantic_chunks"] = cosine_similarity_query_chunks(
            db, qvec, case_type=req.case_type, limit=req.limit
        )

    if not req.keyword and not req.semantic_query_text:
        stmt = select(Case).limit(req.limit)
        if req.case_type:
            stmt = stmt.where(Case.case_type == req.case_type)
        if req.sex:
            stmt = stmt.where(or_(Case.sex.is_(None), Case.sex == req.sex))
        if req.age_min is not None and req.age_max is not None:
            stmt = stmt.where(
                or_(
                    Case.age_min.is_(None),
                    Case.age_max.is_(None),
                    and_(Case.age_min <= req.age_max, req.age_min <= Case.age_max),
                )
            )
        cases = db.execute(stmt).scalars().all()
        results["cases"] = [
            {
                "case_uuid": str(c.case_uuid),
                "type": c.case_type.value,
                "title": c.title,
                "source": {"system": c.source_system, "id": c.source_case_id},
            }
            for c in cases
        ]

    return results


@app.post("/match/{case_uuid}")
def run_match(case_uuid: str, req: MatchRequest, db: Session = Depends(get_db)) -> dict:
    try:
        cid = uuid.UUID(case_uuid)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid case_uuid")

    seed = db.execute(select(Case).where(Case.case_uuid == cid)).scalar_one_or_none()
    if seed is None:
        raise HTTPException(status_code=404, detail="Seed case not found")

    run = MatchRun(config_json=req.model_dump())
    db.add(run)
    db.flush()

    candidates = match_candidates(
        db=db,
        seed=seed,
        max_km=req.max_km,
        age_pad=req.age_pad,
        stature_pad_cm=req.stature_pad_cm,
        limit=req.limit,
    )

    for c in candidates:
        mc = MatchCandidate(
            run_uuid=run.run_uuid,
            case_uuid_left=seed.case_uuid,
            case_uuid_right=uuid.UUID(c["candidate_case_uuid"]),
            hard_filter_pass=True,
            score_total=float(c["score_total"]),
            score_breakdown_json=c["score_breakdown"],
            top_evidence=None,
        )
        db.add(mc)

    db.commit()
    return {
        "ok": True,
        "run_uuid": str(run.run_uuid),
        "seed": {"case_uuid": case_uuid, "type": seed.case_type.value},
        "candidates": candidates,
    }


@app.post("/external/search")
def external_search(req: ExternalSearchRequest) -> dict:
    source = req.source.strip().lower()
    if source == "gedmatch":
        connector = ExternalSearchConnector(GEDMATCH_API_BASE, "gedmatch")
    elif source == "ftdna":
        connector = ExternalSearchConnector(FTDNA_API_BASE, "ftdna")
    elif source in {"dnajustice", "dna_justice", "dna-justice"}:
        connector = ExternalSearchConnector(DNA_JUSTICE_API_BASE, "dna_justice")
    else:
        raise HTTPException(status_code=400, detail="Unknown source. Use gedmatch, ftdna, or dnajustice.")

    data = connector.search(req.query, req.filters)
    return {"source": source, "query": req.query, "filters": req.filters, "raw": data}


@app.post("/leads")
def lead_search(req: LeadSearchRequest, db: Session = Depends(get_db)) -> dict:
    try:
        cid = uuid.UUID(req.case_uuid)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid case_uuid")

    seed = db.execute(select(Case).where(Case.case_uuid == cid)).scalar_one_or_none()
    if seed is None:
        raise HTTPException(status_code=404, detail="Seed case not found")

    candidates = match_candidates(
        db=db,
        seed=seed,
        max_km=req.max_km,
        age_pad=req.age_pad,
        stature_pad_cm=req.stature_pad_cm,
        limit=req.limit,
    )

    leads = [c for c in candidates if c["score_total"] >= req.min_score]
    web_context: Dict[str, Any] = {}

    if req.include_web_search and leads:
        provider = WebSearchProvider()
        if provider.enabled():
            query_parts = [seed.title or "", seed.source_case_id or "", seed.case_type.value, "FIGG"]
            query = " ".join([part for part in query_parts if part]).strip()
            web_context = provider.search(query, limit=req.web_results_limit)
        else:
            web_context = {"warning": "Web search provider not configured."}

    return {
        "ok": True,
        "seed": {"case_uuid": str(seed.case_uuid), "type": seed.case_type.value, "title": seed.title},
        "min_score": req.min_score,
        "lead_count": len(leads),
        "leads": leads,
        "web_context": web_context,
    }


@app.post("/dev/create_case")
def dev_create_case(payload: dict, db: Session = Depends(get_db)) -> dict:
    ct = payload.get("case_type")
    if ct not in ("MP", "UHR"):
        raise HTTPException(status_code=400, detail="case_type must be MP or UHR")
    case = upsert_case_from_payload(db, payload, case_type=CaseType(ct), source_system="agency_upload")
    doc_text = payload.get("doc_text") or ""
    if doc_text.strip():
        ingest_document_text(db, case, doc_type="upload", source_url=None, text_body=doc_text)
    db.commit()
    return {"ok": True, "case_uuid": str(case.case_uuid)}


if __name__ == "__main__":
    with SessionLocal() as db:
        init_db(db)
    print("FCIX initialized. Run with: uvicorn FCIX_ONEFILE_PLATFORM:app --reload")
