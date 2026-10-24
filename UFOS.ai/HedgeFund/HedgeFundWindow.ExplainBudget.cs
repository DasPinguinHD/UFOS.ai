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
    /// Engine settings: "AI explanations — daily budget (USD)" (Strategy Platform §11.2/§11.3, API §11.6) next to the
    /// engine's AI daily budget. GET /v2/settings/explain fills the input and the hint with today's spend (UTC day,
    /// refreshed with the state poll every 30 s); an edit is saved 600 ms after the last keystroke with
    /// PUT /v2/settings/explain {daily_budget_usd} (0 = explanations off; audited by the backend). The input stays
    /// empty and disabled until the backend answered — no default is shown; a backend error is shown verbatim (a 503,
    /// e.g. an unreadable stored budget, keeps the input enabled: saving a value is the repair path). The range check
    /// uses the backend's max_daily_budget_usd; the SPEC maximum (50) only when the backend did not send one (format
    /// check only — the backend validates authoritatively).
    /// </summary>
    public partial class HedgeFundWindow
    {
        private const double ExplainBudgetSpecMax = 50.0;   // SPEC §11.2 explain_daily_budget_usd max 50
        private static readonly TimeSpan ExplainSettingsReloadInterval = TimeSpan.FromSeconds(30);

        private ExplainSettings? _explainSettings;
        private DateTime _explainSettingsLoadedAt = DateTime.MinValue;
        private DispatcherTimer? _explainDebounce;
        private double? _explainPending;
        private bool _explainLoading;
        private bool _explainSaving;
        private bool _applyingExplain;

        private double ExplainBudgetMax => _explainSettings?.MaxDailyBudgetUsd is double m && m > 0 ? m : ExplainBudgetSpecMax;

        private async Task LoadExplainSettingsAsync()
        {
            if (_explainLoading || _explainSaving || _explainPending != null) return;
            _explainLoading = true;
            try
            {
                var res = await PlatformClient.GetExplainSettingsAsync();
                _explainSettingsLoadedAt = DateTime.UtcNow;
                if (_explainSaving || _explainPending != null) return;   // an edit is in flight: its answer wins
                if (!res.Ok || res.Value == null)
                {
                    // 503 (e.g. the stored budget is unreadable): PUT repairs it, so the input stays usable
                    ExplainBudgetInput.IsEnabled = res.Ok || res.Status == PlatformCallStatus.Unavailable;
                    SetExplainHint(res.Ok ? "Unexpected response from the backend (/v2/settings/explain) — backend and app versions may not match." : res.Message, true);
                    return;
                }
                ApplyExplainSettings(res.Value);
            }
            finally
            {
                _explainLoading = false;
            }
        }

        private void ApplyExplainSettings(ExplainSettings s)
        {
            _explainSettings = s;
            if (!ExplainBudgetInput.IsKeyboardFocusWithin)
            {
                _applyingExplain = true;
                try
                {
                    ExplainBudgetInput.Text = s.DailyBudgetUsd is double b ? b.ToString("0.00", Inv) : "";
                }
                finally
                {
                    _applyingExplain = false;
                }
            }
            ExplainBudgetInput.IsEnabled = true;
            SetExplainHint(ExplainSpendText(s), false);
        }

        private static string ExplainSpendText(ExplainSettings s)
        {
            if (s.DailyBudgetUsd == null) return "The backend reported no budget — enter one to set it.";
            var text = "Today" + (string.IsNullOrWhiteSpace(s.Day) ? "" : " (UTC " + s.Day + ")") + ": " + FundFormat.Usd(s.SpentTodayUsd) + " spent";
            if (s.RemainingUsd is double r) text += " · " + FundFormat.Usd(r) + " left";
            text += s.Enabled == false ? " · explanations off (0)" : " · 0 = off";
            return text;
        }

        private void SetExplainHint(string text, bool error)
        {
            ExplainBudgetHintText.Text = text;
            ExplainBudgetHintText.Foreground = error ? FundPalette.Short : FundPalette.Faint;
        }

        private void ExplainBudgetInput_TextChanged(object sender, TextChangedEventArgs e)
        {
            if (!_uiReady || _applyingExplain) return;
            var max = ExplainBudgetMax;
            var text = (ExplainBudgetInput.Text ?? "").Trim().Replace(',', '.');
            if (double.TryParse(text, NumberStyles.Float, Inv, out var value) && !double.IsNaN(value) && value >= 0 && value <= max)
            {
                _explainPending = Math.Round(value, 2);
                SetExplainHint("Saving…", false);
                if (_explainDebounce == null)
                {
                    _explainDebounce = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(600) };
                    _explainDebounce.Tick += ExplainDebounce_Tick;
                }
                _explainDebounce.Stop();
                _explainDebounce.Start();
            }
            else
            {
                _explainPending = null;
                _explainDebounce?.Stop();
                SetExplainHint("Enter 0–" + max.ToString("0.##", Inv) + " (0 = explanations off)", true);
            }
        }

        private async void ExplainDebounce_Tick(object? sender, EventArgs e)
        {
            _explainDebounce?.Stop();
            if (_explainSaving || _explainPending is not double value) return;
            _explainSaving = true;
            _explainPending = null;
            try
            {
                var res = await PlatformClient.PutExplainSettingsAsync(value);
                if (!res.Ok || res.Value == null)
                {
                    SetExplainHint("Not saved: " + (res.Ok ? "unexpected response from the backend (/v2/settings/explain)." : res.Message), true);
                    return;
                }
                if (_explainPending == null) ApplyExplainSettings(res.Value);
                else _explainSettings = res.Value;
            }
            finally
            {
                _explainSaving = false;
                if (_explainPending != null) _explainDebounce?.Start();   // typed again while saving: save the newest value
            }
        }
    }
}
