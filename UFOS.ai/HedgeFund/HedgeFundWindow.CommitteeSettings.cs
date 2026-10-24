using System;
using System.Threading.Tasks;
using System.Windows;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.Platform;

namespace UFOS.ai.HedgeFund
{
    /// <summary>
    /// Engine settings: "Run the investment committee automatically after each validation" + its model (Strategy
    /// Platform stage 5b, SPEC §14.3, API §14.7) below the ideation budget (the committee spends that budget).
    /// GET /v2/settings/committee fills the controls (refreshed with the state poll every 30 s); a click saves both
    /// values at once with PUT /v2/settings/committee {auto_after_validation, model} (audited). The controls stay
    /// disabled until the backend answered and show no default; a value the backend did not report stays unset and
    /// the toggle/radio click then sets it; a failed PUT shows the backend's text verbatim and reloads.
    /// </summary>
    public partial class HedgeFundWindow
    {
        private static readonly TimeSpan CommitteeSettingsReloadInterval = TimeSpan.FromSeconds(30);
        private DateTime _committeeSettingsLoadedAt = DateTime.MinValue;
        private bool _committeeLoading;
        private bool _committeeSaving;

        private async Task LoadCommitteeSettingsAsync()
        {
            if (_committeeLoading || _committeeSaving) return;
            _committeeLoading = true;
            try
            {
                var res = await PlatformClient.GetCommitteeSettingsAsync();
                _committeeSettingsLoadedAt = DateTime.UtcNow;
                if (_committeeSaving) return;
                if (!res.Ok || res.Value == null)
                {
                    SetCommitteeEnabled(res.Ok || res.Status == PlatformCallStatus.Unavailable);
                    SetCommitteeHint(res.Ok ? "Unexpected response from the backend (/v2/settings/committee) — backend and app versions may not match." : res.Message, true);
                    return;
                }
                ApplyCommitteeSettings(res.Value);
            }
            finally
            {
                _committeeLoading = false;
            }
        }

        private void ApplyCommitteeSettings(CommitteeSettings s)
        {
            CommitteeAutoToggle.IsChecked = s.AutoAfterValidation;   // null = unset (shown, never defaulted)
            CommitteeStandardRadio.IsChecked = s.Model == PlatformClient.ExplainModelStandard;
            CommitteePremiumRadio.IsChecked = s.Model == PlatformClient.ExplainModelPremium;
            SetCommitteeEnabled(true);
            var missing = s.AutoAfterValidation == null || (s.Model != PlatformClient.ExplainModelStandard && s.Model != PlatformClient.ExplainModelPremium);
            SetCommitteeHint(missing
                ? "The backend reported no valid value for " + (s.AutoAfterValidation == null ? "the automatic run" : "the model") + " — choose it to set it."
                : s.AutoAfterValidation == true
                    ? "On: after every finished validation (not holdout) the committee runs with the " + (s.Model == PlatformClient.ExplainModelPremium ? "Premium" : "Standard")
                      + " model if the ideation budget allows; otherwise the report notes \"committee skipped\"."
                    : "Off: the validation report and the Library offer \"Convene committee…\" instead.", missing);
        }

        private void SetCommitteeEnabled(bool on)
        {
            CommitteeAutoToggle.IsEnabled = on;
            CommitteeStandardRadio.IsEnabled = on;
            CommitteePremiumRadio.IsEnabled = on;
        }

        private void SetCommitteeHint(string text, bool error)
        {
            CommitteeSettingsHintText.Text = text;
            CommitteeSettingsHintText.Foreground = error ? FundPalette.Short : FundPalette.Faint;
        }

        private async void CommitteeSetting_Click(object sender, RoutedEventArgs e)
        {
            if (!_uiReady || _committeeSaving) return;
            var auto = CommitteeAutoToggle.IsChecked;
            var model = CommitteePremiumRadio.IsChecked == true ? PlatformClient.ExplainModelPremium
                      : CommitteeStandardRadio.IsChecked == true ? PlatformClient.ExplainModelStandard : null;
            if (auto == null || model == null)
            {
                // PUT needs both values: wait until the other one is chosen too (nothing is assumed)
                SetCommitteeHint("Choose " + (auto == null ? "on or off for the automatic run" : "the model") + " to save.", true);
                return;
            }
            _committeeSaving = true;
            SetCommitteeEnabled(false);
            SetCommitteeHint("Saving…", false);
            try
            {
                var res = await PlatformClient.PutCommitteeSettingsAsync(auto.Value, model);
                if (!res.Ok || res.Value == null)
                {
                    SetCommitteeHint("Not saved: " + (res.Ok ? "unexpected response from the backend (/v2/settings/committee)." : res.Message), true);
                    _committeeSettingsLoadedAt = DateTime.MinValue;   // the next poll shows the backend's unchanged values
                    return;
                }
                ApplyCommitteeSettings(res.Value);
            }
            finally
            {
                _committeeSaving = false;
                SetCommitteeEnabled(true);
            }
        }
    }
}
