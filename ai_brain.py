"""
ai_brain.py - Multi-Provider AI Trade Decision Engine
=====================================================

Supports:
  - Claude (Anthropic)  - claude-3-5-haiku-latest
  - Gemini (Google)     - gemini-1.5-flash

Provider selection is stored in DB setting: ai_provider = claude|gemini|both
Keys are stored in DB settings (preferred) or environment variables.
Falls back to deterministic rule-based scoring if providers are unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

log = logging.getLogger("delta_bot")


# --- SDK availability ---------------------------------------------------------
try:
    import anthropic as _anthropic_sdk
    _CLAUDE_SDK = True
except Exception:
    _CLAUDE_SDK = False

try:
    import google.generativeai as _genai_sdk
    _GEMINI_SDK = True
except Exception:
    _GEMINI_SDK = False


@dataclass
class AIDecision:
    action: str
    direction: str
    confidence: float
    reasoning: str
    sl_suggestion: str
    size_suggestion: str
    warnings: list
    raw_response: str
    provider: str = ""


SYSTEM_PROMPT = (
    "You are an expert crypto futures trader and quantitative analyst.\n"
    "You analyse technical signals for Delta Exchange perpetual futures and make precise trading decisions.\n\n"
    "Your job:\n"
    "1. Read the provided market data, signals, regime, and account state\n"
    "2. Decide: ENTER (take a trade), SKIP (no trade), HOLD (keep existing), or EXIT (close position)\n"
    "3. Be CONSERVATIVE - capital preservation is more important than catching every move\n"
    "4. Never recommend trading when confidence < 60%\n"
    "5. Account for fees: trades need a real edge to be worth taking.\n\n"
    "Key rules you must enforce:\n"
    "- SKIP if market regime is VOLATILE\n"
    "- SKIP if fewer than 3 indicators agree\n"
    "- SKIP if daily loss > 2% already\n"
    "- SKIP if signals conflict heavily (buys and sells roughly equal)\n"
    "- REDUCE size if recent consecutive losses >= 2\n"
    "- Always give a clear, concise reason (1-2 sentences max)\n\n"
    "Respond ONLY with valid JSON (no markdown):\n"
    "{\n"
    '  "action": "ENTER" | "SKIP" | "HOLD" | "EXIT",\n'
    '  "direction": "BUY" | "SELL" | "",\n'
    '  "confidence": 0.0-1.0,\n'
    '  "reasoning": "concise reason in plain English",\n'
    '  "sl_suggestion": "tight" | "normal" | "wide",\n'
    '  "size_suggestion": "reduce" | "normal" | "increase",\n'
    '  "warnings": ["warning1", "warning2"]\n'
    "}\n"
)


class _BaseProvider:
    name = "base"

    def call(self, prompt: str) -> Optional[str]:
        raise NotImplementedError

    def review(self, prompt: str) -> Optional[str]:
        return self.call(prompt)

    def parse(self, raw: str, symbol: str) -> Optional[AIDecision]:
        try:
            clean = (raw or "").strip()
            if clean.startswith("```"):
                parts = clean.split("```")
                clean = parts[1] if len(parts) > 1 else clean
                clean = clean.strip()
                if clean.lower().startswith("json"):
                    clean = clean[4:].strip()
            data = json.loads(clean)
            decision = AIDecision(
                action=data.get("action", "SKIP"),
                direction=data.get("direction", ""),
                confidence=float(data.get("confidence", 0.0) or 0.0),
                reasoning=data.get("reasoning", "No reason provided"),
                sl_suggestion=data.get("sl_suggestion", "normal"),
                size_suggestion=data.get("size_suggestion", "normal"),
                warnings=data.get("warnings", []) or [],
                raw_response=raw,
                provider=self.name,
            )
            return decision
        except Exception as e:
            log.error(f"  [{self.name.upper()}] [{symbol}]: parse error: {e} | raw={(raw or '')[:200]}")
            return None


class ClaudeProvider(_BaseProvider):
    name = "claude"
    DEFAULT_MODEL = "claude-3-5-haiku-latest"

    def __init__(self, api_key: str, model: str = ""):
        self.MODEL = (model or os.getenv("CLAUDE_MODEL") or self.DEFAULT_MODEL).strip()
        self.MAX_TOKENS = 800
        self._client = None
        if not _CLAUDE_SDK:
            log.warning("Claude SDK missing - install: pip install anthropic")
            return
        if not api_key:
            return
        try:
            self._client = _anthropic_sdk.Anthropic(api_key=api_key)
            log.info("Claude provider initialised")
        except Exception as e:
            log.warning(f"Claude init error: {e}")

    @property
    def available(self) -> bool:
        return self._client is not None

    def call(self, prompt: str) -> Optional[str]:
        if not self._client:
            return None
        try:
            resp = self._client.messages.create(
                model=self.MODEL,
                max_tokens=self.MAX_TOKENS,
                temperature=0.1,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            log.error(f"Claude call error: {e}")
            return None

    def review(self, prompt: str) -> Optional[str]:
        if not self._client:
            return None
        try:
            resp = self._client.messages.create(
                model=self.MODEL,
                max_tokens=700,
                temperature=0.2,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception as e:
            log.error(f"Claude review error: {e}")
            return None


class GeminiProvider(_BaseProvider):
    name = "gemini"
    DEFAULT_MODEL = "gemini-1.5-flash"

    def __init__(self, api_key: str, model: str = ""):
        self.MODEL = (model or os.getenv("GEMINI_MODEL") or self.DEFAULT_MODEL).strip()
        self._model = None
        if not _GEMINI_SDK:
            log.warning("Gemini SDK missing - install: pip install google-generativeai")
            return
        if not api_key:
            return
        try:
            _genai_sdk.configure(api_key=api_key)
            self._model = _genai_sdk.GenerativeModel(
                model_name=self.MODEL,
                system_instruction=SYSTEM_PROMPT,
                generation_config=_genai_sdk.types.GenerationConfig(
                    temperature=0.1,
                    max_output_tokens=800,
                ),
            )
            log.info("Gemini provider initialised")
        except Exception as e:
            log.warning(f"Gemini init error: {e}")

    @property
    def available(self) -> bool:
        return self._model is not None

    def call(self, prompt: str) -> Optional[str]:
        if not self._model:
            return None
        try:
            resp = self._model.generate_content(prompt)
            return (resp.text or "").strip()
        except Exception as e:
            log.error(f"Gemini call error: {e}")
            return None


class AIBrain:
    CACHE_SECONDS = 30

    def __init__(self):
        self._cache: dict[str, tuple[float, AIDecision]] = {}
        self._call_count = 0
        self._error_count = 0
        self._claude: Optional[ClaudeProvider] = None
        self._gemini: Optional[GeminiProvider] = None
        self._cfg_state: tuple = ()
        self._init_providers()

    # --- Settings ------------------------------------------------------------

    @staticmethod
    def _db(key: str) -> str:
        try:
            import database as _db

            return _db.get_setting(key) or ""
        except Exception:
            return ""

    def _init_providers(self):
        claude_key = self._db("anthropic_api_key") or os.getenv("ANTHROPIC_API_KEY", "")
        gemini_key = self._db("gemini_api_key") or os.getenv("GEMINI_API_KEY", "")

        claude_model = self._db("claude_model") or os.getenv("CLAUDE_MODEL", "")
        gemini_model = self._db("gemini_model") or os.getenv("GEMINI_MODEL", "")

        self._cfg_state = (claude_key, gemini_key, claude_model, gemini_model)

        self._claude = ClaudeProvider(claude_key, model=claude_model) if claude_key else None
        self._gemini = GeminiProvider(gemini_key, model=gemini_model) if gemini_key else None

        if not self._claude and not self._gemini:
            log.warning("AIBrain: No AI provider configured/online. Configure in Settings.")

    def _reload_if_needed(self):
        claude_key = self._db("anthropic_api_key") or os.getenv("ANTHROPIC_API_KEY", "")
        gemini_key = self._db("gemini_api_key") or os.getenv("GEMINI_API_KEY", "")
        claude_model = self._db("claude_model") or os.getenv("CLAUDE_MODEL", "")
        gemini_model = self._db("gemini_model") or os.getenv("GEMINI_MODEL", "")
        state = (claude_key, gemini_key, claude_model, gemini_model)
        if state != self._cfg_state:
            log.info("AIBrain: Settings change detected - reloading providers")
            self._init_providers()

    # --- Provider selection --------------------------------------------------

    def _active_provider(self) -> Optional[_BaseProvider]:
        """
        Strict selection:
        - ai_provider='claude' -> Claude only
        - ai_provider='gemini' -> Gemini only
        - ai_provider='both'   -> handled by decide_ensemble (no single provider)
        - otherwise            -> no provider
        """
        sel = (self._db("ai_provider") or "").lower().strip()
        if sel == "claude":
            return self._claude if (self._claude and self._claude.available) else None
        if sel == "gemini":
            return self._gemini if (self._gemini and self._gemini.available) else None
        return None

    def _ensemble_providers(self) -> list[_BaseProvider]:
        providers: list[_BaseProvider] = []
        if self._claude and self._claude.available:
            providers.append(self._claude)
        if self._gemini and self._gemini.available:
            providers.append(self._gemini)
        return providers

    def _any_provider(self) -> Optional[_BaseProvider]:
        # Used for non-trading utilities (reviews). Prefer any available provider.
        if self._claude and self._claude.available:
            return self._claude
        if self._gemini and self._gemini.available:
            return self._gemini
        return None

    # --- Public API ----------------------------------------------------------

    def decide_ensemble(
        self,
        symbol,
        signals,
        regime_info,
        trend_bias,
        recent_trades,
        open_positions,
        account_balance,
        daily_pnl,
    ) -> AIDecision:
        """
        Decide using the selected provider. If ai_provider == "both", query Claude+Gemini and
        only ENTER when there is consensus on action+direction.
        """
        self._reload_if_needed()

        cached = self._get_cache(symbol)
        if cached:
            return cached

        prompt = self._build_prompt(
            symbol, signals, regime_info, trend_bias,
            recent_trades, open_positions, account_balance, daily_pnl
        )

        sel = (self._db("ai_provider") or "").lower().strip()
        if sel not in ("claude", "gemini", "both"):
            out = self._fallback(signals, regime_info, daily_pnl)
            self._set_cache(symbol, out)
            return out

        if sel == "both":
            # Strict: only run ensemble if BOTH providers are available and BOTH return a decision.
            if not (self._claude and self._claude.available and self._gemini and self._gemini.available):
                out = self._fallback(signals, regime_info, daily_pnl)
                self._set_cache(symbol, out)
                return out

            d0 = None
            d1 = None
            try:
                self._call_count += 1
                raw0 = self._claude.call(prompt)
                d0 = self._claude.parse(raw0, symbol) if raw0 else None
            except Exception as e:
                log.warning(f"AIBrain: Claude error: {e}")
                self._error_count += 1

            try:
                self._call_count += 1
                raw1 = self._gemini.call(prompt)
                d1 = self._gemini.parse(raw1, symbol) if raw1 else None
            except Exception as e:
                log.warning(f"AIBrain: Gemini error: {e}")
                self._error_count += 1

            if not (d0 and d1):
                out = self._fallback(signals, regime_info, daily_pnl)
                self._set_cache(symbol, out)
                return out

            # Consensus rule: require same ENTER direction, else SKIP.
            if (d0.action == "ENTER" and d1.action == "ENTER" and d0.direction == d1.direction):
                avg_conf = max(0.0, min(0.95, (float(d0.confidence) + float(d1.confidence)) / 2.0))
                out = AIDecision(
                    action="ENTER",
                    direction=d0.direction,
                    confidence=avg_conf,
                    reasoning=f"Consensus: {d0.direction} (Claude+Gemini)",
                    sl_suggestion="normal",
                    size_suggestion="normal",
                    warnings=[],
                    raw_response="\n\n".join([d0.raw_response, d1.raw_response]),
                    provider="ensemble",
                )
                self._set_cache(symbol, out)
                return out

            avg_conf = max(0.0, min(0.95, (float(d0.confidence) + float(d1.confidence)) / 2.0))
            out = AIDecision(
                action="SKIP",
                direction="",
                confidence=avg_conf,
                reasoning=f"Ensemble: no consensus (Claude={d0.action}/{d0.direction}, Gemini={d1.action}/{d1.direction})",
                sl_suggestion="normal",
                size_suggestion="normal",
                warnings=["No strong AI consensus for entry"],
                raw_response="\n\n".join([d0.raw_response, d1.raw_response]),
                provider="ensemble",
            )
            self._set_cache(symbol, out)
            return out

        # Single-provider path
        provider = self._active_provider()
        if provider:
            try:
                self._call_count += 1
                raw = provider.call(prompt)
                if raw:
                    decision = provider.parse(raw, symbol)
                    if decision:
                        self._set_cache(symbol, decision)
                        return decision
            except Exception as e:
                log.warning(f"AIBrain: provider error: {e}")
                self._error_count += 1

        out = self._fallback(signals, regime_info, daily_pnl)
        self._set_cache(symbol, out)
        return out

    def decide(self, *args, **kwargs) -> AIDecision:
        return self.decide_ensemble(*args, **kwargs)

    def review_losing_trades(self, trade_history: list) -> str:
        provider = self._any_provider()
        if not provider:
            return "No AI provider available."
        losses = [t for t in trade_history if getattr(t, "net_pnl_pct", 0) < 0]
        if not losses:
            return "No losing trades to review."
        data = [
            {
                "symbol": t.symbol,
                "side": t.side,
                "pnl": round(getattr(t, "pnl_pct", 0.0), 3),
                "net_pnl": round(getattr(t, "net_pnl_pct", 0.0), 3),
                "reason": t.reason,
                "regime": getattr(t, "regime", "?"),
                "confirmations": getattr(t, "confirmations", 0),
            }
            for t in losses[-20:]
        ]
        prompt = (
            f"Analyse these losing trades from my Delta Exchange bot:\n"
            f"{json.dumps(data, indent=2)}\n\n"
            "Identify:\n1) common exit reason\n2) regime patterns\n3) symbol patterns\n"
            "4) 3 specific config changes using exact names.\n"
        )
        return provider.review(prompt) or "Review failed."

    def explain_trade(self, symbol, side, entry, sl, tp, reasoning, provider: str = "") -> str:
        try:
            pnl_sl = round(abs(entry - sl) / entry * 100, 2) if entry else 0
            pnl_tp = round(abs(tp - entry) / entry * 100, 2) if entry else 0
            rr = round(pnl_tp / pnl_sl, 1) if pnl_sl > 0 else 0
        except Exception:
            pnl_sl, pnl_tp, rr = 0, 0, 0

        label = {"claude": "Claude AI", "gemini": "Gemini AI", "ensemble": "AI (Consensus)"}.get(provider, "AI Brain")
        return (
            f"{label} Trade Signal\n"
            f"-----------------\n"
            f"Symbol: {symbol}\n"
            f"Side:   {side}\n"
            f"Entry:  {entry}\n"
            f"SL:     {sl} (-{pnl_sl}%)\n"
            f"TP:     {tp} (+{pnl_tp}%)\n"
            f"R:R     1:{rr}\n"
            f"-----------------\n"
            f"Reason: {reasoning}"
        )

    # --- Prompt builder ------------------------------------------------------

    def _build_prompt(self, symbol, signals, regime_info, trend_bias, recent_trades, open_positions, balance, daily_pnl):
        sig_lines = []
        for s in signals:
            if getattr(s, "direction", None):
                sig_lines.append(f"  {s.name}: {s.direction} (str={getattr(s, 'strength', 0):.1f}) - {getattr(s, 'detail', '')}")
            else:
                sig_lines.append(f"  {s.name}: HOLD - {getattr(s, 'detail', '')}")

        buys = sum(1 for s in signals if getattr(s, "direction", "") == "BUY")
        sells = sum(1 for s in signals if getattr(s, "direction", "") == "SELL")
        holds = sum(1 for s in signals if not getattr(s, "direction", ""))

        recent = [
            f"  {t.symbol} {t.side}: net={getattr(t, 'net_pnl_pct', 0):+.2f}% | {t.reason}"
            for t in list(recent_trades)[-5:]
        ]
        pos = [
            f"  {p.symbol} {p.side} @ {p.entry_price} | SL={p.sl_price}"
            for p in open_positions
            if getattr(p, "active", False)
        ]
        cons_l = sum(1 for t in list(recent_trades)[-5:] if getattr(t, "net_pnl_pct", 0) < 0)

        nl = "\n"
        return (
            "=== TRADE DECISION REQUEST ===\n"
            f"Symbol: {symbol}\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            "--- MARKET REGIME ---\n"
            f"Regime: {regime_info.get('regime','?')}  ADX: {regime_info.get('adx',0):.1f}  "
            f"ATR%: {regime_info.get('atr_pct',0):.3f}%  Trend: {trend_bias or 'NEUTRAL'}\n\n"
            f"--- SIGNALS ({buys} BUY / {sells} SELL / {holds} HOLD) ---\n"
            f"{nl.join(sig_lines) if sig_lines else '  None'}\n\n"
            "--- ACCOUNT ---\n"
            f"Balance: ${float(balance):,.2f}  Daily PnL: {float(daily_pnl):+.3f}%  "
            f"Consec. losses: {cons_l}  Open: {len(pos)}\n"
            f"{nl.join(pos) if pos else '  None'}\n\n"
            f"--- RECENT TRADES ---\n{nl.join(recent) if recent else '  None'}\n\n"
            f"Should I trade {symbol}? JSON only."
        )

    # --- Fallback ------------------------------------------------------------

    def _fallback(self, signals, regime_info, daily_pnl) -> AIDecision:
        try:
            from market_detector import MarketRegime

            volatile = MarketRegime.VOLATILE
        except Exception:
            volatile = "VOLATILE"

        regime = regime_info.get("regime", "")
        atr_pct = float(regime_info.get("atr_pct", 0) or 0)
        buys = [s for s in signals if getattr(s, "direction", "") == "BUY"]
        sells = [s for s in signals if getattr(s, "direction", "") == "SELL"]
        buy_strength = sum(float(getattr(s, "strength", 0) or 0) for s in buys)
        sell_strength = sum(float(getattr(s, "strength", 0) or 0) for s in sells)

        def skip(reason: str, warnings=None) -> AIDecision:
            return AIDecision("SKIP", "", 0.0, reason, "normal", "normal", warnings or [], "", "fallback")

        if regime == volatile:
            return skip("Volatile regime", ["volatile"])
        if float(daily_pnl) < -2.0:
            return skip("Daily loss limit", ["daily_pnl<-2%"])
        if atr_pct < 0.4:
            return skip("ATR too low")

        if len(buys) >= 3 and buy_strength > sell_strength * 1.5:
            conf = min(0.90, buy_strength / (buy_strength + sell_strength + 0.01))
            return AIDecision("ENTER", "BUY", conf, f"{len(buys)} bullish signals", "normal", "normal", [], "", "fallback")
        if len(sells) >= 3 and sell_strength > buy_strength * 1.5:
            conf = min(0.90, sell_strength / (buy_strength + sell_strength + 0.01))
            return AIDecision("ENTER", "SELL", conf, f"{len(sells)} bearish signals", "normal", "normal", [], "", "fallback")
        return skip("Insufficient agreement")

    # --- Cache & stats -------------------------------------------------------

    def _get_cache(self, symbol: str) -> Optional[AIDecision]:
        try:
            ts, d = self._cache.get(symbol, (0.0, None))
            if d and (time.time() - ts) < self.CACHE_SECONDS:
                return d
        except Exception:
            return None
        return None

    def _set_cache(self, symbol: str, decision: AIDecision):
        self._cache[symbol] = (time.time(), decision)

    def get_stats(self) -> dict:
        return {
            "api_calls": self._call_count,
            "api_errors": self._error_count,
            "claude_online": bool(self._claude and self._claude.available),
            "gemini_online": bool(self._gemini and self._gemini.available),
            "provider": self._db("ai_provider") or "claude",
        }
