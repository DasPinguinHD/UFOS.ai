using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.Newsroom;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// Strategy Platform stage 9 (SPEC §19.3, WPF notes §19.13): Windows toasts for the backend's monitoring alerts (decay
    /// pause, reconciliation halt, kill switch, drawdown kill, daily loss limit, dead-man switch, broker disconnected,
    /// incubation finished) — app-wide like <see cref="FundNotifications"/>: started once by the Hedge Fund window and kept
    /// running while UFOS.ai runs, because a toast must also reach the user when the window is hidden or closed. It polls
    /// <c>GET /v2/monitoring/alerts?after=&lt;last seen id&gt;</c> every 15 s, and only while the HedgeFund backend is online.
    /// - The first successful poll only remembers the newest id and the unacked count (no replay of old alerts after a
    ///   start — they are in the Monitoring → Alerts list and counted in the rail badge).
    /// - Every NEW alert with <c>toast: true</c> → ONE toast (backend title + body, plain text, severity in the title;
    ///   a burst of more than 4 → one summary toast). Toasts are a notification channel only: every alert stays in the list.
    /// - Toasts on/off: the Monitoring setting "Windows toasts for monitoring alerts" (hedgefund-settings.json
    ///   "monitoring_toasts", default on) AND the Hedge Fund "Mute notifications" switch (re-read on every poll).
    /// - Clicking a toast opens the Hedge Fund window on Monitoring → Alerts with that alert selected
    ///   (FundNotifications.OnActivated → <see cref="RequestOpen"/> when the window is not open).
    /// The view's own polls (overview, details) stop when the view is hidden; this notifier does not.
    /// A failing request only keeps the previous state (logged once) — nothing is invented.
    /// </summary>
    internal static class MonitoringNotifier
    {
        private const string LogModule = "HedgeFund";
        private static readonly TimeSpan Interval = TimeSpan.FromSeconds(15);
        private const int MaxToastsPerPoll = 4;
        private static DispatcherTimer? _timer;
        private static long? _lastId;
        private static bool _busy;
        private static bool _failureLogged;
        private static long? _pendingOpenAlertId;
        private static bool _pendingOpen;

        /// <summary>
        /// Why monitoring toasts cannot be shown right now (Windows toasts unavailable, or the toast setting unreadable), or null.
        /// Shown in Monitoring → Alerts and as a banner — a failed toast is never dropped silently (the alert stays in the list).
        /// </summary>
        public static string? ToastProblem { get; private set; }

        /// <summary>
        /// Why the alert poll — the toast source of the platform AND (after the switch-over) of the Council of AIs — cannot be read right
        /// now, or null. Shown in Monitoring and in the Council view (S10-C R3: no silent poll failure); cleared by the next good poll.
        /// </summary>
        public static string? PollProblem { get; private set; }

        /// <summary>Raised on the UI thread when <see cref="PollProblem"/> changes.</summary>
        public static event Action? ProblemChanged;

        private static void SetPollProblem(string? problem)
        {
            if (string.Equals(problem, PollProblem, StringComparison.Ordinal)) return;
            PollProblem = problem;
            try { ProblemChanged?.Invoke(); }
            catch (Exception ex) { LogOnce("Monitoring alert problem handler failed.", ex); }
        }

        /// <summary>Unacked alerts at the last successful poll (null = never polled successfully).</summary>
        public static int? Unacked { get; private set; }

        /// <summary>Raised on the UI thread after a poll: the new alerts (may be empty) and the unacked count.</summary>
        public static event Action<IReadOnlyList<MonAlert>, int?>? Changed;

        /// <summary>Idempotent; call from the UI thread.</summary>
        public static void EnsureStarted()
        {
            if (_timer != null) return;
            _timer = new DispatcherTimer(DispatcherPriority.Background) { Interval = Interval };
            _timer.Tick += async (_, _) => await PollAsync();
            _timer.Start();
            _ = PollAsync();
        }

        /// <summary>The Monitoring view acked alerts or loaded the list: adopt the backend's unacked count at once.</summary>
        public static void Report(int? unacked)
        {
            if (unacked.HasValue) Unacked = unacked;
            try { Changed?.Invoke(Array.Empty<MonAlert>(), Unacked); }
            catch (Exception ex) { LogOnce("Monitoring alert handler failed.", ex); }
        }

        /// <summary>A toast was clicked while no Hedge Fund window was open: the next window opens Monitoring → Alerts.</summary>
        public static void RequestOpen(long? alertId)
        {
            _pendingOpen = true;
            _pendingOpenAlertId = alertId;
        }

        /// <summary>True (once) when a toast asked to open Monitoring; <paramref name="alertId"/> = the clicked alert.</summary>
        public static bool TakePendingOpen(out long? alertId)
        {
            alertId = _pendingOpenAlertId;
            var pending = _pendingOpen;
            _pendingOpen = false;
            _pendingOpenAlertId = null;
            return pending;
        }

        private static async Task PollAsync()
        {
            if (_busy) return;
            if (!NewsroomBackend.Status.StartsWith("online", StringComparison.OrdinalIgnoreCase))
            {
                SetPollProblem("Alert toasts paused — the Hedge Fund backend is not running (" + NewsroomBackend.Status + ").");
                return;
            }
            _busy = true;
            try
            {
                if (_lastId == null)
                {
                    var first = await PlatformClient.GetMonitoringAlertsAsync(0, 1, unackedOnly: false);
                    if (!first.Ok || first.Value == null)
                    {
                        if (first.HttpStatus == 404)
                        {
                            SetPollProblem("The running backend has no monitoring alerts (/v2/monitoring/alerts) — it is probably outdated; no alert toasts.");
                            return;
                        }
                        LogOnce("Monitoring alerts could not be read (" + first.Message + ").", null);
                        SetPollProblem(Unreadable(first.Message));
                        return;
                    }
                    _failureLogged = false;
                    SetPollProblem(null);
                    _lastId = first.Value.LastId ?? 0;
                    Unacked = first.Value.Unacked;
                    // Lead decision 2026-10-24: a CRITICAL alert raised before this first poll (e.g. council_switchover_failed at the backend's
                    // own start) is toasted once — critical only, each id once, remembered across restarts. Everything else is not replayed.
                    await ToastUnackedCriticalOnceAsync();
                    Changed?.Invoke(Array.Empty<MonAlert>(), Unacked);
                    return;
                }

                var res = await PlatformClient.GetMonitoringAlertsAsync(_lastId.Value, 100, unackedOnly: false);
                if (!res.Ok || res.Value == null)
                {
                    LogOnce("Monitoring alerts could not be read (" + res.Message + ") — the last known state is kept.", null);
                    SetPollProblem(Unreadable(res.Message));
                    return;
                }
                _failureLogged = false;
                SetPollProblem(null);
                var fresh = (res.Value.Alerts ?? new List<MonAlert>()).Where(a => a.Id > _lastId.Value).OrderBy(a => a.Id).ToList();
                if (fresh.Count > 0) _lastId = Math.Max(_lastId.Value, fresh.Max(a => a.Id));
                if (res.Value.LastId is long last && last > _lastId.Value && fresh.Count == 0) _lastId = last;
                Unacked = res.Value.Unacked;

                if (fresh.Count > 0 && ToastsWanted())
                {
                    var toasts = fresh.Where(a => a.Toast).ToList();
                    if (toasts.Count > MaxToastsPerPoll)
                    {
                        var worst = toasts.Any(a => a.Severity == "critical") ? "critical" : toasts.Any(a => a.Severity == "warning") ? "warning" : "info";
                        Track(FundNotifications.ShowMonitoringAlert(toasts.Count + " monitoring alerts",
                            string.Join(" · ", toasts.Take(6).Select(a => a.Title ?? a.Kind ?? "")), worst, toasts.Max(a => a.Id)));
                    }
                    else
                    {
                        foreach (var a in toasts)
                            Track(FundNotifications.ShowMonitoringAlert(a.Title ?? a.Kind ?? "Monitoring alert", a.Body ?? "", a.Severity, a.Id));
                    }
                }
                try { Changed?.Invoke(fresh, Unacked); }
                catch (Exception ex) { LogOnce("Monitoring alert handler failed.", ex); }
            }
            catch (Exception ex)
            {
                LogOnce("Monitoring alert poll failed.", ex);
                SetPollProblem(Unreadable(ex.GetType().Name));
            }
            finally
            {
                _busy = false;
            }
        }

        /// <summary>
        /// Monitoring toasts on (default) and the Hedge Fund notifications not muted — both re-read every poll. A setting that cannot
        /// be read is NOT assumed: no toast, and <see cref="ToastProblem"/> says why.
        /// </summary>
        public static bool ToastsWanted()
        {
            try
            {
                var wanted = FundSettings.MonitoringToasts && !FundSettings.MuteNotifications;
                if (ToastProblem != null && ToastProblem.StartsWith(SettingPrefix, StringComparison.Ordinal)) ToastProblem = null;
                return wanted;
            }
            catch (Exception ex)
            {
                ToastProblem = SettingPrefix + " (" + ex.GetType().Name + ": " + ex.Message + ") — no toasts are shown until it can be read; every alert stays in Monitoring → Alerts.";
                return false;
            }
        }

        private const string SettingPrefix = "The Windows toast setting could not be read";

        // ---- first poll: unacked CRITICAL alerts once (lead decision 2026-10-24) ----

        private const int MaxRemembered = 500;
        private static string ToastedPath => System.IO.Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "UFOS.ai", "monitoring-toasted.json");

        private static async Task ToastUnackedCriticalOnceAsync()
        {
            // the newest 500 ids at most (the list is ordered by id from "after"; a backend start raises its alert near the top)
            var res = await PlatformClient.GetMonitoringAlertsAsync(Math.Max(0, (_lastId ?? 0) - 500), 500, unackedOnly: true);
            if (!res.Ok || res.Value == null)
            {
                SetPollProblem(Unreadable(res.Message) + " (unseen critical alerts from before this start are in Monitoring → Alerts)");
                return;
            }
            var critical = (res.Value.Alerts ?? new List<MonAlert>())
                .Where(a => !a.Acked && a.Severity == "critical" && a.Toast)
                .OrderBy(a => a.Id).ToList();
            if (critical.Count == 0) return;
            var toasted = ReadToasted();
            var fresh = critical.Where(a => !toasted.Contains(a.Id)).ToList();
            if (fresh.Count == 0) return;
            if (ToastsWanted())
            {
                if (fresh.Count > MaxToastsPerPoll)
                    Track(FundNotifications.ShowMonitoringAlert(fresh.Count + " unseen critical alerts",
                        string.Join(" · ", fresh.Take(6).Select(a => a.Title ?? a.Kind ?? "")), "critical", fresh.Max(a => a.Id)));
                else
                    foreach (var a in fresh)
                        Track(FundNotifications.ShowMonitoringAlert(a.Title ?? a.Kind ?? "Critical alert", a.Body ?? "", "critical", a.Id));
            }
            // muted / toasts off: consumed like every other alert (not toasted later) — they stay in the list and the rail badge
            foreach (var a in fresh) toasted.Add(a.Id);
            WriteToasted(toasted);
        }

        /// <summary>The ids already toasted by a first poll. An unreadable file is logged and treated as empty (worst case: one repeat).</summary>
        private static HashSet<long> ReadToasted()
        {
            var set = new HashSet<long>();
            try
            {
                if (!System.IO.File.Exists(ToastedPath)) return set;
                using var doc = System.Text.Json.JsonDocument.Parse(System.IO.File.ReadAllText(ToastedPath));
                if (doc.RootElement.TryGetProperty("ids", out var ids) && ids.ValueKind == System.Text.Json.JsonValueKind.Array)
                    foreach (var e in ids.EnumerateArray())
                        if (e.TryGetInt64(out var id)) set.Add(id);
            }
            catch (Exception ex)
            {
                try { AppLog.Warn(LogModule, "monitoring-toasted.json could not be read — an already shown critical alert may be toasted once more.", ex); } catch { }
            }
            return set;
        }

        private static void WriteToasted(HashSet<long> ids)
        {
            try
            {
                var keep = ids.OrderByDescending(i => i).Take(MaxRemembered).OrderBy(i => i).ToArray();
                var dir = System.IO.Path.GetDirectoryName(ToastedPath)!;
                System.IO.Directory.CreateDirectory(dir);
                var temp = ToastedPath + ".tmp";
                System.IO.File.WriteAllText(temp, System.Text.Json.JsonSerializer.Serialize(new { ids = keep }));
                System.IO.File.Move(temp, ToastedPath, overwrite: true);
            }
            catch (Exception ex)
            {
                try { AppLog.Warn(LogModule, "monitoring-toasted.json could not be written — these critical alerts may be toasted again after a restart.", ex); } catch { }
            }
        }

        private static string Unreadable(string reason)
            => "Alerts could not be read (" + reason + ", " + DateTime.Now.ToString("HH:mm", System.Globalization.CultureInfo.InvariantCulture)
               + ") — no alert toasts until the next good poll (every 15 s); open Monitoring → Alerts when it works again.";

        /// <summary>Records the outcome of one toast: a failure (not a mute) becomes <see cref="ToastProblem"/>; a success clears it.</summary>
        private static void Track(bool shown)
        {
            if (shown) { ToastProblem = null; return; }
            var failure = FundNotifications.MonitoringToastFailure;
            if (failure != null)
                ToastProblem = "Windows toasts are not available (" + failure + ") — monitoring alerts are only shown in Monitoring → Alerts and the rail badge.";
        }

        private static void LogOnce(string message, Exception? ex)
        {
            if (_failureLogged) return;
            _failureLogged = true;
            try
            {
                if (ex != null) AppLog.Warn(LogModule, message, ex);
                else AppLog.Warn(LogModule, message);
            }
            catch { }
        }
    }
}
