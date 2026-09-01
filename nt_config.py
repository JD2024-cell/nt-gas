"""
Northern Territory Gas Production Configuration

Central configuration for NT gas fields, facility mappings, and basin groupings.
This module provides the authoritative mapping between field names and AEMO facility identifiers.
"""

# NT Gas Field Configuration
# Maps friendly field names to AEMO GBB facility identifiers and metadata
NT_FIELDS = {
    "Mereenie": {
        "basin": "Amadeus",
        "aemo_match": ["Mereenie"],  # AEMO FacilityName patterns to match
        "status": "producing",
        "operator": "Central Petroleum",
        "color": "#E74C3C"  # Red
    },
    "Palm Valley": {
        "basin": "Amadeus",
        "aemo_match": ["Palm Valley"],
        "status": "producing",
        "operator": "Central Petroleum",
        "color": "#3498DB"  # Blue
    },
    "Blacktip": {
        "basin": "Bonaparte",
        "aemo_match": ["Yelcherr", "Blacktip"],  # Multiple AEMO names for same field
        "status": "producing",
        "operator": "Various",
        "color": "#2ECC71"  # Green
    },
    "Shenandoah South": {
        "basin": "Beetaloo",
        "aemo_match": ["Shenandoah", "Tamboran", "Sturt Plateau"],  # AEMO facility names
        "status": "awaiting_aemo",
        "operator": "Tamboran Resources",
        "color": "#F39C12"  # Orange
    },
    "Carpentaria": {
        "basin": "Beetaloo",
        "aemo_match": ["Carpentaria", "Beetaloo Energy"],  # AEMO facility names
        "status": "awaiting_aemo",
        "operator": "Beetaloo Energy Australia",
        "color": "#9B59B6"  # Purple
    }
}

# Basin groupings for aggregation and display
BASINS = {
    "Amadeus": {
        "fields": ["Mereenie", "Palm Valley"],
        "color": "#E67E22"
    },
    "Bonaparte": {
        "fields": ["Blacktip"],
        "color": "#16A085"
    },
    "Beetaloo": {
        "fields": ["Shenandoah South", "Carpentaria"],
        "color": "#8E44AD"
    }
}

# Display order for fields
FIELD_DISPLAY_ORDER = [
    "Mereenie",
    "Palm Valley", 
    "Blacktip",
    "Shenandoah South",
    "Carpentaria"
]

def get_field_for_facility(facility_name):
    """
    Map an AEMO facility name to an NT field name.
    
    Args:
        facility_name: AEMO FacilityName from GBB data
        
    Returns:
        NT field name (str) or None if no match
    """
    if not facility_name:
        return None
        
    facility_lower = facility_name.lower()
    
    for field_name, config in NT_FIELDS.items():
        for pattern in config["aemo_match"]:
            if pattern.lower() in facility_lower:
                return field_name
    
    return None

def get_producing_fields():
    """Return list of fields currently producing (have AEMO data)"""
    return [name for name, config in NT_FIELDS.items() if config["status"] == "producing"]

def get_awaiting_fields():
    """Return list of fields awaiting AEMO reporting"""
    return [name for name, config in NT_FIELDS.items() if config["status"] == "awaiting_aemo"]

def get_field_color(field_name):
    """Get the display color for a field"""
    return NT_FIELDS.get(field_name, {}).get("color", "#95A5A6")

def get_basin_fields(basin_name):
    """Get all fields in a basin"""
    return BASINS.get(basin_name, {}).get("fields", [])
