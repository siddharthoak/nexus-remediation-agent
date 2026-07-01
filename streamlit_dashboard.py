"""
OSS Vulnerability Remediation Agent — Observability Dashboard

Co-developed by Neurealm for Premier Inc.
Read-only. Connects to the tracking store and knowledge base configured by
DEPLOYMENT_MODE / COSMOS_ENDPOINT.

Usage:
    # Azure (production):
    export DEPLOYMENT_MODE=azure
    export COSMOS_ENDPOINT=https://<account>.documents.azure.com:443/
    az login
    streamlit run streamlit_dashboard.py

    # Local (InMemory — useful for layout checks, shows only playbook KB entries):
    export DEPLOYMENT_MODE=local
    streamlit run streamlit_dashboard.py
"""

import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone

import pandas as pd
# Prevent pandas 3.0 Arrow-backed StringDtype SIGBUS on macOS ARM64.
pd.set_option("future.infer_string", False)
import streamlit as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "agents"))

from common.tracking_store import make_tracking_store, TrackingStatus  # noqa: E402
from common.knowledge_store import make_knowledge_store                 # noqa: E402

# ── Brand config ──────────────────────────────────────────────────────────────
_NEUREALM_NAVY  = "#0d2137"
_NEUREALM_BLUE  = "#3b82f6"
_PREMIER_BLUE   = "#003087"
_PREMIER_GOLD   = "#f0ab00"

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="OSS Remediation Agent",
    page_icon="🛡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Brand CSS ─────────────────────────────────────────────────────────────────
st.markdown(f"""
<style>
  /* Co-brand header strip */
  .brand-strip {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.6rem 1.2rem;
    background: linear-gradient(90deg, {_NEUREALM_NAVY} 0%, {_PREMIER_BLUE} 100%);
    border-radius: 8px;
    margin-bottom: 0.5rem;
  }}
  .brand-neurealm {{
    font-size: 1.15rem;
    font-weight: 700;
    color: #ffffff;
    letter-spacing: 0.04em;
  }}
  .brand-neurealm span {{
    color: {_NEUREALM_BLUE};
  }}
  .brand-divider {{
    color: rgba(255,255,255,0.35);
    font-size: 1.4rem;
  }}
  .brand-premier {{
    font-size: 1.05rem;
    font-weight: 600;
    color: #ffffff;
    letter-spacing: 0.02em;
  }}
  .brand-premier span {{
    color: {_PREMIER_GOLD};
  }}

  /* Status badge chips */
  .badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.78rem;
    font-weight: 600;
  }}
  .badge-green  {{ background:#d1fae5; color:#065f46; }}
  .badge-red    {{ background:#fee2e2; color:#991b1b; }}
  .badge-yellow {{ background:#fef3c7; color:#92400e; }}
  .badge-blue   {{ background:#dbeafe; color:#1e40af; }}
  .badge-gray   {{ background:#f3f4f6; color:#374151; }}
  .badge-orange {{ background:#ffedd5; color:#9a3412; }}

  /* Sub-title */
  .page-subtitle {{
    color: #6b7280;
    font-size: 0.92rem;
    margin-top: -0.4rem;
    margin-bottom: 0.8rem;
  }}

  /* Metric label tweak */
  [data-testid="stMetricLabel"] {{ font-size: 0.82rem !important; }}
</style>
""", unsafe_allow_html=True)

# ── Co-brand header ───────────────────────────────────────────────────────────
st.markdown("""
<div class="brand-strip">
  <div class="brand-neurealm">Neu<span>realm</span></div>
  <div class="brand-divider">×</div>
  <div class="brand-premier">Premier <span>Inc.</span></div>
</div>
""", unsafe_allow_html=True)

st.title("OSS Vulnerability Remediation Agent")
st.markdown(
    '<p class="page-subtitle">Autonomous dependency security for Java repositories — '
    'powered by Neurealm for Premier Inc.</p>',
    unsafe_allow_html=True,
)

# ── Sidebar — deployment info + status ────────────────────────────────────────
with st.sidebar:
    st.markdown(f"""
    <div style="background:{_NEUREALM_NAVY};padding:10px 12px;border-radius:6px;margin-bottom:12px;">
      <span style="color:{_NEUREALM_BLUE};font-weight:700;font-size:0.95rem;">Neu</span>
      <span style="color:#fff;font-weight:700;font-size:0.95rem;">realm</span>
      <span style="color:rgba(255,255,255,0.5)"> for </span>
      <span style="color:{_PREMIER_GOLD};font-weight:700;font-size:0.95rem;">Premier</span>
    </div>
    """, unsafe_allow_html=True)

    mode = os.environ.get("DEPLOYMENT_MODE", "")
    cosmos_ep = os.environ.get("COSMOS_ENDPOINT", "")
    if mode == "azure" or (not mode and cosmos_ep):
        st.success("Connected to Azure Cosmos DB")
        if cosmos_ep:
            acct = cosmos_ep.split("//")[-1].split(".")[0]
            st.caption(f"Account: `{acct}`")
    else:
        st.warning("Local mode — InMemory store  \n_Set DEPLOYMENT\\_MODE=azure for production data_")

    st.caption(f"DEPLOYMENT_MODE: `{mode or '(unset)'}`")
    st.divider()
    st.caption("Auto-refreshes every 30 s via cache TTL.  \nClick **↺ Refresh** to force.")


# ── Status color helpers ──────────────────────────────────────────────────────

_STATUS_ICON = {
    TrackingStatus.CI_PASSED.value:          "🟢",
    TrackingStatus.CI_PENDING.value:         "🟡",
    TrackingStatus.CI_FAILED.value:          "🔴",
    TrackingStatus.RETRY_REQUESTED.value:    "🔵",
    TrackingStatus.FAILED_MAX_RETRIES.value: "⛔",
    TrackingStatus.ESCALATED.value:          "⚠️",
    TrackingStatus.CREATED.value:            "⚪",
    TrackingStatus.PR_OPENED.value:          "🟤",
}

_LINEAGE_ICON = {
    TrackingStatus.CI_PASSED.value:          "✅",
    TrackingStatus.CI_FAILED.value:          "❌",
    TrackingStatus.RETRY_REQUESTED.value:    "🔄",
    TrackingStatus.FAILED_MAX_RETRIES.value: "🚫",
    TrackingStatus.ESCALATED.value:          "⚠️",
    TrackingStatus.CI_PENDING.value:         "⏳",
    TrackingStatus.PR_OPENED.value:          "📬",
    TrackingStatus.CREATED.value:            "🆕",
}


# ── Data loaders ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=30)
def load_records() -> pd.DataFrame:
    store = make_tracking_store()
    records = store.get_all()
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame([asdict(r) for r in records])

    if "token_usage" in df.columns:
        df["prompt_tokens"]     = df["token_usage"].apply(
            lambda x: x.get("prompt_tokens", 0)     if isinstance(x, dict) else 0)
        df["completion_tokens"] = df["token_usage"].apply(
            lambda x: x.get("completion_tokens", 0) if isinstance(x, dict) else 0)
        df["total_tokens"]      = df["prompt_tokens"] + df["completion_tokens"]

    return df


@st.cache_data(ttl=60)
def load_kb_entries() -> list:
    try:
        return make_knowledge_store().get_all()
    except Exception as exc:
        st.warning(f"Could not load KB entries: {exc}")
        return []


df = load_records()

col_refresh, col_count = st.columns([1, 9])
with col_refresh:
    if st.button("↺ Refresh"):
        load_records.clear()
        load_kb_entries.clear()
        st.rerun()

# ── Top-of-page status banner ─────────────────────────────────────────────────
if not df.empty:
    created_df = df[df["status"] == TrackingStatus.CREATED.value]
    if not created_df.empty:
        st.info(
            f"⚙️ **Fixer is processing** — {len(created_df)} finding(s) across "
            f"{created_df['component_name'].nunique()} component(s) are queued or in flight.  \n"
            "Records update as PRs are opened. **Auto-refreshing every 10 s.**"
        )
        import streamlit.components.v1 as components
        components.html("<meta http-equiv='refresh' content='10'>", height=0)

if df.empty:
    st.info(
        "No tracking records yet.  \n\n"
        "Start the agents (or trigger a scan), and records will appear here once the Fixer "
        "picks up the Nexus report.  \n\n"
        f"Deployment mode: `{os.environ.get('DEPLOYMENT_MODE', 'unset')}`"
    )
else:
    pr_col = df["pr_number"].dropna() if "pr_number" in df.columns else pd.Series([], dtype=float)
    with col_count:
        st.caption(f"{len(df)} total record(s) across {pr_col.nunique()} PR(s)")


# ── Tabs (always rendered — each handles df.empty independently) ──────────────

tab_history, tab_lineage, tab_metrics, tab_kb = st.tabs([
    "Run History",
    "Retry Lineage",
    "Metrics",
    "Knowledge Base",
])


# ── Tab 1: Run History ────────────────────────────────────────────────────────

with tab_history:
    st.subheader("All Fix Attempts")

    if df.empty:
        st.info("No fix attempts recorded yet.")
    else:
        col_repo, col_status, col_component = st.columns(3)

        with col_repo:
            repos = ["(all)"] + sorted(df["repo"].dropna().unique().tolist())
            repo_filter = st.selectbox("Repository", repos)

        with col_status:
            statuses = ["(all)"] + sorted(df["status"].dropna().unique().tolist())
            status_filter = st.selectbox("Status", statuses)

        with col_component:
            comps = ["(all)"] + sorted(df["component_name"].dropna().unique().tolist())
            component_filter = st.selectbox("Component", comps)

        view = df.copy()
        if repo_filter != "(all)":
            view = view[view["repo"] == repo_filter]
        if status_filter != "(all)":
            view = view[view["status"] == status_filter]
        if component_filter != "(all)":
            view = view[view["component_name"] == component_filter]

        display_cols = [
            "created_at", "repo", "component_name", "old_version", "new_version",
            "status", "attempt_number", "pr_number", "branch_name",
            "time_to_resolution_seconds", "total_tokens", "tracking_id",
        ]
        display_cols = [c for c in display_cols if c in view.columns]

        view = view.copy()
        view["status"] = view["status"].apply(
            lambda s: f"{_STATUS_ICON.get(s, '•')} {s}"
        )

        st.dataframe(
            view[display_cols].sort_values("created_at", ascending=False),
            use_container_width=True,
            hide_index=True,
            column_config={
                "created_at":                 st.column_config.DatetimeColumn("Created",       format="MMM D, HH:mm"),
                "time_to_resolution_seconds": st.column_config.NumberColumn("Resolution (s)",  format="%d s"),
                "total_tokens":               st.column_config.NumberColumn("Tokens"),
                "pr_number":                  st.column_config.NumberColumn("PR #",            format="%d"),
                "attempt_number":             st.column_config.NumberColumn("Attempt"),
            },
        )
        st.caption(f"{len(view)} record(s) shown of {len(df)} total.")


# ── Tab 2: Retry Lineage ──────────────────────────────────────────────────────

with tab_lineage:
    st.subheader("Retry Lineage by PR")

    if df.empty or "pr_number" not in df.columns:
        st.info("No PRs with tracking records yet.")
    else:
        pr_numbers = sorted(df["pr_number"].dropna().astype(int).unique().tolist())
        if not pr_numbers:
            st.info("No PRs opened yet — tracking records exist but no PR numbers assigned.")
        else:
            selected_pr = st.selectbox(
                "Select PR number", pr_numbers, format_func=lambda n: f"PR #{n}"
            )

            lineage = df[df["pr_number"] == selected_pr].sort_values("attempt_number")
            st.markdown(f"**{len(lineage)} attempt(s)** for PR #{selected_pr}")

            for _, row in lineage.iterrows():
                status = row["status"]
                icon   = _LINEAGE_ICON.get(status, "•")
                ts     = pd.to_datetime(row["created_at"]).strftime("%Y-%m-%d %H:%M UTC")

                with st.expander(
                    f"{icon} Attempt {int(row['attempt_number'])} — {status} ({ts})"
                ):
                    c1, c2 = st.columns(2)
                    c1.metric("Component",      row["component_name"])
                    c1.metric("Version change", f"{row['old_version']} → {row['new_version']}")
                    c2.metric("Branch",         row.get("branch_name") or "—")
                    c2.metric("Tracking ID",    str(row["tracking_id"])[:8] + "…")

                    if row.get("parent_tracking_id"):
                        st.caption(f"Parent attempt: `{str(row['parent_tracking_id'])[:8]}…`")

                    if row.get("time_to_resolution_seconds") is not None:
                        st.metric(
                            "Time to this outcome",
                            f"{row['time_to_resolution_seconds'] / 60:.1f} min",
                        )

                    if isinstance(row.get("token_usage"), dict):
                        tu    = row["token_usage"]
                        total = tu.get("prompt_tokens", 0) + tu.get("completion_tokens", 0)
                        st.metric(
                            "Token usage", f"{total:,}",
                            help=(
                                f"Prompt: {tu.get('prompt_tokens',0):,}  |  "
                                f"Completion: {tu.get('completion_tokens',0):,}"
                            ),
                        )

                    if row.get("failure_log_excerpt"):
                        st.markdown("**CI failure excerpt passed to retry prompt:**")
                        st.code(row["failure_log_excerpt"][:2000], language="text")


# ── Tab 3: Metrics ────────────────────────────────────────────────────────────

with tab_metrics:
    st.subheader("Run Metrics")

    if df.empty or "pr_number" not in df.columns:
        st.info("No metrics yet — run data will appear here once PRs are opened.")
    else:
        latest = (
            df.sort_values("attempt_number")
              .groupby("pr_number")
              .last()
              .reset_index()
        )

        total_prs   = len(latest)
        resolved    = (latest["status"] == TrackingStatus.CI_PASSED.value).sum()
        escalated   = latest["status"].isin([
            TrackingStatus.FAILED_MAX_RETRIES.value,
            TrackingStatus.ESCALATED.value,
        ]).sum()
        in_progress = total_prs - resolved - escalated
        rate        = resolved / total_prs * 100 if total_prs else 0.0

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("PRs opened",      total_prs)
        m2.metric("Resolved ✅",      resolved,   help="Status = CI_PASSED")
        m3.metric("In progress ⏳",   in_progress)
        m4.metric("Escalated ⛔",     escalated,  help="FAILED_MAX_RETRIES or ESCALATED")
        m5.metric("Resolution rate", f"{rate:.1f}%")

        st.divider()

        resolved_df = latest[latest["status"] == TrackingStatus.CI_PASSED.value]
        if not resolved_df.empty and "time_to_resolution_seconds" in resolved_df.columns:
            valid = resolved_df["time_to_resolution_seconds"].dropna()
            if not valid.empty:
                t1, t2, t3 = st.columns(3)
                t1.metric("Avg time-to-resolution", f"{valid.mean() / 60:.1f} min")
                t2.metric("p50",                    f"{valid.median() / 60:.1f} min")
                t3.metric("p95",                    f"{valid.quantile(0.95) / 60:.1f} min")
                st.divider()

        if "total_tokens" in df.columns:
            total_tokens = int(df["total_tokens"].sum())
            avg_per_pr   = df.groupby("pr_number")["total_tokens"].sum().mean()
            tk1, tk2 = st.columns(2)
            tk1.metric("Total tokens consumed", f"{total_tokens:,}")
            tk2.metric("Avg tokens per PR",     f"{avg_per_pr:,.0f}" if not pd.isna(avg_per_pr) else "—")

            st.markdown("**Token usage by attempt number**")
            token_by_attempt = (
                df.groupby("attempt_number")["total_tokens"]
                  .sum()
                  .reset_index()
                  .rename(columns={"attempt_number": "Attempt", "total_tokens": "Total tokens"})
            )
            st.bar_chart(token_by_attempt.set_index("Attempt"))
            st.divider()

        st.markdown("**Attempt status distribution**")
        status_counts = df["status"].value_counts().reset_index()
        status_counts.columns = ["Status", "Count"]
        st.bar_chart(status_counts.set_index("Status"))

        if "attempt_number" in df.columns:
            st.markdown("**Retry depth per PR** (max attempts used)")
            depth = (
                df.groupby("pr_number")["attempt_number"]
                  .max()
                  .value_counts()
                  .sort_index()
                  .reset_index()
            )
            depth.columns = ["Max attempts", "PR count"]
            st.bar_chart(depth.set_index("Max attempts"))


# ── Tab 4: Knowledge Base ─────────────────────────────────────────────────────

with tab_kb:
    st.subheader("Knowledge Base")

    kb_entries = load_kb_entries()

    _SOURCE_LABEL = {
        "tier1_learned":   "🟢 Tier 1 — Learned",
        "tier2_playbook":  "📖 Tier 2 — Playbook",
        "knowledge_agent": "🤖 Knowledge Agent",
    }
    _CONFIDENCE_ICON = {"high": "🔵", "medium": "🟡", "low": "🔴"}

    if not kb_entries:
        st.info(
            "No KB entries yet.  \n\n"
            "**Tier 2 playbook** entries appear once the fixer runs (loaded from `playbooks/*.yaml`).  \n"
            "**Knowledge Agent** entries are added during each fresh scan.  \n"
            "**Tier 1 learned** entries are added by the Watcher after a PR passes CI."
        )
    else:
        source_counts: dict = {}
        for e in kb_entries:
            source_counts[e.source] = source_counts.get(e.source, 0) + 1

        c1, c2, c3 = st.columns(3)
        c1.metric("Tier 1 — Learned",   source_counts.get("tier1_learned", 0))
        c2.metric("Tier 2 — Playbooks", source_counts.get("tier2_playbook", 0))
        c3.metric("Knowledge Agent",    source_counts.get("knowledge_agent", 0))

        st.divider()

        source_filter = st.selectbox(
            "Filter by source",
            ["(all)"] + sorted({e.source for e in kb_entries}),
        )
        filtered = kb_entries if source_filter == "(all)" else [
            e for e in kb_entries if e.source == source_filter
        ]

        _tier_order = {"tier1_learned": 3, "tier2_playbook": 2, "knowledge_agent": 1}
        for entry in sorted(filtered, key=lambda e: _tier_order.get(e.source, 0), reverse=True):
            label = (
                f"{_SOURCE_LABEL.get(entry.source, entry.source)}  |  "
                f"{entry.component_name}  |  "
                f"{entry.from_version or f'major {entry.from_major}'} → "
                f"{entry.to_version or f'major {entry.to_major}'}  |  "
                f"{_CONFIDENCE_ICON.get(entry.confidence, '')} {entry.confidence}"
            )
            with st.expander(label):
                c1, c2 = st.columns(2)
                c1.write(f"**Entry ID:** `{entry.entry_id[:8]}…`")
                c2.write(f"**Source:** `{entry.source}`")

                if entry.breaking_changes:
                    st.markdown("**Breaking changes:**")
                    for bc in entry.breaking_changes:
                        st.markdown(f"- {bc}")

                if entry.migration_steps:
                    st.markdown("**Migration steps:**")
                    for i, step in enumerate(entry.migration_steps, 1):
                        st.markdown(f"{i}. {step}")

                if entry.patterns:
                    st.markdown(f"**Find → replace patterns ({len(entry.patterns)}):**")
                    for p in entry.patterns:
                        col_f, col_r = st.columns(2)
                        col_f.code(p.get("find", ""),    language="java")
                        col_r.code(p.get("replace", ""), language="java")
                        if p.get("description"):
                            st.caption(p["description"])

                if entry.api_removals:
                    st.markdown("**Removed APIs:**")
                    st.code("\n".join(entry.api_removals), language="text")
