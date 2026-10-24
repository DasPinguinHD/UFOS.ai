using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Threading;
using Microsoft.Toolkit.Uwp.Notifications;
using UFOS.ai.HedgeFund.Newsroom;
using UFOS.ai.HedgeFund.Platform;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.Fund
{
    /// <summary>
    /// Windows toasts for the Hedge Fund engine's events (SPEC §13): polls
    /// GET /fund/events?after_id=… every 15 s app-wide — started once by the Hedge Fund
    /// window and kept running after it closes, as long as the UFOS.ai app runs (the
    /// backend lives that long too, SPEC §14). Only the kinds flip / order (autopilot
    /// fills) / halt / margin_call / autopilot (the engine's dead-man switch turned the
    /// autopilot off after params.autopilot_heartbeat_minutes without the app) are shown.
    /// "autopilot" events are also published via <see cref="AutopilotSwitchedOff"/> /
    /// <see cref="LastAutopilotEvent"/> so the Hedge Fund window can explain the state.
    ///
    /// Clicking a toast (ToastNotificationManagerCompat.OnActivated) brings the open Hedge
    /// Fund window to the front, or else the app's main window; it never opens a new window
    /// and never triggers an engine action. Stage 9 (SPEC §19.13): a monitoring toast
    /// (<see cref="ShowMonitoringAlert"/>, arguments ufos=monitoring;alert=&lt;id&gt;) also switches
    /// the open Hedge Fund window to Monitoring → Alerts with that alert; without an open
    /// window the next one opens there (<see cref="MonitoringNotifier.RequestOpen"/>).
    ///
    /// The last seen event id lives in memory only. On the first successful poll the
    /// current maximum id is remembered and nothing is shown, so old events are never
    /// replayed after a start. Muting (hedgefund-settings.json) is re-read on every
    /// poll; muted events are still consumed (not shown later).
    ///
    /// Toasts never crash the app: every call is wrapped in try/catch and logged once.
    /// </summary>
    internal static class FundNotifications
    {
        private static readonly TimeSpan Interval = TimeSpan.FromSeconds(15);
        private static readonly HashSet<string> ToastKinds = new(StringComparer.OrdinalIgnoreCase) { "flip", "order", "halt", "margin_call", "autopilot" };
        private const int MaxToastsPerPoll = 4;
        private const string LogModule = "HedgeFund";

        private static DispatcherTimer? _timer;
        private static long? _lastId;
        private static bool _busy;
        private static bool _toastFailureLogged;
        private static bool _activationHooked;

        /// <summary>
        /// Raised on the UI thread for every NEW "autopilot" event (also when muted), i.e. the
        /// engine switched the autopilot off by itself (dead-man switch).
        /// </summary>
        public static event Action<FundEvent>? AutopilotSwitchedOff;

        /// <summary>Latest "autopilot" event seen since the app started (including the start-up scan), or null.</summary>
        public static FundEvent? LastAutopilotEvent { get; private set; }

        /// <summary>Idempotent; call from the UI thread.</summary>
        public static void EnsureStarted()
        {
            if (_timer != null) return;
            var dispatcher = Application.Current?.Dispatcher ?? Dispatcher.CurrentDispatcher;
            _timer = new DispatcherTimer(Interval, DispatcherPriority.Background, async (_, _) => await PollAsync(), dispatcher);
            _timer.Start();
            HookToastActivation();
            _ = PollAsync();
        }

        /// <summary>
        /// Toast click → bring the Hedge Fund window (or the app) to the front. Runs on a
        /// background thread, so it only marshals to the dispatcher; any failure is logged.
        /// When the app is not running, Windows starts it through the registered activator —
        /// it then simply starts as usual (nothing here runs an engine action).
        /// </summary>
        private static void HookToastActivation()
        {
            if (_activationHooked) return;
            _activationHooked = true;
            try
            {
                ToastNotificationManagerCompat.OnActivated += args =>
                {
                    try
                    {
                        var (monitoring, alertId) = ReadTarget(args?.Argument);
                        var app = Application.Current;
                        if (monitoring) app?.Dispatcher.BeginInvoke(new Action(() => OpenMonitoring(alertId)));
                        else app?.Dispatcher.BeginInvoke(new Action(BringToFront));
                    }
                    catch (Exception ex)
                    {
                        try { AppLog.Warn(LogModule, "Toast activation could not be handled.", ex); } catch { }
                    }
                };
            }
            catch (Exception ex)
            {
                try { AppLog.Warn(LogModule, "Toast activation is not available.", ex); } catch { }
            }
        }

        private static void BringToFront()
        {
            try
            {
                var app = Application.Current;
                if (app == null) return;
                Window? target = app.Windows.OfType<HedgeFundWindow>().FirstOrDefault(w => w.IsLoaded);
                target ??= app.MainWindow;
                if (target == null || !target.IsLoaded) return;
                if (!target.IsVisible) target.Show();
                if (target.WindowState == WindowState.Minimized) target.WindowState = WindowState.Normal;
                target.Activate();
                // Activate() alone may only flash the taskbar button when another app has focus.
                target.Topmost = true;
                target.Topmost = false;
                target.Focus();
            }
            catch (Exception ex)
            {
                try { AppLog.Warn(LogModule, "Could not bring the Hedge Fund window to the front.", ex); } catch { }
            }
        }

        /// <summary>The toast's arguments: (true, alert id) for a monitoring toast, else (false, null). Never throws.</summary>
        private static (bool Monitoring, long? AlertId) ReadTarget(string? argument)
        {
            if (string.IsNullOrWhiteSpace(argument)) return (false, null);
            try
            {
                var parsed = ToastArguments.Parse(argument);
                if (!parsed.TryGetValue("ufos", out string view) || view != "monitoring") return (false, null);
                long? id = null;
                if (parsed.TryGetValue("alert", out string idText) && long.TryParse(idText, NumberStyles.Integer, CultureInfo.InvariantCulture, out var n)) id = n;
                return (true, id);
            }
            catch
            {
                return (false, null);
            }
        }

        /// <summary>Monitoring toast click: the open Hedge Fund window shows Monitoring → Alerts; else the main window comes to the front.</summary>
        private static void OpenMonitoring(long? alertId)
        {
            try
            {
                var app = Application.Current;
                if (app == null) return;
                var window = app.Windows.OfType<HedgeFundWindow>().FirstOrDefault(w => w.IsLoaded);
                if (window == null)
                {
                    MonitoringNotifier.RequestOpen(alertId);
                    BringToFront();
                    return;
                }
                BringToFront();
                window.ShowMonitoring(alertId);
            }
            catch (Exception ex)
            {
                try { AppLog.Warn(LogModule, "Could not open Monitoring from a toast.", ex); } catch { }
            }
        }

        private static async Task PollAsync()
        {
            if (_busy) return;
            // Nothing to ask while the backend isn't up (saves a failing request every 15 s).
            if (!NewsroomBackend.Status.StartsWith("online", StringComparison.OrdinalIgnoreCase)) return;
            _busy = true;
            try
            {
                if (_lastId == null)
                {
                    _lastId = await FindCurrentMaxIdAsync();
                    return;
                }

                var result = await FundClient.GetEventsAsync(_lastId.Value);
                if (!result.Ok || result.Value == null || result.Value.Count == 0) return;

                var events = result.Value.OrderBy(e => e.Id).ToList();
                _lastId = Math.Max(_lastId.Value, events.Max(e => e.Id));

                foreach (var e in events.Where(IsAutopilotEvent))
                {
                    LastAutopilotEvent = e;
                    try { AutopilotSwitchedOff?.Invoke(e); } catch (Exception ex) { try { AppLog.Warn(LogModule, "Autopilot event handler failed.", ex); } catch { } }
                }

                var toShow = events.Where(e => ToastKinds.Contains(e.Kind ?? "")).ToList();
                if (toShow.Count == 0 || FundSettings.MuteNotifications) return;

                if (toShow.Count > MaxToastsPerPoll)
                {
                    // Burst (e.g. a kill switch liquidating everything): one summary toast.
                    ShowToast("Hedge Fund · " + toShow.Count + " engine events",
                        string.Join(" · ", toShow.Take(6).Select(e => e.Title ?? e.Kind ?? "")),
                        mayContainAi: toShow.Any(e => string.Equals(e.Kind, "flip", StringComparison.OrdinalIgnoreCase)));
                    return;
                }
                foreach (var e in toShow)
                {
                    ShowToast(TitleFor(e), e.Body ?? "",
                        mayContainAi: string.Equals(e.Kind, "flip", StringComparison.OrdinalIgnoreCase));
                }
            }
            catch (Exception ex)
            {
                try { AppLog.Warn(LogModule, "Hedge Fund event poll failed.", ex); } catch { }
            }
            finally
            {
                _busy = false;
            }
        }

        /// <summary>Pages through /fund/events (50 per call) to the newest id without showing anything.</summary>
        private static async Task<long?> FindCurrentMaxIdAsync()
        {
            long max = 0;
            for (var page = 0; page < 200; page++)
            {
                var result = await FundClient.GetEventsAsync(max);
                if (!result.Ok || result.Value == null) return page == 0 ? null : max;
                if (result.Value.Count == 0) break;
                // Not shown as a toast (no replay), but remembered so the window can explain
                // why the autopilot is off after a restart.
                var autopilot = result.Value.Where(IsAutopilotEvent).OrderBy(e => e.Id).LastOrDefault();
                if (autopilot != null) LastAutopilotEvent = autopilot;
                var pageMax = result.Value.Max(e => e.Id);
                if (pageMax <= max) break;
                max = pageMax;
                if (result.Value.Count < 50) break;
            }
            return max;
        }

        /// <summary>
        /// Market Research MR7: shows one Research alert (title + message, plain text from the backend) through the same
        /// toast path and the same mute option (hedgefund-settings.json "mute_notifications", re-read every call).
        /// Returns false when muted (the alert is still in the Research alert log). Never throws.
        /// </summary>
        public static bool ShowResearchAlert(string title, string message)
        {
            bool muted;
            try { muted = FundSettings.MuteNotifications; }
            catch { muted = false; }
            if (muted) return false;
            ShowToast(title, message, mayContainAi: false);
            return true;
        }

        /// <summary>
        /// Strategy Platform stage 9 (SPEC §19.3): one toast for a backend monitoring alert (title + body as plain text from
        /// the backend, severity in the title). Clicking it opens Monitoring → Alerts with that alert. Honours "mute
        /// notifications" (re-read every call); the caller checks the monitoring toast setting. Returns false when muted or
        /// when toasts are unavailable (the alert is still in the Monitoring alert list). Never throws.
        /// </summary>
        /// <summary>Why the last monitoring toast could not be shown (exception type + message), or null after a shown toast.</summary>
        public static string? MonitoringToastFailure { get; private set; }

        public static bool ShowMonitoringAlert(string title, string body, string? severity, long alertId)
        {
            bool muted;
            try { muted = FundSettings.MuteNotifications; }
            catch { muted = false; }
            if (muted) return false;
            var level = severity switch
            {
                "critical" => "Critical · ",
                "warning" => "Warning · ",
                _ => "",
            };
            try
            {
                HookToastActivation();
                new ToastContentBuilder()
                    .AddArgument("ufos", "monitoring")
                    .AddArgument("alert", alertId.ToString(CultureInfo.InvariantCulture))
                    .AddText(Trim("Hedge Fund · " + level + title, 120))
                    .AddText(Trim(body, 240))
                    .AddAttributionText("Paper / demo trading · simulated money")
                    .Show();
                MonitoringToastFailure = null;
                return true;
            }
            catch (Exception ex)
            {
                MonitoringToastFailure = ex.GetType().Name + ": " + ex.Message;
                if (!_toastFailureLogged)
                {
                    _toastFailureLogged = true;
                    try { AppLog.Warn(LogModule, "Windows toast notifications are not available; monitoring alerts are only shown in the Hedge Fund window.", ex); } catch { }
                }
                return false;
            }
        }

        private static bool IsAutopilotEvent(FundEvent e) => string.Equals(e.Kind, "autopilot", StringComparison.OrdinalIgnoreCase);

        private static string TitleFor(FundEvent e)
        {
            var kind = (e.Kind ?? "").ToLowerInvariant() switch
            {
                "flip" => "Signal flip",
                "order" => "Autopilot order",
                "halt" => "Engine halted",
                "margin_call" => "Margin call",
                "autopilot" => "Autopilot switched off",
                _ => "Engine event",
            };
            var title = string.IsNullOrWhiteSpace(e.Title) ? kind : e.Title!;
            return "Hedge Fund · " + title;
        }

        private static void ShowToast(string title, string body, bool mayContainAi)
        {
            try
            {
                var builder = new ToastContentBuilder()
                    .AddText(Trim(title, 120))
                    .AddText(Trim(body, 240))
                    .AddAttributionText(mayContainAi
                        ? "Paper trading · may include AI-generated text (may be inaccurate)"
                        : "Paper trading · simulated money");
                builder.Show();
            }
            catch (Exception ex)
            {
                if (_toastFailureLogged) return;
                _toastFailureLogged = true;
                try { AppLog.Warn(LogModule, "Windows toast notifications are not available; engine events are only shown in the Hedge Fund window.", ex); } catch { }
            }
        }

        private static string Trim(string text, int max)
        {
            text = (text ?? "").Trim();
            return text.Length <= max ? text : text[..(max - 1)] + "…";
        }
    }
}
