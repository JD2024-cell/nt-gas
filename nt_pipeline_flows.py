"""
AEMO GBB Pipeline Connection Flow ingestion and derived NT supply flows.

Streamlit-free so it can be unit tested, run from the command line and reused.
Storage is a dedicated table (`gbb_pipeline_connection_flow`), independent of the production
`gbb_records` table. Command line (uses DATABASE_URL):

    python -m nt_pipeline_flows init      # one-off ~27 MB history load (explicit)
    python -m nt_pipeline_flows refresh   # latest-31-days refresh (captures revisions)
    python -m nt_pipeline_flows status

Tracked connection points (identified by FacilityId + ConnectionPointId + FlowDirection, never
by name, because names repeat and change). Headline points:

  Bladin Point gas supply (Ichthys - inferred)       WPP 580030  Bladin Point Entry DS   1808064 RECEIPT
  Wickham Point gas supply (Darwin LNG - inferred)   WPP 580030  Wickham Point Entry DS  1808025 RECEIPT
  NGP westward (gross)   AGP 580010  AGP NGP Entry Delivery  1808073 RECEIPT
  NGP eastward (gross)   AGP 580010  NGP                     1808011 DELIVERY
  Mt Isa receipt         NGP 580050  Mt Isa                  1490098 RECEIPT  (cross-check only)
  Mt Isa delivery        NGP 580050  Mt Isa                  1404296 DELIVERY (cross-check only)

Diagnostic-only points (reporting-boundary checks, never displayed as supply):
  NGP Tennant Creek delivery 1808074, NGP Phillip Creek receipt 1808028,
  CGP NGP exit 1490097, CGP NGP entry 1404225.

NGP net westward is a NET flow at the AGP interface: max(0, 1808073 - 1808011). The two reporters
(AGP and NGP operators) sometimes split the same net flow into different gross westward/eastward
quantities, so only the net is comparable across reporting boundaries.

Revision policy (see build_upsert_statement): an AEMO record is only authoritative when it carries
a LastUpdated newer than the stored one. A NULL or invalid newer submission (e.g. Quality NIL/OOR)
replaces a stored valid value and the prior value is kept in `superseded_quantity`. A record that
is simply absent from a download, an older record, or a 'Not Available' placeholder never
overwrites a stored value. Dingo has no verified reporting point and is not ingested.
"""

import argparse
import io
import logging
import os
import threading
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from sqlalchemy import (
    Column, Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint,
    and_, case, create_engine, func, inspect, not_, or_, select, text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import declarative_base
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

FlowBase = declarative_base()

BASE_URL = "https://nemweb.com.au/Reports/Current/GBB/"
HISTORICAL_ZIPS = (
    "GasBBPipelineConnectionFlow_2018_2023.zip",
    "GasBBPipelineConnectionFlow_2023_2028.zip",
)
LAST31_CSV = "GasBBPipelineConnectionFlowLast31.CSV"
QUANTITY_UNIT = "TJ/day"

TRACKED_POINTS = {
    "ichthys_receipt": {"facility_id": 580030, "connection_point_id": 1808064, "flow_direction": "RECEIPT"},
    "dlng_receipt": {"facility_id": 580030, "connection_point_id": 1808025, "flow_direction": "RECEIPT"},
    "ngp_west_gross": {"facility_id": 580010, "connection_point_id": 1808073, "flow_direction": "RECEIPT"},
    "ngp_east_gross": {"facility_id": 580010, "connection_point_id": 1808011, "flow_direction": "DELIVERY"},
    "mt_isa_receipt": {"facility_id": 580050, "connection_point_id": 1490098, "flow_direction": "RECEIPT"},
    "mt_isa_delivery": {"facility_id": 580050, "connection_point_id": 1404296, "flow_direction": "DELIVERY"},
    # Diagnostic-only: NGP-operator and CGP-operator views of the same pipeline ends.
    "ngp_tennant_delivery": {"facility_id": 580050, "connection_point_id": 1808074, "flow_direction": "DELIVERY"},
    "ngp_phillip_receipt": {"facility_id": 580050, "connection_point_id": 1808028, "flow_direction": "RECEIPT"},
    "cgp_ngp_exit": {"facility_id": 540059, "connection_point_id": 1490097, "flow_direction": "DELIVERY"},
    "cgp_ngp_entry": {"facility_id": 540059, "connection_point_id": 1404225, "flow_direction": "RECEIPT"},
}

ACTIVE_FLOW_THRESHOLD = 1.0  # TJ/d, used only for the "last active" caption (ignores sub-TJ residuals)

# Net AGP-end vs Mt Isa-end comparison defaults. Configurable via NgpCheckConfig / environment.
NGP_CHECK_ABS_TOL = 3.0       # TJ/d
NGP_CHECK_REL_TOL = 0.25      # fraction of the larger absolute flow
NGP_CHECK_REL_MIN_FLOW = 5.0  # TJ/d; percentage differences are not reported below this flow

# Dashboard series. `column` is a column of compute_additional_flows().
DISPLAY_SOURCES = {
    "ichthys_domestic": {
        "label": "Bladin Point gas supply (Ichthys - inferred)",
        "chart_label": "Bladin Point gas supply (Ichthys - inferred)",
        "column": "ichthys_domestic",
        "color": "#1f77b4",
        "note": "WPP Bladin Point Entry DS (1808064), an AEMO connection-point receipt. Attribution to INPEX Ichthys is inferred from the connection-point name and location, not independently confirmed.",
    },
    "darwin_lng_domestic": {
        "label": "Wickham Point gas supply (Darwin LNG - inferred)",
        "chart_label": "Wickham Point gas supply (Darwin LNG - inferred)",
        "column": "darwin_lng_domestic",
        "color": "#d62728",
        "note": "WPP Wickham Point Entry DS (1808025), an AEMO connection-point receipt. Attribution to Darwin LNG is inferred from the connection-point name and location, not independently confirmed.",
    },
    "ngp_net_westward": {
        "label": "NGP net westward flow (AGP interface)",
        "chart_label": "NGP net westward flow (AGP interface)",
        "column": "ngp_net_westward",
        "color": "#2ca02c",
        "note": "Net flow at the AGP interface: max(0, AGP receipt 1808073 - AGP delivery 1808011).",
    },
}

_KEY_COLUMNS = ["gas_date", "facility_id", "connection_point_id", "flow_direction"]


class AemoUnavailableError(RuntimeError):
    """AEMO NEMweb could not be reached or refused the request (TLS, network, HTTP)."""


class AemoDataError(RuntimeError):
    """A download completed but its content is truncated or not in the expected format."""


class IngestionBusyError(RuntimeError):
    """Another ingestion is already running against this database."""


# ============================================================================
# Schema
# ============================================================================

class PipelineConnectionFlow(FlowBase):
    """AEMO Pipeline Connection Flow, one row per gas date and connection point direction."""
    __tablename__ = "gbb_pipeline_connection_flow"

    id = Column(Integer, primary_key=True)
    gas_date = Column(Date, nullable=False)
    facility_id = Column(Integer, nullable=False)
    facility_name = Column(String(255))
    connection_point_id = Column(Integer, nullable=False)
    connection_point_name = Column(String(255))
    flow_direction = Column(String(20), nullable=False)
    actual_quantity = Column(Float)  # as reported by AEMO; NULL when missing or unpublished
    quantity_unit = Column(String(10), nullable=False)
    quality = Column(String(30))
    state = Column(String(10))
    location_name = Column(String(255))
    location_id = Column(Integer)
    last_updated = Column(DateTime)  # AEMO LastUpdated
    source_dataset = Column(String(80))
    ingested_at = Column(DateTime, nullable=False)  # first insert
    updated_at = Column(DateTime, nullable=False)   # last ingestion that accepted a record for this row
    superseded_quantity = Column(Float)             # value replaced by the most recent revision
    superseded_last_updated = Column(DateTime)      # AEMO LastUpdated of that replaced value
    superseded_at = Column(DateTime)                # when that replacement was ingested
    invalidated_at = Column(DateTime)               # a newer AEMO record invalidated a previously valid value

    # Revisions supersede the stored row, so no source identifier is needed for uniqueness.
    __table_args__ = (
        UniqueConstraint(*_KEY_COLUMNS, name="uix_gbb_pcf_key"),
        Index("ix_gbb_pcf_point_date", "connection_point_id", "flow_direction", "gas_date"),
    )


class PipelineFlowIngestionLog(FlowBase):
    """One row per ingestion attempt; drives refresh throttling and admin visibility."""
    __tablename__ = "gbb_pipeline_flow_ingestion_log"

    id = Column(Integer, primary_key=True)
    mode = Column(String(20), nullable=False)    # 'history' | 'latest'
    status = Column(String(20), nullable=False)  # 'running' | 'success' | 'failed'
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime)
    rows_submitted = Column(Integer)
    rows_inserted = Column(Integer)
    rows_revised = Column(Integer)
    rows_invalidated = Column(Integer)
    absent_in_window = Column(Integer)
    message = Column(Text)

    __table_args__ = (Index("ix_gbb_pflog_started", "started_at"),)


_OWN_TABLES = [PipelineConnectionFlow.__table__, PipelineFlowIngestionLog.__table__]


def ensure_schema(engine):
    """Create only the pipeline-flow tables and add any columns missing from an older version."""
    FlowBase.metadata.create_all(engine, tables=_OWN_TABLES)
    inspector = inspect(engine)
    for table in _OWN_TABLES:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing:
                ddl_type = column.type.compile(dialect=engine.dialect)
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl_type}"))


# ============================================================================
# HTTP access (TLS verification is always on)
# ============================================================================

def _verify_setting():
    """
    Honour a valid REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE / SSL_CERT_FILE. A path that does not exist
    would make every request fail, so fall back to the bundled certifi roots. Never disables checks.
    """
    for var in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE"):
        path = os.environ.get(var)
        if path:
            if os.path.exists(path):
                return path
            logger.warning("%s points to a missing file (%s); using the default certifi CA bundle", var, path)
    import certifi
    return certifi.where()


def _session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(500, 502, 503, 504), allowed_methods=("GET",))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.verify = _verify_setting()
    return session


def _get(url, timeout):
    """GET with TLS verification and errors translated into AemoUnavailableError / AemoDataError."""
    name = url.rsplit("/", 1)[-1]
    try:
        with _session() as session:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            content = response.content
    except requests.exceptions.SSLError as exc:
        raise AemoUnavailableError(
            "TLS certificate verification failed for nemweb.com.au. Verification has not been disabled. "
            "If this network uses a corporate proxy, point REQUESTS_CA_BUNDLE at a valid CA bundle "
            "that includes the proxy certificate."
        ) from exc
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise AemoUnavailableError(
            f"AEMO NEMweb is unreachable ({exc.__class__.__name__}). Stored data remain available; "
            "the refresh will be retried later."
        ) from exc
    except requests.exceptions.HTTPError as exc:
        raise AemoUnavailableError(f"AEMO NEMweb returned HTTP {exc.response.status_code} for {name}.") from exc
    expected = response.headers.get("Content-Length")
    if expected and not response.headers.get("Content-Encoding") and int(expected) != len(content):
        raise AemoDataError(f"Truncated download of {name}: {len(content)} of {expected} bytes.")
    return content


# ============================================================================
# Fetching and normalisation
# ============================================================================

def _tracked_keys_frame():
    return pd.DataFrame(TRACKED_POINTS.values()).rename(columns={
        "facility_id": "FacilityId",
        "connection_point_id": "ConnectionPointId",
        "flow_direction": "FlowDirection",
    })


_REQUIRED_RAW_COLUMNS = {"GasDate", "FacilityId", "ConnectionPointId", "FlowDirection", "ActualQuantity", "LastUpdated"}
_OUT_COLUMNS = _KEY_COLUMNS + [
    "facility_name", "connection_point_name", "actual_quantity", "quality", "state",
    "location_name", "location_id", "last_updated", "source_dataset",
]


def normalise_raw_flow_frame(raw, source_dataset):
    """
    Keep only tracked (FacilityId, ConnectionPointId, FlowDirection) rows and map to DB columns.
    Missing quantities stay NULL. Within the frame the row with the latest LastUpdated wins.
    """
    if raw is None or raw.empty:
        return pd.DataFrame(columns=_OUT_COLUMNS)
    missing = _REQUIRED_RAW_COLUMNS - set(raw.columns)
    if missing:
        raise AemoDataError(f"{source_dataset}: unexpected format, missing columns {sorted(missing)}")

    frame = raw.copy()
    for col in ("FacilityId", "ConnectionPointId"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["FlowDirection"] = frame["FlowDirection"].astype(str).str.strip().str.upper()
    frame = frame.merge(
        _tracked_keys_frame(), how="inner",
        on=["FacilityId", "ConnectionPointId", "FlowDirection"],
    )
    if frame.empty:
        return pd.DataFrame(columns=_OUT_COLUMNS)

    out = pd.DataFrame({
        "gas_date": pd.to_datetime(frame["GasDate"], errors="coerce").dt.normalize(),
        "facility_id": frame["FacilityId"].astype("int64"),
        "facility_name": frame["FacilityName"] if "FacilityName" in frame else None,
        "connection_point_id": frame["ConnectionPointId"].astype("int64"),
        "connection_point_name": frame["ConnectionPointName"] if "ConnectionPointName" in frame else None,
        "flow_direction": frame["FlowDirection"],
        "actual_quantity": pd.to_numeric(frame["ActualQuantity"], errors="coerce"),
        "quality": frame["Quality"].astype("string").str.strip() if "Quality" in frame else pd.NA,
        "state": frame["State"] if "State" in frame else None,
        "location_name": frame["LocationName"] if "LocationName" in frame else None,
        "location_id": pd.to_numeric(frame["LocationId"], errors="coerce") if "LocationId" in frame else np.nan,
        "last_updated": pd.to_datetime(frame["LastUpdated"], errors="coerce"),
        "source_dataset": source_dataset,
    })
    out = out.dropna(subset=["gas_date"])
    out = out.sort_values("last_updated", na_position="first", kind="stable")
    return out.drop_duplicates(_KEY_COLUMNS, keep="last").reset_index(drop=True)[_OUT_COLUMNS]


def _read_filtered_csv(stream, source_dataset, chunksize=200_000):
    parts = [
        normalise_raw_flow_frame(chunk, source_dataset)
        for chunk in pd.read_csv(stream, chunksize=chunksize, encoding_errors="replace")
    ]
    parts = [p for p in parts if not p.empty]
    if not parts:
        return normalise_raw_flow_frame(None, source_dataset)
    return combine_flow_frames(parts)


def combine_flow_frames(frames):
    """Merge normalised frames; for a duplicate key the latest LastUpdated wins (ties: later frame)."""
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame(columns=_OUT_COLUMNS)
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values("last_updated", na_position="first", kind="stable")
    return combined.drop_duplicates(_KEY_COLUMNS, keep="last").reset_index(drop=True)


def fetch_last31(timeout=60):
    """Latest reporting window (captures recent revisions)."""
    content = _get(BASE_URL + LAST31_CSV, timeout)
    frame = _read_filtered_csv(io.BytesIO(content), "Last31 CSV")
    if frame.empty:
        raise AemoDataError("The latest-31-days file contained no tracked connection points.")
    return frame


def fetch_history(zip_names=HISTORICAL_ZIPS, timeout=300):
    """Full history from the AEMO archive zips, filtered to tracked points while streaming."""
    frames = []
    for name in zip_names:
        content = _get(BASE_URL + name, timeout)
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                csv_name = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
                with archive.open(csv_name) as handle:
                    frames.append(_read_filtered_csv(handle, name))
        except (zipfile.BadZipFile, StopIteration) as exc:
            raise AemoDataError(f"{name} is not a readable archive: {exc}") from exc
    combined = combine_flow_frames(frames)
    if combined.empty:
        raise AemoDataError("The historical archives contained no tracked connection points.")
    return combined


# ============================================================================
# Persistence
# ============================================================================

def _count(conn, where=None):
    stmt = select(func.count()).select_from(PipelineConnectionFlow.__table__)
    if where is not None:
        stmt = stmt.where(where)
    return conn.execute(stmt).scalar_one()


def build_upsert_statement(dialect_name):
    """
    INSERT ... ON CONFLICT DO UPDATE for PostgreSQL or SQLite.

    Row-level rules (t = stored row, ex = incoming AEMO record):
    - Only a record with LastUpdated >= the stored LastUpdated may update a row (older or untimed
      records are ignored, so a stale download can never resurrect a corrected value).
    - A NULL incoming quantity replaces a stored valid value only when it is a strictly newer
      submission that is not a 'Not Available' placeholder (an explicit AEMO/participant
      correction). Otherwise the stored value is kept.
    - Overwritten values are preserved in superseded_quantity / superseded_last_updated;
      invalidated_at records when a valid value was replaced by a NULL or non-OK record.
    """
    if dialect_name == "postgresql":
        insert_fn = pg_insert
    elif dialect_name == "sqlite":
        insert_fn = sqlite_insert
    else:
        raise RuntimeError(f"Unsupported database dialect for flow upsert: {dialect_name}")

    t = PipelineConnectionFlow.__table__.c
    stmt = insert_fn(PipelineConnectionFlow.__table__)
    ex = stmt.excluded

    incoming_newer = and_(ex.last_updated.isnot(None), or_(t.last_updated.is_(None), ex.last_updated > t.last_updated))
    placeholder = func.lower(func.coalesce(ex.quality, "")) == "not available"
    keep_existing = and_(
        ex.actual_quantity.is_(None),
        t.actual_quantity.isnot(None),
        not_(and_(incoming_newer, not_(placeholder))),
    )
    value_replaced = and_(
        t.actual_quantity.isnot(None),
        or_(ex.actual_quantity.is_(None), ex.actual_quantity != t.actual_quantity),
    )
    incoming_invalid = or_(
        ex.actual_quantity.is_(None),
        and_(ex.quality.isnot(None), func.upper(ex.quality) != "OK"),
    )

    def prefer_incoming(column):
        return case((keep_existing, t[column]), else_=ex[column])

    set_ = {
        "actual_quantity": prefer_incoming("actual_quantity"),
        "quality": prefer_incoming("quality"),
        "last_updated": prefer_incoming("last_updated"),
        "source_dataset": prefer_incoming("source_dataset"),
        "updated_at": prefer_incoming("updated_at"),
        "superseded_quantity": case(
            (keep_existing, t.superseded_quantity), (value_replaced, t.actual_quantity), else_=t.superseded_quantity),
        "superseded_last_updated": case(
            (keep_existing, t.superseded_last_updated), (value_replaced, t.last_updated), else_=t.superseded_last_updated),
        "superseded_at": case(
            (keep_existing, t.superseded_at), (value_replaced, ex.updated_at), else_=t.superseded_at),
        "invalidated_at": case(
            (keep_existing, t.invalidated_at),
            (and_(incoming_invalid, t.actual_quantity.isnot(None)), ex.updated_at),
            (incoming_invalid, t.invalidated_at),
            else_=None,
        ),
        "facility_name": ex.facility_name,
        "connection_point_name": ex.connection_point_name,
        "state": ex.state,
        "location_name": ex.location_name,
        "location_id": ex.location_id,
    }
    return stmt.on_conflict_do_update(
        index_elements=_KEY_COLUMNS,
        set_=set_,
        where=or_(t.last_updated.is_(None), and_(ex.last_updated.isnot(None), ex.last_updated >= t.last_updated)),
    )


def upsert_flow_records(engine, records, batch_size=2000, now=None):
    """
    Idempotent upsert on (gas_date, facility_id, connection_point_id, flow_direction).
    Returns {'submitted', 'inserted', 'updated', 'revised', 'invalidated'}.
    """
    if records is None or records.empty:
        return {"submitted": 0, "inserted": 0, "updated": 0, "revised": 0, "invalidated": 0}

    stmt = build_upsert_statement(engine.dialect.name)

    now = now or datetime.now()
    frame = records.copy()
    frame["gas_date"] = pd.to_datetime(frame["gas_date"]).dt.date
    frame["quantity_unit"] = QUANTITY_UNIT
    frame["ingested_at"] = now
    frame["updated_at"] = now
    frame["location_id"] = frame["location_id"].astype("Int64")
    frame["quality"] = frame["quality"].astype(object)
    payload = frame.astype(object).where(frame.notna(), None)
    rows = [
        {k: (v.to_pydatetime() if isinstance(v, pd.Timestamp) else v) for k, v in row.items()}
        for row in payload.to_dict("records")
    ]
    for row in rows:
        if row["location_id"] is not None:
            row["location_id"] = int(row["location_id"])

    t = PipelineConnectionFlow.__table__.c
    with engine.begin() as conn:
        before = _count(conn)
        for start in range(0, len(rows), batch_size):
            conn.execute(stmt, rows[start:start + batch_size])
        after = _count(conn)
        changed = _count(conn, t.updated_at == now)
        revised = _count(conn, t.superseded_at == now)
        invalidated = _count(conn, t.invalidated_at == now)
    inserted = after - before
    return {"submitted": len(rows), "inserted": inserted, "updated": max(changed - inserted, 0),
            "revised": revised, "invalidated": invalidated}


def find_absent_records(engine, latest_frame):
    """
    Stored valid rows inside the latest file's date window whose key is not in that file.
    Absence is ambiguous (a missed download versus an AEMO withdrawal), so these rows are
    reported, never altered or deleted.
    """
    if latest_frame is None or latest_frame.empty:
        return 0
    window_start = pd.to_datetime(latest_frame["gas_date"]).min().date()
    window_end = pd.to_datetime(latest_frame["gas_date"]).max().date()
    t = PipelineConnectionFlow.__table__.c
    stmt = select(t.gas_date, t.facility_id, t.connection_point_id, t.flow_direction).where(
        and_(t.gas_date >= window_start, t.gas_date <= window_end, t.actual_quantity.isnot(None)))
    with engine.connect() as conn:
        stored = pd.read_sql(stmt, conn)
    if stored.empty:
        return 0
    stored["gas_date"] = pd.to_datetime(stored["gas_date"])
    keys = latest_frame[_KEY_COLUMNS].copy()
    keys["gas_date"] = pd.to_datetime(keys["gas_date"])
    merged = stored.merge(keys, how="left", on=_KEY_COLUMNS, indicator=True)
    return int((merged["_merge"] == "left_only").sum())


# ----------------------------------------------------------------------------
# Controlled ingestion: logging, locking and refresh policy
# ----------------------------------------------------------------------------

_LOCAL_LOCK = threading.Lock()
_ADVISORY_LOCK_KEY = 7_240_181


class _IngestionLock:
    """Cross-process lock on PostgreSQL (advisory lock), per-process lock otherwise."""

    def __init__(self, engine):
        self.engine = engine
        self.conn = None

    def __enter__(self):
        if self.engine.dialect.name == "postgresql":
            self.conn = self.engine.connect()
            got = self.conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}).scalar()
            if not got:
                self.conn.close()
                raise IngestionBusyError("Another pipeline flow ingestion is already running.")
        elif not _LOCAL_LOCK.acquire(blocking=False):
            raise IngestionBusyError("Another pipeline flow ingestion is already running.")
        return self

    def __exit__(self, *exc):
        if self.engine.dialect.name == "postgresql":
            try:
                self.conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY})
            finally:
                self.conn.close()
        else:
            _LOCAL_LOCK.release()


def _log_start(engine, mode):
    with engine.begin() as conn:
        result = conn.execute(PipelineFlowIngestionLog.__table__.insert().values(
            mode=mode, status="running", started_at=datetime.now()))
        return result.inserted_primary_key[0]


def _log_finish(engine, log_id, status, message=None, result=None, absent=None):
    result = result or {}
    with engine.begin() as conn:
        conn.execute(PipelineFlowIngestionLog.__table__.update()
                     .where(PipelineFlowIngestionLog.__table__.c.id == log_id)
                     .values(status=status, finished_at=datetime.now(), message=message,
                             rows_submitted=result.get("submitted"), rows_inserted=result.get("inserted"),
                             rows_revised=result.get("revised"), rows_invalidated=result.get("invalidated"),
                             absent_in_window=absent))


def last_ingestion(engine, mode=None, status=None):
    """Most recent ingestion log row as a dict (or None)."""
    t = PipelineFlowIngestionLog.__table__.c
    stmt = select(PipelineFlowIngestionLog.__table__).order_by(t.started_at.desc(), t.id.desc()).limit(1)
    if mode:
        stmt = stmt.where(t.mode == mode)
    if status:
        stmt = stmt.where(t.status == status)
    with engine.connect() as conn:
        row = conn.execute(stmt).mappings().first()
    return dict(row) if row else None


def _run_ingestion(engine, mode, loader, check_absent=False):
    ensure_schema(engine)
    with _IngestionLock(engine):
        log_id = _log_start(engine, mode)
        try:
            frame = loader()
            absent = find_absent_records(engine, frame) if check_absent else None
            result = upsert_flow_records(engine, frame)
            note = (f"{absent} stored record(s) in the latest window are absent from AEMO's file "
                    "(retained, not altered)" if absent else None)
            _log_finish(engine, log_id, "success", note, result, absent)
            result["absent_in_window"] = absent
            return result
        except Exception as exc:
            _log_finish(engine, log_id, "failed", f"{exc.__class__.__name__}: {exc}")
            raise


def initialise_history(engine):
    """Explicit one-off (or full-reload) ingestion: archive zips first, then Last31 so revisions win."""
    return _run_ingestion(engine, "history", lambda: combine_flow_frames([fetch_history(), fetch_last31()]))


backfill_history = initialise_history


def refresh_latest(engine):
    """Routine refresh of the latest reporting window to capture revisions."""
    return _run_ingestion(engine, "latest", fetch_last31, check_absent=True)


def get_ingestion_state(engine):
    """Return (row_count, latest_updated_at, latest_gas_date) for stored rows."""
    t = PipelineConnectionFlow.__table__.c
    with engine.connect() as conn:
        row = conn.execute(select(func.count(), func.max(t.updated_at), func.max(t.gas_date))).one()
    return int(row[0]), row[1], row[2]


def missing_points(engine):
    """Tracked points with no stored rows (e.g. a database loaded before diagnostic points were added)."""
    t = PipelineConnectionFlow.__table__.c
    with engine.connect() as conn:
        present = {r[0] for r in conn.execute(select(t.connection_point_id).distinct())}
    return [k for k, p in TRACKED_POINTS.items() if p["connection_point_id"] not in present]


def refresh_decision(engine, now=None, stale_after=timedelta(hours=1), retry_after=timedelta(minutes=15),
                     running_timeout=timedelta(minutes=30)):
    """
    Returns (should_refresh, reason). Ordinary dashboard viewing never starts a history load:
    an uninitialised table is reported, not filled. Failed attempts are retried only after
    `retry_after`, and the decision is shared by every session through the log table.
    """
    now = now or datetime.now()
    row_count, last_update, _ = get_ingestion_state(engine)
    if row_count == 0:
        return False, "not_initialised"
    last_attempt = last_ingestion(engine)
    if last_attempt:
        if last_attempt["status"] == "running" and now - last_attempt["started_at"] < running_timeout:
            return False, "running"
        if now - last_attempt["started_at"] < retry_after:
            return False, "recent_attempt"
    last_ok = last_ingestion(engine, status="success")
    reference = last_ok["finished_at"] if last_ok and last_ok["finished_at"] else last_update
    if reference is not None and now - reference < stale_after:
        return False, "fresh"
    return True, "stale"


def count_invalidated(engine):
    t = PipelineConnectionFlow.__table__.c
    with engine.connect() as conn:
        return _count(conn, t.invalidated_at.isnot(None))


def load_flow_records(engine):
    """Load stored tracked records as a DataFrame (nulls preserved)."""
    table = PipelineConnectionFlow.__table__
    ids = sorted({p["connection_point_id"] for p in TRACKED_POINTS.values()})
    stmt = select(table).where(table.c.connection_point_id.in_(ids)).order_by(table.c.gas_date)
    with engine.connect() as conn:
        frame = pd.read_sql(stmt, conn)
    frame["gas_date"] = pd.to_datetime(frame["gas_date"])
    return frame


# ============================================================================
# Derived flows
# ============================================================================

@dataclass(frozen=True)
class NgpCheckConfig:
    """
    Thresholds for comparing the AGP-end net flow with the Mt Isa-end net flow.
    A day is flagged when |difference| > abs_tol AND |difference| > rel_tol x the larger flow.
    Percentage differences are only reported when that flow is at least rel_min_flow.
    """
    abs_tol: float = NGP_CHECK_ABS_TOL
    rel_tol: float = NGP_CHECK_REL_TOL
    rel_min_flow: float = NGP_CHECK_REL_MIN_FLOW

    def __post_init__(self):
        if not (self.abs_tol >= 0 and 0 <= self.rel_tol <= 10 and self.rel_min_flow >= 0):
            raise ValueError(f"Invalid NGP check thresholds: {self}")

    @classmethod
    def from_env(cls, environ=None, prefix="NT_NGP_CHECK_"):
        """Read NT_NGP_CHECK_ABS_TOL / _REL_TOL / _REL_MIN_FLOW; invalid values fall back to defaults."""
        environ = os.environ if environ is None else environ
        values = {}
        for field, name in (("abs_tol", "ABS_TOL"), ("rel_tol", "REL_TOL"), ("rel_min_flow", "REL_MIN_FLOW")):
            raw = environ.get(prefix + name)
            if raw is None or str(raw).strip() == "":
                continue
            try:
                values[field] = float(raw)
            except ValueError:
                logger.warning("Ignoring non-numeric %s%s=%r", prefix, name, raw)
        try:
            return cls(**values)
        except ValueError:
            logger.warning("Ignoring out-of-range NGP check thresholds %s; using defaults", values)
            return cls()


def build_point_series(records, valid_qualities=("OK",)):
    """
    Wide frame indexed by every calendar gas date, one column per tracked point.
    A date with no record, a NULL quantity, or a non-OK Quality is NaN, never zero.
    """
    columns = list(TRACKED_POINTS)
    if records is None or records.empty:
        return pd.DataFrame(columns=columns, dtype=float)

    frame = records.copy()
    frame["gas_date"] = pd.to_datetime(frame["gas_date"]).dt.normalize()
    bad_quality = frame["quality"].notna() & ~frame["quality"].isin(valid_qualities)
    frame.loc[bad_quality, "actual_quantity"] = np.nan

    index = pd.date_range(frame["gas_date"].min(), frame["gas_date"].max(), freq="D")
    wide = pd.DataFrame(index=index, columns=columns, dtype=float)
    for name, point in TRACKED_POINTS.items():
        subset = frame[
            (frame["facility_id"] == point["facility_id"])
            & (frame["connection_point_id"] == point["connection_point_id"])
            & (frame["flow_direction"] == point["flow_direction"])
        ].drop_duplicates("gas_date", keep="last").set_index("gas_date")["actual_quantity"]
        wide[name] = subset.reindex(index)
    wide.index.name = "gas_date"
    return wide


def _flag(diff, magnitude, config):
    """True where the difference exceeds both the absolute and relative tolerance (NaN -> False)."""
    return (diff.abs() > np.maximum(config.abs_tol, config.rel_tol * magnitude)).fillna(False)


def compute_additional_flows(records, valid_qualities=("OK",), config=None):
    """
    Derived daily flows (TJ/d), indexed by gas date. Missing inputs propagate as NaN.

    ngp_net_signed    = AGP receipt 1808073 - AGP delivery 1808011 (negative = net eastward); kept for analysis
    ngp_net_westward  = max(0, ngp_net_signed) - headline metric, NaN when an input is missing
    mt_isa_net_signed = Mt Isa receipt 1490098 - Mt Isa delivery 1404296 (cross-check only, never added)

    Cross-check, compared separately for each direction (westward part = max(0, net), eastward part
    = max(0, -net)) so a westward day is never judged against an eastward one:
    ngp_check_west_diff / ngp_check_east_diff, their statuses, and ngp_check_status
    ('ok' | 'discrepancy' | 'unavailable') with ngp_check_direction ('westward' | 'eastward' | 'both' | '').

    Diagnostics (NaN when the diagnostic points are not stored):
    ngp_net_signed_ngp_side   = Tennant Creek delivery - Phillip Creek receipt (NGP operator's west-end net)
    ngp_west_boundary_diff    = AGP-reported net - NGP-reported net at the same pipeline end
    ngp_pipeline_imbalance    = gas into the NGP - gas out of it, from NGP-operator points
                                (Mt Isa rcpt + Phillip Ck rcpt - Tennant Ck dlv - Mt Isa dlv)
    qld_boundary_diff         = Mt Isa-end net - CGP-reported net at the same end
    """
    config = config or NgpCheckConfig()
    points = build_point_series(records, valid_qualities)
    out = pd.DataFrame(index=points.index)
    out["ichthys_domestic"] = points["ichthys_receipt"]
    out["darwin_lng_domestic"] = points["dlng_receipt"]
    out["ngp_gross_westward"] = points["ngp_west_gross"]
    out["ngp_gross_eastward"] = points["ngp_east_gross"]
    out["ngp_net_signed"] = points["ngp_west_gross"] - points["ngp_east_gross"]
    out["ngp_net_westward"] = out["ngp_net_signed"].clip(lower=0)
    out["mt_isa_net_signed"] = points["mt_isa_receipt"] - points["mt_isa_delivery"]
    out["ngp_check_diff"] = out["ngp_net_signed"] - out["mt_isa_net_signed"]

    agp_net, mti_net = out["ngp_net_signed"], out["mt_isa_net_signed"]
    magnitude = np.maximum(agp_net.abs(), mti_net.abs())
    out["ngp_check_rel_diff"] = (out["ngp_check_diff"] / magnitude).where(magnitude >= config.rel_min_flow)

    west_a, west_b = agp_net.clip(lower=0), mti_net.clip(lower=0)
    east_a, east_b = (-agp_net).clip(lower=0), (-mti_net).clip(lower=0)
    out["ngp_check_west_diff"] = west_a - west_b
    out["ngp_check_east_diff"] = east_a - east_b
    west_flag = _flag(out["ngp_check_west_diff"], np.maximum(west_a, west_b), config)
    east_flag = _flag(out["ngp_check_east_diff"], np.maximum(east_a, east_b), config)
    unavailable = out["ngp_check_diff"].isna()
    for name, flag in (("west", west_flag), ("east", east_flag)):
        status = pd.Series("ok", index=out.index, dtype=object)
        status[flag] = "discrepancy"
        status[unavailable] = "unavailable"
        out[f"ngp_check_{name}_status"] = status

    status = pd.Series("ok", index=out.index, dtype=object)
    status[west_flag | east_flag] = "discrepancy"
    status[unavailable] = "unavailable"
    out["ngp_check_status"] = status
    direction = pd.Series("", index=out.index, dtype=object)
    direction[west_flag & ~east_flag] = "westward"
    direction[east_flag & ~west_flag] = "eastward"
    direction[west_flag & east_flag] = "both"
    out["ngp_check_direction"] = direction

    out["ngp_net_signed_ngp_side"] = points["ngp_tennant_delivery"] - points["ngp_phillip_receipt"]
    out["ngp_west_boundary_diff"] = agp_net - out["ngp_net_signed_ngp_side"]
    out["ngp_pipeline_imbalance"] = (
        points["mt_isa_receipt"] + points["ngp_phillip_receipt"]
        - points["ngp_tennant_delivery"] - points["mt_isa_delivery"]
    )
    out["qld_boundary_diff"] = mti_net - (points["cgp_ngp_exit"] - points["cgp_ngp_entry"])
    return out


def summarise_flow(series, window=7, active_threshold=ACTIVE_FLOW_THRESHOLD):
    """Latest value, trailing-window average and last available / active gas dates for one series."""
    valid = series.dropna()
    if valid.empty:
        return {"has_data": False}
    latest_date = valid.index.max()
    win = series.loc[latest_date - pd.Timedelta(days=window - 1):latest_date]
    active = series[series > active_threshold]
    return {
        "has_data": True,
        "latest": float(valid.loc[latest_date]),
        "latest_date": latest_date,
        "avg_7d": float(win.mean()) if win.notna().sum() == window else None,
        "days_in_window": int(win.notna().sum()),
        "last_active_date": active.index.max() if not active.empty else None,
        "first_date": valid.index.min(),
        "observations": int(valid.size),
    }


# ============================================================================
# Command line
# ============================================================================

def _engine_from_env():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    options = {}
    if url.startswith("postgresql"):
        options["pool_pre_ping"] = True
    return create_engine(url, **options)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nt_pipeline_flows", description="AEMO pipeline connection flow ingestion")
    parser.add_argument("command", choices=("init", "refresh", "status"))
    args = parser.parse_args(argv)
    engine = _engine_from_env()
    ensure_schema(engine)
    try:
        if args.command == "init":
            print("Loading full history (about 27 MB)...")
            print(initialise_history(engine))
        elif args.command == "refresh":
            print(refresh_latest(engine))
        else:
            count, updated, latest = get_ingestion_state(engine)
            print({"rows": count, "last_updated_at": updated, "latest_gas_date": latest,
                   "points_without_rows": missing_points(engine), "invalidated_rows": count_invalidated(engine),
                   "last_attempt": last_ingestion(engine)})
    except (AemoUnavailableError, AemoDataError, IngestionBusyError) as exc:
        raise SystemExit(f"Failed: {exc}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
