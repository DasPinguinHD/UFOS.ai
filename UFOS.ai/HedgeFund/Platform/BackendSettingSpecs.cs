using System.Collections.Generic;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// The backends' non-secret settings as they moved from .env to &lt;backend&gt;\data\settings.json (S10-E, ENV_API §4):
    /// .env name → JSON key (lower case), type, CODE DEFAULT. Used only by the one-time .env import (EnvMigration), which
    /// copies a value only when it differs from the default. The defaults are the ones the backends' config.py used with
    /// os.getenv (2026-10-24) and must equal ENV_API_DEFAULTS.json / the settings schemas in the backends — change together.
    /// Ports and host switches are NOT here: the ports are fixed, and ALLOW_REMOTE_ACCESS / WS_HOST / HTTP_HOST are GONE (user
    /// decision 2026-10-24 16:56, with Docker removed — every backend always binds 127.0.0.1, api_auth.bind_host()). The
    /// import drops those names from a .env and reports them (EnvMigration.ProcessSwitches). Checked against the backends'
    /// config.SCHEMA by backend/tests/test_s10_env.py (ImportSpecsMatchSchemasTest).
    /// </summary>
    internal static class BackendSettingSpecs
    {
        private const string OpenRouterBaseUrl = "https://openrouter.ai/api/v1";
        private const string OpenRouterModel = "deepseek/deepseek-chat-v3-0324";

        private static EnvSettingSpec I(EnvModule m, string env, string def) => new(m, env, env.ToLowerInvariant(), EnvSettingKind.Integer, def);
        private static EnvSettingSpec N(EnvModule m, string env, string def) => new(m, env, env.ToLowerInvariant(), EnvSettingKind.Number, def);
        private static EnvSettingSpec T(EnvModule m, string env, string def) => new(m, env, env.ToLowerInvariant(), EnvSettingKind.Text, def);

        public static readonly IReadOnlyList<EnvSettingSpec> All = new[]
        {
            // Hedge Fund (HedgeFund/backend/data/settings.json)
            T(EnvModule.HedgeFund, "OPENROUTER_BASE_URL", OpenRouterBaseUrl),
            T(EnvModule.HedgeFund, "OPENROUTER_MODEL", OpenRouterModel),
            T(EnvModule.HedgeFund, "OPENROUTER_ANALYSIS_PREMIUM_MODEL", "anthropic/claude-opus-5.5"),
            I(EnvModule.HedgeFund, "MARKET_DATA_POLL_SECONDS", "30"),
            I(EnvModule.HedgeFund, "SEC_EDGAR_POLL_SECONDS", "120"),
            N(EnvModule.HedgeFund, "SEC_EDGAR_MIN_REQUEST_INTERVAL_SECONDS", "0.5"),
            I(EnvModule.HedgeFund, "FRED_POLL_SECONDS", "21600"),
            I(EnvModule.HedgeFund, "CALENDAR_POLL_SECONDS", "21600"),
            I(EnvModule.HedgeFund, "INSIDER_DISPLAY_DAYS", "5"),
            I(EnvModule.HedgeFund, "INSIDER_DISPLAY_MAX", "300"),
            I(EnvModule.HedgeFund, "INSIDER_RETENTION_DAYS", "5"),
            I(EnvModule.HedgeFund, "AI_RESULTS_RETENTION_DAYS", "90"),

            // Global Monitoring (Global Monitoring/backend/data/settings.json)
            I(EnvModule.GlobalMonitoring, "NEWS_POLL_SECONDS", "600"),
            N(EnvModule.GlobalMonitoring, "NEWS_MAX_AGE_HOURS", "48"),
            I(EnvModule.GlobalMonitoring, "GDACS_POLL_SECONDS", "600"),
            N(EnvModule.GlobalMonitoring, "GDACS_MAX_HOURS_SINCE_END", "48"),
            I(EnvModule.GlobalMonitoring, "OPENSKY_POLL_SECONDS", "240"),
            N(EnvModule.GlobalMonitoring, "OPENSKY_MIN_NOTABLE_ALTITUDE_METERS", "1500"),
            I(EnvModule.GlobalMonitoring, "YAHOO_POLL_SECONDS", "90"),
            T(EnvModule.GlobalMonitoring, "OPENROUTER_BASE_URL", OpenRouterBaseUrl),
            T(EnvModule.GlobalMonitoring, "OPENROUTER_MODEL", OpenRouterModel),
            I(EnvModule.GlobalMonitoring, "LOCALNEWS_MAX_RECORDS", "8"),
            I(EnvModule.GlobalMonitoring, "LOCALNEWS_CACHE_SECONDS", "1800"),
            I(EnvModule.GlobalMonitoring, "LOCALNEWS_ERROR_CACHE_SECONDS", "45"),
            N(EnvModule.GlobalMonitoring, "EVENT_RETENTION_DAYS", "7"),
            N(EnvModule.GlobalMonitoring, "PRICE_RETENTION_DAYS", "730"),
            N(EnvModule.GlobalMonitoring, "FLIGHT_TRACK_RETENTION_HOURS", "24"),
            N(EnvModule.GlobalMonitoring, "LOCALNEWS_RETENTION_HOURS", "24"),
            N(EnvModule.GlobalMonitoring, "AI_OUTPUT_RETENTION_DAYS", "90"),
            N(EnvModule.GlobalMonitoring, "FLIGHT_CACHE_MAX_AGE_HOURS", "2"),
            I(EnvModule.GlobalMonitoring, "ASSESS_REUSE_SECONDS", "1800"),

            // LiveStream Agent (LiveStreamAgent/backend/data/settings.json); youtube_url has no default (required) and is
            // edited in the LiveStream Agent window afterwards (user decision 2026-10-24 16:25).
            T(EnvModule.LiveStreamAgent, "YOUTUBE_URL", ""),
            T(EnvModule.LiveStreamAgent, "OPENROUTER_BASE_URL", OpenRouterBaseUrl),
            T(EnvModule.LiveStreamAgent, "OPENROUTER_MODEL", OpenRouterModel),
            I(EnvModule.LiveStreamAgent, "MARKET_DATA_POLL_SECONDS", "15"),
            I(EnvModule.LiveStreamAgent, "VERDICT_INTERVAL_SECONDS", "90"),
            I(EnvModule.LiveStreamAgent, "VERDICT_MAX_REASON_CHARS", "720"),
            I(EnvModule.LiveStreamAgent, "VERDICT_CONFIDENCE_SCALE", "100"),
            I(EnvModule.LiveStreamAgent, "VERDICT_INITIAL_DELAY_SECONDS", "60"),
        };
    }
}
