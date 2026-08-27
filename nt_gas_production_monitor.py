"""
NT Gas Production Monitor

Public-facing dashboard for Northern Territory gas production monitoring.
Data source: AEMO Gas Bulletin Board via nemweb.com.au
"""

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import requests
import zipfile
import io
from io import StringIO
from datetime import datetime, timedelta
import os

# Database imports
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, UniqueConstraint
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.sql import func
from sqlalchemy.dialects.postgresql import insert

# Local configuration
from nt_config import (
    NT_FIELDS, BASINS, FIELD_DISPLAY_ORDER,
    get_field_for_facility, get_producing_fields, 
    get_awaiting_fields, get_field_color
)

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
    /* Main container styling */
    .block-container {
        padding-top: 2rem;
        padding-bottom: 2rem;
        max-width: 1400px;
    }
    
    /* Title styling */
    h1 {
        color: #1a1a1a;
        font-weight: 700;
        margin-bottom: 0.5rem;
    }
    
    h2 {
        color: #2c3e50;
        font-weight: 600;
        margin-top: 2rem;
        margin-bottom: 1rem;
        border-bottom: 2px solid #e0e0e0;
        padding-bottom: 0.5rem;
    }
    
    h3 {
        color: #34495e;
        font-weight: 600;
    }
    
    /* Metric styling */
    [data-testid="stMetricValue"] {
        font-size: 2.5rem;
        font-weight: 700;
    }
    
    [data-testid="stMetricLabel"] {
        font-size: 1rem;
        font-weight: 500;
        color: #555;
    }
    
    /* Card styling */
    .field-card {
        background: white;
        padding: 1.5rem;
        border-radius: 8px;
        border: 1px solid #e0e0e0;
        box-shadow: 0 2px 4px rgba(0,0,0,0.05);
        margin-bottom: 1rem;
    }
    
    /* Subtle divider */
    hr {
        margin: 2rem 0;
        border: none;
        border-top: 1px solid #e0e0e0;
    }
    
    /* Remove extra padding from metric containers */
    [data-testid="stMetricDelta"] {
        font-size: 0.9rem;
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

def get_database_connection():
    """Get database connection using DATABASE_URL environment variable"""
    try:
        database_url = os.environ.get('DATABASE_URL')
        if not database_url:
            st.error("⚠️ DATABASE_URL environment variable not configured")
            st.stop()
        
        engine = create_engine(database_url)
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        return engine, Session
    except Exception as e:
        st.error(f"Database connection failed: {str(e)}")
        st.stop()

# ============================================================================
# Data Fetching and Processing
# ============================================================================

@st.cache_data(ttl=3600)
def fetch_aemo_data():
    """
    Fetch and parse AEMO Gas Bulletin Board data.
    Reuses existing proven download logic.
    """
    url = "https://nemweb.com.au/Reports/Current/GBB/GasBBActualFlowStorage.zip"
    
    try:
        response = requests.get(url, timeout=60, stream=True)
        response.raise_for_status()
        
        downloaded_data = b''
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                downloaded_data += chunk
        
        with zipfile.ZipFile(io.BytesIO(downloaded_data)) as zip_file:
            csv_files = [f for f in zip_file.namelist() if f.lower().endswith('.csv')]
            if not csv_files:
                st.error("No CSV files found in AEMO archive")
                return None
            
            with zip_file.open(csv_files[0]) as csv_file:
                csv_content = csv_file.read().decode('utf-8')
                df = pd.read_csv(StringIO(csv_content))
                
                # Parse dates
                df['GasDate'] = pd.to_datetime(df['GasDate'])
                if 'LastUpdated' in df.columns:
                    df['LastUpdated'] = pd.to_datetime(df['LastUpdated'])
                
                return df
                
    except Exception as e:
        st.error(f"Failed to fetch AEMO data: {str(e)}")
        return None

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
    """
    if df is None or df.empty:
        return False
    
    try:
        # Use PostgreSQL INSERT ... ON CONFLICT for efficient upsert
        records = df.to_dict('records')
        
        with engine.begin() as connection:
            for record in records:
                stmt = insert(GBBRecord).values(**record)
                stmt = stmt.on_conflict_do_update(
                    constraint='uix_gas_date_facility',
                    set_={
                        'facility_name': stmt.excluded.facility_name,
                        'facility_type': stmt.excluded.facility_type,
                        'demand': stmt.excluded.demand,
                        'supply': stmt.excluded.supply,
                        'transfer_in': stmt.excluded.transfer_in,
                        'transfer_out': stmt.excluded.transfer_out,
                        'held_in_storage': stmt.excluded.held_in_storage,
                        'cushion_gas_storage': stmt.excluded.cushion_gas_storage,
                        'state': stmt.excluded.state,
                        'location_name': stmt.excluded.location_name,
                        'location_id': stmt.excluded.location_id,
                        'last_updated': stmt.excluded.last_updated,
                        'imported_date': stmt.excluded.imported_date
                    }
                )
                connection.execute(stmt)
        
        return True
        
    except Exception as e:
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
        records = session.query(GBBRecord).all()
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
            'total_current': 0,
            'fields': {},
            'daily_total': pd.DataFrame(),
            'daily_by_field': pd.DataFrame()
        }
    
    # Get latest gas date
    latest_date = nt_df['gas_date'].max()
    
    # Latest production by field
    latest_data = nt_df[nt_df['gas_date'] == latest_date]
    
    # Group by field and sum (in case multiple facilities map to same field)
    field_current = latest_data.groupby('nt_field')['supply'].sum().to_dict()
    
    # Daily totals over time
    daily_by_field = nt_df.groupby(['gas_date', 'nt_field'])['supply'].sum().reset_index()
    daily_total = daily_by_field.groupby('gas_date')['supply'].sum().reset_index()
    daily_total.columns = ['gas_date', 'total_supply']
    
    # Calculate field-level averages
    field_metrics = {}
    for field in get_producing_fields():
        field_data = nt_df[nt_df['nt_field'] == field]
        
        if not field_data.empty:
            # Daily aggregation for this field
            field_daily = field_data.groupby('gas_date')['supply'].sum().reset_index()
            field_daily = field_daily.sort_values('gas_date')
            
            latest_supply = field_current.get(field, 0)
            
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
                'avg_7d': avg_7d,
                'avg_30d': avg_30d,
                'trend': trend,
                'has_data': True
            }
        else:
            field_metrics[field] = {
                'current': 0,
                'avg_7d': 0,
                'avg_30d': 0,
                'trend': "stable",
                'has_data': False
            }
    
    # Total current production
    total_current = sum(field_current.values())
    
    # Previous day comparison
    if len(daily_total) > 1:
        prev_day = daily_total.iloc[-2]['total_supply']
        change_vs_prev = total_current - prev_day
    else:
        change_vs_prev = 0
    
    # 7-day average total
    if len(daily_total) >= 7:
        avg_7d_total = daily_total.tail(7)['total_supply'].mean()
    else:
        avg_7d_total = daily_total['total_supply'].mean() if len(daily_total) > 0 else 0
    
    return {
        'latest_date': latest_date,
        'total_current': total_current,
        'change_vs_prev': change_vs_prev,
        'avg_7d_total': avg_7d_total,
        'fields': field_metrics,
        'daily_total': daily_total,
        'daily_by_field': daily_by_field
    }

# ============================================================================
# Rendering Functions
# ============================================================================

def render_header(metrics):
    """Render page header with title and data freshness"""
    st.title("NT Gas Production Monitor")
    
    if metrics['latest_date']:
        date_str = metrics['latest_date'].strftime("%d %B %Y")
        st.caption(f"**Northern Territory gas production at a glance** • Latest data: {date_str}")
    else:
        st.caption("**Northern Territory gas production at a glance**")
    
    st.markdown("<br>", unsafe_allow_html=True)

def render_headline_kpi(metrics):
    """Render main production KPI with context"""
    col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
    
    with col1:
        st.metric(
            label="Total NT Gas Production",
            value=f"{metrics['total_current']:.1f} TJ/d",
            delta=f"{metrics['change_vs_prev']:+.1f} TJ/d vs prev day" if metrics['change_vs_prev'] != 0 else None
        )
    
    with col2:
        st.metric(
            label="7-Day Average",
            value=f"{metrics['avg_7d_total']:.1f} TJ/d"
        )
    
    with col3:
        if metrics['avg_7d_total'] > 0:
            change_vs_7d = metrics['total_current'] - metrics['avg_7d_total']
            st.metric(
                label="vs 7-Day Avg",
                value=f"{change_vs_7d:+.1f} TJ/d"
            )
        else:
            st.metric(label="vs 7-Day Avg", value="—")
    
    with col4:
        producing_count = len([f for f, m in metrics['fields'].items() if m['has_data']])
        st.metric(label="Active Fields", value=str(producing_count))

def render_field_cards(metrics):
    """Render individual field production cards"""
    st.markdown("---")
    st.subheader("Field Production")
    
    # Arrange cards in rows
    for i in range(0, len(FIELD_DISPLAY_ORDER), 3):
        cols = st.columns(3)
        
        for j, field_name in enumerate(FIELD_DISPLAY_ORDER[i:i+3]):
            with cols[j]:
                field_config = NT_FIELDS[field_name]
                field_metrics = metrics['fields'].get(field_name, {})
                
                # Card container
                if field_metrics.get('has_data', False):
                    # Producing field
                    st.markdown(f"### {field_name}")
                    st.caption(f"{field_config['basin']} Basin • {field_config['operator']}")
                    
                    col_a, col_b = st.columns(2)
                    with col_a:
                        st.metric(
                            label="Current",
                            value=f"{field_metrics['current']:.1f} TJ/d"
                        )
                    with col_b:
                        trend_emoji = {"up": "📈", "down": "📉", "stable": "➡️"}
                        st.metric(
                            label="Trend",
                            value=trend_emoji[field_metrics['trend']]
                        )
                    
                    col_c, col_d = st.columns(2)
                    with col_c:
                        st.caption(f"**7-day avg:** {field_metrics['avg_7d']:.1f} TJ/d")
                    with col_d:
                        st.caption(f"**30-day avg:** {field_metrics['avg_30d']:.1f} TJ/d")
                else:
                    # Awaiting AEMO data
                    st.markdown(f"### {field_name}")
                    st.caption(f"{field_config['basin']} Basin • {field_config['operator']}")
                    st.info("⏳ Awaiting AEMO GBB reporting")

def render_nt_history_chart(metrics):
    """Render stacked area chart of NT production history"""
    st.markdown("---")
    st.subheader("NT Gas Production History")
    
    if metrics['daily_by_field'].empty:
        st.warning("No historical data available")
        return
    
    # Period selection
    col1, col2 = st.columns([3, 1])
    
    with col2:
        period = st.selectbox(
            "Time Period",
            ["30 days", "3 months", "6 months", "1 year", "All"],
            index=3
        )
    
    # Filter data by period
    df = metrics['daily_by_field'].copy()
    df = df.sort_values('gas_date')
    
    if period == "30 days":
        cutoff = datetime.now() - timedelta(days=30)
    elif period == "3 months":
        cutoff = datetime.now() - timedelta(days=90)
    elif period == "6 months":
        cutoff = datetime.now() - timedelta(days=180)
    elif period == "1 year":
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
        line=dict(color='#1a1a1a', width=3),
        showlegend=True
    ))
    
    fig.update_layout(
        height=500,
        hovermode='x unified',
        xaxis_title='',
        yaxis_title='Production (TJ/day)',
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1
        ),
        plot_bgcolor='white',
        paper_bgcolor='white',
        xaxis=dict(showgrid=True, gridcolor='#f0f0f0'),
        yaxis=dict(showgrid=True, gridcolor='#f0f0f0')
    )
    
    st.plotly_chart(fig, use_container_width=True)

def render_basin_composition(metrics):
    """Render basin-level production breakdown"""
    st.markdown("---")
    st.subheader("Production by Basin")
    
    # Calculate basin totals
    basin_totals = {}
    for basin_name, basin_config in BASINS.items():
        total = sum(
            metrics['fields'].get(field, {}).get('current', 0)
            for field in basin_config['fields']
            if metrics['fields'].get(field, {}).get('has_data', False)
        )
        basin_totals[basin_name] = total
    
    total_all = sum(basin_totals.values())
    
    cols = st.columns(len(BASINS))
    
    for i, (basin_name, basin_config) in enumerate(BASINS.items()):
        with cols[i]:
            st.markdown(f"#### {basin_name} Basin")
            
            basin_total = basin_totals[basin_name]
            percentage = (basin_total / total_all * 100) if total_all > 0 else 0
            
            st.metric(
                label="Production",
                value=f"{basin_total:.1f} TJ/d"
            )
            st.caption(f"**{percentage:.1f}%** of NT total")
            
            # List fields
            for field in basin_config['fields']:
                field_metrics = metrics['fields'].get(field, {})
                if field_metrics.get('has_data', False):
                    st.caption(f"• {field}: {field_metrics['current']:.1f} TJ/d")
                else:
                    st.caption(f"• {field}: awaiting data")

def render_field_analysis(metrics, nt_df):
    """Render field comparison/analysis section"""
    st.markdown("---")
    st.subheader("Field Trends Analysis")
    
    if nt_df.empty:
        st.warning("No data available for field analysis")
        return
    
    producing = get_producing_fields()
    
    if not producing:
        st.info("No producing fields available for comparison")
        return
    
    selected_fields = st.multiselect(
        "Select fields to compare",
        producing,
        default=producing[:2] if len(producing) >= 2 else producing
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
        height=400,
        hovermode='x unified',
        xaxis_title='',
        yaxis_title='Production (TJ/day)',
        plot_bgcolor='white',
        paper_bgcolor='white',
        xaxis=dict(showgrid=True, gridcolor='#f0f0f0'),
        yaxis=dict(showgrid=True, gridcolor='#f0f0f0'),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1
        )
    )
    
    st.plotly_chart(fig, use_container_width=True)

def render_admin_section(engine, session_maker):
    """Render collapsed admin/diagnostics section"""
    with st.expander("🔧 Admin & Diagnostics"):
        st.markdown("### Data Refresh")
        
        col1, col2 = st.columns([2, 1])
        
        with col1:
            st.info("Refresh AEMO data to update the dashboard with latest production figures")
        
        with col2:
            if st.button("🔄 Refresh AEMO Data", type="primary"):
                with st.spinner("Fetching latest AEMO data..."):
                    raw_df = fetch_aemo_data()
                    
                    if raw_df is not None:
                        normalized_df = normalize_aemo_data(raw_df)
                        
                        if normalized_df is not None:
                            success = upsert_gbb_data(engine, session_maker, normalized_df)
                            
                            if success:
                                st.success("✅ Data refreshed successfully")
                                st.cache_data.clear()
                                st.rerun()
                            else:
                                st.error("❌ Failed to update database")
        
        st.markdown("---")
        st.markdown("### Database Status")
        
        try:
            session = session_maker()
            record_count = session.query(GBBRecord).count()
            nt_count = session.query(GBBRecord).filter(GBBRecord.state == 'NT').count()
            latest_import = session.query(func.max(GBBRecord.imported_date)).scalar()
            session.close()
            
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
        except Exception as e:
            st.error(f"Database query failed: {str(e)}")

# ============================================================================
# Main Application
# ============================================================================

def main():
    """Main application entry point"""
    
    # Initialize database
    engine, Session = get_database_connection()
    
    # Get NT data
    nt_df = get_nt_data(Session)
    
    # Calculate metrics
    metrics = calculate_nt_metrics(nt_df)
    
    # Render dashboard
    render_header(metrics)
    render_headline_kpi(metrics)
    render_field_cards(metrics)
    render_nt_history_chart(metrics)
    render_basin_composition(metrics)
    render_field_analysis(metrics, nt_df)
    
    st.markdown("---")
    
    # Admin section
    render_admin_section(engine, Session)
    
    # Footer
    st.markdown("<br><br>", unsafe_allow_html=True)
    st.caption("Data source: AEMO Gas Bulletin Board • nemweb.com.au")

if __name__ == "__main__":
    main()
