"""
NT Gas Production Monitor

Public-facing dashboard for Northern Territory gas production monitoring.
Data source: AEMO Gas Bulletin Board via nemweb.com.au
"""

import os

os.environ.setdefault("STREAMLIT_SERVER_FILE_WATCHER_TYPE", "none")

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import requests
import zipfile
import io
import logging
from io import StringIO
from datetime import datetime, timedelta
# Database imports
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, UniqueConstraint
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.sql import func
from sqlalchemy.pool import NullPool

# Local configuration
from nt_config import (
    NT_FIELDS, BASINS, FIELD_DISPLAY_ORDER,
    get_field_for_facility, get_producing_fields, 
    get_awaiting_fields, get_field_color
)

logger = logging.getLogger(__name__)

# Page configuration
st.set_page_config(
    page_title="NT Gas Production Monitor",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom CSS for professional styling
st.markdown("""
<style>
    /* Main container - reduced padding by 35% */
    .block-container {
        padding-top: 1.3rem;
        padding-bottom: 1.3rem;
        max-width: 1200px;
    }
    
    /* Title styling - tighter spacing */
    h1 {
        color: #1a1a1a;
        font-weight: 700;
        margin-bottom: 0.25rem;
        font-size: 2rem;
    }
    
    h2 {
        color: #2c3e50;
        font-weight: 600;
        margin-top: 1.2rem;
        margin-bottom: 0.6rem;
        border-bottom: 1px solid #e8e8e8;
        padding-bottom: 0.3rem;
        font-size: 1.4rem;
    }
    
    h3 {
        color: #34495e;
        font-weight: 600;
        margin-top: 0;
        margin-bottom: 0.3rem;
        font-size: 1.1rem;
    }
    
    /* Prominent primary KPI */
    .primary-kpi [data-testid="stMetricValue"] {
        font-size: 3.5rem;
        font-weight: 800;
        color: #0066cc;
    }
    
    .primary-kpi [data-testid="stMetricLabel"] {
        font-size: 1.1rem;
        font-weight: 600;
        color: #333;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }

    [data-testid="stHorizontalBlock"] > [data-testid="column"]:first-child [data-testid="stMetricValue"] {
        font-size: 3.5rem;
        font-weight: 800;
        color: #0066cc;
    }

    [data-testid="stHorizontalBlock"] > [data-testid="column"]:first-child [data-testid="stMetricLabel"] {
        font-size: 1.1rem;
        font-weight: 600;
        color: #333;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    
    /* Standard metrics */
    [data-testid="stMetricValue"] {
        font-size: 1.8rem;
        font-weight: 700;
    }
    
    [data-testid="stMetricLabel"] {
        font-size: 0.85rem;
        font-weight: 500;
        color: #666;
    }
    
    [data-testid="stMetricDelta"] {
        font-size: 0.8rem;
    }
    
    /* Compact field cards */
    .field-card {
        background: #fafafa;
        padding: 1rem;
        border-radius: 6px;
        border: 1px solid #e0e0e0;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        margin-bottom: 0.75rem;
        transition: all 0.2s ease;
    }
    
    .field-card:hover {
        border-color: #0066cc;
        box-shadow: 0 2px 6px rgba(0,102,204,0.1);
    }
    
    .field-card-header {
        font-size: 1rem;
        font-weight: 700;
        color: #1a1a1a;
        margin-bottom: 0.2rem;
    }
    
    .field-card-meta {
        font-size: 0.75rem;
        color: #888;
        margin-bottom: 0.6rem;
    }
    
    .field-card-value {
        font-size: 1.6rem;
        font-weight: 700;
        color: #0066cc;
    }
    
    .field-card-avg {
        font-size: 0.75rem;
        color: #666;
    }
    
    /* Trend indicator pill */
    .trend-pill {
        display: inline-block;
        padding: 0.15rem 0.5rem;
        border-radius: 12px;
        font-size: 0.7rem;
        font-weight: 600;
        margin-left: 0.5rem;
    }
    
    .trend-up {
        background: #d4edda;
        color: #155724;
    }
    
    .trend-down {
        background: #f8d7da;
        color: #721c24;
    }
    
    .trend-stable {
        background: #e2e3e5;
        color: #383d41;
    }
    
    /* Period selector buttons */
    .period-selector {
        display: flex;
        gap: 0.5rem;
        justify-content: flex-end;
        margin-bottom: 0.75rem;
    }
    
    .period-btn {
        padding: 0.4rem 0.9rem;
        border-radius: 4px;
        border: 1px solid #d0d0d0;
        background: white;
        cursor: pointer;
        font-size: 0.8rem;
        font-weight: 500;
        color: #555;
        transition: all 0.15s;
    }
    
    .period-btn:hover {
        background: #f5f5f5;
        border-color: #0066cc;
    }
    
    .period-btn-active {
        background: #0066cc;
        color: white;
        border-color: #0066cc;
    }
    
    /* Basin composition bar */
    .basin-bar {
        display: flex;
        height: 60px;
        border-radius: 6px;
        overflow: hidden;
        margin: 0.5rem 0 1rem 0;
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }
    
    .basin-segment {
        display: flex;
        align-items: center;
        justify-content: center;
        color: white;
        font-weight: 600;
        font-size: 0.9rem;
        padding: 0.5rem;
        text-align: center;
        transition: all 0.2s;
    }
    
    .basin-segment:hover {
        filter: brightness(1.1);
    }
    
    .basin-legend {
        display: flex;
        gap: 1.5rem;
        margin-top: 0.5rem;
        font-size: 0.85rem;
    }
    
    .basin-legend-item {
        display: flex;
        align-items: center;
        gap: 0.4rem;
    }
    
    .basin-color-box {
        width: 18px;
        height: 18px;
        border-radius: 3px;
    }
    
    /* Dividers - reduced spacing by 35% */
    hr {
        margin: 1.3rem 0;
        border: none;
        border-top: 1px solid #e8e8e8;
    }
    
    /* Compact sections */
    .stPlotlyChart {
        margin-bottom: 0.5rem;
    }
    
    /* Footer styling */
    .dashboard-footer {
        margin-top: 2rem;
        padding-top: 1rem;
        border-top: 1px solid #e8e8e8;
        font-size: 0.8rem;
        color: #666;
        line-height: 1.5;
    }
    
    .dashboard-footer a {
        color: #0066cc;
        text-decoration: none;
    }
    
    .dashboard-footer a:hover {
        text-decoration: underline;
    }
    
    /* Beetaloo emerging panel */
    .beetaloo-panel {
        background: #f9f9f9;
        border: 1px solid #e0e0e0;
        border-radius: 6px;
        padding: 1rem;
        margin-bottom: 0.75rem;
    }
    
    .beetaloo-panel h4 {
        margin: 0 0 0.3rem 0;
        font-size: 1rem;
        color: #1a1a1a;
    }
    
    .beetaloo-panel .subtitle {
        font-size: 0.75rem;
        color: #888;
        margin-bottom: 0.5rem;
    }
    
    .beetaloo-panel .notice {
        font-size: 0.85rem;
        color: #666;
        font-style: italic;
    }
    
    /* Reduce Streamlit default spacing */
    .element-container {
        margin-bottom: 0.5rem;
    }
</style>
""", unsafe_allow_html=True)

# Database setup
Base = declarative_base()

class GBBRecord(Base):
    """AEMO Gas Bulletin Board record model - reused from existing schema"""
    __tablename__ = 'gbb_records'
    
    id = Column(Integer, primary_key=True)
    gas_date = Column(DateTime)
    facility_name = Column(String(255))
    facility_id = Column(Integer)
    facility_type = Column(String(50))
    demand = Column(Float)
    supply = Column(Float)
    transfer_in = Column(Float)
    transfer_out = Column(Float)
    held_in_storage = Column(Float)
    cushion_gas_storage = Column(Float)
    state = Column(String(10))
    location_name = Column(String(255))
    location_id = Column(Integer)
    last_updated = Column(DateTime)
    imported_date = Column(DateTime, default=func.now())
    
    # Unique constraint for upsert logic
    __table_args__ = (
        UniqueConstraint('gas_date', 'facility_id', name='uix_gas_date_facility'),
    )

@st.cache_resource
def get_database_connection():
    """Get database connection using DATABASE_URL environment variable"""
    try:
        database_url = os.environ.get('DATABASE_URL')
        if not database_url:
            raise RuntimeError("DATABASE_URL environment variable not configured")
        
        engine_options = {}
        if database_url.startswith("sqlite"):
            engine_options["connect_args"] = {
                "timeout": 60,
                "check_same_thread": False
            }
            engine_options["poolclass"] = NullPool
        elif database_url.startswith("postgresql"):
            engine_options["connect_args"] = {
                "connect_timeout": 15,
                "sslmode": "require"
            }
            engine_options["pool_pre_ping"] = True

        engine = create_engine(database_url, **engine_options)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        return engine, Session
    except Exception as e:
        raise RuntimeError(f"Database connection failed: {str(e)}") from e

# ============================================================================
# Data Fetching and Processing
# ============================================================================

@st.cache_data(ttl=900)
def fetch_aemo_data():
    """
    Fetch and merge AEMO historical and recent Actual Flow data.

    The Last31 dataset has priority for duplicate (GasDate, FacilityId)
    records because it can contain more recent AEMO revisions.
    """
    historical_url = "https://nemweb.com.au/Reports/Current/GBB/GasBBActualFlowStorage.zip"
    recent_url = "https://nemweb.com.au/Reports/Current/GBB/GasBBActualFlowStorageLast31.CSV"

    def filter_nt_production(frame):
        if frame is None or frame.empty:
            return pd.DataFrame()
        frame = frame[
            frame['State'].eq('NT') & frame['FacilityType'].eq('PROD')
        ].copy()
        frame['GasDate'] = pd.to_datetime(frame['GasDate'], errors='coerce')
        if 'LastUpdated' in frame.columns:
            frame['LastUpdated'] = pd.to_datetime(frame['LastUpdated'], errors='coerce')
        else:
            frame['LastUpdated'] = pd.NaT
        return frame.dropna(subset=['GasDate', 'FacilityId'])

    historical = None
    recent = None

    try:
        response = requests.get(historical_url, timeout=60, stream=True)
        response.raise_for_status()
        downloaded_data = b''.join(
            chunk for chunk in response.iter_content(chunk_size=8192) if chunk
        )
        with zipfile.ZipFile(io.BytesIO(downloaded_data)) as zip_file:
            csv_files = [name for name in zip_file.namelist() if name.lower().endswith('.csv')]
            if not csv_files:
                raise ValueError("No CSV files found in historical AEMO archive")
            with zip_file.open(csv_files[0]) as csv_file:
                chunks = [
                    filter_nt_production(chunk)
                    for chunk in pd.read_csv(csv_file, chunksize=50000)
                ]
            historical = pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
            logger.info(
                "AEMO historical ZIP latest NT gas date: %s",
                historical['GasDate'].max() if not historical.empty else None
            )
    except Exception as exc:
        st.warning(f"Historical AEMO Actual Flow archive unavailable: {exc}")
        logger.warning("Historical AEMO download failed: %s", exc)

    try:
        response = requests.get(recent_url, timeout=60)
        response.raise_for_status()
        recent_raw = pd.read_csv(io.StringIO(response.text))
        recent_raw['GasDate'] = pd.to_datetime(recent_raw['GasDate'], errors='coerce')
        if 'LastUpdated' in recent_raw.columns:
            recent_raw['LastUpdated'] = pd.to_datetime(recent_raw['LastUpdated'], errors='coerce')
        else:
            recent_raw['LastUpdated'] = pd.NaT

        recent_nt = recent_raw[
            recent_raw["State"].eq("NT") &
            recent_raw["FacilityType"].eq("PROD")
        ].copy()

        recent = filter_nt_production(recent_raw)
        logger.info(
            "AEMO Last31 latest NT gas date: %s",
            recent['GasDate'].max() if not recent.empty else None
        )
    except Exception as exc:
        st.warning(f"AEMO Last31 Actual Flow data unavailable; using historical data: {exc}")
        logger.warning("AEMO Last31 download failed: %s", exc)

    available = [
        frame.assign(_source_priority=priority, _source_name=source_name)
        for frame, priority, source_name in (
            (historical, 0, 'historical ZIP'),
            (recent, 1, 'Last31 CSV')
        )
        if frame is not None and not frame.empty
    ]
    if not available:
        st.error("Failed to fetch both AEMO Actual Flow datasets")
        return None

    combined = pd.concat(available, ignore_index=True)
    duplicate_mask = combined.duplicated(subset=['GasDate', 'FacilityId'], keep=False)
    duplicate_sources = combined.loc[duplicate_mask, '_source_name'].value_counts().to_dict()
    combined = combined.sort_values(
        ['GasDate', 'FacilityId', '_source_priority', 'LastUpdated'],
        na_position='first'
    )
    replacements = int(duplicate_mask.sum() / 2) if duplicate_mask.any() else 0
    combined = combined.drop_duplicates(
        subset=['GasDate', 'FacilityId'], keep='last'
    )
    merged_latest = combined['GasDate'].max()
    logger.info(
        "AEMO merged latest NT gas date: %s; duplicate groups resolved: %d; "
        "duplicate source rows: %s",
        merged_latest,
        replacements,
        duplicate_sources
    )
    logger.info(
        "AEMO merged latest gas date by tracked facility: %s",
        combined[combined['FacilityName'].str.contains(
            r'Mereenie|Palm Valley|Sturt Plateau|SPCF|Blacktip|Yelcherr|Yellerr',
            case=False,
            na=False
        )]
        .groupby('FacilityName')['GasDate'].max().to_dict()
    )
    return combined.drop(columns=['_source_priority', '_source_name'])

def normalize_aemo_data(df):
    """
    Normalize AEMO column names to database schema.
    Preserves nulls and does not fabricate data.
    """
    if df is None or df.empty:
        return None
    
    column_mapping = {
        'GasDate': 'gas_date',
        'FacilityName': 'facility_name',
        'FacilityId': 'facility_id',
        'FacilityType': 'facility_type',
        'Demand': 'demand',
        'Supply': 'supply',
        'TransferIn': 'transfer_in',
        'TransferOut': 'transfer_out',
        'HeldInStorage': 'held_in_storage',
        'CushionGasStorage': 'cushion_gas_storage',
        'State': 'state',
        'LocationName': 'location_name',
        'LocationId': 'location_id',
        'LastUpdated': 'last_updated'
    }
    
    normalized_df = df.rename(columns={k: v for k, v in column_mapping.items() if k in df.columns})
    normalized_df['imported_date'] = datetime.now()
    
    # Ensure string columns are properly sized
    if 'facility_name' in normalized_df.columns:
        normalized_df['facility_name'] = normalized_df['facility_name'].astype(str).str[:255]
    if 'facility_type' in normalized_df.columns:
        normalized_df['facility_type'] = normalized_df['facility_type'].astype(str).str[:50]
    if 'state' in normalized_df.columns:
        normalized_df['state'] = normalized_df['state'].astype(str).str[:10]
    if 'location_name' in normalized_df.columns:
        normalized_df['location_name'] = normalized_df['location_name'].astype(str).str[:255]
    
    return normalized_df

def upsert_gbb_data(engine, session_maker, df):
    """
    Upsert GBB data to database using (gas_date, facility_id) as unique key.
    Does NOT delete existing records - preserves history.
    Works with both SQLite and PostgreSQL.
    """
    if df is None or df.empty:
        return False
    
    try:
        session = session_maker()

        if session.query(GBBRecord).count() == 0:
            batch_size = 1000
            for start in range(0, len(df), batch_size):
                records = df.iloc[start:start + batch_size].to_dict('records')
                session.bulk_insert_mappings(
                    GBBRecord,
                    records
                )
                session.commit()
            session.close()
            return True

        records = df.to_dict('records')
        
        for record in records:
            # Check if record exists
            existing = session.query(GBBRecord).filter_by(
                gas_date=record['gas_date'],
                facility_id=record['facility_id']
            ).first()
            
            if existing:
                # Update existing record
                for key, value in record.items():
                    if key != 'id':  # Don't update primary key
                        setattr(existing, key, value)
            else:
                # Insert new record
                new_record = GBBRecord(**record)
                session.add(new_record)
        
        session.commit()
        session.close()
        return True
        
    except Exception as e:
        if session:
            session.rollback()
            session.close()
        st.error(f"Failed to upsert data: {str(e)}")
        return False

def get_nt_data(session_maker):
    """
    Retrieve NT facility data from database and map to field names.
    Returns DataFrame with field mapping applied.
    """
    session = None
    try:
        session = session_maker()
        records = session.query(GBBRecord).filter(
            GBBRecord.state == 'NT'
        ).all()
        session.close()
        
        if not records:
            return pd.DataFrame()

        # Convert to DataFrame
        data = []
        for record in records:
            data.append({
                'gas_date': record.gas_date,
                'facility_name': record.facility_name,
                'facility_id': record.facility_id,
                'facility_type': record.facility_type,
                'supply': record.supply,
                'demand': record.demand,
                'transfer_in': record.transfer_in,
                'state': record.state,
                'last_updated': record.last_updated
            })
        
        df = pd.DataFrame(data)
        
        # Map facilities to NT fields
        df['nt_field'] = df['facility_name'].apply(get_field_for_facility)
        
        # Filter to NT fields only
        nt_df = df[df['nt_field'].notna()].copy()
        
        return nt_df
        
    except Exception as e:
        if session:
            session.close()
        st.error(f"Failed to retrieve NT data: {str(e)}")
        return pd.DataFrame()

@st.cache_data(ttl=300, show_spinner=False)
def get_cached_nt_data(database_url):
    """Cache the mapped NT dataframe between chart interactions."""
    engine_options = {}
    if database_url.startswith("sqlite"):
        engine_options["connect_args"] = {
            "timeout": 60,
            "check_same_thread": False
        }
        engine_options["poolclass"] = NullPool

    engine = create_engine(database_url, **engine_options)
    session_maker = sessionmaker(bind=engine)
    try:
        return get_nt_data(session_maker)
    finally:
        engine.dispose()

@st.cache_data(ttl=900, show_spinner=False)
def fetch_aemo_reporting_status(nt_df, latest_nt_date):
    """Load AEMO missing/late reports for reporting diagnostics only."""
    missing_url = "https://nemweb.com.au/Reports/Current/GBB/GasBBMissingActualFlowAndStorage.CSV"
    late_url = "https://nemweb.com.au/Reports/Current/GBB/GasBBLateActualFlowAndStorage.zip"

    tracked = (
        nt_df.sort_values('gas_date')
        .groupby('nt_field', as_index=False)
        .tail(1)[['nt_field', 'facility_id', 'facility_name', 'gas_date']]
        .rename(columns={
            'nt_field': 'Dashboard Source',
            'facility_id': 'FacilityId',
            'facility_name': 'AEMO Facility Name',
            'gas_date': 'Latest Published Actual Flow'
        })
    )
    if tracked.empty:
        return pd.DataFrame()

    def read_missing():
        response = requests.get(missing_url, timeout=60)
        response.raise_for_status()
        frame = pd.read_csv(io.StringIO(response.text))
        frame['GasDate'] = pd.to_datetime(frame['GasDate'], errors='coerce')
        return frame

    def read_late():
        response = requests.get(late_url, timeout=60)
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            csv_name = next(name for name in archive.namelist() if name.lower().endswith('.csv'))
            with archive.open(csv_name) as csv_file:
                frame = pd.read_csv(csv_file)
        frame['GasDate'] = pd.to_datetime(frame['GasDate'], errors='coerce')
        return frame

    try:
        missing = read_missing()
    except Exception as exc:
        logger.warning("AEMO missing Actual Flow report unavailable: %s", exc)
        missing = pd.DataFrame(columns=['GasDate', 'FacilityId', 'FacilityName'])

    try:
        late = read_late()
    except Exception as exc:
        logger.warning("AEMO late Actual Flow report unavailable: %s", exc)
        late = pd.DataFrame(columns=['GasDate', 'FacilityId', 'FacilityName'])

    rows = []
    for source in tracked.itertuples(index=False):
        missing_rows = missing[missing['FacilityId'].eq(source.FacilityId)]
        late_rows = late[late['FacilityId'].eq(source.FacilityId)]
        current_gap_start = source[3].normalize() + pd.Timedelta(days=1)
        missing_dates = sorted(
            missing_rows.loc[
                missing_rows['GasDate'].between(current_gap_start, latest_nt_date),
                'GasDate'
            ].dropna().dt.normalize().unique()
        )
        late_dates = sorted(
            late_rows.loc[
                late_rows['GasDate'].between(current_gap_start, latest_nt_date),
                'GasDate'
            ].dropna().dt.normalize().unique()
        )
        lag_days = (latest_nt_date - source[3]).days

        if missing_dates:
            status = 'Missing reports'
        elif late_dates:
            status = 'Late according to AEMO'
        elif lag_days == 0:
            status = 'Current'
        else:
            status = 'Lagging - no later Actual Flow data currently published'

        rows.append({
            'Dashboard Source': source[0],
            'AEMO Facility Name': source[2],
            'Latest Published Actual Flow': source[3].strftime('%d %b %Y'),
            'Reporting Lag (days)': lag_days,
            'Missing gas dates identified by AEMO': ', '.join(
                pd.Timestamp(date).strftime('%d %b') for date in missing_dates
            ) or 'None identified',
            'Late gas dates identified by AEMO': ', '.join(
                pd.Timestamp(date).strftime('%d %b') for date in late_dates
            ) or 'None identified',
            'Status': status
        })

    return pd.DataFrame(rows)

# ============================================================================
# Metrics Calculation
# ============================================================================

def calculate_nt_metrics(nt_df):
    """
    Calculate NT production metrics from facility data.
    Returns dict with current, historical, and field-level metrics.
    """
    if nt_df.empty:
        return {
            'latest_date': None,
            'common_date': None,
            'included_sources': [],
            'latest_reported_total': None,
            'latest_reported_by_source': {},
            'total_current': 0,
            'total_complete': False,
            'change_vs_prev': 0,
            'avg_7d_total': None,
            'avg_30d_total': None,
            'complete_days': pd.DataFrame(),
            'fields': {},
            'daily_total': pd.DataFrame(),
            'daily_by_field': pd.DataFrame(),
            'qc_table': pd.DataFrame()
        }
    
    # Aggregate each mapped source by gas date without filling missing dates.
    latest_date = nt_df['gas_date'].max()
    source_names = [
        field for field in FIELD_DISPLAY_ORDER
        if field in set(nt_df['nt_field'].dropna().unique())
    ]
    source_daily = (
        nt_df.dropna(subset=['gas_date', 'supply'])
        .groupby(['gas_date', 'nt_field'], as_index=False)['supply']
        .sum()
        .sort_values(['gas_date', 'nt_field'])
    )
    source_counts = source_daily.groupby('gas_date')['nt_field'].nunique()
    complete_dates = source_counts[source_counts == len(source_names)].index
    daily_total = (
        source_daily[source_daily['gas_date'].isin(complete_dates)]
        .groupby('gas_date', as_index=False)['supply']
        .sum()
        .rename(columns={'supply': 'total_supply'})
        .sort_values('gas_date')
    )
    common_date = daily_total['gas_date'].max() if not daily_total.empty else None
    
    # Calculate field-level averages
    field_metrics = {}
    latest_reported_by_source = {}
    for field in get_producing_fields():
        field_daily = source_daily[source_daily['nt_field'] == field].copy()
        
        if not field_daily.empty:
            latest_row = field_daily.iloc[-1]
            latest_supply = latest_row['supply']
            field_latest_date = latest_row['gas_date']
            reporting_lag = (latest_date - field_latest_date).days
            latest_reported_by_source[field] = {
                'value': latest_supply,
                'date': field_latest_date
            }
            
            # 7-day average
            last_7_days = field_daily.tail(7)
            avg_7d = last_7_days['supply'].mean() if len(last_7_days) > 0 else 0
            
            # 30-day average
            last_30_days = field_daily.tail(30)
            avg_30d = last_30_days['supply'].mean() if len(last_30_days) > 0 else 0
            
            # Trend (compare current to 7-day avg)
            trend = "stable"
            if avg_7d > 0:
                pct_change = ((latest_supply - avg_7d) / avg_7d) * 100
                if pct_change > 5:
                    trend = "up"
                elif pct_change < -5:
                    trend = "down"
            
            field_metrics[field] = {
                'current': latest_supply,
                'latest_date': field_latest_date,
                'reporting_lag': reporting_lag,
                'avg_7d': avg_7d,
                'avg_30d': avg_30d,
                'trend': trend,
                'has_data': True
            }
        else:
            field_metrics[field] = {
                'current': 0,
                'latest_date': None,
                'reporting_lag': None,
                'avg_7d': 0,
                'avg_30d': 0,
                'trend': "stable",
                'has_data': False
            }
    
    # Headline totals use only a common gas date across all included sources.
    total_complete = common_date is not None
    total_current = (
        daily_total.loc[daily_total['gas_date'].eq(common_date), 'total_supply'].iloc[0]
        if total_complete else None
    )
    latest_reported_total = sum(
        source_data['value'] for source_data in latest_reported_by_source.values()
    )
    
    # Previous day comparison
    if len(daily_total) > 1:
        prev_day = daily_total.iloc[-2]['total_supply']
        change_vs_prev = total_current - prev_day
    else:
        change_vs_prev = 0
    
    # 7-day average total
    complete_days = daily_total.tail(7).copy()
    avg_7d_total = (
        complete_days['total_supply'].mean()
        if len(complete_days) == 7 else None
    )
    avg_30d_total = (
        daily_total.tail(30)['total_supply'].mean()
        if len(daily_total) >= 30 else None
    )

    qc_rows = []
    for source_name in source_names:
        source_data = source_daily[source_daily['nt_field'] == source_name]
        latest_source = source_data.iloc[-1]
        qc_rows.append({
            'Production Source': source_name,
            'Basin': NT_FIELDS[source_name]['basin'],
            'Latest AEMO Gas Date': latest_source['gas_date'],
            'Latest Reported Production (TJ/d)': latest_source['supply'],
            '7-Day Average (TJ/d)': source_data.tail(7)['supply'].mean(),
            '30-Day Average (TJ/d)': source_data.tail(30)['supply'].mean(),
            'Reporting Lag (days)': (latest_date - latest_source['gas_date']).days
        })
    
    return {
        'latest_date': latest_date,
        'common_date': common_date,
        'included_sources': source_names,
        'latest_reported_total': latest_reported_total,
        'latest_reported_by_source': latest_reported_by_source,
        'total_current': total_current,
        'total_complete': total_complete,
        'change_vs_prev': change_vs_prev,
        'avg_7d_total': avg_7d_total,
        'avg_30d_total': avg_30d_total,
        'complete_days': complete_days,
        'fields': field_metrics,
        'daily_total': daily_total,
        'daily_by_field': source_daily,
        'qc_table': pd.DataFrame(qc_rows)
    }

def validate_nt_metrics(metrics):
    """Check headline invariants before rendering public KPI values."""
    errors = []
    if metrics['common_date'] is None:
        errors.append('No common headline gas date exists')
    else:
        common_rows = metrics['daily_by_field'][
            metrics['daily_by_field']['gas_date'].eq(metrics['common_date'])
        ]
        if set(common_rows['nt_field']) != set(metrics['included_sources']):
            errors.append('Common headline date does not contain every required source')
        if common_rows['supply'].isna().any():
            errors.append('A missing source value entered the headline total')
        if abs(common_rows['supply'].sum() - metrics['total_current']) > 1e-9:
            errors.append('Displayed NT total does not equal same-date source sum')

    if metrics['avg_7d_total'] is not None:
        complete_days = metrics['complete_days']
        if len(complete_days) != 7:
            errors.append('7-day average does not contain exactly seven complete days')
        if len(complete_days) and complete_days['total_supply'].isna().any():
            errors.append('A missing value entered the 7-day average')
        for gas_date in complete_days['gas_date']:
            day_sources = set(
                metrics['daily_by_field'].loc[
                    metrics['daily_by_field']['gas_date'].eq(gas_date),
                    'nt_field'
                ]
            )
            if day_sources != set(metrics['included_sources']):
                errors.append(f'7-day average date {gas_date.date()} is missing a required source')

    return errors

def calculate_basin_metrics(nt_df, common_date=None):
    """
    Calculate basin-level performance metrics from NT facility data.
    Returns dict with basin-level current, averages, trends, and stability metrics.
    """
    if nt_df.empty:
        return {}
    
    # Map each row to its basin
    nt_df_copy = nt_df.copy()
    nt_df_copy['basin'] = nt_df_copy['nt_field'].map(
        lambda field: NT_FIELDS.get(field, {}).get('basin')
    )
    
    # Filter to rows with valid basin mapping
    basin_df = nt_df_copy[nt_df_copy['basin'].notna()].copy()
    
    if basin_df.empty:
        return {}
    
    # Aggregate to basin-level daily production
    daily_basin = basin_df.groupby(['gas_date', 'basin'])['supply'].sum().reset_index()
    daily_basin = daily_basin.sort_values('gas_date')
    
    # Basin point-in-time metrics use the common NT date when one exists.
    basin_latest_dates = daily_basin.groupby('basin')['gas_date'].max().to_dict()
    comparison_date = common_date if common_date is not None else None
    basin_current_values = {
        basin_name: daily_basin.loc[
            daily_basin['basin'].eq(basin_name)
            & daily_basin['gas_date'].eq(
                comparison_date if comparison_date is not None else basin_latest_dates[basin_name]
            ),
            'supply'
        ].sum()
        for basin_name in basin_latest_dates
    }
    total_nt_current = sum(basin_current_values.values())
    
    basin_metrics = {}
    
    for basin_name in BASINS.keys():
        basin_data = daily_basin[daily_basin['basin'] == basin_name].copy()
        
        if basin_data.empty:
            # Basin has no production data
            basin_metrics[basin_name] = {
                'status': 'awaiting_data',
                'comparison_date': common_date,
                'current': 0,
                'avg_30d': 0,
                'nt_share': 0,
                'change_90d': None,
                'vs_peak_30d': None,
                'stability': None,
                'producing_days': 0,
                'total_days': 0
            }
            continue
        
        # 1. Current production. Use the common NT date so basin comparisons align.
        latest_date = common_date if common_date is not None else basin_latest_dates[basin_name]
        current_data = basin_data[basin_data['gas_date'] == latest_date]
        current = current_data['supply'].iloc[0] if not current_data.empty else None
        
        # 2. 30-day average
        last_30_days = basin_data.tail(30)
        avg_30d = last_30_days['supply'].mean() if len(last_30_days) > 0 else 0
        
        # 3. NT Share
        nt_share = (current / total_nt_current * 100) if current is not None and total_nt_current > 0 else None
        
        # 4. 90-day change (30d avg now vs 30d avg 90 days ago)
        change_90d = None
        if len(basin_data) >= 120:  # Need at least 120 days for this calc
            # Current 30d average (already calculated)
            # 30d average ending 90 days ago
            cutoff_date = latest_date - pd.Timedelta(days=90)
            historical_data = basin_data[basin_data['gas_date'] <= cutoff_date]
            if len(historical_data) >= 30:
                prev_30d_avg = historical_data.tail(30)['supply'].mean()
                if prev_30d_avg > 0:
                    change_90d = ((avg_30d / prev_30d_avg - 1) * 100)
        
        # 5. vs Peak 30d
        vs_peak_30d = None
        peak_30d_value = None
        peak_30d_date = None
        
        if len(basin_data) >= 30:
            # Calculate rolling 30d average for entire history
            basin_data['rolling_30d'] = basin_data['supply'].rolling(window=30, min_periods=30).mean()
            peak_row = basin_data.loc[basin_data['rolling_30d'].idxmax()]
            peak_30d_value = peak_row['rolling_30d']
            peak_30d_date = peak_row['gas_date']
            
            if peak_30d_value > 0:
                vs_peak_30d = ((avg_30d / peak_30d_value - 1) * 100)
                logger.info(
                    "%s Basin vs Peak 30d: current=%.3f TJ/d, peak=%.3f TJ/d, "
                    "peak period ending=%s, result=%.2f%%",
                    basin_name,
                    avg_30d,
                    peak_30d_value,
                    peak_30d_date.strftime('%Y-%m-%d'),
                    vs_peak_30d
                )
        
        # 6. Supply Stability (coefficient of variation over last 30 days)
        stability = None
        cv = None
        producing_days = 0
        
        if len(last_30_days) >= 7:  # Need reasonable sample
            mean_prod = last_30_days['supply'].mean()
            std_prod = last_30_days['supply'].std()
            
            # Count producing days (production > 0)
            producing_days = (last_30_days['supply'] > 0).sum()
            total_days = len(last_30_days)
            
            if mean_prod > 0:
                cv = (std_prod / mean_prod) * 100
                
                # Classify stability
                if cv < 5:
                    stability = "High"
                elif cv < 15:
                    stability = "Moderate"
                else:
                    stability = "Variable"
        
        # Determine status
        if len(basin_data) < 7:
            status = 'establishing'
        else:
            status = 'active'
        
        basin_metrics[basin_name] = {
            'status': status,
            'comparison_date': latest_date,
            'current': current,
            'avg_30d': avg_30d,
            'nt_share': nt_share,
            'change_90d': change_90d,
            'vs_peak_30d': vs_peak_30d,
            'peak_30d_value': peak_30d_value,
            'peak_30d_date': peak_30d_date,
            'stability': stability,
            'cv': cv,
            'producing_days': producing_days,
            'total_days': len(last_30_days)
        }
    
    return basin_metrics

# ============================================================================
# Rendering Functions
# ============================================================================

def render_header(metrics):
    """Render page header with title and data freshness"""
    st.title("NT Gas Production Monitor")
    
    if metrics['latest_date']:
        date_str = metrics['latest_date'].strftime("%d %b %Y")
        st.caption(
            "Northern Territory gas production at a glance • "
            f"Latest AEMO data: {date_str} • Reporting dates vary by facility"
        )
        latest_by_basin = {}
        for source_name, source_metrics in metrics['fields'].items():
            if source_metrics.get('has_data'):
                basin = NT_FIELDS[source_name]['basin']
                latest_by_basin[basin] = max(
                    latest_by_basin.get(basin, source_metrics['latest_date']),
                    source_metrics['latest_date']
                )
        lag_summary = '; '.join(
            f"{basin} data available through {date.strftime('%d %b %Y')}"
            for basin, date in latest_by_basin.items()
        )
        if lag_summary:
            st.caption(f"Reporting lag: {lag_summary}.")
    else:
        st.caption("Northern Territory gas production at a glance")

def render_headline_kpi(metrics):
    """Render main production KPI with prominent total and supporting metrics"""
    col1, col2, col3 = st.columns([2.3, 2.3, 1.4])
    
    with col1:
        st.metric(
            label="Latest Reported NT Production",
            value=(
                f"{metrics['latest_reported_total']:.1f} TJ/d"
                if metrics['latest_reported_total'] is not None else "N/A"
            )
        )
        st.caption("Latest available values by source - reporting dates vary")
        st.caption("Source dates: see Data Freshness")

    with col2:
        # Prominent aligned KPI
        delta_text = None
        if (
            metrics['total_complete']
            and metrics['avg_7d_total'] is not None
            and metrics['avg_7d_total'] > 0
        ):
            change_vs_7d = metrics['total_current'] - metrics['avg_7d_total']
            delta_text = f"{change_vs_7d:+.1f} TJ/d vs 7-day avg"
        
        st.metric(
            label="Latest Fully Aligned NT Production",
            value=(
                f"{metrics['total_current']:.1f} TJ/d"
                if metrics['total_complete']
                else "Incomplete latest-day reporting"
            ),
            delta=delta_text
        )
        if metrics['common_date'] is not None:
            st.caption(f"Common gas date: {metrics['common_date'].strftime('%d %b %Y')}")
        else:
            st.caption("No common gas date available across included sources")
    
    with col3:
        st.metric(
            label="7-Day Average",
            value=(
                f"{metrics['avg_7d_total']:.1f} TJ/d"
                if metrics['avg_7d_total'] is not None else "N/A"
            )
        )
        if metrics['avg_7d_total'] is None:
            st.caption("Insufficient aligned reporting")
        producing_count = len([f for f, m in metrics['fields'].items() if m['has_data']])
        st.caption(f"Tracked sources: {producing_count}")

def render_field_cards(metrics, nt_df):
    """Render compact professional field production cards"""
    st.markdown("---")
    st.subheader("Production by Field / Facility")
    
    producing_fields = get_producing_fields()
    cols = st.columns(len(producing_fields))
    
    for i, field_name in enumerate(producing_fields):
        with cols[i]:
            field_config = NT_FIELDS[field_name]
            field_metrics = metrics['fields'].get(field_name, {})
            
            if field_metrics.get('has_data', False):
                # Calculate trend indicator
                trend = field_metrics['trend']
                if trend == 'up':
                    trend_html = '<span class="trend-pill trend-up">↑ Production Rising</span>'
                elif trend == 'down':
                    trend_html = '<span class="trend-pill trend-down">↓ Production Falling</span>'
                else:
                    trend_html = '<span class="trend-pill trend-stable">→ Production Stable</span>'

                data_to = field_metrics['latest_date'].strftime('%d %b %Y')
                lag = field_metrics['reporting_lag']
                lag_html = (
                    f'<br>Reporting lag: {lag} days'
                    if lag > 2 else ''
                )
                
                card_html = f"""
                <div class="field-card">
                    <div class="field-card-header">{field_name} {trend_html}</div>
                    <div class="field-card-meta">{field_config['basin']} Basin</div>
                    <div class="field-card-value">{field_metrics['current']:.1f} <span style="font-size: 0.6em; color: #666;">TJ/d</span></div>
                    <div class="field-card-avg">7-day: {field_metrics['avg_7d']:.1f} TJ/d • 30-day: {field_metrics['avg_30d']:.1f} TJ/d</div>
                    <div class="field-card-meta">Facility-specific reporting windows</div>
                    <div class="field-card-meta">Data to: {data_to}{lag_html}</div>
                </div>
                """
                st.markdown(card_html, unsafe_allow_html=True)
            else:
                # No data available (shouldn't happen for producing fields but handle gracefully)
                card_html = f"""
                <div class="field-card">
                    <div class="field-card-header">{field_name}</div>
                    <div class="field-card-meta">{field_config['basin']} Basin</div>
                    <div class="field-card-avg" style="color: #999; font-style: italic;">No AEMO data available</div>
                </div>
                """
                st.markdown(card_html, unsafe_allow_html=True)

def render_data_freshness(metrics, nt_df):
    """Render source-level reporting dates and numerical reconciliation details."""
    with st.expander("Data Freshness"):
        st.caption(
            "Recent observations are sourced from AEMO's Last31 Actual Flow dataset; "
            "historical observations are sourced from the full Actual Flow archive."
        )
        st.caption(
            "AEMO Reporting Status uses AEMO's Missing Actual Flow and Storage and "
            "Late Actual Flow and Storage reports. These diagnostics do not alter production data."
        )
        reporting_status = fetch_aemo_reporting_status(
            nt_df,
            metrics['latest_date']
        )
        if not reporting_status.empty:
            st.markdown("**AEMO Reporting Status**")
            st.dataframe(reporting_status, hide_index=True, width="stretch")
        if metrics['qc_table'].empty:
            st.info("No production source data available")
            return

        freshness = metrics['qc_table'][[
            'Production Source', 'Basin', 'Latest AEMO Gas Date',
            'Reporting Lag (days)'
        ]].copy()
        freshness['Latest AEMO Gas Date'] = freshness['Latest AEMO Gas Date'].dt.strftime('%d %b %Y')
        freshness['Reporting Lag'] = freshness['Reporting Lag (days)'].apply(
            lambda lag: 'Current' if lag == 0 else f'{lag} days'
        )
        st.dataframe(
            freshness[['Production Source', 'Basin', 'Latest AEMO Gas Date', 'Reporting Lag']],
            hide_index=True,
            width="stretch"
        )

        st.markdown("**Production source QC**")
        qc_display = metrics['qc_table'].copy()
        qc_display['Latest AEMO Gas Date'] = qc_display['Latest AEMO Gas Date'].dt.strftime('%d %b %Y')
        st.dataframe(qc_display, hide_index=True, width="stretch")

        st.markdown("**Data lineage diagnostics**")
        lineage_rows = []
        for source_name in metrics['included_sources']:
            source_df = nt_df[nt_df['nt_field'].eq(source_name)]
            latest_row = source_df.sort_values('gas_date').iloc[-1]
            lineage_rows.append({
                'Dashboard Name': source_name,
                'Raw AEMO Facility Name': ', '.join(sorted(source_df['facility_name'].dropna().unique())),
                'Database Table/Source': 'gbb_records (mapped NT PROD)',
                'State': latest_row['state'],
                'Latest Date': latest_row['gas_date'].strftime('%d %b %Y'),
                'Latest Value': latest_row['supply'],
                'Record Count': len(source_df)
            })
        st.dataframe(pd.DataFrame(lineage_rows), hide_index=True, width="stretch")

        if metrics['total_complete']:
            common_rows = metrics['daily_by_field'][
                metrics['daily_by_field']['gas_date'].eq(metrics['common_date'])
            ]
            source_values = common_rows.set_index('nt_field')['supply']
            summed_total = source_values.sum()
            st.caption(
                f"Latest AEMO gas date: {metrics['latest_date'].strftime('%d %b %Y')} | "
                f"Common gas date: {metrics['common_date'].strftime('%d %b %Y')}"
            )
            st.caption(
                f"Source values on common date: "
                f"{', '.join(f'{name} {source_values.get(name, 0):.1f} TJ/d' for name in metrics['included_sources'])}"
            )
            if metrics['avg_7d_total'] is not None:
                displayed_difference = metrics['total_current'] - metrics['avg_7d_total']
                st.caption(
                    f"Summed NT total: {summed_total:.1f} TJ/d | Displayed NT total: "
                    f"{metrics['total_current']:.1f} TJ/d | 7-day NT average: "
                    f"{metrics['avg_7d_total']:.1f} TJ/d | Difference vs 7-day average: "
                    f"{displayed_difference:+.1f} TJ/d"
                )
                st.caption("Seven complete NT days used for the displayed 7-day average:")
                st.dataframe(
                    metrics['complete_days'].rename(columns={
                        'gas_date': 'Gas date',
                        'total_supply': 'NT total (TJ/d)'
                    }),
                    hide_index=True,
                    width="stretch"
                )
            else:
                st.caption("7-day NT average: N/A - insufficient aligned reporting")
        else:
            st.warning("No common gas date is available; a mixed-date NT total is not displayed.")
    

def render_nt_history_chart(metrics):
    """Render stacked area chart of NT production history with compact period selector"""
    st.markdown("---")
    st.subheader("NT Gas Production History")
    
    if metrics['daily_by_field'].empty:
        st.warning("No historical data available")
        return
    
    # Compact period selector using radio buttons styled horizontally
    period = st.radio(
        "Time Period",
        ["1M", "3M", "6M", "1Y", "All"],
        index=3,
        horizontal=True,
        label_visibility="collapsed"
    )
    
    # Filter data by period
    df = metrics['daily_by_field'].copy()
    df = df.sort_values('gas_date')
    
    if period == "1M":
        cutoff = datetime.now() - timedelta(days=30)
    elif period == "3M":
        cutoff = datetime.now() - timedelta(days=90)
    elif period == "6M":
        cutoff = datetime.now() - timedelta(days=180)
    elif period == "1Y":
        cutoff = datetime.now() - timedelta(days=365)
    else:
        cutoff = df['gas_date'].min()
    
    df = df[df['gas_date'] >= cutoff]
    
    # Create stacked area chart
    fig = go.Figure()
    
    # Add area trace for each producing field
    for field_name in FIELD_DISPLAY_ORDER:
        if field_name in get_producing_fields():
            field_data = df[df['nt_field'] == field_name].copy()
            
            if not field_data.empty:
                fig.add_trace(go.Scatter(
                    x=field_data['gas_date'],
                    y=field_data['supply'],
                    name=field_name,
                    mode='lines',
                    stackgroup='one',
                    fillcolor=get_field_color(field_name),
                    line=dict(width=0.5, color=get_field_color(field_name))
                ))
    
    # Add total line overlay
    daily_total = df.groupby('gas_date')['supply'].sum().reset_index()
    fig.add_trace(go.Scatter(
        x=daily_total['gas_date'],
        y=daily_total['supply'],
        name='Total NT Production',
        mode='lines',
        line=dict(color='#1a1a1a', width=2.5),
        showlegend=True
    ))
    
    fig.update_layout(
        height=450,
        hovermode='x unified',
        xaxis_title='',
        yaxis_title='Production (TJ/day)',
        margin=dict(t=10, b=40, l=50, r=20),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.01,
            xanchor="right",
            x=1,
            font=dict(size=11)
        ),
        plot_bgcolor='white',
        paper_bgcolor='white',
        xaxis=dict(showgrid=True, gridcolor='#f0f0f0'),
        yaxis=dict(showgrid=True, gridcolor='#f0f0f0')
    )
    
    st.plotly_chart(fig, width='stretch')

def render_basin_composition(metrics):
    """Render compact visual basin-level production breakdown"""
    st.markdown("---")
    st.subheader("Production by Basin")
    
    # Calculate basin totals
    basin_totals = {}
    basin_colors = {
        'Amadeus': '#e67e22',
        'Bonaparte': '#3498db',
        'Beetaloo': '#95a5a6'
    }
    
    snapshot_values = {
        source_name: source_data['value']
        for source_name, source_data in metrics['latest_reported_by_source'].items()
    }

    for basin_name, basin_config in BASINS.items():
        total = sum(snapshot_values.get(field, 0) for field in basin_config['fields'])
        basin_totals[basin_name] = total
    
    total_all = sum(basin_totals.values())
    
    if total_all > 0:
        # Create horizontal composition bar
        bar_segments = []
        for basin_name, total in basin_totals.items():
            if total > 0:
                percentage = (total / total_all) * 100
                color = basin_colors.get(basin_name, '#95a5a6')
                segment = f'<div class="basin-segment" style="flex: {percentage}; background-color: {color};">{basin_name}<br>{total:.1f} TJ/d</div>'
                bar_segments.append(segment)
        
        if bar_segments:
            bar_html = '<div class="basin-bar">' + ''.join(bar_segments) + '</div>'
            st.markdown(bar_html, unsafe_allow_html=True)
        
        # Legend with details - use columns instead of custom HTML
        st.markdown("") 
        cols = st.columns(3)
        
        for i, (basin_name, basin_config) in enumerate(BASINS.items()):
            with cols[i]:
                total = basin_totals[basin_name]
                percentage = (total / total_all * 100) if total_all > 0 else 0
                color = basin_colors.get(basin_name, '#95a5a6')
                
                # Color box and label using markdown
                color_box = f'<div style="display: inline-block; width: 12px; height: 12px; background-color: {color}; border-radius: 2px; margin-right: 6px; vertical-align: middle;"></div>'
                st.markdown(f'{color_box} **{basin_name} Basin**', unsafe_allow_html=True)
                st.caption(f"{total:.1f} TJ/d ({percentage:.0f}% of NT total)")
                basin_dates = [
                    metrics['fields'][field]['latest_date']
                    for field in basin_config['fields']
                    if metrics['fields'].get(field, {}).get('has_data', False)
                ]
                if basin_dates:
                    st.caption(f"Latest report: {max(basin_dates).strftime('%d %b %Y')}")
                
                # List producing fields
                producing = [f for f in basin_config['fields'] 
                            if metrics['fields'].get(f, {}).get('has_data', False)]
                if producing:
                    st.caption(f"Sources: {', '.join(producing)}")
                else:
                    st.caption("No active production")
    else:
        st.info("No production data available for basin breakdown")

def render_basin_performance(basin_metrics):
    """Render basin-level performance comparison metrics"""
    st.markdown("---")
    st.subheader("Basin Performance")
    
    if not basin_metrics:
        st.info("Insufficient data for basin performance analysis")
        return
    
    # Define basin display order
    basin_order = ['Amadeus', 'Bonaparte', 'Beetaloo']
    basin_colors = {
        'Amadeus': '#e67e22',
        'Bonaparte': '#3498db',
        'Beetaloo': '#95a5a6'
    }
    
    # Create performance table
    for basin_name in basin_order:
        if basin_name not in basin_metrics:
            continue
        
        metrics = basin_metrics[basin_name]
        color = basin_colors.get(basin_name, '#666')
        
        # Basin header with color indicator
        st.markdown(f'<div style="margin-top: 1rem; margin-bottom: 0.5rem;"><span style="display: inline-block; width: 4px; height: 18px; background-color: {color}; margin-right: 8px; border-radius: 2px; vertical-align: middle;"></span><strong style="font-size: 1.1rem; color: #1a1a1a;">{basin_name.upper()} BASIN</strong></div>', unsafe_allow_html=True)
        
        # Handle different statuses
        if metrics['status'] == 'awaiting_data':
            st.caption("⏳ Awaiting production data")
            continue
        
        if metrics['status'] == 'establishing':
            st.caption("📊 Establishing baseline (fewer than 7 days of data)")
            if metrics['current'] is not None and metrics['current'] > 0:
                st.caption(f"Current production: {metrics['current']:.1f} TJ/d")
            continue
        
        # Create metric columns
        cols = st.columns([1.2, 1.2, 0.9, 1.3, 1.3, 1.4])
        
        with cols[0]:
            st.metric(
                "Current",
                f"{metrics['current']:.1f} TJ/d"
                if metrics['current'] is not None else "N/A"
            )
            basin_comparison_date = metrics.get('comparison_date')
            if metrics['current'] is not None and basin_comparison_date is not None:
                st.caption(f"Latest report: {basin_comparison_date.strftime('%d %b %Y')}")
        
        with cols[1]:
            st.metric("30d Avg", f"{metrics['avg_30d']:.1f} TJ/d")
        
        with cols[2]:
            st.metric(
                "NT Share",
                f"{metrics['nt_share']:.1f}%"
                if metrics['nt_share'] is not None else "N/A"
            )
        
        with cols[3]:
            # 90d Change
            if metrics['change_90d'] is not None:
                change = metrics['change_90d']
                if abs(change) < 1:
                    direction = "→"
                    color_class = "#666"
                elif change > 0:
                    direction = "↑"
                    color_class = "#155724"
                else:
                    direction = "↓"
                    color_class = "#721c24"
                
                st.markdown(f'<div style="font-size: 0.85rem; font-weight: 500; color: #666; margin-bottom: 0.25rem;">90d Change</div><div style="font-size: 1.8rem; font-weight: 700; color: {color_class};">{direction} {abs(change):.1f}%</div>', unsafe_allow_html=True)
            else:
                st.markdown('<div style="font-size: 0.85rem; font-weight: 500; color: #666; margin-bottom: 0.25rem;">90d Change</div><div style="font-size: 0.9rem; color: #999; font-style: italic;">Insufficient history</div>', unsafe_allow_html=True)
        
        with cols[4]:
            # vs Peak 30d
            if metrics['vs_peak_30d'] is not None:
                peak_pct = metrics['vs_peak_30d']
                peak_val = metrics.get('peak_30d_value', 0)
                peak_date = metrics.get('peak_30d_date')
                
                if peak_date and isinstance(peak_date, pd.Timestamp):
                    peak_date_str = peak_date.strftime("%d %b %Y")
                    tooltip = f"Peak 30d avg: {peak_val:.1f} TJ/d on {peak_date_str}"
                else:
                    tooltip = f"Peak 30d avg: {peak_val:.1f} TJ/d"
                
                st.markdown(f'<div style="font-size: 0.85rem; font-weight: 500; color: #666; margin-bottom: 0.25rem;">vs Peak 30d</div><div style="font-size: 1.8rem; font-weight: 700; color: #666;" title="{tooltip}">{peak_pct:+.1f}%</div>', unsafe_allow_html=True)
            else:
                st.markdown('<div style="font-size: 0.85rem; font-weight: 500; color: #666; margin-bottom: 0.25rem;">vs Peak 30d</div><div style="font-size: 0.9rem; color: #999; font-style: italic;">Establishing baseline</div>', unsafe_allow_html=True)
        
        with cols[5]:
            # Supply Stability
            if metrics['stability']:
                stability_colors = {
                    'High': '#155724',
                    'Moderate': '#856404',
                    'Variable': '#721c24'
                }
                stab_color = stability_colors.get(metrics['stability'], '#666')
                cv_val = metrics.get('cv', 0)
                prod_days = metrics.get('producing_days', 0)
                total_days = metrics.get('total_days', 0)
                
                tooltip = f"Producing days: {prod_days}/{total_days} days. Supply Stability measures variability in reported daily production over the latest 30 days. It does not represent facility availability or reservoir performance."
                
                st.markdown(f'<div style="font-size: 0.85rem; font-weight: 500; color: #666; margin-bottom: 0.25rem;">Supply Stability ℹ️</div><div style="font-size: 1.1rem; font-weight: 700; color: {stab_color};" title="{tooltip}">{metrics["stability"]}</div><div style="font-size: 0.8rem; color: #666;">CV {cv_val:.1f}%</div>', unsafe_allow_html=True)
            else:
                st.markdown('<div style="font-size: 0.85rem; font-weight: 500; color: #666; margin-bottom: 0.25rem;">Supply Stability</div><div style="font-size: 0.9rem; color: #999;">N/A</div>', unsafe_allow_html=True)
    
    # Add methodology note
    with st.expander("📖 Basin Performance Methodology"):
        st.markdown("""
        **Basin Performance Metrics - Calculation Methods**
        
        All production calculations are based exclusively on publicly available AEMO Gas Bulletin Board data. No production values are estimated or fabricated where data has not been reported.

        **Reporting dates**

        AEMO reporting availability may vary by facility. Current values on individual field/facility cards represent the latest reported value for that source and may therefore relate to different gas dates. NT-wide totals, shares and rolling averages use aligned common reporting dates and do not combine production from different gas dates. Missing production reports are not treated as zero.

        **Latest Reported vs Fully Aligned**

        **Latest Reported** is the latest observation for each tracked source and may combine values from different gas dates. It is a latest available market snapshot, not a same-day total.

        **Fully Aligned** is the sum of all included sources on the latest gas date where every required source has a valid observation. NT-wide rolling averages and historical comparisons use only fully aligned dates.
        
        **Current**: Basin production on the common aligned NT gas date when available. If no common date exists, the value is not used for NT Share comparisons.
        
        **30d Avg**: Arithmetic mean of total daily basin production over the latest 30 gas days. 
        Production is first aggregated to basin/day, then averaged.
        
        **NT Share**: Basin's percentage of total NT gas production on the common aligned gas date:  
        `(basin current / total NT current) × 100`
        
        **90d Change**: Compares the latest 30-day average with the 30-day average ending 90 days earlier.  
        `(current 30d avg / previous 30d avg - 1) × 100`  
        Direction indicators: ↑ positive change, ↓ negative change, → approximately unchanged (±1%)  
        Requires at least 120 days of history. Designed to show trend without single-day volatility.
        
        **vs Peak 30d**: Current 30-day average compared to the highest historical 30-day rolling average:  
        `(current 30d avg / historical peak 30d avg - 1) × 100`  
        Typically 0% or negative. Peak uses 30-day rolling average, not single highest day.
        
        **Production status**: Card status compares the latest source value with its trailing 7-day average. 
        “Production Rising” is more than 5% above that average, “Production Falling” is more than 5% below it, and “Production Stable” is within ±5% (when the 7-day average is positive).

        **Supply Stability**: Measures consistency of reported daily production over 30 days using coefficient of variation (CV):
        `CV = (standard deviation / mean) × 100`  
        - **High** = CV < 5%
        - **Moderate** = CV 5-15%  
        - **Variable** = CV > 15%

        **Important**: Supply Stability describes variability in reported AEMO production data only. It does not measure facility reliability, plant availability or reservoir performance. Reported production may vary due to market conditions, maintenance, operational decisions, reporting availability or other factors.
        
        **Status Indicators**:
        - **Awaiting production data**: No AEMO GBB data available for this basin yet
        - **Establishing baseline**: Fewer than 7 days of production data available
        - **Insufficient history**: Fewer than required days for specific metric (e.g., 120 days for 90d Change)
        """)

def render_field_analysis(metrics, nt_df):
    """Render compact field comparison/analysis section"""
    st.markdown("---")
    st.subheader("Field Comparison Analysis")
    
    with st.expander("Chart Controls", expanded=True):
        if nt_df.empty:
            st.info("No data available for field analysis")
            return
        
        available_fields = set(nt_df['nt_field'].dropna().unique())
        producing = [
            field_name for field_name in FIELD_DISPLAY_ORDER
            if field_name in available_fields
        ]
        
        if not producing:
            st.info("No producing fields available for comparison")
            return
        
        default_fields = [
            field_name for field_name in ["Shenandoah South", "Blacktip"]
            if field_name in producing
        ]
        if not default_fields:
            default_fields = producing[:2]

        selected_fields = st.multiselect(
            "Select fields",
            producing,
            default=default_fields,
            format_func=lambda field_name: (
                "Sturt Plateau / Shenandoah South"
                if field_name == "Shenandoah South"
                else field_name
            )
        )
        
        if not selected_fields:
            st.info("Select one or more fields to view trends")
            return
        
        # Create comparison chart
        fig = go.Figure()
        
        for field_name in selected_fields:
            field_data = nt_df[nt_df['nt_field'] == field_name].copy()
            field_daily = field_data.groupby('gas_date')['supply'].sum().reset_index()
            field_daily = field_daily.sort_values('gas_date')
            
            fig.add_trace(go.Scatter(
                x=field_daily['gas_date'],
                y=field_daily['supply'],
                name=field_name,
                mode='lines',
                line=dict(width=2, color=get_field_color(field_name))
            ))
        
        fig.update_layout(
            height=350,
            hovermode='x unified',
            xaxis_title='',
            yaxis_title='Production (TJ/day)',
            margin=dict(t=10, b=40, l=50, r=20),
            plot_bgcolor='white',
            paper_bgcolor='white',
            xaxis=dict(showgrid=True, gridcolor='#f0f0f0'),
            yaxis=dict(showgrid=True, gridcolor='#f0f0f0'),
            legend=dict(
                orientation="h",
                yanchor="bottom",
                y=1.01,
                xanchor="right",
                x=1,
                font=dict(size=11)
            )
        )
        
        st.plotly_chart(fig, width='stretch')

def render_admin_section(engine, session_maker):
    """Render collapsed admin/diagnostics section"""
    with st.expander("Admin & Diagnostics"):
        st.markdown("### Data Refresh")
        st.caption(
            "Refresh combines AEMO's full Actual Flow archive with the recent Last31 "
            "Actual Flow dataset. Last31 takes precedence for duplicate gas-date/facility records."
        )
        
        col1, col2 = st.columns([2, 1])
        
        with col1:
            st.info("Refresh AEMO data to update the dashboard with latest production figures")
        
        with col2:
            if st.button("Refresh AEMO Data", type="primary"):
                with st.spinner("Fetching latest AEMO data..."):
                    fetch_aemo_data.clear()
                    st.cache_data.clear()
                    raw_df = fetch_aemo_data()
                    
                    if raw_df is not None:
                        normalized_df = normalize_aemo_data(raw_df)
                        
                        if normalized_df is not None:
                            success = upsert_gbb_data(engine, session_maker, normalized_df)
                            
                            if success:
                                st.success("Data refreshed successfully")
                                st.cache_data.clear()
                                st.rerun()
                            else:
                                st.error("Failed to update database")
        
        st.markdown("---")
        st.markdown("### Database Status")
        
        try:
            session = session_maker()
            record_count = session.query(GBBRecord).count()
            nt_count = session.query(GBBRecord).filter(GBBRecord.state == 'NT').count()
            latest_import = session.query(func.max(GBBRecord.imported_date)).scalar()
            
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Total Records", f"{record_count:,}")
            with col2:
                st.metric("NT Records", f"{nt_count:,}")
            with col3:
                if latest_import:
                    st.metric("Last Import", latest_import.strftime("%Y-%m-%d %H:%M"))
                else:
                    st.metric("Last Import", "Never")
            
            # Diagnostic: Show actual NT-related facilities
            st.markdown("---")
            st.markdown("### Facility Diagnostics")
            
            # Get all unique states
            states_query = session.query(GBBRecord.state, func.count(GBBRecord.id)).group_by(GBBRecord.state).all()
            st.write("**States in database:**", dict(states_query))
            
            # Search for potential NT facilities by name patterns
            matching_facilities = session.query(
                GBBRecord.facility_name, 
                GBBRecord.state, 
                GBBRecord.facility_type,
                func.count(GBBRecord.id).label('count')
            ).filter(
                (GBBRecord.state == 'NT') & (
                func.lower(GBBRecord.facility_name).contains('mereenie') |
                func.lower(GBBRecord.facility_name).contains('palm') |
                func.lower(GBBRecord.facility_name).contains('blacktip') |
                func.lower(GBBRecord.facility_name).contains('yelcherr') |
                func.lower(GBBRecord.facility_name).contains('yellerr')
                )
            ).group_by(GBBRecord.facility_name, GBBRecord.state, GBBRecord.facility_type).all()
            
            if matching_facilities:
                st.write("**Found NT-related facilities:**")
                for fac in matching_facilities:
                    st.write(f"- **{fac[0]}** (State: {fac[1]}, Type: {fac[2]}, Records: {fac[3]})")
            else:
                st.warning("No facilities matching NT field names found in database")
            
            session.close()
            
        except Exception as e:
            st.error(f"Database query failed: {str(e)}")

# ============================================================================
# Main Application
# ============================================================================

def main():
    """Main application entry point"""
    
    # Initialize database
    loading_placeholder = st.empty()
    loading_placeholder.info("Connecting to the production database...")
    try:
        engine, Session = get_database_connection()
    except Exception as exc:
        loading_placeholder.empty()
        st.error(f"Database connection failed: {exc}")
        st.stop()
    database_url = os.environ.get('DATABASE_URL')
    
    # Auto-fetch AEMO data on first load or if data is stale
    try:
        session = Session()
        record_count = session.query(GBBRecord).count()
        
        # Check if we need to fetch data
        should_fetch = False
        fetch_reason = ""
        
        if record_count == 0:
            # No data at all - definitely fetch
            should_fetch = True
            fetch_reason = "No data in database"
        else:
            # Automatically refresh if data is older than 1 hour
            latest_import = session.query(func.max(GBBRecord.imported_date)).scalar()
            if latest_import:
                hours_since_import = (datetime.now() - latest_import).total_seconds() / 3600
                if hours_since_import > 1:
                    should_fetch = True
                    fetch_reason = f"Data is {hours_since_import:.1f} hours old"
            else:
                should_fetch = True
                fetch_reason = "No previous import recorded"
        
        session.close()
        
        # Auto-fetch if needed
        if should_fetch:
            with st.spinner(f"Loading AEMO data... ({fetch_reason})"):
                st.caption("Downloading and filtering NT AEMO records...")
                fetch_aemo_data.clear()
                raw_df = fetch_aemo_data()
                
                if raw_df is not None:
                    st.caption(f"Preparing {len(raw_df):,} NT records for database import...")
                    normalized_df = normalize_aemo_data(raw_df)
                    
                    if normalized_df is not None:
                        st.caption("Saving NT records to PostgreSQL in batches...")
                        success = upsert_gbb_data(engine, Session, normalized_df)
                        
                        if success:
                            st.success("✅ AEMO data loaded successfully")
                            # Clear cache to ensure fresh calculations
                            st.cache_data.clear()
                        else:
                            st.warning("⚠️ Failed to load AEMO data - dashboard may show incomplete information")
                else:
                    st.warning("⚠️ Unable to fetch AEMO data - dashboard may show incomplete information")
    
    except Exception as e:
        st.warning(f"⚠️ Auto-fetch check failed: {str(e)}")
    
    # Get cached NT data; chart interactions do not re-query the database.
    try:
        nt_df = get_cached_nt_data(database_url)
    except Exception as exc:
        loading_placeholder.empty()
        st.error(f"Unable to load cached production data: {exc}")
        st.stop()
    finally:
        loading_placeholder.empty()
    
    # Calculate metrics
    metrics = calculate_nt_metrics(nt_df)
    
    # Calculate basin-level performance metrics
    basin_metrics = calculate_basin_metrics(nt_df, metrics['common_date'])

    qc_errors = validate_nt_metrics(metrics)
    if qc_errors:
        st.error("Headline QC failed: " + "; ".join(qc_errors))
        metrics['total_complete'] = False
        metrics['total_current'] = None
        metrics['avg_7d_total'] = None
    
    # Render dashboard - prioritized visual hierarchy
    render_header(metrics)
    render_headline_kpi(metrics)
    render_nt_history_chart(metrics)  # Moved up - dominant visual
    render_field_cards(metrics, nt_df)
    render_data_freshness(metrics, nt_df)
    render_basin_composition(metrics)
    render_basin_performance(basin_metrics)  # New basin performance section
    render_field_analysis(metrics, nt_df)  # Optional deeper analysis at bottom
    
    # Admin section
    render_admin_section(engine, Session)
    
    # Improved footer
    st.markdown("---")
    
    # Data attribution
    st.markdown(
        "**Data Attribution:** Gas production data is sourced from the "
        "[AEMO Gas Bulletin Board](https://www.aemo.com.au/energy-systems/gas/gas-bulletin-board-gbb) "
        "via [NEMWeb](https://nemweb.com.au/Reports/Current/GBB/). "
        "Field/basin classifications, aggregation and derived performance metrics are calculated by this dashboard."
    )
    
    # Dashboard information
    st.markdown(
        "**Dashboard Information:** This is an independently developed public dashboard using publicly available data. "
        "Reporting dates may vary by facility and the latest available AEMO gas date does not necessarily represent "
        "the latest reported production date for every facility."
    )
    
    # Disclaimer
    st.caption(
        "**Disclaimer:** This dashboard is provided for general informational purposes only. "
        "It is not affiliated with or endorsed by AEMO, any gas producer, my employer or any government agency. "
        "For official market information, refer to [AEMO](https://www.aemo.com.au)."
    )

if __name__ == "__main__":
    main()
