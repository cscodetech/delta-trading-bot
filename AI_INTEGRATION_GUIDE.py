"""
HOW TO INTEGRATE ai_brain.py INTO bot.py
==========================================

STEP 1 — Add to .env file:
    ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx

    OR (recommended) set keys/providers in the Dashboard:
      Settings → AI Brain → save Anthropic/Gemini keys (stored in DB)

    Optional: you can run without AI by keeping AI Brain OFF.

STEP 2 — pip install anthropic:
    pip install anthropic --break-system-packages

STEP 3 — In bot.py, add import at the top:
    from ai_brain import AIBrain, AIDecision

STEP 4 — In TradingBot.__init__(), add:
    self.ai = AIBrain()

STEP 5 — Replace the try_entry() signal aggregation block.
    Find this section in try_entry():

        # ── Aggregate signals (OLD WAY) ──────────────────
        trend_bias = get_trend_bias(df_trend) if df_trend is not None else None
        confirm_ok = check_confirmation_tf(df_confirm, ...)
        agg = aggregate_signals(signals, regime_weights, trend_bias, confirm_ok)

        if not agg["direction"]:
            return
        direction = agg["direction"]

    REPLACE with the patched version below (copy from _patched_try_entry).

STEP 6 — Add daily review call in the daily report section of bot.py:
    Find where daily stats are logged (search for "last_report_date") and add:

        if self.ai:
            review = self.ai.review_losing_trades(self.risk.trade_history)
            log.info(f"AI Daily Review:\\n{review}")
            alerts.send(f"📊 AI Daily Review:\\n{review}")

STEP 7 — In alerts.py, use AI explanation for trade alerts:
    Find where you send the entry alert and replace with:

        msg = self.ai.explain_trade(
            symbol, direction, entry_price, sl_tp["sl_price"], sl_tp["tp_price"],
            decision.reasoning
        )
        alerts.send(msg)
"""

# ═══════════════════════════════════════════════════════════════
# PATCHED try_entry() — replace the aggregation block in bot.py
# ═══════════════════════════════════════════════════════════════

PATCHED_TRY_ENTRY_SNIPPET = '''
    # ── AI Decision Engine ────────────────────────────────────
    trend_bias = get_trend_bias(df_trend) if df_trend is not None else None

    decision = self.ai.decide(
        symbol         = symbol,
        signals        = signals,
        regime_info    = regime_info,
        trend_bias     = trend_bias,
        recent_trades  = self.risk.trade_history[-10:],
        open_positions = list(self.positions.values()),
        account_balance= self.risk.current_balance,
        daily_pnl      = ((self.risk.current_balance - self.risk._daily_start_balance)
                          / self.risk._daily_start_balance * 100
                          if self.risk._daily_start_balance > 0 else 0.0),
    )

    # Log AI warnings
    for warning in decision.warnings:
        log.warning(f"  AI warning [{symbol}]: {warning}")

    # Gate on AI decision
    if decision.action != "ENTER":
        log.info(f"  AI [{symbol}]: {decision.action} — {decision.reasoning}")
        db.log_filter_block(symbol, "ai_brain", decision.reasoning)
        return

    if decision.confidence < 0.60:
        log.info(f"  AI [{symbol}]: confidence {decision.confidence:.0%} too low — skipping")
        db.log_filter_block(symbol, "ai_brain", f"low confidence: {decision.confidence:.0%}")
        return

    direction = decision.direction   # "BUY" or "SELL"
    confirmations = int(decision.confidence * 8)   # approximate

    # Adjust size based on AI suggestion
    if decision.size_suggestion == "reduce":
        size_multiplier = 0.5
    elif decision.size_suggestion == "increase":
        size_multiplier = 1.3
    else:
        size_multiplier = 1.0

    # Adjust SL width based on AI suggestion
    import config as _cfg
    original_sl_mult = _cfg.SL_ATR_MULT
    if decision.sl_suggestion == "tight":
        _cfg.SL_ATR_MULT = original_sl_mult * 0.8
    elif decision.sl_suggestion == "wide":
        _cfg.SL_ATR_MULT = original_sl_mult * 1.3

    # ... rest of try_entry() continues unchanged ...
    # (size = int(base_size * size_multiplier), then place order)
    # Remember to restore: _cfg.SL_ATR_MULT = original_sl_mult after sizing
'''


# ═══════════════════════════════════════════════════════════════
# COST ESTIMATION
# ═══════════════════════════════════════════════════════════════
"""
Claude Haiku 3.5 pricing:
  Input:  ~$0.80 per million tokens
  Output: ~$4.00 per million tokens

Each decide() call:
  Input:  ~500 tokens  → $0.0004
  Output: ~100 tokens  → $0.0004
  Total:  ~$0.0008 per call

With MAX_TRADES_PER_DAY = 3 and 60-second polling:
  ~50–100 API calls per day (most are cached/skipped)
  Cost: ~$0.04–0.08 per day  (under $3/month)

The review_losing_trades() call:
  ~$0.005 per daily review call

Total estimated cost: $3–5 per month
vs potential profit improvement: significant
"""
