"""
streamlit_app.py

Phase 5 - NILM dashboard.

Reads directly from DuckDB (gold + silver layers) and renders three views:
    Tab 1 - Daily total kWh over time (line chart)
    Tab 2 - Appliance breakdown by day (stacked bar chart)
    Tab 3 - HVAC load vs temperature (scatter plot)

Sidebar controls:
    Date range filter (applies to all tabs)
    Temperature unit toggle: °C / °F (display only)

Run:
    streamlit run streamlit_app.py
"""

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st

DB_PATH = "data/smart_meter.duckdb"

APPLIANCE_COLORS = {
    "hvac":     "#e07b39",
    "dryer":    "#5b8db8",
    "washer":   "#7bbf7b",
    "cooking":  "#c97bb5",
    "baseline": "#9e9e9e",
}


# ---------------------------------------------------------------------------
# Data loading (cached per session)
# ---------------------------------------------------------------------------

@st.cache_data
def load_daily_summary():
    con = duckdb.connect(DB_PATH, read_only=True)
    df = con.execute("""
        SELECT
            g.usage_date,
            sum(g.estimated_kwh)                                                        AS total_kwh,
            sum(CASE WHEN g.appliance = 'hvac'     THEN g.estimated_kwh ELSE 0 END)    AS hvac_kwh,
            sum(CASE WHEN g.appliance = 'washer'   THEN g.estimated_kwh ELSE 0 END)    AS washer_kwh,
            sum(CASE WHEN g.appliance = 'dryer'    THEN g.estimated_kwh ELSE 0 END)    AS dryer_kwh,
            sum(CASE WHEN g.appliance = 'cooking'  THEN g.estimated_kwh ELSE 0 END)    AS cooking_kwh,
            sum(CASE WHEN g.appliance = 'baseline' THEN g.estimated_kwh ELSE 0 END)    AS baseline_kwh,
            avg(i.temp_c)                                                               AS avg_temp_c
        FROM gold.appliance_estimates g
        JOIN (
            SELECT CAST(interval_start_dt AS DATE) AS usage_date, avg(temp_c) AS temp_c
            FROM silver.interval_features
            GROUP BY 1
        ) i USING (usage_date)
        GROUP BY g.usage_date
        ORDER BY g.usage_date
    """).fetchdf()
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

tab1, tab2, tab3 = st.tabs(["Daily Total", "Appliance Breakdown", "HVAC vs Temperature"])


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
