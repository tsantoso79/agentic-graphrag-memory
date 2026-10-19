"""
Agent Memory Dashboard -- Streamlit app for exploring the agent_memory database.
Run: streamlit run dashboard.py
"""
import os
import psycopg2
import psycopg2.extras
import pandas as pd
import streamlit as st
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta

# -- Config ---------------------------------------------------------------
DB_URI = os.environ.get("AGENT_MEMORY_DB_URI")
if not DB_URI:
    st.error("Set AGENT_MEMORY_DB_URI environment variable")
    st.stop()

st.set_page_config(
    page_title="Agent Memory Dashboard",
    page_icon=":brain:",
    layout="wide",
    initial_sidebar_state="expanded",
)

# -- DB Connection --------------------------------------------------------
@st.cache_resource
def get_conn():
    conn = psycopg2.connect(DB_URI)
    conn.autocommit = True
    return conn

def query(sql, params=None):
    """Run a read query and return a DataFrame."""
    conn = get_conn()
    try:
        return pd.read_sql(sql, conn, params=params)
    except Exception:
        st.cache_resource.clear()
        conn = get_conn()
        return pd.read_sql(sql, conn, params=params)

@st.cache_data(ttl=120)
def query_cached(sql, _params_tuple=None):
    """Cached query for slow-changing data (distinct lists, counts)."""
    params = dict(_params_tuple) if _params_tuple else None
    return query(sql, params)

def distinct_values(table, column):
    """Get sorted distinct values for a column (cached)."""
    return query_cached(
        f"SELECT DISTINCT {column} FROM {table} ORDER BY {column}"
    )[column].tolist()

# -- Sidebar Navigation --------------------------------------------------
pages = [
    "Overview",
    "Memories",
    "Sessions",
    "Reflections & Lessons",
    "Pending Transcripts",
    "Backtest Results",
    "Strategies",
    "Knowledge Base",
    "Agents",
]
page = st.sidebar.radio("Navigate", pages)
st.sidebar.markdown("---")
if st.sidebar.button("Refresh data"):
    st.cache_data.clear()
    st.rerun()

# =========================================================================
# PAGE: Overview
# =========================================================================
if page == "Overview":
    st.title("Agent Memory -- Overview")

    counts = query_cached("""
        SELECT 'Sessions' as "Table", COUNT(*) as "Records" FROM sessions
        UNION ALL SELECT 'Memories', COUNT(*) FROM memories
        UNION ALL SELECT 'Active Memories', COUNT(*) FROM memories WHERE superseded_by IS NULL
        UNION ALL SELECT 'Reflections', COUNT(*) FROM reflections
        UNION ALL SELECT 'Lessons Learned', COUNT(*) FROM lessons_learned
        UNION ALL SELECT 'Pending Transcripts', COUNT(*) FROM pending_transcripts
        UNION ALL SELECT 'Backtest Results', COUNT(*) FROM backtest_results
        UNION ALL SELECT 'Strategies', COUNT(*) FROM strategies
        UNION ALL SELECT 'Papers', COUNT(*) FROM papers
        UNION ALL SELECT 'Paper Chunks', COUNT(*) FROM paper_chunks
    """)

    def get_count(label):
        return int(counts.loc[counts["Table"] == label, "Records"].iloc[0])

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Sessions", get_count("Sessions"))
    col2.metric("Active Memories", get_count("Active Memories"))
    col3.metric("Reflections", get_count("Reflections"))
    col4.metric("Lessons", get_count("Lessons Learned"))

    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Pending Transcripts", get_count("Pending Transcripts"))
    col6.metric("Backtest Results", get_count("Backtest Results"))
    col7.metric("Papers", get_count("Papers"))
    col8.metric("Paper Chunks", get_count("Paper Chunks"))

    st.markdown("---")

    c1, c2 = st.columns(2)

    with c1:
        st.subheader("Memory Importance Distribution")
        imp = query_cached("""
            SELECT importance, COUNT(*) as count
            FROM memories WHERE superseded_by IS NULL
            GROUP BY importance ORDER BY importance
        """)
        fig = px.bar(imp, x="importance", y="count",
                     color="importance",
                     color_continuous_scale=["#ef4444", "#f59e0b", "#22c55e"],
                     labels={"importance": "Importance (1-10)", "count": "Count"})
        fig.update_layout(showlegend=False, height=350)
        st.plotly_chart(fig, use_container_width=True)

    with c2:
        st.subheader("Memory Categories")
        cat = query_cached("""
            SELECT category, COUNT(*) as count
            FROM memories WHERE superseded_by IS NULL
            GROUP BY category ORDER BY count DESC
        """)
        fig = px.pie(cat, values="count", names="category", hole=0.4)
        fig.update_layout(height=350)
        st.plotly_chart(fig, use_container_width=True)

    c3, c4 = st.columns(2)

    with c3:
        st.subheader("Memory Domains")
        dom = query_cached("""
            SELECT domain, COUNT(*) as count
            FROM memories WHERE superseded_by IS NULL
            GROUP BY domain ORDER BY count DESC
        """)
        fig = px.bar(dom, x="domain", y="count", color="domain")
        fig.update_layout(showlegend=False, height=350)
        st.plotly_chart(fig, use_container_width=True)

    with c4:
        st.subheader("Sessions Over Time (weekly)")
        weekly = query_cached("""
            SELECT date_trunc('week', started_at)::date as week,
                   COUNT(*) as sessions
            FROM sessions GROUP BY week ORDER BY week
        """)
        if not weekly.empty:
            fig = px.area(weekly, x="week", y="sessions")
            fig.update_layout(height=350)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No session data yet.")

    st.subheader("Agent Provenance")
    prov = query_cached("""
        SELECT agent_id,
               (SELECT COUNT(*) FROM sessions s WHERE s.agent_id = a.agent_id) as sessions,
               (SELECT COUNT(*) FROM memories m WHERE m.agent_id = a.agent_id) as memories,
               (SELECT COUNT(*) FROM reflections r WHERE r.agent_id = a.agent_id) as reflections,
               display_name, agent_type, last_seen
        FROM agents a ORDER BY agent_id
    """)
    st.dataframe(prov, use_container_width=True, hide_index=True)

# =========================================================================
# PAGE: Memories
# =========================================================================
elif page == "Memories":
    st.title("Memories Explorer")

    f1, f2, f3, f4 = st.columns(4)
    with f1:
        categories = distinct_values("memories", "category")
        sel_cat = st.multiselect("Category", categories, default=categories)
    with f2:
        domains = distinct_values("memories", "domain")
        sel_dom = st.multiselect("Domain", domains, default=domains)
    with f3:
        imp_range = st.slider("Importance", 1, 10, (1, 10))
    with f4:
        show_superseded = st.checkbox("Show superseded", value=False)

    search = st.text_input("Search content (keyword)")

    if not sel_cat or not sel_dom:
        st.warning("Select at least one category and domain.")
        st.stop()

    where_clauses = ["category = ANY(%(cats)s)", "domain = ANY(%(doms)s)",
                     "importance >= %(imp_lo)s", "importance <= %(imp_hi)s"]
    params = {"cats": sel_cat, "doms": sel_dom, "imp_lo": imp_range[0], "imp_hi": imp_range[1]}

    if not show_superseded:
        where_clauses.append("superseded_by IS NULL")
    if search:
        where_clauses.append("search_text @@ plainto_tsquery('english', %(search)s)")
        params["search"] = search

    where_sql = " AND ".join(where_clauses)
    memories_df = query(f"""
        SELECT id, created_at, category, domain, importance, content,
               tags, agent_id, recalled_count, last_recalled,
               startup_count, inject_count, recall_count, useful_pct,
               error_signature, superseded_by, ttl_days, project
        FROM memories
        WHERE {where_sql}
        ORDER BY importance DESC, created_at DESC
    """, params)

    st.caption(f"{len(memories_df)} memories matched")

    if not memories_df.empty:
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Total", len(memories_df))
        s2.metric("Avg Importance", f"{memories_df['importance'].mean():.1f}")
        s3.metric("Never Recalled", int((memories_df['recalled_count'] == 0).sum()))
        s4.metric("Low Value (1-3)", int((memories_df['importance'] <= 3).sum()))

        # Exposure tracking metrics
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("Total Startup Exposures", int(memories_df['startup_count'].sum()))
        e2.metric("Total Inject Exposures", int(memories_df['inject_count'].sum()))
        e3.metric("Total Recall Exposures", int(memories_df['recall_count'].sum()))
        scored = memories_df['useful_pct'].notna().sum()
        avg_useful = memories_df['useful_pct'].mean() if scored > 0 else 0
        e4.metric("Avg Usefulness", f"{avg_useful:.0f}%" if scored > 0 else "No data")

    st.dataframe(
        memories_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "content": st.column_config.TextColumn("Content", width="large"),
            "created_at": st.column_config.DatetimeColumn("Created", format="YYYY-MM-DD HH:mm"),
            "tags": st.column_config.ListColumn("Tags"),
        },
    )

    st.markdown("---")
    st.subheader("Memory Quality Insights")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Low-value memories (importance 1-3)**")
        low = query("""
            SELECT id, importance, category, domain,
                   LEFT(content, 120) as content_preview, recalled_count
            FROM memories
            WHERE superseded_by IS NULL AND importance <= 3
            ORDER BY importance, recalled_count, created_at
        """)
        st.dataframe(low, use_container_width=True, hide_index=True)

    with c2:
        st.markdown("**Stale memories (never recalled, 30+ days old)**")
        stale = query("""
            SELECT id, importance, category, domain,
                   LEFT(content, 120) as content_preview,
                   created_at::date as created
            FROM memories
            WHERE superseded_by IS NULL
              AND recalled_count = 0
              AND created_at < NOW() - INTERVAL '30 days'
            ORDER BY created_at
        """)
        st.dataframe(stale, use_container_width=True, hide_index=True)

    # Exposure tracking section
    st.markdown("---")
    st.subheader("Exposure Tracking")

    # Exposure distribution by source
    exposure_dist = query("""
        SELECT source, COUNT(*) as count
        FROM memory_exposures
        GROUP BY source ORDER BY count DESC
    """)
    if not exposure_dist.empty:
        fig_exp = px.bar(exposure_dist, x="source", y="count",
                         title="Exposure Distribution by Source",
                         color="source",
                         color_discrete_map={"startup": "#636EFA", "inject": "#EF553B", "recall": "#00CC96"})
        st.plotly_chart(fig_exp, use_container_width=True)
    else:
        st.info("No exposure data yet. Data will appear after the next session.")

    n1, n2 = st.columns(2)
    with n1:
        st.markdown("**Noise Candidates** (inject > 10, never manually recalled)")
        noise = query("""
            SELECT id, LEFT(content, 120) as content_preview,
                   inject_count, recall_count, useful_pct, category, domain
            FROM memories
            WHERE superseded_by IS NULL
              AND inject_count > 10 AND recall_count = 0
            ORDER BY inject_count DESC
            LIMIT 20
        """)
        st.dataframe(noise, use_container_width=True, hide_index=True)

    with n2:
        st.markdown("**Dormant Memories** (never surfaced, 30+ days old)")
        dormant = query("""
            SELECT id, LEFT(content, 120) as content_preview,
                   importance, category, domain, created_at::date as created
            FROM memories
            WHERE superseded_by IS NULL
              AND startup_count = 0 AND inject_count = 0 AND recall_count = 0
              AND created_at < NOW() - INTERVAL '30 days'
            ORDER BY created_at
            LIMIT 20
        """)
        st.dataframe(dormant, use_container_width=True, hide_index=True)

    # Usefulness scores
    st.markdown("**Recently Scored Memories**")
    u1, u2 = st.columns(2)
    with u1:
        st.markdown("*Most useful*")
        most_useful = query("""
            SELECT m.id, LEFT(m.content, 120) as content_preview,
                   m.useful_pct, m.category, m.domain,
                   m.startup_count + m.inject_count + m.recall_count as total_exposures
            FROM memories m
            WHERE m.useful_pct IS NOT NULL
            ORDER BY m.useful_pct DESC, total_exposures DESC
            LIMIT 10
        """)
        st.dataframe(most_useful, use_container_width=True, hide_index=True)
    with u2:
        st.markdown("*Least useful*")
        least_useful = query("""
            SELECT m.id, LEFT(m.content, 120) as content_preview,
                   m.useful_pct, m.category, m.domain,
                   m.startup_count + m.inject_count + m.recall_count as total_exposures
            FROM memories m
            WHERE m.useful_pct IS NOT NULL
            ORDER BY m.useful_pct ASC, total_exposures DESC
            LIMIT 10
        """)
        st.dataframe(least_useful, use_container_width=True, hide_index=True)

# =========================================================================
# PAGE: Sessions
# =========================================================================
elif page == "Sessions":
    st.title("Sessions")

    f1, f2, f3 = st.columns(3)
    with f1:
        days_back = st.selectbox("Time range", [7, 14, 30, 90, 365, 9999],
                                 format_func=lambda x: "All time" if x == 9999 else f"Last {x} days",
                                 index=2)
    with f2:
        outcomes = query_cached(
            "SELECT DISTINCT outcome FROM sessions WHERE outcome IS NOT NULL"
        )["outcome"].tolist()
        outcomes.insert(0, "(all)")
        outcomes.append("(null)")
        sel_outcome = st.selectbox("Outcome", outcomes)
    with f3:
        agents_list = distinct_values("sessions", "agent_id")
        sel_agent = st.multiselect("Agent", agents_list, default=agents_list)

    if not sel_agent:
        st.warning("Select at least one agent.")
        st.stop()

    where_parts = ["agent_id = ANY(%(agents)s)"]
    params = {"agents": sel_agent}
    if days_back != 9999:
        where_parts.append(f"started_at > NOW() - INTERVAL '{days_back} days'")
    if sel_outcome == "(null)":
        where_parts.append("outcome IS NULL")
    elif sel_outcome != "(all)":
        where_parts.append("outcome = %(outcome)s")
        params["outcome"] = sel_outcome

    full_where = " AND ".join(where_parts)

    sessions_df = query(f"""
        SELECT session_id, conversation_id, started_at, ended_at, outcome, summary,
               tasks_completed, tags, token_estimate, agent_id, project,
               claude_session_id, transcript_path
        FROM sessions
        WHERE {full_where}
        ORDER BY started_at DESC
    """, params)

    st.caption(f"{len(sessions_df)} sessions")

    if not sessions_df.empty:
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Total", len(sessions_df))
        with_outcome = sessions_df[sessions_df["outcome"].notna()]
        s2.metric("With Outcome", len(with_outcome))
        s3.metric("Null Outcome", len(sessions_df) - len(with_outcome))
        if sessions_df["token_estimate"].notna().any():
            avg_tokens = sessions_df["token_estimate"].mean()
            s4.metric("Avg Tokens", f"{avg_tokens:,.0f}" if pd.notna(avg_tokens) else "N/A")
        else:
            s4.metric("Avg Tokens", "N/A")

        # Session identity tracking
        t1, t2, t3 = st.columns(3)
        has_conv_id = int(sessions_df["conversation_id"].notna().sum())
        has_claude_sid = int(sessions_df["claude_session_id"].notna().sum())
        has_transcript = int(sessions_df["transcript_path"].notna().sum())
        t1.metric("Has conversation_id", f"{has_conv_id}/{len(sessions_df)}")
        t2.metric("Has claude_session_id", f"{has_claude_sid}/{len(sessions_df)}")
        t3.metric("Has transcript_path", f"{has_transcript}/{len(sessions_df)}")

    st.dataframe(
        sessions_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "summary": st.column_config.TextColumn("Summary", width="large"),
            "started_at": st.column_config.DatetimeColumn("Started", format="YYYY-MM-DD HH:mm"),
            "ended_at": st.column_config.DatetimeColumn("Ended", format="YYYY-MM-DD HH:mm"),
            "tasks_completed": st.column_config.ListColumn("Tasks Completed"),
            "tags": st.column_config.ListColumn("Tags"),
            "claude_session_id": st.column_config.TextColumn("Claude Session ID", width="small"),
            "transcript_path": st.column_config.TextColumn("Transcript Path", width="medium"),
            "conversation_id": st.column_config.TextColumn("Conversation ID", width="small"),
        },
    )

    # Chart uses same filters as table
    st.subheader("Session Activity (filtered)")
    daily = query(f"""
        SELECT started_at::date as day, COUNT(*) as sessions
        FROM sessions
        WHERE {full_where}
        GROUP BY day ORDER BY day
    """, params)
    if not daily.empty:
        fig = px.bar(daily, x="day", y="sessions")
        fig.update_layout(height=300)
        st.plotly_chart(fig, use_container_width=True)

# =========================================================================
# PAGE: Reflections & Lessons
# =========================================================================
elif page == "Reflections & Lessons":
    st.title("Reflections & Lessons Learned")

    tab1, tab2 = st.tabs(["Reflections", "Lessons Learned"])

    with tab1:
        st.subheader("Reflections")
        f1, f2 = st.columns(2)
        with f1:
            ref_outcomes = ["(all)", "success", "partial", "failed"]
            sel_ref_out = st.selectbox("Outcome", ref_outcomes, key="ref_out")
        with f2:
            ref_domains = distinct_values("reflections", "domain")
            sel_ref_dom = st.multiselect("Domain", ref_domains, default=ref_domains, key="ref_dom")
        ref_tasks = distinct_values("reflections", "task_type")
        sel_ref_task = st.multiselect("Task Type (deselect to filter)", ref_tasks, default=ref_tasks, key="ref_task")

        if not sel_ref_dom or not sel_ref_task:
            st.warning("Select at least one domain and task type.")
            st.stop()

        ref_where = ["domain = ANY(%(doms)s)", "task_type = ANY(%(tasks)s)"]
        ref_params = {"doms": sel_ref_dom, "tasks": sel_ref_task}
        if sel_ref_out != "(all)":
            ref_where.append("outcome = %(outcome)s")
            ref_params["outcome"] = sel_ref_out

        refs = query(f"""
            SELECT id, created_at, task_type, domain, outcome,
                   what_worked, what_failed,
                   array_to_string(lessons, ' | ') as lessons,
                   confidence, agent_id,
                   array_to_string(tags, ', ') as tags
            FROM reflections
            WHERE {" AND ".join(ref_where)}
            ORDER BY created_at DESC
        """, ref_params)

        st.caption(f"{len(refs)} reflections")

        if not refs.empty:
            c1, c2 = st.columns([1, 3])
            with c1:
                out_counts = refs["outcome"].value_counts()
                fig = px.pie(values=out_counts.values, names=out_counts.index, hole=0.4)
                fig.update_layout(height=250, margin=dict(t=20, b=20))
                st.plotly_chart(fig, use_container_width=True)
            with c2:
                st.dataframe(refs, use_container_width=True, hide_index=True,
                             column_config={
                                 "what_worked": st.column_config.TextColumn("Worked", width="medium"),
                                 "what_failed": st.column_config.TextColumn("Failed", width="medium"),
                                 "lessons": st.column_config.TextColumn("Lessons", width="large"),
                                 "created_at": st.column_config.DatetimeColumn("Date", format="YYYY-MM-DD"),
                                 "tags": st.column_config.TextColumn("Tags"),
                             })

    with tab2:
        st.subheader("Lessons Learned")
        lessons = query("""
            SELECT id, title, principle, domain, confidence,
                   applied_count, last_applied, evidence,
                   error_signature, tags,
                   created_at, updated_at
            FROM lessons_learned
            ORDER BY confidence DESC, applied_count DESC
        """)

        st.caption(f"{len(lessons)} lessons")

        if not lessons.empty:
            s1, s2, s3 = st.columns(3)
            s1.metric("Total Lessons", len(lessons))
            s2.metric("Avg Confidence", f"{lessons['confidence'].mean():.1f}")
            s3.metric("Never Applied", int((lessons['applied_count'] == 0).sum()))

            st.dataframe(lessons, use_container_width=True, hide_index=True,
                         column_config={
                             "principle": st.column_config.TextColumn("Principle", width="large"),
                             "evidence": st.column_config.ListColumn("Evidence"),
                             "tags": st.column_config.ListColumn("Tags"),
                         })

# =========================================================================
# PAGE: Pending Transcripts
# =========================================================================
elif page == "Pending Transcripts":
    st.title("Pending Transcripts Pipeline")

    pt = query("""
        SELECT id, created_at, session_id, status, processed_at,
               processed_by, memories_created, user_message_count,
               total_size_bytes, had_compaction, agent_id,
               LEFT(compact_summary, 200) as summary_preview,
               error_message
        FROM pending_transcripts
        ORDER BY created_at DESC
    """)

    if not pt.empty:
        s1, s2, s3, s4 = st.columns(4)
        status_counts = pt["status"].value_counts()
        s1.metric("Total", len(pt))
        s2.metric("Completed", int(status_counts.get("completed", 0)))
        s3.metric("Pending", int(status_counts.get("pending", 0)))
        s4.metric("Failed/Skipped",
                  int(status_counts.get("failed", 0) + status_counts.get("skipped", 0)))

        total_mems = pt["memories_created"].sum()
        avg_mems = pt.loc[pt["status"] == "completed", "memories_created"].mean()
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Memories Created (total)", int(total_mems))
        m2.metric("Avg per Transcript", f"{avg_mems:.1f}" if pd.notna(avg_mems) else "N/A")
        avg_size = pt["total_size_bytes"].mean()
        m3.metric("Avg Size", f"{avg_size/1024:.0f} KB" if pd.notna(avg_size) else "N/A")
        m4.metric("Had Compaction", int(pt["had_compaction"].sum()))

    sel_status = st.multiselect("Status filter",
                                ["pending", "processing", "completed", "failed", "skipped"],
                                default=["pending", "processing", "completed", "failed", "skipped"])
    filtered = pt[pt["status"].isin(sel_status)] if not pt.empty else pt

    st.dataframe(
        filtered,
        use_container_width=True,
        hide_index=True,
        column_config={
            "summary_preview": st.column_config.TextColumn("Summary Preview", width="large"),
            "created_at": st.column_config.DatetimeColumn("Created", format="YYYY-MM-DD HH:mm"),
            "processed_at": st.column_config.DatetimeColumn("Processed", format="YYYY-MM-DD HH:mm"),
        },
    )

    failed = pt[pt["status"] == "failed"] if not pt.empty else pt
    if not failed.empty:
        st.subheader("Failed Transcripts")
        st.dataframe(failed[["id", "session_id", "error_message", "created_at"]],
                     use_container_width=True, hide_index=True)

# =========================================================================
# PAGE: Backtest Results
# =========================================================================
elif page == "Backtest Results":
    st.title("Backtest Results")

    bt = query("""
        SELECT id, created_at, session_id, ea_name, ea_version,
               symbol, timeframe, date_from, date_to, source,
               total_trades, profit_factor, net_profit, max_drawdown_pct,
               win_rate, sharpe_ratio, avg_trade, max_consec_losses,
               parameters, notes, tags, agent_id
        FROM backtest_results
        ORDER BY created_at DESC
    """)

    if bt.empty:
        st.info("No backtest results stored yet. Use `store-backtest` to add results.")
        st.stop()

    # Filters
    f1, f2, f3 = st.columns(3)
    with f1:
        ea_names = bt["ea_name"].dropna().unique().tolist()
        sel_ea = st.multiselect("EA Name", ea_names, default=ea_names)
    with f2:
        symbols = bt["symbol"].dropna().unique().tolist()
        sel_sym = st.multiselect("Symbol", symbols, default=symbols)
    with f3:
        sources = bt["source"].dropna().unique().tolist()
        sel_src = st.multiselect("Source", sources, default=sources)

    filtered = bt[
        bt["ea_name"].isin(sel_ea) &
        bt["symbol"].isin(sel_sym) &
        bt["source"].isin(sel_src)
    ]

    st.caption(f"{len(filtered)} results")

    if not filtered.empty:
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Total Results", len(filtered))
        pf_mean = filtered["profit_factor"].mean()
        s2.metric("Avg Profit Factor", f"{pf_mean:.2f}" if pd.notna(pf_mean) else "N/A")
        wr_mean = filtered["win_rate"].mean()
        s3.metric("Avg Win Rate", f"{wr_mean:.1f}%" if pd.notna(wr_mean) else "N/A")
        dd_mean = filtered["max_drawdown_pct"].mean()
        s4.metric("Avg Max DD", f"{dd_mean:.1f}%" if pd.notna(dd_mean) else "N/A")

        # Profit factor vs win rate scatter
        if filtered["profit_factor"].notna().any() and filtered["win_rate"].notna().any():
            st.subheader("Profit Factor vs Win Rate")
            fig = px.scatter(filtered, x="win_rate", y="profit_factor",
                             color="ea_name", size="total_trades",
                             hover_data=["symbol", "timeframe", "net_profit"],
                             labels={"win_rate": "Win Rate %", "profit_factor": "Profit Factor"})
            fig.update_layout(height=400)
            st.plotly_chart(fig, use_container_width=True)

    st.dataframe(
        filtered,
        use_container_width=True,
        hide_index=True,
        column_config={
            "created_at": st.column_config.DatetimeColumn("Date", format="YYYY-MM-DD HH:mm"),
            "net_profit": st.column_config.NumberColumn("Net Profit", format="$%.2f"),
            "profit_factor": st.column_config.NumberColumn("PF", format="%.2f"),
            "win_rate": st.column_config.NumberColumn("Win %", format="%.1f"),
            "max_drawdown_pct": st.column_config.NumberColumn("DD %", format="%.1f"),
            "tags": st.column_config.ListColumn("Tags"),
        },
    )

# =========================================================================
# PAGE: Strategies
# =========================================================================
elif page == "Strategies":
    st.title("Strategies & Evolution")

    tab1, tab2 = st.tabs(["Strategies", "Evolution History"])

    with tab1:
        strategies = query("""
            SELECT id, name, version, status, description,
                   entry_logic, exit_logic, risk_management,
                   default_params, tags, created_at
            FROM strategies
            ORDER BY name, created_at DESC
        """)

        if strategies.empty:
            st.info("No strategies registered yet.")
        else:
            s1, s2 = st.columns(2)
            s1.metric("Total Strategies", len(strategies))
            active = int((strategies["status"] == "active").sum())
            s2.metric("Active", active)

            st.dataframe(strategies, use_container_width=True, hide_index=True,
                         column_config={
                             "description": st.column_config.TextColumn("Description", width="large"),
                             "entry_logic": st.column_config.TextColumn("Entry", width="medium"),
                             "exit_logic": st.column_config.TextColumn("Exit", width="medium"),
                             "tags": st.column_config.ListColumn("Tags"),
                             "created_at": st.column_config.DatetimeColumn("Created", format="YYYY-MM-DD"),
                         })

    with tab2:
        evo = query("""
            SELECT se.id, s.name as strategy, se.change_type,
                   se.old_values, se.new_values, se.rationale,
                   se.backtest_id, se.agent_id, se.tags, se.created_at
            FROM strategy_evolution se
            JOIN strategies s ON s.id = se.strategy_id
            ORDER BY se.created_at DESC
        """)

        if evo.empty:
            st.info("No strategy evolution records yet.")
        else:
            st.caption(f"{len(evo)} evolution records")

            # Timeline
            fig = px.scatter(evo, x="created_at", y="strategy", color="change_type",
                             hover_data=["rationale"],
                             title="Strategy Evolution Timeline")
            fig.update_layout(height=300)
            st.plotly_chart(fig, use_container_width=True)

            st.dataframe(evo, use_container_width=True, hide_index=True,
                         column_config={
                             "rationale": st.column_config.TextColumn("Rationale", width="large"),
                             "created_at": st.column_config.DatetimeColumn("Date", format="YYYY-MM-DD HH:mm"),
                             "tags": st.column_config.ListColumn("Tags"),
                         })

# =========================================================================
# PAGE: Knowledge Base
# =========================================================================
elif page == "Knowledge Base":
    st.title("Knowledge Base (Papers)")

    papers = query("""
        SELECT p.id, p.title, p.author, p.doc_type, p.tags,
               COUNT(pc.id) as chunks, p.created_at
        FROM papers p
        LEFT JOIN paper_chunks pc ON pc.paper_id = p.id
        GROUP BY p.id ORDER BY p.created_at DESC
    """)

    if not papers.empty:
        s1, s2, s3 = st.columns(3)
        s1.metric("Papers", len(papers))
        s2.metric("Total Chunks", int(papers["chunks"].sum()))
        s3.metric("Avg Chunks/Paper", f"{papers['chunks'].mean():.0f}")

        c1, c2 = st.columns(2)
        with c1:
            if papers["doc_type"].notna().any():
                dtype = papers["doc_type"].value_counts()
                fig = px.pie(values=dtype.values, names=dtype.index, hole=0.4,
                             title="Document Types")
                fig.update_layout(height=300)
                st.plotly_chart(fig, use_container_width=True)
        with c2:
            fig = px.bar(papers.sort_values("chunks", ascending=False).head(20),
                         x="title", y="chunks", title="Top 20 Papers by Chunk Count")
            fig.update_layout(height=300, xaxis_tickangle=-45)
            st.plotly_chart(fig, use_container_width=True)

        st.dataframe(papers, use_container_width=True, hide_index=True,
                     column_config={
                         "tags": st.column_config.ListColumn("Tags"),
                         "created_at": st.column_config.DatetimeColumn("Added", format="YYYY-MM-DD"),
                     })

        # Paper detail viewer
        st.markdown("---")
        st.subheader("Paper Content Viewer")

        paper_options = {f"{r['title'][:80]} (id:{r['id']})": r['id'] for _, r in papers.iterrows()}
        sel_paper_label = st.selectbox("Select a paper", list(paper_options.keys()))
        sel_paper_id = paper_options[sel_paper_label]

        # Paper metadata
        paper_meta = query("""
            SELECT title, author, doc_type, source_file, tags, created_at
            FROM papers WHERE id = %(pid)s
        """, {"pid": int(sel_paper_id)})
        if not paper_meta.empty:
            pm = paper_meta.iloc[0]
            mc1, mc2, mc3 = st.columns(3)
            mc1.markdown(f"**Author:** {pm['author'] or 'N/A'}")
            mc2.markdown(f"**Type:** {pm['doc_type'] or 'N/A'}")
            mc3.markdown(f"**Added:** {pm['created_at']}")

        # Chunk search
        chunk_search = st.text_input("Search within paper (keyword)", key="chunk_search")

        chunks_query = """
            SELECT chunk_index, heading, content
            FROM paper_chunks
            WHERE paper_id = %(pid)s
        """
        chunks_params = {"pid": int(sel_paper_id)}
        if chunk_search:
            chunks_query += " AND content ILIKE %(q)s"
            chunks_params["q"] = f"%{chunk_search}%"
        chunks_query += " ORDER BY chunk_index"

        chunks_df = query(chunks_query, chunks_params)
        st.caption(f"{len(chunks_df)} chunks" + (f" matching '{chunk_search}'" if chunk_search else ""))

        if not chunks_df.empty:
            # Show chunks as expandable sections
            for _, chunk in chunks_df.iterrows():
                heading = chunk["heading"] or f"Chunk {chunk['chunk_index']}"
                with st.expander(f"[{chunk['chunk_index']}] {heading}", expanded=False):
                    st.markdown(chunk["content"])
    else:
        st.info("No papers in the knowledge base yet.")

# =========================================================================
# PAGE: Agents
# =========================================================================
elif page == "Agents":
    st.title("Agent Registry")

    agents_df = query("SELECT * FROM agents ORDER BY agent_id")
    st.dataframe(agents_df, use_container_width=True, hide_index=True,
                 column_config={
                     "created_at": st.column_config.DatetimeColumn("Registered", format="YYYY-MM-DD HH:mm"),
                     "last_seen": st.column_config.DatetimeColumn("Last Seen", format="YYYY-MM-DD HH:mm"),
                 })

    st.markdown("---")
    st.subheader("Per-Agent Activity")

    # Single query for all agent counts
    agent_counts = query("""
        SELECT a.agent_id, a.display_name,
               COALESCE(s.cnt, 0) as sessions,
               COALESCE(m.cnt, 0) as memories,
               COALESCE(r.cnt, 0) as reflections,
               COALESCE(pt.cnt, 0) as pending_transcripts
        FROM agents a
        LEFT JOIN (SELECT agent_id, COUNT(*) as cnt FROM sessions GROUP BY agent_id) s ON s.agent_id = a.agent_id
        LEFT JOIN (SELECT agent_id, COUNT(*) as cnt FROM memories GROUP BY agent_id) m ON m.agent_id = a.agent_id
        LEFT JOIN (SELECT agent_id, COUNT(*) as cnt FROM reflections GROUP BY agent_id) r ON r.agent_id = a.agent_id
        LEFT JOIN (SELECT agent_id, COUNT(*) as cnt FROM pending_transcripts GROUP BY agent_id) pt ON pt.agent_id = a.agent_id
        ORDER BY a.agent_id
    """)
    st.dataframe(agent_counts, use_container_width=True, hide_index=True)

    # Recent sessions per agent
    for _, agent in agents_df.iterrows():
        aid = agent["agent_id"]
        with st.expander(f"{agent['display_name']} ({aid}) -- Recent Sessions"):
            recent = query("""
                SELECT session_id, started_at, outcome, LEFT(summary, 100) as summary,
                       claude_session_id, conversation_id
                FROM sessions WHERE agent_id = %(aid)s
                ORDER BY started_at DESC LIMIT 5
            """, {"aid": aid})
            if not recent.empty:
                st.dataframe(recent, use_container_width=True, hide_index=True)
            else:
                st.info("No sessions for this agent yet.")
