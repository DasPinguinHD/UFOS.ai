using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// "Network" section of Keys &amp; data (Strategy Platform §10.1/§10.4): data-loading concurrency.
    /// Adaptive (default, recommended for Wi-Fi: AIMD per source, 1–8) or a fixed number 1–8, via
    /// GET/PUT /v2/settings/network (audited by the backend), plus the limiter's current concurrency per
    /// source (alpaca, tiingo). Rules: stored_invalid (or a null adaptive/fixed) → amber notice with the backend's
    /// `error` and the raw stored values, the null value shown as unset (never a default), controls enabled and
    /// "Save" only after an explicit choice — the PUT repairs the stored values; a failed PUT shows the backend's
    /// text verbatim (normal mode: the controls return to the backend's unchanged state). The input range comes from the backend's min/max; the
    /// SPEC range 1–8 is used only as the client-side format check when they are not sent (the backend
    /// validates authoritatively either way).
    /// </summary>
    public partial class KeysAndDataWindow
    {
        private const int SpecFixedMin = 1;   // SPEC §10.1 network.fixed 1–8
        private const int SpecFixedMax = 8;
        private static readonly string[] NetworkSources = { "alpaca", "tiingo" };

        private NetworkSettings? _network;
        private int _networkGeneration;
        private bool _networkSaving;
        private bool _networkRepair;   // stored_invalid or a null adaptive/fixed: explicit choice + Save required

        private int FixedMin => _network?.Min ?? SpecFixedMin;
        private int FixedMax => _network?.Max ?? SpecFixedMax;

        private async Task RefreshNetworkAsync()
        {
            var generation = ++_networkGeneration;
            NetworkCurrentText.Text = "Loading…";
            NetworkCurrentText.Foreground = FundPalette.Dim;
            var result = await PlatformClient.GetNetworkSettingsAsync();
            if (generation != _networkGeneration) return;
            if (!result.Ok || result.Value == null)
            {
                DisableNetwork(result.Ok
                    ? "Unexpected response from the backend (/v2/settings/network) — backend and app versions may not match."
                    : result.Offline ? PlatformClient.BackendNotRunning : result.Message);
                return;
            }
            ApplyNetwork(result.Value);
        }

        private void DisableNetwork(string reason)
        {
            _network = null;
            _networkRepair = false;
            AdaptiveCheckBox.IsEnabled = false;
            FixedPanel.Visibility = Visibility.Collapsed;
            FixedErrorText.Visibility = Visibility.Collapsed;
            NetworkRepairBorder.Visibility = Visibility.Collapsed;
            ApplyFixedButton.IsEnabled = false;
            NetworkCurrentText.Text = reason;
            NetworkCurrentText.Foreground = FundPalette.Risk;
        }

        /// <summary>
        /// Shows the backend's settings. Repair mode when the backend says stored_invalid (its `error` text and the raw
        /// stored values in an amber notice) or when adaptive/fixed is null: a null value is shown as UNSET (toggle
        /// indeterminate, empty number) — never as a default — the controls stay enabled, a toggle click does not save,
        /// and "Save" needs an explicit Adaptive on/off choice plus a valid number (PUT repairs the stored values).
        /// </summary>
        private void ApplyNetwork(NetworkSettings s)
        {
            _network = s;
            _networkRepair = s.StoredInvalid == true || s.Adaptive == null || s.Fixed == null;
            AdaptiveCheckBox.IsThreeState = false;      // a click from "unset" goes to off/on, never back to unset
            AdaptiveCheckBox.IsChecked = s.Adaptive;    // null = indeterminate (unset)
            AdaptiveCheckBox.IsEnabled = !_networkSaving;
            FixedPanel.Visibility = _networkRepair || s.Adaptive == false ? Visibility.Visible : Visibility.Collapsed;
            FixedLabelText.Text = "Fixed number of parallel requests (" + FixedMin.ToString(CultureInfo.InvariantCulture)
                                  + "–" + FixedMax.ToString(CultureInfo.InvariantCulture) + ")"
                                  + (_networkRepair ? " — used when Adaptive is off" : "");
            ApplyFixedButton.Content = _networkRepair ? "Save" : "Apply";
            FixedBox.Text = s.Fixed is int f ? f.ToString(CultureInfo.InvariantCulture) : "";   // TextChanged validates + updates the button
            if (!_networkRepair) FixedErrorText.Visibility = Visibility.Collapsed;
            ShowRepairNotice(s);
            NetworkCurrentText.Text = CurrentText(s);
            NetworkCurrentText.Foreground = FundPalette.Dim;
            UpdateApplyFixed();
        }

        private void ShowRepairNotice(NetworkSettings s)
        {
            if (!_networkRepair)
            {
                NetworkRepairBorder.Visibility = Visibility.Collapsed;
                return;
            }
            var lines = new List<string>();
            if (s.StoredInvalid == true)
            {
                lines.Add("Stored network settings are invalid — choose the values and save to repair.");
                // the backend's text, verbatim
                lines.Add(string.IsNullOrWhiteSpace(s.Error) ? "(The backend sent no error text.)" : s.Error!);
                if (s.Stored is JsonElement raw && raw.ValueKind == JsonValueKind.Object)
                    lines.Add("Stored: " + string.Join(" · ", raw.EnumerateObject().Select(p => p.Name + " = " + FundFormat.Plain(p.Value))));
            }
            else
            {
                var missing = new List<string>();
                if (s.Adaptive == null) missing.Add("adaptive");
                if (s.Fixed == null) missing.Add("fixed");
                lines.Add("The backend reported no value for " + string.Join(" and ", missing)
                          + " — nothing is assumed. Choose the values and save.");
            }
            lines.Add("Data loading fails until valid settings are saved.");
            NetworkRepairText.Text = string.Join("\n", lines);
            NetworkRepairBorder.Visibility = Visibility.Visible;
        }

        /// <summary>"Current concurrency: Alpaca 4 · Tiingo 4 — adaptive …" from `current`; a source the backend does not
        /// list is shown as "not reported"; an unset mode says so.</summary>
        private static string CurrentText(NetworkSettings s)
        {
            var mode = s.Adaptive switch
            {
                true => " — adaptive: halves on a timeout, +1 after 20 successes in a row",
                false => " — fixed " + (s.Fixed is int f ? f.ToString(CultureInfo.InvariantCulture) : "(not set)") + " per source",
                null => " — mode not set",
            };
            if (s.Current is not JsonElement cur || cur.ValueKind == JsonValueKind.Null || cur.ValueKind == JsonValueKind.Undefined)
                return "Current concurrency: not reported by the backend" + mode;
            if (cur.ValueKind != JsonValueKind.Object)
                return "Current concurrency: " + ValidationReportView.ValueText(cur) + mode;
            var parts = new List<string>();
            foreach (var src in NetworkSources)
                parts.Add(Cap(src) + " " + (BtJson.Get(cur, src) is JsonElement v ? ValidationReportView.ValueText(v) : "not reported"));
            foreach (var p in cur.EnumerateObject())
                if (!NetworkSources.Contains(p.Name, StringComparer.OrdinalIgnoreCase))
                    parts.Add(Cap(p.Name) + " " + ValidationReportView.ValueText(p.Value));
            return "Current concurrency: " + string.Join(" · ", parts) + mode
                   + " · updated " + DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture);
        }

        private static string Cap(string s) => s.Length == 0 ? s : char.ToUpperInvariant(s[0]) + s[1..];

        /// <summary>The typed number when it is a whole number inside the range, else null (and the reason).</summary>
        private int? ParseFixed(out string? problem)
        {
            problem = null;
            var text = (FixedBox.Text ?? "").Trim();
            if (!int.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out var n) || n < FixedMin || n > FixedMax)
            {
                problem = "Enter a whole number from " + FixedMin.ToString(CultureInfo.InvariantCulture) + " to " + FixedMax.ToString(CultureInfo.InvariantCulture) + ".";
                return null;
            }
            return n;
        }

        private void UpdateApplyFixed()
        {
            var n = ParseFixed(out _);
            ApplyFixedButton.IsEnabled = !_networkSaving && _network != null && n != null
                                         && (_networkRepair
                                             ? AdaptiveCheckBox.IsChecked != null                       // explicit on/off choice
                                             : _network.Adaptive == false && n != _network.Fixed);
            FixedBox.IsEnabled = !_networkSaving && _network != null;
        }

        private void FixedBox_TextChanged(object sender, TextChangedEventArgs e)
        {
            if (_network == null) return;
            ParseFixed(out var problem);
            FixedErrorText.Text = problem ?? "";
            FixedErrorText.Visibility = problem == null ? Visibility.Collapsed : Visibility.Visible;
            UpdateApplyFixed();
        }

        private void FixedBox_KeyDown(object sender, KeyEventArgs e)
        {
            if (e.Key != Key.Enter) return;
            e.Handled = true;
            if (ApplyFixedButton.IsEnabled) ApplyFixedButton_Click(ApplyFixedButton, new RoutedEventArgs());
        }

        private async void AdaptiveCheckBox_Click(object sender, RoutedEventArgs e)
        {
            if (_network == null) return;
            if (_networkRepair)
            {
                // repair mode: the click is the explicit choice; nothing is saved until "Save"
                UpdateApplyFixed();
                return;
            }
            if (_network.Fixed is not int storedFixed) return;
            // On: adaptive with the stored fixed value kept. Off: the fixed input appears with the stored value,
            // which is saved right away (PUT on change); a different number is then saved with "Apply".
            await SaveNetworkSafeAsync(AdaptiveCheckBox.IsChecked == true, storedFixed);
        }

        private async void ApplyFixedButton_Click(object sender, RoutedEventArgs e)
        {
            var n = ParseFixed(out var problem);
            if (n == null)
            {
                FixedErrorText.Text = problem ?? "";
                FixedErrorText.Visibility = Visibility.Visible;
                return;
            }
            if (_networkRepair)
            {
                if (AdaptiveCheckBox.IsChecked is not bool adaptive)
                {
                    FixedErrorText.Text = "Choose Adaptive on or off first.";
                    FixedErrorText.Visibility = Visibility.Visible;
                    return;
                }
                await SaveNetworkSafeAsync(adaptive, n.Value);
                return;
            }
            await SaveNetworkSafeAsync(false, n.Value);
        }

        private async void RefreshNetworkButton_Click(object sender, RoutedEventArgs e)
        {
            try
            {
                ShowNetworkMessage("", FundPalette.Dim);
                await RefreshNetworkAsync();
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Keys & data: network settings refresh failed.", ex);
                ShowNetworkMessage("Refreshing the network settings failed: " + ex.Message, FundPalette.Kill);
            }
        }

        private async Task SaveNetworkSafeAsync(bool adaptive, int fixedConcurrency)
        {
            try
            {
                await SaveNetworkAsync(adaptive, fixedConcurrency);
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Keys & data: saving the network settings failed.", ex);
                _networkSaving = false;
                ShowNetworkMessage("Saving the network settings failed: " + ex.Message, FundPalette.Kill);
                await RefreshNetworkAsync();
            }
        }

        private async Task SaveNetworkAsync(bool adaptive, int fixedConcurrency)
        {
            _networkSaving = true;
            AdaptiveCheckBox.IsEnabled = false;
            ApplyFixedButton.IsEnabled = false;
            FixedBox.IsEnabled = false;
            ShowNetworkMessage("Saving…", FundPalette.Dim);
            var result = await PlatformClient.PutNetworkSettingsAsync(adaptive, fixedConcurrency);
            _networkSaving = false;
            if (!result.Ok)
            {
                // the backend's `error` text plus its per-field `errors`, verbatim; the setting is unchanged
                var fields = result.Status == PlatformCallStatus.BadRequest ? FundClient.ParseErrors(result.RawBody ?? "") : Array.Empty<string>();
                var text = result.Message + (fields.Count > 0 && !(fields.Count == 1 && fields[0] == result.Message) ? "\n" + string.Join("\n", fields) : "");
                ShowNetworkMessage("Not saved — " + text, FundPalette.Kill);
                if (_network != null && _networkRepair)
                {
                    // repair mode: keep the user's choices so they can be corrected and saved again
                    AdaptiveCheckBox.IsEnabled = true;
                    UpdateApplyFixed();
                }
                else if (_network != null) ApplyNetwork(_network);   // back to the backend's unchanged state
                else await RefreshNetworkAsync();
                return;
            }
            ShowNetworkMessage(adaptive
                ? "Saved: adaptive concurrency (recorded in the audit log)."
                : "Saved: fixed " + fixedConcurrency.ToString(CultureInfo.InvariantCulture) + " parallel requests per source (recorded in the audit log).",
                FundPalette.Long);
            // show the backend's authoritative state (incl. the current concurrency) rather than the request
            await RefreshNetworkAsync();
        }

        private void ShowNetworkMessage(string text, Brush brush)
        {
            NetworkMessageText.Text = text;
            NetworkMessageText.Foreground = brush;
            NetworkMessageBorder.Visibility = string.IsNullOrWhiteSpace(text) ? Visibility.Collapsed : Visibility.Visible;
        }
    }
}
