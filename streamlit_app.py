"""
streamlit_app.py

Phase 5 - NILM dashboard.

Reads directly from DuckDB (gold + silver layers) and renders four views:
    Tab 1 - Daily total kWh over time (line chart)
    Tab 2 - Appliance breakdown by day (stacked bar chart)
    Tab 3 - HVAC load vs temperature (scatter plot)
    Tab 4 - Daily load profile for a selected day (stacked area chart,
            rule-based interval-level overlay)

Sidebar controls:
    Date range filter (applies to all tabs)
    Temperature unit toggle: °C / °F (display only)

Run:
    streamlit run streamlit_app.py
"""

import duckdb
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

DB_PATH = "data/smart_meter.duckdb"

APPLIANCE_COLORS = {
    "hvac":     "#e07b39",
    "dryer":    "#5b8db8",
    "washer":   "#7bbf7b",
    "cooking":  "#c97bb5",
    "baseline": "#9e9e9e",
}

LOAD_PROFILE_COLORS = {
    "baseline": "#bdbdbd",
    "morning":  "#9c6eb0",
    "washer":   "#7bbf7b",
    "dryer":    "#4db6ac",
    "cooking":  "#e07b39",
    "hvac":     "#5b8db8",
    "other":    "#fffde7",
}


# ---------------------------------------------------------------------------
# Data loading (cached per session)
# ---------------------------------------------------------------------------

@st.cache_data
def load_day_intervals(date_str: str) -> pd.DataFrame:
    con = duckdb.connect(DB_PATH, read_only=True)
    df = con.execute("""
        SELECT interval_start_dt, usage_kwh, hour_of_day, temp_c
        FROM silver.interval_features
        WHERE CAST(interval_start_dt AS DATE) = ?
        ORDER BY interval_start_dt
    """, [date_str]).fetchdf()
    con.close()
    return df


def apply_load_rules(df: pd.DataFrame) -> pd.DataFrame:
    """
    Rule-based interval-level load disaggregation.

    Bands: baseline (fixed floor), hvac, morning, cooking, washer, dryer, other.
    Baseline is capped at min(usage_kwh, 0.15) and never modified afterwards.
    Variable bands are scaled proportionally when their sum would exceed the
    headroom above baseline. Unclassified load goes to 'other' (never negative).
    """
    result = df[["interval_start_dt", "usage_kwh", "hour_of_day", "temp_c"]].copy()

    kwh  = result["usage_kwh"].values
    hour = result["hour_of_day"].values
    temp = result["temp_c"].fillna(result["temp_c"].mean()).values

    # Fixed floor — never touched again
    baseline = np.minimum(kwh, 0.15)

    hot    = temp > 23
    peak_h = (hour >= 10) & (hour <= 22)
    hvac = np.where(
        hot & peak_h,  np.maximum(0, kwh * (temp - 23) / 15 * 0.6),
        np.where(hot,  np.maximum(0, kwh * (temp - 23) / 15 * 0.2), 0.0),
    )

    morning = np.where(
        (hour >= 6) & (hour <= 8) & (kwh > 0.25),
        np.maximum(0, kwh * 0.35), 0.0,
    )

    cooking = np.where(
        (hour >= 18) & (hour <= 20), np.maximum(0, kwh * 0.40),
        np.where((hour >= 12) & (hour <= 13), np.maximum(0, kwh * 0.25), 0.0),
    )

    washer = np.where(
        (hour >= 8) & (hour <= 17) & (kwh > 0.3),
        np.maximum(0, kwh * 0.20), 0.0,
    )

    dryer = np.where(
        (hour >= 9) & (hour <= 18) & (kwh > 0.35),
        np.maximum(0, kwh * 0.25), 0.0,
    )

    # Scale variable bands to fit within headroom above baseline
    available    = np.maximum(0, kwh - baseline)
    variable_sum = hvac + morning + cooking + washer + dryer
    scale        = np.where(variable_sum > available, available / np.maximum(variable_sum, 1e-9), 1.0)
    hvac    *= scale
    morning *= scale
    cooking *= scale
    washer  *= scale
    dryer   *= scale

    # Unclassified remainder — never negative, never rolled into baseline
    other = np.maximum(0, kwh - (baseline + hvac + morning + cooking + washer + dryer))

    result["baseline"] = baseline
    result["hvac"]     = hvac
    result["morning"]  = morning
    result["cooking"]  = cooking
    result["washer"]   = washer
    result["dryer"]    = dryer
    result["other"]    = other

    return result


@st.cache_data
def load_daily_summary():
    con = duckdb.connect(DB_PATH, read_only=True)
    df = con.execute(
        "SELECT * FROM gold.daily_summary ORDER BY usage_date"
    ).fetchdf()
    con.close()
    df["usage_date"] = pd.to_datetime(df["usage_date"])
    return df


# ---------------------------------------------------------------------------
# App layout
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Smart Meter NILM", layout="wide")
st.title("Smart Meter NILM Dashboard")

daily = load_daily_summary()

# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

st.sidebar.header("Filters")

date_min = daily["usage_date"].min().date()
date_max = daily["usage_date"].max().date()

date_range = st.sidebar.date_input(
    "Date range",
    value=(date_min, date_max),
    min_value=date_min,
    max_value=date_max,
)

if isinstance(date_range, (list, tuple)) and len(date_range) == 2:
    start_date, end_date = pd.Timestamp(date_range[0]), pd.Timestamp(date_range[1])
else:
    start_date, end_date = pd.Timestamp(date_min), pd.Timestamp(date_max)

temp_unit = st.sidebar.radio("Temperature unit", ["°C", "°F"])

df = daily[(daily["usage_date"] >= start_date) & (daily["usage_date"] <= end_date)].copy()

if temp_unit == "°F":
    df["avg_temp_display"] = df["avg_temp_c"] * 9 / 5 + 32
    temp_label = "Avg Temperature (°F)"
else:
    df["avg_temp_display"] = df["avg_temp_c"]
    temp_label = "Avg Temperature (°C)"

st.sidebar.markdown(f"**{len(df)} days** selected")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab1, tab2, tab3, tab4 = st.tabs(
    ["Daily Total", "Appliance Breakdown", "HVAC vs Temperature", "Daily Load Profile"]
)


# --- Tab 1: Daily total kWh line chart ---
with tab1:
    st.subheader("Daily Total Consumption")

    fig = px.line(
        df,
        x="usage_date",
        y="total_kwh",
        labels={"usage_date": "Date", "total_kwh": "Total kWh"},
        markers=True,
    )
    fig.update_traces(line_color="#4a90d9", marker_size=4)
    fig.update_layout(hovermode="x unified")
    st.plotly_chart(fig, use_container_width=True)

    col1, col2, col3 = st.columns(3)
    col1.metric("Avg kWh / day", f"{df['total_kwh'].mean():.1f}")
    col2.metric("Max kWh / day", f"{df['total_kwh'].max():.1f}")
    col3.metric("Min kWh / day", f"{df['total_kwh'].min():.1f}")


# --- Tab 2: Stacked bar chart ---
with tab2:
    st.subheader("Appliance Breakdown by Day")

    appliance_cols = ["hvac_kwh", "washer_kwh", "dryer_kwh", "cooking_kwh", "baseline_kwh"]
    df_long = df.melt(
        id_vars="usage_date",
        value_vars=appliance_cols,
        var_name="appliance",
        value_name="kwh",
    )
    df_long["appliance"] = df_long["appliance"].str.replace("_kwh", "", regex=False)

    fig = px.bar(
        df_long,
        x="usage_date",
        y="kwh",
        color="appliance",
        color_discrete_map=APPLIANCE_COLORS,
        labels={"usage_date": "Date", "kwh": "kWh", "appliance": "Appliance"},
        category_orders={"appliance": ["hvac", "dryer", "washer", "cooking", "baseline"]},
    )
    fig.update_layout(barmode="stack", hovermode="x unified", legend_title="Appliance")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Average daily attribution")
    avg = df[appliance_cols].mean().rename(lambda c: c.replace("_kwh", ""))
    avg_pct = (avg / avg.sum() * 100).round(1)
    summary_df = pd.DataFrame({"Avg kWh/day": avg.round(2), "Share (%)": avg_pct})
    st.dataframe(summary_df.sort_values("Avg kWh/day", ascending=False), use_container_width=True)


# --- Tab 3: HVAC vs temperature scatter ---
with tab3:
    st.subheader(f"HVAC Load vs {temp_label}")

    fig = px.scatter(
        df,
        x="avg_temp_display",
        y="hvac_kwh",
        hover_data={"usage_date": True, "total_kwh": ":.1f"},
        labels={
            "avg_temp_display": temp_label,
            "hvac_kwh": "HVAC kWh / day",
            "usage_date": "Date",
            "total_kwh": "Total kWh",
        },
        color="hvac_kwh",
        color_continuous_scale="Oranges",
    )
    fig.update_traces(marker_size=8)
    fig.update_layout(coloraxis_showscale=False)
    st.plotly_chart(fig, use_container_width=True)

    threshold = 23.0 if temp_unit == "°C" else 73.4
    days_ac = (df["avg_temp_display"] > threshold).sum()
    st.caption(
        f"{days_ac} of {len(df)} days above {threshold:.0f}{temp_unit} "
        f"(ac_proxy threshold used in feature engineering)"
    )


# --- Tab 4: Daily load profile (rule-based interval overlay) ---
with tab4:
    st.subheader("Daily Load Profile")

    profile_date = st.date_input(
        "Select date",
        value=start_date.date(),
        min_value=start_date.date(),
        max_value=end_date.date(),
        key="profile_date",
    )

    intervals = load_day_intervals(str(profile_date))

    if intervals.empty:
        st.warning(f"No interval data for {profile_date}.")
    else:
        bands = apply_load_rules(intervals)

        day_total = intervals["usage_kwh"].sum()
        avg_temp_c = intervals["temp_c"].mean()
        temp_display = avg_temp_c if temp_unit == "°C" else avg_temp_c * 9 / 5 + 32

        st.caption(
            f"{profile_date}  |  Total: {day_total:.2f} kWh  |  "
            f"Avg temp: {temp_display:.1f}{temp_unit}"
        )

        band_cols = ["baseline", "morning", "washer", "dryer", "cooking", "hvac", "other"]
        long = bands.melt(
            id_vars="interval_start_dt",
            value_vars=band_cols,
            var_name="appliance",
            value_name="kwh",
        )

        fig = px.area(
            long,
            x="interval_start_dt",
            y="kwh",
            color="appliance",
            color_discrete_map=LOAD_PROFILE_COLORS,
            labels={"interval_start_dt": "Time", "kwh": "kWh", "appliance": "Load"},
            category_orders={"appliance": band_cols},
        )

        temp_vals = (
            intervals["temp_c"]
            if temp_unit == "°C"
            else intervals["temp_c"] * 9 / 5 + 32
        )
        fig.add_trace(
            go.Scatter(
                x=intervals["interval_start_dt"],
                y=temp_vals,
                name=f"Temp ({temp_unit})",
                yaxis="y2",
                mode="lines",
                line=dict(color="#ef5350", width=1.5, dash="dot"),
            )
        )
        fig.update_layout(
            hovermode="x unified",
            legend_title="Load",
            yaxis2=dict(
                title=f"Temperature ({temp_unit})",
                overlaying="y",
                side="right",
                showgrid=False,
            ),
        )
        st.plotly_chart(fig, use_container_width=True)
