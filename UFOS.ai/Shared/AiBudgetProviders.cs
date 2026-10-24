using System.Collections.Generic;

namespace UFOS.ai.Shared
{
    /// <summary>A registered AI-budget backend (contract UFOS.ai/docs/ai-budgets-spec.md §6.1).</summary>
    internal sealed record AiBudgetProvider(
        string ModuleId,
        string Label,
        string[] BackendRelativeDir,
        int Port,
        IReadOnlyList<string> CostLogModules,
        string StartHint) : IAiBudgetProvider;

    /// <summary>
    /// THE list of module backends shown in the MainWindow "AI budgets" window, in display order.
    ///
    /// A new module plugs in with ONE entry here (contract §5 step 2; see IAiBudgetProvider for the
    /// backend side): module id (= the payload's "module"), label, backend folder relative to the repo
    /// (the folder that contains main.py), loopback port of its HTTP API, its cost-log "module"
    /// names and a start hint for the "backend not running" state. Duplicate module ids are rejected
    /// by AiBudgetRegistry at first use.
    ///
    /// The Financial Newsroom has no backend of its own: its caps (newsroom_analysis,
    /// newsroom_insider_summary) are entries of the HedgeFund payload (group "Financial Newsroom").
    /// The main app's "Summarize with AI" (Services/OpenRouterClient.cs) has no backend: its cap is the
    /// app-local provider AiBudgetAppCaps ("UFOS.ai app", IAiBudgetLocalSource, contract §5 step 2b).
    /// </summary>
    internal static class AiBudgetProviders
    {
        public static IReadOnlyList<IAiBudgetProvider> All { get; } = new IAiBudgetProvider[]
        {
            new AiBudgetProvider(
                "hedgefund", "Hedge Fund", new[] { "HedgeFund", "backend" }, 8867,
                new[] { "HedgeFund" },
                "Open the Hedge Fund module to start its backend."),
            new AiBudgetProvider(
                "global-monitoring", "Global Monitoring", new[] { "Global Monitoring", "backend" }, 8768,
                new[] { "Global Monitoring" },
                "Open the Global Monitoring module to start its backend."),
            new AiBudgetProvider(
                "livestream-agent", "LiveStream Agent", new[] { "LiveStreamAgent", "backend" }, 8766,
                new[] { "LiveStreamAgent" },
                "Open the LiveStream Agent module to start its backend."),
            // App-local provider (no backend): the main app's "Summarize with AI" daily cap (§5 step 2b).
            AiBudgetAppCaps.Instance,
        };
    }
}
