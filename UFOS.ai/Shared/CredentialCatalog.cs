using System;
using System.Collections.Generic;
using System.Linq;

namespace UFOS.ai.Shared
{
    /// <summary>One credential of the solution. <see cref="LegacyEnvNames"/> exist ONLY for the one-time "Import from old .env
    /// files…" migration (⚙ Keys &amp; data) — nothing reads a .env any more (S10-E).</summary>
    internal sealed record CredentialEntry(
        string Name,
        string Label,
        string Target,
        string UsedBy,
        IReadOnlyList<string> LegacyEnvNames,
        bool IsSecretValue = true);

    /// <summary>
    /// Every credential of every UFOS.ai module (S10-E, user decision 2026-10-24: keys only in the Windows Credential Manager).
    /// Contract: scratchpad ENV_API.md §2 → SPEC; the Python readers use the same targets (HedgeFund
    /// strategy_platform/secrets.py, Global Monitoring + LiveStream Agent backend/credstore.py). Change them together.
    /// Names are unique across modules. Target scheme: "UFOS.ai/&lt;Module&gt;/&lt;name&gt;".
    ///
    /// One shared OpenRouter key: the existing Hedge Fund credential serves the Hedge Fund, Global Monitoring, the LiveStream
    /// Agent and the main app (ENV_API §2 [DECISION], recommended; AI budgets stay per module).
    /// </summary>
    internal static class CredentialCatalog
    {
        public const string OpenRouterApiKey = "openrouter-api-key";
        public const string DeepgramApiKey = "deepgram-api-key";
        public const string OpenSkyClientId = "opensky-client-id";
        public const string OpenSkyClientSecret = "opensky-client-secret";

        private const string Hf = "UFOS.ai/HedgeFund/";
        private const string Gm = "UFOS.ai/GlobalMonitoring/";
        private const string Lsa = "UFOS.ai/LiveStreamAgent/";
        private const string HedgeFund = "Hedge Fund";

        private static readonly string[] None = Array.Empty<string>();

        public static readonly IReadOnlyList<CredentialEntry> All = new[]
        {
            new CredentialEntry(OpenRouterApiKey, "OpenRouter API key", Hf + OpenRouterApiKey,
                "Hedge Fund, Global Monitoring, LiveStream Agent, main window (one shared key)", new[] { "OPENROUTER_API_KEY" }),
            new CredentialEntry("fred-api-key", "FRED API key", Hf + "fred-api-key", HedgeFund, new[] { "FRED_API_KEY" }),
            // Not a secret in the strict sense (sent to SEC as the User-Agent header), stored the same way.
            new CredentialEntry("sec-edgar-user-agent", "SEC EDGAR user agent (name + e-mail)", Hf + "sec-edgar-user-agent", HedgeFund,
                new[] { "SEC_EDGAR_USER_AGENT" }, IsSecretValue: false),
            new CredentialEntry("alpaca-paper-key-id", "Alpaca paper API key ID", Hf + "alpaca-paper-key-id", HedgeFund,
                new[] { "ALPACA_API_KEY_ID", "APCA_API_KEY_ID" }),
            new CredentialEntry("alpaca-paper-secret-key", "Alpaca paper API secret key", Hf + "alpaca-paper-secret-key", HedgeFund,
                new[] { "ALPACA_API_SECRET_KEY", "APCA_API_SECRET_KEY" }),
            // Stage 3 (SPEC §8.1): daily prices before 2016-01-04 — never had a .env name.
            new CredentialEntry("tiingo-api-key", "Tiingo API key", Hf + "tiingo-api-key", HedgeFund, None),
            // Stage 8b (SPEC §18.14): Binance Spot DEMO MODE keys (demo.binance.com → API Management, HMAC).
            new CredentialEntry("binance-demo-api-key", "Binance Spot demo API key", Hf + "binance-demo-api-key", HedgeFund, None),
            new CredentialEntry("binance-demo-api-secret", "Binance Spot demo API secret (HMAC)", Hf + "binance-demo-api-secret", HedgeFund, None),
            // Stage 9d (SPEC §20.11): EODHD end-of-day closes incl. EU venues (Shadow Account / insights).
            new CredentialEntry("eodhd-api-key", "EODHD API key", Hf + "eodhd-api-key", HedgeFund, None),
            // S10-E: Global Monitoring + LiveStream Agent (formerly their backend\.env).
            new CredentialEntry(OpenSkyClientId, "OpenSky client ID (optional — anonymous without)", Gm + OpenSkyClientId, "Global Monitoring",
                new[] { "OPENSKY_CLIENT_ID" }),
            new CredentialEntry(OpenSkyClientSecret, "OpenSky client secret (optional — anonymous without)", Gm + OpenSkyClientSecret, "Global Monitoring",
                new[] { "OPENSKY_CLIENT_SECRET" }),
            new CredentialEntry(DeepgramApiKey, "Deepgram API key", Lsa + DeepgramApiKey, "LiveStream Agent", new[] { "DEEPGRAM_API_KEY" }),
        };

        /// <summary>Keys of RETIRED integrations (Kraken Futures demo, retired 2026-10-19, S10-K) — listed only while stored, removal only.</summary>
        public static readonly IReadOnlyList<CredentialEntry> Retired = new[]
        {
            new CredentialEntry("kraken-demo-api-key", "Kraken Futures demo API key (retired)", Hf + "kraken-demo-api-key", "nothing (retired)", None),
            new CredentialEntry("kraken-demo-api-secret", "Kraken Futures demo API secret (retired)", Hf + "kraken-demo-api-secret", "nothing (retired)", None),
        };

        public static CredentialEntry? Find(string name) => All.FirstOrDefault(e => e.Name == name);

        /// <summary>A catalog entry by name; throws for an unknown name (a programming error, never user input).</summary>
        public static CredentialEntry Get(string name)
            => Find(name) ?? throw new ArgumentException("Unknown credential name: " + name, nameof(name));
    }
}
