using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
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
    /// </summary>
    public partial class HedgeFundWindow
    {
        private CouncilView? _councilView;
        /// <summary>True while council.switchover is "done" or "pending": the old engine is replaced (its /fund routes answer 410).</summary>
        private bool _councilReplaced;

        private void InitCouncil()
        {
            _councilView = new CouncilView();
            _councilView.StateChanged += ApplyCouncilState;
            CouncilHost.Child = _councilView;
        }

        private void ApplyCouncilState(CouncilState? state)
        {
            if (state == null) return;   // unreadable: keep the current layout (the view says why)
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
