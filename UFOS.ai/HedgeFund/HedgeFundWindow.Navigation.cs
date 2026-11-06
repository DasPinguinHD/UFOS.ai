using System;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using UFOS.ai.HedgeFund.Platform;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund
{
    /// <summary>
    /// Stage 11 (UFOS.ai/docs/tasks/STAGE11_OVERVIEW_TASK.md §3): the 5-group rail — Overview · Strategies · Trading · Research ·
    /// Risk Lab. The rail items (GroupName "HedgeFundRail") only choose a group; the views are still selected by the existing Nav*
    /// radio buttons (GroupName "HedgeFundNav", <see cref="NavButton_Checked"/>): Strategies = Library · Studio · Loop and Trading =
    /// Portfolio &amp; Trading · Monitoring · Shadow Account · Legacy: Council &amp; old engine (NavOverviewButton, the former Overview,
    /// unchanged) are sub-tabs; Overview (NavHomeButton → Platform/OverviewView), Research and Risk Lab (placeholder) are hidden
    /// selectors. Every view selection syncs the rail and the sub-tab strip (<see cref="SyncRailWithView"/>); a group remembers its
    /// last sub-tab.
    /// </summary>
    public partial class HedgeFundWindow
    {
        private OverviewView? _overviewView;
        private RiskLabPlaceholderView? _riskLabView;
        private bool _navReady;
        private bool _syncingRail;
        private RadioButton? _lastStrategiesTab;
        private RadioButton? _lastTradingTab;

        /// <summary>Window_Loaded: shows the new Overview (the start view) and loads the Council state once (see below).</summary>
        private void InitNavigation()
        {
            _navReady = true;
            SyncRailWithView(NavHomeButton);
            EnsureOverviewView();
            OverviewHost.Visibility = Visibility.Visible;
            _ = ActivateOverviewAsync();
            // The Council view lives in Trading › Legacy now (hidden at start) and polls only while visible: one load at start, so the
            // window knows the switch-over state (_councilReplaced) from the beginning, as when it was on the start page.
            if (_councilView != null) _ = _councilView.RefreshAsync();
        }

        private async Task ActivateOverviewAsync()
        {
            try { if (_overviewView != null) await _overviewView.ActivateAsync(); }
            catch (Exception ex) { try { AppLog.Warn("HedgeFund", "Overview could not be activated.", ex); } catch { } }
        }

        private void EnsureOverviewView()
        {
            if (_overviewView != null) return;
            _overviewView = new OverviewView();
            _overviewView.NavigateRequested += NavigateTo;
            OverviewHost.Child = _overviewView;
        }

        private void EnsureRiskLabView()
        {
            if (_riskLabView != null) return;
            _riskLabView = new RiskLabPlaceholderView();
            RiskLabHost.Child = _riskLabView;
        }

        /// <summary>A rail item: shows its group's view (the last sub-tab used, else the first).</summary>
        private void RailButton_Checked(object sender, RoutedEventArgs e)
        {
            if (_syncingRail || !_navReady) return;   // raised during InitializeComponent, or by SyncRailWithView itself
            RadioButton target;
            if (ReferenceEquals(sender, RailStrategiesButton)) target = _lastStrategiesTab ?? NavLibraryButton;
            else if (ReferenceEquals(sender, RailTradingButton)) target = _lastTradingTab ?? NavTradingButton;
            else if (ReferenceEquals(sender, RailResearchButton)) target = NavResearchButton;
            else if (ReferenceEquals(sender, RailRiskLabButton)) target = NavRiskLabButton;
            else target = NavHomeButton;
            if (target.IsChecked == true) SyncRailWithView(target);
            else target.IsChecked = true;   // raises NavButton_Checked → shows the view and syncs the rail
        }

        /// <summary>Marks the rail group of a view selector as active and shows that group's sub-tabs (none for single-view groups).</summary>
        private void SyncRailWithView(object viewButton)
        {
            RadioButton rail;
            var strategies = false;
            var trading = false;
            if (ReferenceEquals(viewButton, NavLibraryButton) || ReferenceEquals(viewButton, NavStudioButton) || ReferenceEquals(viewButton, NavLoopButton))
            {
                rail = RailStrategiesButton;
                strategies = true;
                _lastStrategiesTab = (RadioButton)viewButton;
            }
            else if (ReferenceEquals(viewButton, NavTradingButton) || ReferenceEquals(viewButton, NavMonitoringButton)
                     || ReferenceEquals(viewButton, NavShadowButton) || ReferenceEquals(viewButton, NavOverviewButton))
            {
                rail = RailTradingButton;
                trading = true;
                _lastTradingTab = (RadioButton)viewButton;
            }
            else if (ReferenceEquals(viewButton, NavResearchButton)) rail = RailResearchButton;
            else if (ReferenceEquals(viewButton, NavRiskLabButton)) rail = RailRiskLabButton;
            else rail = RailOverviewButton;

            StrategiesTabs.Visibility = strategies ? Visibility.Visible : Visibility.Collapsed;
            TradingTabs.Visibility = trading ? Visibility.Visible : Visibility.Collapsed;
            SubTabStrip.Visibility = strategies || trading ? Visibility.Visible : Visibility.Collapsed;
            if (rail.IsChecked != true)
            {
                _syncingRail = true;
                try { rail.IsChecked = true; }
                finally { _syncingRail = false; }
            }
        }

        /// <summary>Navigation asked for by the Overview (attention strip, links, activity rows). Unknown targets do nothing.</summary>
        private void NavigateTo(string target)
        {
            if (_closed) return;
            switch (target)
            {
                case OverviewTargets.Alerts:
                    ShowMonitoring(null);   // Monitoring › Alerts
                    return;
                case OverviewTargets.OptionsArchive:
                    // no hook for the Options Archive section of Keys & data: the window opens on its first section
                    OpenKeysWindow(brokers: false);
                    return;
            }
            RadioButton? button = target switch
            {
                OverviewTargets.Library => NavLibraryButton,
                OverviewTargets.Studio => NavStudioButton,
                OverviewTargets.Loop => NavLoopButton,
                OverviewTargets.Trading => NavTradingButton,
                OverviewTargets.Orders => NavTradingButton,
                OverviewTargets.Monitoring => NavMonitoringButton,
                OverviewTargets.Shadow => NavShadowButton,
                OverviewTargets.Legacy => NavOverviewButton,
                OverviewTargets.Research => NavResearchButton,
                OverviewTargets.RiskLab => NavRiskLabButton,
                _ => null,
            };
            if (button != null && button.IsChecked != true) button.IsChecked = true;   // raises NavButton_Checked
        }
    }
}
