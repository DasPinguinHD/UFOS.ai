using System;
using System.Windows;
using UFOS.ai.HedgeFund.Platform;

namespace UFOS.ai.HedgeFund
{
    /// <summary>
    /// Navigation rail entries "Monitoring" and "Shadow Account" (Strategy Platform stage 9 — SPEC §19, WPF notes §19.13):
    /// Platform/MonitoringView and Platform/ShadowAccountView, created on first use like the other sections. The Monitoring view
    /// polls only while it is visible; the Shadow Account view does not poll at all; closing the window stops both for good.
    /// Monitoring toasts come from the app-wide <see cref="MonitoringNotifier"/> (started in Window_Loaded); its unacked count
    /// is shown as a badge on the rail entry, and a toast click opens Monitoring → Alerts (<see cref="ShowMonitoring"/>).
    /// </summary>
    public partial class HedgeFundWindow
    {
        private MonitoringView? _monitoringView;
        private ShadowAccountView? _shadowView;

        private void EnsureMonitoringView()
        {
            if (_monitoringView != null) return;
            _monitoringView = new MonitoringView();
            _monitoringView.BrokersRequested += () => OpenKeysWindow(brokers: true);
            // S10-C: a Council alert → the Council view at the top of the Overview
            _monitoringView.CouncilRequested += () => { if (NavOverviewButton.IsChecked != true) NavOverviewButton.IsChecked = true; OverviewScroll.ScrollToTop(); };
            MonitoringHost.Child = _monitoringView;
        }

        private void EnsureShadowView()
        {
            if (_shadowView != null) return;
            _shadowView = new ShadowAccountView();
            ShadowHost.Child = _shadowView;
        }

        /// <summary>Toast click / banner: show this window on Monitoring → Alerts (optionally one alert, marked as seen).</summary>
        internal async void ShowMonitoring(long? alertId)
        {
            if (_closed) return;
            try
            {
                BringToFront();   // live click-test 2026-10-22: restores the last non-minimized state, activates + focuses
                EnsureMonitoringView();
                if (NavMonitoringButton.IsChecked != true) NavMonitoringButton.IsChecked = true; // raises NavButton_Checked → ActivateAsync
                if (_monitoringView != null) await _monitoringView.ShowAlertAsync(alertId);
            }
            catch (Exception ex)
            {
                try { UFOS.ai.Logging.AppLog.Warn("HedgeFund", "Could not open Monitoring from a toast.", ex); } catch { }
            }
        }

        /// <summary>
        /// Stage 9b (SPEC §20.3): the MainWindow "Portfolio Insight" card opens Monitoring → Portfolio insights. Public because the
        /// main app (another assembly) calls it on the window it opened or found; never throws.
        /// </summary>
        public async void ShowPortfolioInsights()
        {
            if (_closed) return;
            try
            {
                BringToFront();   // live click-test 2026-10-22: restores the last non-minimized state, activates + focuses
                EnsureMonitoringView();
                if (NavMonitoringButton.IsChecked != true) NavMonitoringButton.IsChecked = true; // raises NavButton_Checked → ActivateAsync
                if (_monitoringView != null) await _monitoringView.ShowInsightsAsync();
            }
            catch (Exception ex)
            {
                try { UFOS.ai.Logging.AppLog.Warn("HedgeFund", "Could not open Portfolio insights.", ex); } catch { }
            }
        }

        /// <summary>Rail badge: number of alerts not yet marked as seen (hidden at 0 / unknown).</summary>
        private void MonitoringNotifier_Changed(System.Collections.Generic.IReadOnlyList<MonAlert> fresh, int? unacked)
        {
            if (_closed) return;
            UpdateMonitoringBadge(unacked);
        }

        private void UpdateMonitoringBadge(int? unacked)
        {
            if (NavMonitoringBadge == null) return;
            if (unacked is int n && n > 0)
            {
                NavMonitoringBadge.Text = "● " + (n > 99 ? "99+" : n.ToString(System.Globalization.CultureInfo.InvariantCulture));
                NavMonitoringBadge.Visibility = Visibility.Visible;
                System.Windows.Automation.AutomationProperties.SetItemStatus(NavMonitoringButton, n.ToString(System.Globalization.CultureInfo.InvariantCulture) + " alerts not yet seen");
            }
            else
            {
                NavMonitoringBadge.Visibility = Visibility.Collapsed;
                System.Windows.Automation.AutomationProperties.SetItemStatus(NavMonitoringButton, "");
            }
        }
    }
}
