using System;
using System.Globalization;
using System.Threading.Tasks;
using System.Windows.Controls;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.Platform;

namespace UFOS.ai.HedgeFund
{
    /// <summary>
    /// Engine settings: "AI ideation (Studio) — monthly budget / daily cap (USD)" (Strategy Platform stage 5a, SPEC
    /// §13.1, API §13.5) next to the other AI budgets. GET /v2/settings/ideation fills both inputs and the hint with
    /// this month's / today's spend (UTC; refreshed with the state poll every 30 s); an edit is saved 600 ms after the
    /// last keystroke with PUT /v2/settings/ideation {monthly_budget_usd, daily_cap_usd} — both values are required, so
    /// both inputs must hold a valid number (monthly 0 = ideation off; audited by the backend). The inputs stay empty
    /// and disabled until the backend answered — no default is shown; a backend error is shown verbatim (a 503 keeps
    /// them enabled: saving is the repair path). The range check uses the backend's max values; the SPEC maximum (200)
    /// only when the backend did not send one (format check only — the backend validates authoritatively).
    /// </summary>
    public partial class HedgeFundWindow
    {
        private const double IdeationSpecMax = 200.0;   // SPEC §13.5: numbers 0..200
        private static readonly TimeSpan IdeationSettingsReloadInterval = TimeSpan.FromSeconds(30);

        private IdeationSettings? _ideationSettings;
        private DateTime _ideationSettingsLoadedAt = DateTime.MinValue;
        private DispatcherTimer? _ideationDebounce;
        private (double Monthly, double Daily)? _ideationPending;
        private bool _ideationLoading;
        private bool _ideationSaving;
        private bool _applyingIdeation;

        private double IdeationMonthlyMax => _ideationSettings?.MaxMonthlyBudgetUsd is double m && m > 0 ? m : IdeationSpecMax;
        private double IdeationDailyMax => _ideationSettings?.MaxDailyCapUsd is double d && d > 0 ? d : IdeationSpecMax;

        private async Task LoadIdeationSettingsAsync()
        {
            if (_ideationLoading || _ideationSaving || _ideationPending != null) return;
            _ideationLoading = true;
            try
            {
                var res = await PlatformClient.GetIdeationSettingsAsync();
                _ideationSettingsLoadedAt = DateTime.UtcNow;
                if (_ideationSaving || _ideationPending != null) return;   // an edit is in flight: its answer wins
                if (!res.Ok || res.Value == null)
                {
                    var enable = res.Ok || res.Status == PlatformCallStatus.Unavailable;   // 503: PUT repairs the stored values
                    IdeationMonthlyInput.IsEnabled = enable;
                    IdeationDailyInput.IsEnabled = enable;
                    SetIdeationHint(res.Ok ? "Unexpected response from the backend (/v2/settings/ideation) — backend and app versions may not match." : res.Message, true);
                    return;
                }
                ApplyIdeationSettings(res.Value);
            }
            finally
            {
                _ideationLoading = false;
            }
        }

        private void ApplyIdeationSettings(IdeationSettings s)
        {
            _ideationSettings = s;
            _applyingIdeation = true;
            try
            {
                if (!IdeationMonthlyInput.IsKeyboardFocusWithin)
                    IdeationMonthlyInput.Text = s.MonthlyBudgetUsd is double m ? m.ToString("0.00", Inv) : "";
                if (!IdeationDailyInput.IsKeyboardFocusWithin)
                    IdeationDailyInput.Text = s.DailyCapUsd is double d ? d.ToString("0.00", Inv) : "";
            }
            finally
            {
                _applyingIdeation = false;
            }
            IdeationMonthlyInput.IsEnabled = true;
            IdeationDailyInput.IsEnabled = true;
            var missing = s.MonthlyBudgetUsd == null || s.DailyCapUsd == null
                ? "The backend reported no value for " + (s.MonthlyBudgetUsd == null && s.DailyCapUsd == null ? "either budget" : s.MonthlyBudgetUsd == null ? "the monthly budget" : "the daily cap")
                  + " — enter both to set them. "
                : "";
            SetIdeationHint(missing + StudioJson.BudgetLine(s), missing.Length > 0 || !string.IsNullOrWhiteSpace(s.PausedReason));
        }

        private void SetIdeationHint(string text, bool error)
        {
            IdeationBudgetHintText.Text = text;
            IdeationBudgetHintText.Foreground = error ? FundPalette.Short : FundPalette.Faint;
        }

        private void IdeationBudgetInput_TextChanged(object sender, TextChangedEventArgs e)
        {
            if (!_uiReady || _applyingIdeation) return;
            var mMax = IdeationMonthlyMax;
            var dMax = IdeationDailyMax;
            var mOk = TryBudget(IdeationMonthlyInput.Text, mMax, out var monthly);
            var dOk = TryBudget(IdeationDailyInput.Text, dMax, out var daily);
            if (mOk && dOk)
            {
                _ideationPending = (Math.Round(monthly, 2), Math.Round(daily, 2));
                SetIdeationHint("Saving…", false);
                if (_ideationDebounce == null)
                {
                    _ideationDebounce = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(600) };
                    _ideationDebounce.Tick += IdeationDebounce_Tick;
                }
                _ideationDebounce.Stop();
                _ideationDebounce.Start();
            }
            else
            {
                _ideationPending = null;
                _ideationDebounce?.Stop();
                SetIdeationHint((mOk ? "" : "Monthly budget: enter 0–" + mMax.ToString("0.##", Inv) + " (0 = ideation off). ")
                                + (dOk ? "" : "Daily cap: enter 0–" + dMax.ToString("0.##", Inv) + "."), true);
            }
        }

        private static bool TryBudget(string? raw, double max, out double value)
        {
            var text = (raw ?? "").Trim().Replace(',', '.');
            return double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out value) && !double.IsNaN(value) && value >= 0 && value <= max;
        }

        private async void IdeationDebounce_Tick(object? sender, EventArgs e)
        {
            _ideationDebounce?.Stop();
            if (_ideationSaving || _ideationPending == null) return;
            var value = _ideationPending.Value;
            _ideationSaving = true;
            _ideationPending = null;
            try
            {
                var res = await PlatformClient.PutIdeationSettingsAsync(value.Monthly, value.Daily);
                if (!res.Ok || res.Value == null)
                {
                    SetIdeationHint("Not saved: " + (res.Ok ? "unexpected response from the backend (/v2/settings/ideation)." : res.Message), true);
                    return;
                }
                if (_ideationPending == null) ApplyIdeationSettings(res.Value);
                else _ideationSettings = res.Value;
            }
            finally
            {
                _ideationSaving = false;
                if (_ideationPending != null) _ideationDebounce?.Start();   // typed again while saving: save the newest values
            }
        }
    }
}
