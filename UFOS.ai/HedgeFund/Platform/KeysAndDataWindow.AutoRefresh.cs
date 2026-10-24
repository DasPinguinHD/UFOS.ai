using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Newsroom;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// Keys &amp; data — window-wide auto refresh (user request 2026-10-24): every 10 s while the backend is not reachable yet
    /// (GET /health) or while a state change settles (Register / Unregister / Run once of the options archive, broker connect /
    /// disconnect — until the expected state is reached, at most 2 minutes, then a clear "still not confirmed" note), otherwise
    /// every 60 s. Covers the key status, data sources, network (reloaded while it has no data), the options archive card incl.
    /// the read-only schtasks /Query; brokers keep their own 5 s poll. Only while the window is visible and not minimized; one
    /// refresh at a time (a tick during a refresh is skipped). The manual ↻ buttons stay.
    /// </summary>
    public partial class KeysAndDataWindow
    {
        private static readonly TimeSpan AutoFastInterval = TimeSpan.FromSeconds(10);
        private static readonly TimeSpan AutoNormalInterval = TimeSpan.FromSeconds(60);
        private static readonly TimeSpan SettleMax = TimeSpan.FromMinutes(2);

        private sealed class SettleEntry
        {
            public string Key = "";
            public DateTime Deadline;
            public Func<bool> Reached = () => true;
            public Action OnTimeout = () => { };
        }

        private readonly List<SettleEntry> _settle = new();
        private DispatcherTimer? _autoTimer;
        private bool _autoBusy;
        private bool _autoClosed;
        private bool _backendReachable = true;   // until the first /health answer

        /// <summary>Called from the constructor (after the section inits).</summary>
        private void InitAutoRefresh()
        {
            _autoTimer = new DispatcherTimer { Interval = AutoFastInterval };
            _autoTimer.Tick += async (_, _) => await AutoRefreshAsync();
            IsVisibleChanged += (_, _) => UpdateAutoTimer();
            StateChanged += (_, _) => UpdateAutoTimer();
            Loaded += (_, _) => UpdateAutoTimer();
            Closed += (_, _) =>
            {
                _autoClosed = true;
                _autoTimer?.Stop();
            };
        }

        private bool AutoFast => !_backendReachable || _settle.Count > 0;

        private void UpdateAutoTimer()
        {
            if (_autoTimer == null) return;
            var interval = AutoFast ? AutoFastInterval : AutoNormalInterval;
            if (_autoTimer.Interval != interval) _autoTimer.Interval = interval;
            if (!_autoClosed && IsLoaded && IsVisible && WindowState != WindowState.Minimized) { if (!_autoTimer.IsEnabled) _autoTimer.Start(); }
            else _autoTimer.Stop();
        }

        /// <summary>
        /// A state change to wait for: <paramref name="reached"/> is checked after every auto refresh (10 s cadence while any entry is
        /// open); after 2 minutes without it, <paramref name="onTimeout"/> shows the "still not confirmed" note. Same key = replaced.
        /// </summary>
        private void AddSettle(string key, Func<bool> reached, Action onTimeout)
        {
            _settle.RemoveAll(e => e.Key == key);
            _settle.Add(new SettleEntry { Key = key, Deadline = DateTime.UtcNow + SettleMax, Reached = reached, OnTimeout = onTimeout });
            if (_autoTimer != null)
            {
                _autoTimer.Stop();                   // restart so the first settle check comes 10 s after the action
                UpdateAutoTimer();
            }
        }

        private async Task AutoRefreshAsync()
        {
            if (_autoBusy || _autoClosed || !IsVisible || WindowState == WindowState.Minimized) return;
            _autoBusy = true;
            try
            {
                var wasDown = !_backendReachable;
                _backendReachable = await NewsroomBackend.IsRunningAsync();
                if (_autoClosed) return;
                // the key rows are rebuilt by a key refresh (focus) — only while the backend is / was down (its status line changes then)
                if (!_keyActionRunning && (wasDown || !_backendReachable)) await RefreshBackendKeysAsync(quiet: true);
                await RefreshSourcesAsync(quiet: true);
                if (_backendReachable && (wasDown || _network == null)) await RefreshNetworkAsync();
                await RefreshOptionsArchiveAsync(queryTask: true, fromTimer: true);
                EvaluateSettle();
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Keys & data: auto refresh failed.", ex);
            }
            finally
            {
                _autoBusy = false;
                UpdateAutoTimer();
            }
        }

        private void EvaluateSettle()
        {
            if (_settle.Count == 0) return;
            var now = DateTime.UtcNow;
            foreach (var e in _settle.ToList())
            {
                bool reached;
                try { reached = e.Reached(); }
                catch (Exception) { reached = false; }
                if (reached) _settle.Remove(e);
                else if (now >= e.Deadline)
                {
                    _settle.Remove(e);
                    try { e.OnTimeout(); } catch (Exception ex) { AppLog.Warn(LogModule, "Keys & data: settle note failed.", ex); }
                }
            }
            RenderOptionsArchive();
        }
    }
}
