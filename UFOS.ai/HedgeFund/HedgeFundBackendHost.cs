using System;
using System.Threading.Tasks;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.Newsroom;

namespace UFOS.ai.HedgeFund
{
    /// <summary>
    /// Stage 9b (strategy_platform SPEC §20.8, "backend started from the shell"): a small PUBLIC facade over the internal
    /// <see cref="NewsroomBackend"/> for the main app (another assembly) — the MainWindow "Portfolio Insight" card and the
    /// "Start the Hedge Fund backend together with UFOS.ai" setting. It adds no behaviour of its own: starting through it is
    /// exactly what opening the Hedge Fund window does (same EnsureStartedAsync: a running backend with current code is reused
    /// via /version, the process is bound to UFOS.ai or detached per hedgefund-backend.json, the engines force the autopilot /
    /// auto trading off at every start). A Hedge Fund window opened later in the same app process finds the started process and
    /// attaches without a restart. Internals stay internal.
    /// </summary>
    public static class HedgeFundBackendHost
    {
        /// <summary>Short state text ("not started", "checking…", "starting… …", "online", "offline — reason", …).</summary>
        public static string Status => NewsroomBackend.Status;

        /// <summary>True once the backend answers (also a reused / detached one).</summary>
        public static bool IsOnline => NewsroomBackend.Status.StartsWith("online", StringComparison.OrdinalIgnoreCase);

        /// <summary>True while a start is in progress (checking / installing packages / starting).</summary>
        public static bool IsStarting
            => NewsroomBackend.Status.StartsWith("checking", StringComparison.OrdinalIgnoreCase)
               || NewsroomBackend.Status.StartsWith("starting", StringComparison.OrdinalIgnoreCase);

        /// <summary>Raised on ANY thread when <see cref="Status"/> changes — marshal to the UI thread before touching controls.</summary>
        public static event Action? StatusChanged
        {
            add => NewsroomBackend.StatusChanged += value;
            remove => NewsroomBackend.StatusChanged -= value;
        }

        /// <summary>Idempotent start (the same call the Hedge Fund window makes). Call from the UI thread; never awaited by the UI.</summary>
        public static Task EnsureStartedAsync() => NewsroomBackend.EnsureStartedAsync();

        /// <summary>GET /health (600 ms) — true when a backend answers on 127.0.0.1:8867 (also one this app did not start).</summary>
        public static Task<bool> IsRunningAsync() => NewsroomBackend.IsRunningAsync();

        /// <summary>The setting "Start the Hedge Fund backend together with UFOS.ai" (hedgefund-backend.json, OFF by default).</summary>
        public static bool StartWithApp => FundSettings.StartWithApp;
    }
}
