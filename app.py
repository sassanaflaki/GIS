import json
import io
import requests
import pandas as pd
import geopandas as gpd
import streamlit as st
import folium
from streamlit_folium import st_folium
from folium.plugins import MeasureControl, Fullscreen

st.set_page_config(page_title="GIS Layer Viewer", layout="wide")
st.title("🗺️ GIS Layer Viewer — URL Loader")
st.caption("Load GeoJSON / ArcGIS FeatureServer / OGC WFS → filter → map → inspect")

@st.cache_data(show_spinner=False)
def load_geojson_from_url(url: str) -> dict:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.json()

@st.cache_data(show_spinner=False)
def load_gdf_from_geojson_obj(obj: dict) -> gpd.GeoDataFrame:
    gdf = gpd.GeoDataFrame.from_features(obj["features"]) if "features" in obj else gpd.read_file(io.StringIO(json.dumps(obj)))
    if gdf.empty:
        return gdf
    # Normalize CRS to WGS84 for web maps
    try:
        if gdf.crs is None:
            gdf.set_crs(4326, inplace=True)
        else:
            gdf = gdf.to_crs(4326)
    except Exception:
        pass
    # Drop invalid geometries
    gdf = gdf[~gdf.geometry.is_empty & gdf.geometry.notnull()]
    return gdf

@st.cache_data(show_spinner=True)
def load_layer(source_type: str, url: str, layer_id: str | None = None, typename: str | None = None) -> gpd.GeoDataFrame:
    """
    Load as GeoDataFrame from multiple backends:
    - GeoJSON URL → direct
    - ArcGIS FeatureServer → /query?where=1=1&outFields=*&f=geojson
      * If `url` ends with /FeatureServer, provide layer_id (e.g., "0").
      * If `url` ends with /FeatureServer/<layer_id>, layer_id optional.
    - OGC WFS → GetFeature with outputFormat=application/json
    """
    st.session_state.setdefault("_debug", {})

    if source_type == "GeoJSON":
        obj = load_geojson_from_url(url)
        return load_gdf_from_geojson_obj(obj)

    elif source_type == "ArcGIS FeatureServer":
        base_url = url.rstrip("/")
        if base_url.lower().endswith("/featureserver"):
            if not layer_id:
                raise ValueError("ArcGIS FeatureServer base URL provided. Please specify layer ID (e.g., 0).")
            layer_url = f"{base_url}/{layer_id}"
        else:
            layer_url = base_url
        query_url = (
            f"{layer_url}/query?where=1%3D1&outFields=*&f=geojson&outSR=4326&returnGeometry=true"
        )
        obj = load_geojson_from_url(query_url)
        st.session_state["_debug"]["arcgis_query_url"] = query_url
        return load_gdf_from_geojson_obj(obj)

    elif source_type == "OGC WFS":
        if not typename:
            raise ValueError("WFS requires a typename (layer name).")
        params = {
            "service": "WFS",
            "version": "2.0.0",
            "request": "GetFeature",
            "typeName": typename,
            "outputFormat": "application/json",
            "srsName": "EPSG:4326",
        }
        sep = "&" if "?" in url else "?"
        query_url = url + sep + "&".join([f"{k}={v}" for k, v in params.items()])
        obj = load_geojson_from_url(query_url)
        st.session_state["_debug"]["wfs_query_url"] = query_url
        return load_gdf_from_geojson_obj(obj)

    else:
        raise ValueError("Unsupported source type.")

# Sidebar inputs
with st.sidebar:
    st.header("🔗 Data Source")
    source_type = st.selectbox("Source Type", ["GeoJSON", "ArcGIS FeatureServer", "OGC WFS"])

    default_url = {
        "GeoJSON": "https://raw.githubusercontent.com/glynnbird/usstatesgeojson/master/california.geojson",
        "ArcGIS FeatureServer": "https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services/USA_States_Generalized/FeatureServer/0",
        "OGC WFS": "https://demo.mapserver.org/cgi-bin/wfs?",
    }[source_type]

    url = st.text_input("Layer URL", value=default_url)

    layer_id = None
    typename = None
    if source_type == "ArcGIS FeatureServer" and url.rstrip("/").lower().endswith("/featureserver"):
        layer_id = st.text_input("Layer ID (e.g., 0)", value="0")
    if source_type == "OGC WFS":
        typename = st.text_input("WFS typename", value="countries")

    load_btn = st.button("Load layer", type="primary")

if load_btn:
    try:
        gdf = load_layer(source_type, url, layer_id=layer_id, typename=typename)
    except Exception as e:
        st.error(f"Failed to load layer: {e}")
        st.stop()

    if gdf.empty:
        st.warning("Layer loaded but empty.")
        st.stop()

    st.success(f"Loaded {len(gdf):,} features. CRS set to EPSG:4326 for web mapping.")

    # Column controls
    with st.sidebar:
        st.subheader("🧮 Attributes")
        all_cols = [c for c in gdf.columns if c != "geometry"]
        show_cols = st.multiselect("Columns to display in popups/table", options=all_cols, default=all_cols[:8])

        st.subheader("🔍 Filter")
        filter_col = st.selectbox("Filter by column", ["(no filter)"] + all_cols)
        filtered = gdf.copy()
        if filter_col != "(no filter)":
            if pd.api.types.is_numeric_dtype(filtered[filter_col]):
                mn, mx = float(filtered[filter_col].min()), float(filtered[filter_col].max())
                vmin, vmax = st.slider("Numeric range", min_value=mn, max_value=mx, value=(mn, mx))
                filtered = filtered[(filtered[filter_col] >= vmin) & (filtered[filter_col] <= vmax)]
            else:
                vals = sorted(map(str, filtered[filter_col].dropna().unique().tolist()))
                chosen = st.multiselect("Values", vals, default=vals[: min(10, len(vals))])
                filtered = filtered[filtered[filter_col].astype(str).isin(chosen)]

        st.caption(f"Filtered features: {len(filtered):,} / {len(gdf):,}")

    # Map
    bounds = filtered.total_bounds  # [minx, miny, maxx, maxy]
    center = [(bounds[1] + bounds[3]) / 2, (bounds[0] + bounds[2]) / 2]

    m = folium.Map(location=center, zoom_start=6, tiles="cartodbpositron", control_scale=True)
    m.add_child(MeasureControl(primary_length_unit='meters', secondary_length_unit='miles'))
    Fullscreen().add_to(m)

    style = dict(weight=1, opacity=0.8, color="#3388ff", fillOpacity=0.2)

    tooltip = folium.GeoJsonTooltip(fields=show_cols, aliases=[f"{c}:" for c in show_cols], sticky=True)
    popup = folium.GeoJsonPopup(fields=show_cols, aliases=[f"{c}:" for c in show_cols], max_width=400)

    gj = folium.GeoJson(
        data=json.loads(filtered.to_json()),
        name="Layer",
        style_function=lambda _: style,
        tooltip=tooltip,
        popup=popup,
        highlight_function=lambda _: {"weight": 3, "color": "#ff7800"},
    )
    gj.add_to(m)

    folium.LayerControl().add_to(m)

    st_map = st_folium(m, height=650, use_container_width=True, returned_objects=["last_object_clicked"])

    st.subheader("📋 Attribute table (filtered)")
    st.dataframe(filtered[show_cols].reset_index(drop=True), use_container_width=True)

    # Clicked feature attributes (if any)
    clicked = st_map.get("last_object_clicked") if isinstance(st_map, dict) else None
    if clicked and "properties" in clicked:
        with st.expander("Selected feature — attributes"):
            props = clicked["properties"]
            st.json(props)

    # Download filtered as GeoJSON
    geojson_bytes = filtered.to_json().encode("utf-8")
    st.download_button(
        label="⬇️ Download filtered GeoJSON",
        data=geojson_bytes,
        file_name="filtered.geojson",
        mime="application/geo+json",
    )

    # Debug URLs
    if st.checkbox("Show debug URLs (queries)"):
        st.write(st.session_state.get("_debug", {}))
else:
    st.info("Enter a URL and click **Load layer** to begin. Try the defaults in the sidebar.")
