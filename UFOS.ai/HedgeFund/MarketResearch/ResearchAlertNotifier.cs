using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>
    /// Market Research MR7: Windows toasts for fired Research alerts, app-wide (like <see cref="FundNotifications"/>):
    /// started once by the Hedge Fund window and kept running while UFOS.ai runs — alerts do NOT run while the app is
    /// closed (no background service; the backend evaluates them only while it runs, "MR7 backend notes").
    /// - Every 60 s (and whenever the Research view reports a change) it asks GET /research/alerts whether any rule is
    ///   enabled; if so the ONE Research socket is kept open with <c>visible: false</c> and the <c>alerts</c> topic,
    ///   also when no Research view is shown (<see cref="ResearchSocket.SetAlertsWanted"/>).
    /// - Every WS <c>alert</c> message → a toast with the backend's title + message (plain text) through
    ///   <see cref="FundNotifications.ShowResearchAlert"/>, which honours the Hedge Fund "mute notifications" option.
    ///   Alerts missed while the socket was down are not toasted afterwards; they are in the alert log.
    /// A failing request only keeps the previous state (logged once) — no stand-in values, nothing invented.
    /// </summary>
    internal static class ResearchAlertNotifier
    {
        private const string LogModule = "HedgeFund";
        private static readonly TimeSpan Interval = TimeSpan.FromSeconds(60);
        private static DispatcherTimer? _timer;
        private static bool _started;
        private static bool _busy;
        private static bool _failureLogged;
        private static readonly HashSet<long> _shownLogIds = new();

        /// <summary>Enabled rules at the last successful check (null = never checked successfully).</summary>
        public static int? EnabledCount { get; private set; }

        public static void EnsureStarted()
        {
            if (_started) return;
            _started = true;
            ResearchSocket.Instance.AlertReceived += Socket_AlertReceived;
            _timer = new DispatcherTimer(DispatcherPriority.Background) { Interval = Interval };
            _timer.Tick += async (_, _) => await RefreshAsync();
            _timer.Start();
            _ = RefreshAsync();
        }

        /// <summary>The Research view loaded or changed the rules: adopt the enabled count without another request.</summary>
        public static void Report(IReadOnlyList<AlertDto> alerts)
        {
            EnabledCount = alerts.Count(a => a.Enabled == true);
            ResearchSocket.Instance.SetAlertsWanted(EnabledCount > 0);
        }

        public static async Task RefreshAsync()
        {
            if (_busy) return;
            _busy = true;
            try
            {
                var r = await ResearchClient.GetAlertsAsync();
                if (r.Ok && r.Value?.Alerts != null)
                {
                    Report(r.Value.Alerts);
                    _failureLogged = false;
                }
                else if (!_failureLogged)
                {
                    _failureLogged = true;
                    AppLog.Warn(LogModule, "Market Research alerts: the alert list could not be read (" + r.Message + ") — toasts keep the last known state.");
                }
            }
            catch (Exception ex)
            {
                if (!_failureLogged) { _failureLogged = true; AppLog.Warn(LogModule, "Market Research alerts: checking the alert list failed.", ex); }
            }
            finally
            {
                _busy = false;
            }
        }

        private static void Socket_AlertReceived(object? sender, WsAlert alert)
        {
            if (alert.LogId is long id && !_shownLogIds.Add(id)) return; // one toast per trigger
            if (_shownLogIds.Count > 2000) _shownLogIds.Clear();
            var title = Clip(string.IsNullOrWhiteSpace(alert.Title) ? "Research alert · " + (alert.Symbol ?? "") : alert.Title!, 120);
            var message = Clip(alert.Message ?? "", 240);
            if (message.Length == 0) return;
            FundNotifications.ShowResearchAlert(title, message);
        }

        private static string Clip(string text, int max) => text.Length <= max ? text : text.Substring(0, max - 1) + "…";
    }
}
