using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.Platform;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund
{
    /// <summary>
    /// S10-C R3: the Council view at the top of the Overview (Platform/CouncilView.cs) and what the switch-over changes in this window
    /// (SPEC §22.8): once the Council is switched over (or the switch-over is pending) the old engine's sections are hidden and its
    /// /fund/state polling stops (those routes answer 410 "replaced"); "Resume…" lists the halted platform accounts (plus the old
    /// engine only while it still runs); the kill result reports the platform / old engine / Council halves.
    /// retire1 (SPEC §22.15): while the Council + old engine are retired the Trading sub-tab reads "Retired: Council &amp; old engine",
    /// the toolbar hides ▶ Run now (the old engine's /fund/run), its hint and "Resume engine", and the kill switch speaks of
    /// "platform + paper-council" only. The start load in Navigation stays, so _councilReplaced is known from the beginning.
    /// </summary>
    public partial class HedgeFundWindow
    {
        private CouncilView? _councilView;
        /// <summary>True while council.switchover is "done" or "pending": the old engine is replaced (its /fund routes answer 410).</summary>
        private bool _councilReplaced;
        /// <summary>True while the Council + old engine are retired (SPEC §22.15; CouncilState.retired == true).</summary>
        private bool _councilRetired;
        private object? _killToolTipDefault;
        private string? _killAutomationDefault;

        private void InitCouncil()
        {
            _councilView = new CouncilView();
            _councilView.StateChanged += ApplyCouncilState;
            CouncilHost.Child = _councilView;
        }

        private void ApplyCouncilState(CouncilState? state)
        {
            if (state == null) return;   // unreadable: keep the current layout (the view says why)
            ApplyRetired(state.IsRetired);
            var replaced = state.Switchover is "done" or "pending";
            FundNotifications.SetCouncilSwitchedOver(replaced);   // the toast source moves to the monitoring alerts (and back after a revert)
            if (replaced == _councilReplaced && LegacyDashboardGrid.Visibility == (replaced ? Visibility.Collapsed : Visibility.Visible)) return;
            _councilReplaced = replaced;
            var legacy = replaced ? Visibility.Collapsed : Visibility.Visible;
            LegacyEngineHeader.Visibility = legacy;
            LegacyDashboardGrid.Visibility = legacy;
            LegacyExecutionSection.Visibility = legacy;
            LegacyHistorySection.Visibility = legacy;
            if (replaced)
            {
                // the old engine's banners belong to the hidden sections
                ConnectionBanner.Visibility = Visibility.Collapsed;
                LegacyBanner.Visibility = Visibility.Collapsed;
                AccountBanner.Visibility = Visibility.Collapsed;
                HaltBanner.Visibility = Visibility.Collapsed;
            }
            else
            {
                _ = RefreshStateAsync();   // reverted: the old engine is back — load its state again
            }
            AppLog.Info("HedgeFund", "Overview: the Council is " + (replaced ? "switched over — old engine sections hidden." : "not switched over — old engine sections shown."));
        }

        /// <summary>retire1 (SPEC §22.15): sub-tab wording, toolbar (▶ Run now, its hint, Resume engine) and the kill switch's texts.</summary>
        private void ApplyRetired(bool retired)
        {
            _killToolTipDefault ??= KillSwitchButton.ToolTip;
            _killAutomationDefault ??= AutomationProperties.GetName(KillSwitchButton);
            if (retired == _councilRetired && NavOverviewButton.Content is string current
                && current == (retired ? RetiredTabText : LegacyTabText)) return;
            _councilRetired = retired;
            NavOverviewButton.Content = retired ? RetiredTabText : LegacyTabText;
            AutomationProperties.SetName(NavOverviewButton, retired
                ? "Retired: Council of AIs and the old engine — retire date, reason, run history, Un-retire"
                : "Legacy: Council of AIs and the old engine — the former Overview, unchanged");
            NavOverviewButton.ToolTip = retired
                ? "The Council of AIs and the old engine are retired: retire date and reason, the Council's run history, Un-retire…"
                : "The former Overview, unchanged: the Council of AIs and the old paper-trading engine (watchlist, account, positions, execution, settings, history)";
            if (retired)
            {
                RunNowButton.Visibility = Visibility.Collapsed;      // ▶ Run now = the old engine's /fund/run
                RunNowHintText.Visibility = Visibility.Collapsed;
                ResumeButton.Visibility = Visibility.Collapsed;      // "Resume engine" (accounts: Portfolio & Trading)
                KillSwitchButton.ToolTip = "Platform kill (Strategy Platform SPEC §17.1): halts every platform paper account incl. paper-council "
                                           + "(auto trading off, working orders cancelled, positions closed; market orders queue until the next open "
                                           + "while the market is closed). The Council and the old engine are retired. Asks for confirmation first.";
                AutomationProperties.SetName(KillSwitchButton, "Kill switch: halt the Strategy Platform and paper-council and close all simulated positions");
            }
            else
            {
                RunNowButton.Visibility = Visibility.Visible;
                RunNowHintText.Visibility = _runNowProminent == true ? Visibility.Visible : Visibility.Collapsed;
                ResumeButton.Visibility = !_councilReplaced && _state?.Halted?.Active == true ? Visibility.Visible : Visibility.Collapsed;
                KillSwitchButton.ToolTip = _killToolTipDefault;
                if (_killAutomationDefault != null) AutomationProperties.SetName(KillSwitchButton, _killAutomationDefault);
            }
            AppLog.Info("HedgeFund", retired ? "Council + old engine retired — toolbar Run now / Resume engine hidden." : "Council + old engine not retired.");
        }

        private const string LegacyTabText = "Legacy: Council & old engine";
        private const string RetiredTabText = "Retired: Council & old engine";

        /// <summary>"Resume…" (SPEC §22.8): halted platform accounts (+ the old engine while it still runs and is halted).</summary>
        private async Task OpenResumeDialogAsync()
        {
            var state = await PlatformClient.GetTradingStateAsync();
            if (!state.Ok || state.Value == null)
            {
                FundDialogs.Info(this, "Resume", "The account list could not be loaded: " + (state.Ok ? "empty answer" : PlatformClient.DescribeFailure(state)));
                return;
            }
            var halted = (state.Value.Accounts ?? new List<TradingAccountSummary>()).Where(a => a.Halted).ToList();
            var oldEngineHalted = !_councilReplaced && _state?.Halted?.Active == true;
            var dialog = new ResumeDialog(this, halted, oldEngineHalted, _councilReplaced);
            dialog.ShowDialog();
            if (dialog.Changed)
            {
                await RefreshStateAsync();
                if (_councilView != null) await _councilView.RefreshAsync();
            }
        }
    }
}
