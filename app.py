"""
Streamlit dashboard for the trading bot.

This is a read-only UI over the existing architecture. It does NOT
replace the engine, risk layer, backtester, or strategies — it
visualizes them.

Tabs:
- Markets:    live Polymarket scan, scored, filterable
- Paper:      paper trades from the SQLite/Postgres trade log
- Equity:     equity curve from recorded fills
- Strategies: per-strategy mode, parameters, recent signals
- Backtest:   inline backtest runner against resolution_arb
- Risk:       live risk state — limits, daily P&L, kill switch

Run:
    streamlit run app.py

Read-only by default. The "Kill Switch" button is the one write
action, and it talks to the same RiskManager state file the engine
reads at startup.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st

# Allow `streamlit run app.py` from project root
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import get_settings
from exchanges.polymarket import GammaClient
from strategies.polymarket.score_based import ScoreBasedStrategy, score_market

# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Trading Bot Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

settings = get_settings()


# ---------------------------------------------------------------------------
# Cached data fetchers
# ---------------------------------------------------------------------------
@st.cache_data(ttl=60)
def fetch_markets(min_volume_24h: float, limit: int) -> list[dict]:
    """Pull active markets via Gamma. Cached 60s — Streamlit reruns
    on every interaction; without this we'd hammer the API."""
    async def _go():
        async with GammaClient(settings.polymarket_gamma_host) as g:
            markets = await g.list_markets(
                active=True, limit=limit,
                min_volume_24h=Decimal(str(min_volume_24h)),
            )
            return [_market_to_dict(m) for m in markets]
    return asyncio.run(_go())


def _market_to_dict(m) -> dict:
    """Flatten a Market model for display."""
    md = m.metadata or {}
    end_str = m.end_date.strftime("%Y-%m-%d %H:%M") if m.end_date else "—"
    hours_left = None
    if m.end_date:
        delta = m.end_date.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
        hours_left = delta.total_seconds() / 3600
    score = score_market(
        volume_24h=float(md.get("volume_24h") or 0),
        liquidity=float(md.get("liquidity") or 0),
    )
    return {
        "Question": m.question or "—",
        "Outcome": m.outcome or "—",
        "End": end_str,
        "Hours left": f"{hours_left:.1f}" if hours_left else "—",
        "Volume 24h": float(md.get("volume_24h") or 0),
        "Liquidity": float(md.get("liquidity") or 0),
        "Score": score,
        "Slug": md.get("slug") or "",
        "URL": f"https://polymarket.com/event/{md.get('slug')}" if md.get("slug") else "",
        "market_id": m.market_id,
        "token_id": m.token_id or "",
    }


def _load_paper_trades() -> pd.DataFrame:
    """Read paper trades from SQLite (primary) with JSONL fallback.

    SQLite path: query OrderRow + FillRow joined for paper-mode orders.
    JSONL fallback: legacy data/paper_log.jsonl, used only if the DB has
    no rows (fresh install) or persistence isn't wired.
    """
    try:
        from data import get_persistence
        p = get_persistence(settings.database_url)
        orders = p.fetch_orders(limit=500, paper_only=True)
        if orders:
            rows = []
            for o in orders:
                rows.append({
                    "timestamp": o.created_at,
                    "strategy": o.strategy,
                    "market_id": o.market_id,
                    "side": o.side,
                    "price": float(o.price),
                    "size": float(o.size),
                    "notional_usd": float(o.price) * float(o.size),
                    "status": o.status,
                    "client_order_id": o.client_order_id,
                    "error": o.error or "",
                })
            return pd.DataFrame(rows)
    except Exception as e:
        st.warning(f"Could not read trades from SQLite: {e}")

    # JSONL fallback
    log_path = Path("data/paper_log.jsonl")
    if not log_path.exists():
        return pd.DataFrame()
    rows = []
    with log_path.open() as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def _load_equity_curve() -> pd.DataFrame:
    """Read equity points from SQLite. Returns empty DataFrame if none."""
    try:
        from data import get_persistence
        p = get_persistence(settings.database_url)
        points = p.fetch_equity_curve(limit=10000)
        if not points:
            return pd.DataFrame()
        rows = [{
            "timestamp": pt.timestamp,
            "cash_usd": float(pt.cash_usd),
            "positions_usd": float(pt.positions_usd),
            "total_equity_usd": float(pt.total_equity_usd),
        } for pt in points]
        return pd.DataFrame(rows)
    except Exception as e:
        st.warning(f"Could not read equity curve from SQLite: {e}")
        return pd.DataFrame()


def _load_open_positions() -> pd.DataFrame:
    try:
        from data import get_persistence
        p = get_persistence(settings.database_url)
        positions = p.list_open_positions()
        if not positions:
            return pd.DataFrame()
        rows = [{
            "platform": pos.platform,
            "market_id": pos.market_id,
            "size": float(pos.size),
            "avg_entry": float(pos.avg_entry),
            "realized_pnl": float(pos.realized_pnl or 0),
            "strategy": pos.strategy or "",
            "opened_at": pos.opened_at,
        } for pos in positions]
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### Trading Bot")
    st.caption(f"Environment: `{settings.environment}`")
    if settings.live_trading_enabled:
        st.error("⚠️ LIVE TRADING ENABLED")
    else:
        st.success("📝 Paper mode (safe)")

    st.markdown("---")
    st.markdown("**Risk caps**")
    st.write(f"Max position: ${settings.max_position_usd}")
    st.write(f"Max exposure: ${settings.max_total_exposure_usd}")
    st.write(f"Max daily loss: ${settings.max_daily_loss_usd}")

    st.markdown("---")
    st.markdown("**Filters**")
    min_vol = st.number_input("Min 24h volume ($)", value=1000.0, step=500.0)
    market_limit = st.slider("Max markets to fetch", 50, 500, 200, 50)

    if st.button("🔄 Refresh data"):
        st.cache_data.clear()
        st.rerun()


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_markets, tab_paper, tab_equity, tab_strategies, tab_backtest, tab_risk = st.tabs([
    "Markets", "Paper Trades", "Equity", "Strategies", "Backtest", "Risk",
])


# ----- Markets tab ---------------------------------------------------------
with tab_markets:
    st.subheader("Live Polymarket Scan")
    st.caption("Scored by the baseline ScoreBasedStrategy — a benchmark, "
               "not the primary edge. Use Strategies tab to see real strategies.")

    try:
        markets = fetch_markets(min_vol, market_limit)
    except Exception as e:
        st.error(f"Failed to fetch markets: {e}")
        markets = []

    if not markets:
        st.info("No markets found. Try lowering min volume or increasing limit.")
    else:
        df = pd.DataFrame(markets)
        df = df.sort_values("Score", ascending=False)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Markets", len(df))
        c2.metric("Median liquidity", f"${df['Liquidity'].median():,.0f}")
        c3.metric("Median 24h vol", f"${df['Volume 24h'].median():,.0f}")
        c4.metric("Avg score", f"{df['Score'].mean():.3f}")

        st.dataframe(
            df[["Question", "Outcome", "End", "Hours left",
                "Volume 24h", "Liquidity", "Score", "URL"]],
            use_container_width=True,
            hide_index=True,
            column_config={
                "Volume 24h": st.column_config.NumberColumn(format="$%.0f"),
                "Liquidity": st.column_config.NumberColumn(format="$%.0f"),
                "Score": st.column_config.NumberColumn(format="%.3f"),
                "URL": st.column_config.LinkColumn(display_text="open"),
            },
        )


# ----- Paper Trades tab ----------------------------------------------------
with tab_paper:
    st.subheader("Paper Trade Log")
    st.caption("Reads from SQLite (configured via DATABASE_URL). "
               "Falls back to data/paper_log.jsonl if the DB is empty.")

    df = _load_paper_trades()
    if df.empty:
        st.info("No paper trades yet. Run: "
                "`python scripts/run.py --strategies polymarket_resolution_arb --mode paper`")
    else:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Trades", len(df))
        c2.metric("Total notional", f"${df['notional_usd'].sum():,.2f}"
                  if "notional_usd" in df.columns else "—")
        if "status" in df.columns:
            c3.metric("Filled", int((df["status"] == "filled").sum()))
            c4.metric("Rejected", int((df["status"] == "rejected").sum()))

        # Optional strategy filter
        if "strategy" in df.columns and df["strategy"].nunique() > 1:
            chosen = st.multiselect("Filter strategies",
                                    sorted(df["strategy"].unique()),
                                    default=sorted(df["strategy"].unique()))
            df = df[df["strategy"].isin(chosen)]

        st.dataframe(df, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown("**Open positions**")
    pos_df = _load_open_positions()
    if pos_df.empty:
        st.info("No open positions.")
    else:
        st.dataframe(pos_df, use_container_width=True, hide_index=True)


# ----- Equity tab ----------------------------------------------------------
with tab_equity:
    st.subheader("Equity Curve")
    st.caption("Equity snapshots are written by the engine every N ticks. "
               "Reads from the SQLite equity_points table.")
    df = _load_equity_curve()
    if df.empty:
        st.info("No equity data yet. Run the engine in paper mode for at least "
                "a few ticks (default snapshot interval is every 10 ticks).")
    else:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.sort_values("timestamp")

        c1, c2, c3 = st.columns(3)
        c1.metric("Latest equity", f"${df['total_equity_usd'].iloc[-1]:,.2f}")
        c2.metric("Cash", f"${df['cash_usd'].iloc[-1]:,.2f}")
        c3.metric("Position value", f"${df['positions_usd'].iloc[-1]:,.2f}")

        st.line_chart(df.set_index("timestamp")[
            ["total_equity_usd", "cash_usd", "positions_usd"]
        ])
        with st.expander("Raw equity points"):
            st.dataframe(df, use_container_width=True, hide_index=True)


# ----- Strategies tab ------------------------------------------------------
with tab_strategies:
    st.subheader("Strategies")
    st.caption("Read-only view of strategies the engine knows about. "
               "Modes are configured in code, not here — by design.")
    rows = [
        {"Strategy": "polymarket_resolution_arb", "Type": "real edge",
         "Default mode": "DISABLED", "Notes": "Boring, real edge. Capital-locked till resolution."},
        {"Strategy": "polymarket_copy_trade", "Type": "real edge",
         "Default mode": "DISABLED", "Notes": "Edge depends on whale selection."},
        {"Strategy": "polymarket_cross_platform_arb", "Type": "stub",
         "Default mode": "DISABLED", "Notes": "Needs Kalshi + odds API clients."},
        {"Strategy": "polymarket_news_event", "Type": "stub",
         "Default mode": "DISABLED", "Notes": "Hardest. Build last."},
        {"Strategy": "polymarket_score_based", "Type": "baseline",
         "Default mode": "DISABLED", "Notes": "Benchmark only. Compare real strategies against this."},
        {"Strategy": "solana_jupiter_arb", "Type": "stub",
         "Default mode": "DISABLED", "Notes": "Most edge captured by MEV bots."},
        {"Strategy": "solana_copy_trade", "Type": "stub",
         "Default mode": "DISABLED", "Notes": "Same idea as Polymarket version."},
    ]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ----- Backtest tab --------------------------------------------------------
with tab_backtest:
    st.subheader("Inline Backtest")
    st.caption("Runs resolution_arb against historical resolved markets. "
               "First run downloads + caches data; subsequent runs are fast.")

    c1, c2, c3 = st.columns(3)
    with c1:
        bt_markets = st.number_input("Markets", value=50, min_value=10, max_value=500, step=10)
    with c2:
        bt_target = st.number_input("Target $/signal", value=25.0, step=5.0)
    with c3:
        bt_min_edge = st.number_input("Min edge ($/share)", value=0.015, step=0.005, format="%.3f")

    if st.button("▶️ Run backtest"):
        from backtest.engine import Backtester, BacktestConfig
        from backtest.history import HistoryFetcher
        from risk.manager import RiskLimits
        from strategies.polymarket.resolution_arb import ResolutionArbStrategy

        async def _run_bt():
            fetcher = HistoryFetcher(
                clob_host=settings.polymarket_host,
                gamma_host=settings.polymarket_gamma_host,
            )
            with st.spinner(f"Fetching {bt_markets} markets..."):
                hist = await fetcher.fetch_resolved_set(max_markets=bt_markets)
            if not hist:
                st.error("No usable historical markets returned.")
                return None
            st.success(f"Fetched {len(hist)} markets")

            def factory(ex):
                return ResolutionArbStrategy(
                    exchange=ex,
                    min_edge_per_share=Decimal(str(bt_min_edge)),
                    max_hours_to_resolution=72.0,
                    target_size_usd=Decimal(str(bt_target)),
                    min_book_depth_usd=Decimal("1"),
                    min_volume_24h=Decimal("0"),
                )
            bt = Backtester(BacktestConfig(spread=Decimal("0.01")))
            with st.spinner("Running backtest..."):
                return await bt.run(factory, hist, RiskLimits(
                    max_position_usd=Decimal(str(settings.max_position_usd)),
                    max_total_exposure_usd=Decimal(str(settings.max_total_exposure_usd)),
                    max_daily_loss_usd=Decimal("1000000"),
                    max_open_positions=999,
                    kill_on_daily_loss=False,
                ))

        result = asyncio.run(_run_bt())
        if result:
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Trades", len(result.realized_trades))
            c2.metric("Total PnL", f"${result.total_pnl:.2f}")
            c3.metric("Win rate", f"{result.win_rate*100:.1f}%")
            c4.metric("ROI on deployed", f"{result.roi_pct:.2f}%")

            if result.realized_trades:
                rows = []
                for t in result.realized_trades:
                    rows.append({
                        "Question": (t.question or "")[:80],
                        "Side": t.side.value,
                        "Entry": float(t.entry_price),
                        "Exit": float(t.exit_price or 0),
                        "Size": float(t.size),
                        "PnL": float(t.pnl_usd or 0),
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

            st.warning(
                "⚠️ Synthetic infinite-depth book and 1¢ spread. "
                "Discount real-world results by 30–50% for slippage."
            )


# ----- Risk tab ------------------------------------------------------------
with tab_risk:
    st.subheader("Risk Limits & Kill Switch")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Configured limits**")
        st.write({
            "Max position (USD)": float(settings.max_position_usd),
            "Max total exposure (USD)": float(settings.max_total_exposure_usd),
            "Max daily loss (USD)": float(settings.max_daily_loss_usd),
            "Max open positions": settings.max_open_positions,
            "Kill on daily loss": settings.kill_on_daily_loss,
        })
    with c2:
        st.markdown("**Kill switch**")
        st.caption("This UI button is convenience. The authoritative kill switch "
                   "is `python scripts/kill.py` — it works even if the dashboard "
                   "is unreachable.")
        if st.button("🛑 Kill all (run scripts/kill.py)", type="primary"):
            st.code("python scripts/kill.py", language="bash")
            st.info("Run that command in your terminal to flatten and disable.")
