using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// "Validate…" dialog (Strategy Platform stage 4, SPEC §9.3/§9.7): the only choice is how far back the
    /// data is loaded (history_from 2016 = default, 2000 = incl. the 2001/2008 crises, needs the Tiingo key).
    /// The run backtests the training range (ends before the locked holdout window), walk-forward folds,
    /// the parameter grid and the stress tests, then evaluates the gates G1–G8. Repeatable; every finished
    /// run counts as a trial of the strategy family. Chrome: FundPopupWindow.
    /// </summary>
    internal sealed class ValidateDialog : FundPopupWindow
    {
        private readonly RadioButton _from2016;
        private readonly RadioButton _from2000;
        private readonly CheckBox _autoResume;
        public string? HistoryFrom => _from2000.IsChecked == true ? "2000" : "2016";
        /// <summary>SPEC §10.3: auto_resume (default false).</summary>
        public bool AutoResume => _autoResume.IsChecked == true;

        public ValidateDialog(Window? owner, string strategyName, int? version, string? trainingText)
            : base(owner, "Validate", 520, 420, resizable: false)
        {
            SizeToContent = SizeToContent.Height;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Validate", 14, FundPalette.TextBright, bold: true));
            var msg = FundUi.Text(strategyName + (version is int v ? " · v" + v.ToString(CultureInfo.InvariantCulture) : "")
                                  + "\nRuns all gates G1–G8 on the training range: a backtest, 5 walk-forward folds, the parameter grid (up to 64 backtests) and the stress tests. "
                                  + "The holdout window is not touched. When every gate passes (or has a documented exception) the version becomes a candidate. "
                                  + "Every finished run counts as a trial of this strategy family and lowers the deflated Sharpe ratio.", 12, FundPalette.Dim);
            msg.Margin = new Thickness(0, 8, 0, 12);
            msg.LineHeight = 18;
            stack.Children.Add(msg);
            if (!string.IsNullOrWhiteSpace(trainingText))
            {
                var t = FundUi.Text(trainingText, 11, FundPalette.Text, mono: true);
                t.Margin = new Thickness(0, 0, 0, 10);
                stack.Children.Add(t);
            }

            var label = FundUi.Text("History from", 10.5, FundPalette.Faint);
            label.Margin = new Thickness(0, 0, 0, 4);
            stack.Children.Add(label);
            _from2016 = new RadioButton
            {
                GroupName = "ValidateHistory",
                Content = "2016 (default — Alpaca SIP history from " + RunBacktestDialog.SeamDate + ")",
                IsChecked = true,
                Foreground = FundPalette.Text,
                FontSize = 12,
                Margin = new Thickness(0, 2, 0, 2),
            };
            AutomationProperties.SetName(_from2016, "History from 2016, the default");
            _from2000 = new RadioButton
            {
                GroupName = "ValidateHistory",
                Content = "2000 (incl. the 2001/2008 crises — needs the Tiingo key)",
                Foreground = FundPalette.Text,
                FontSize = 12,
                Margin = new Thickness(0, 2, 0, 12),
                ToolTip = "Loads data from " + RunBacktestDialog.EarliestStart + ". Prices before " + RunBacktestDialog.SeamDate + " come from Tiingo (set the key in Hedge Fund → Keys & data).",
            };
            AutomationProperties.SetName(_from2000, "History from 2000, including the 2001 and 2008 crises; needs the Tiingo key");
            stack.Children.Add(_from2016);
            stack.Children.Add(_from2000);

            // SPEC §10.3/§10.4: on a connection error the run waits and resumes from its checkpoints, max 5 times.
            _autoResume = RunResume.MakeAutoResumeCheckBox();
            stack.Children.Add(_autoResume);

            var note = FundUi.Text("Takes longer than a backtest (many engine runs). Hypothetical, simulated results — they do not indicate future results.", 10.5, FundPalette.Faint);
            note.FontStyle = FontStyles.Italic;
            note.Margin = new Thickness(0, 0, 0, 12);
            stack.Children.Add(note);

            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            var cancel = FundUi.MakeButton("Cancel");
            cancel.IsCancel = true;
            cancel.Click += (_, _) => { DialogResult = false; };
            var ok = FundUi.MakeButton("Validate", FundPalette.Make(0x267C8CF8), FundPalette.Focus, FundPalette.TextBright);
            ok.IsDefault = true;
            ok.Margin = new Thickness(8, 0, 0, 0);
            ok.Click += (_, _) => { DialogResult = true; };
            buttons.Children.Add(cancel);
            buttons.Children.Add(ok);
            stack.Children.Add(buttons);
            SetBody(stack);
            Loaded += (_, _) => _from2016.Focus();
        }
    }

    /// <summary>Validate dialog → POST /v2/validations → validation report window (progress panel); 409 handling.</summary>
    internal static class ValidationLauncher
    {
        // dlgguard (2026-10-23): one at a time — queued clicks / a second shortcut return at once; the dialog itself is open-once
        public static Task<bool> RunAsync(Window? owner, string strategyId, string strategyName, int? version, string? trainingText) => DialogGuard.RunForResultAsync("validate:" + strategyId, () => RunCoreAsync(owner, strategyId, strategyName, version, trainingText), false);

        public static async Task<bool> RunCoreAsync(Window? owner, string strategyId, string strategyName, int? version, string? trainingText)
        {
            var dialog = new ValidateDialog(owner, strategyName, version, trainingText);
            if (dialog.ShowDialog() != true) return false;
            var result = await PlatformClient.StartValidationAsync(strategyId, version, dialog.HistoryFrom, dialog.AutoResume);
            if (result.Ok && !string.IsNullOrWhiteSpace(result.Value?.RunId))
            {
                ValidationReportWindow.Open(owner, strategyId, strategyName, result.Value!.RunId);
                return true;
            }
            if (result.Ok)
            {
                FundDialogs.Info(owner, "Validate", "Unexpected response from the backend (POST /v2/validations without run_id) — backend and app versions may not match.");
                return false;
            }
            // 409 = another backtest/validation/holdout job is running — the backend's text says so, verbatim.
            FundDialogs.Info(owner, "Validation not started", result.Message);
            return false;
        }
    }

    /// <summary>Reading helpers for GET /v2/strategies/{id}/holdout.</summary>
    internal static class HoldoutText
    {
        public static string? WindowStart(HoldoutStatus? h) => BtJson.Str(h?.Window, "start");
        public static string? WindowEnd(HoldoutStatus? h) => BtJson.Str(h?.Window, "end");

        /// <summary>Default end of normal backtests/validations = the session before the holdout window (SPEC §9.1).</summary>
        public static string? TrainingEnd(HoldoutStatus? h) => h?.TrainingEnd;

        /// <summary>Start of the full locked window (holdout_start; window.start is the unseen window when eligible after contamination).</summary>
        public static string? HoldoutStart(HoldoutStatus? h) => h?.HoldoutStart ?? WindowStart(h);

        /// <summary>Number of holdout events of this family (the route sends a count; a list is counted).</summary>
        public static int? FamilyEventCount(HoldoutStatus? h)
        {
            if (h?.FamilyEvents is not JsonElement e) return null;
            if (e.ValueKind == JsonValueKind.Number && e.TryGetInt32(out var n)) return n;
            if (e.ValueKind == JsonValueKind.Array) return e.GetArrayLength();
            return null;
        }

        /// <summary>Number of OTHER families that already opened an overlapping holdout window of this universe.</summary>
        public static int? UniverseCount(HoldoutStatus? h)
        {
            if (h?.UniverseEvents is not JsonElement e) return null;
            if (e.ValueKind == JsonValueKind.Number && e.TryGetInt32(out var n)) return n;
            if (e.ValueKind == JsonValueKind.Array) return e.GetArrayLength();
            if (e.ValueKind == JsonValueKind.Object && BtJson.Num(e, "count") is double c) return (int)c;
            return null;
        }

        public static List<string> FamilyEventLines(HoldoutStatus? h)
        {
            var lines = new List<string>();
            foreach (var ev in BtJson.Array(h?.FamilyEvents))
            {
                lines.Add(FundFormat.LocalTime(BtJson.Str(ev, "ts") ?? BtJson.Str(ev, "created_at"), "yyyy-MM-dd")
                          + " · " + (BtJson.Str(ev, "strategy_name") ?? BtJson.Str(ev, "strategy_id") ?? "?")
                          + (BtJson.Str(ev, "version") is string v ? " v" + v : "")
                          + " · window " + (BtJson.Str(ev, "window", "start") ?? BtJson.Str(ev, "window_start") ?? "?") + " … " + (BtJson.Str(ev, "window", "end") ?? BtJson.Str(ev, "window_end") ?? "?")
                          + " · " + (BtJson.Str(ev, "result") ?? "?"));
            }
            return lines;
        }

        public const string CanOpenMissing = "The backend did not report holdout eligibility (can_open missing) — backend and app versions may not match.";

        /// <summary>True only when the backend says can_open = true (eligible AND a passing validation of the current
        /// version). A missing can_open is NOT replaced by another field (no-fallback rule): the button stays disabled.</summary>
        public static bool CanOpen(HoldoutStatus h) => h.CanOpen == true;

        /// <summary>Tooltip / inline text of a disabled "Open holdout" button: the backend's reason and the eligibility date.</summary>
        public static string NotEligibleText(HoldoutStatus h)
        {
            if (h.CanOpen == null) return CanOpenMissing;
            var why = !string.IsNullOrWhiteSpace(h.BlockedReason) ? h.BlockedReason : h.Reason;
            var reason = string.IsNullOrWhiteSpace(why) ? "The holdout cannot be opened right now." : why!;
            return reason + (string.IsNullOrWhiteSpace(h.EligibleFrom) ? "" : "\nEligible from " + h.EligibleFrom + ".");
        }
    }

    /// <summary>
    /// "Open holdout (once)…" (SPEC §9.1/§9.5/§9.7): explains the once-per-family rule, shows the window,
    /// the family's holdout events and the universe counter, and requires typing the exact strategy name.
    /// The name is NOT compared here — the backend does that; its 400 text is shown inline, verbatim.
    /// </summary>
    internal sealed class OpenHoldoutDialog : FundPopupWindow
    {
        private readonly string _strategyId;
        private readonly TextBox _confirm;
        private readonly TextBlock _error;
        private readonly Button _ok;
        public string? RunId { get; private set; }

        public OpenHoldoutDialog(Window? owner, string strategyId, string strategyName, HoldoutStatus status)
            : base(owner, "Open holdout", 560, 520, resizable: false)
        {
            _strategyId = strategyId;
            SizeToContent = SizeToContent.Height;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Open holdout (once)", 14, FundPalette.TextBright, bold: true));
            var msg = FundUi.Text(
                "The holdout is the final test on recent data that no member of this strategy family has seen. "
                + "It can be opened ONCE per strategy family — this strategy and every copy, duplicate and version of it. "
                + "Whatever the result, the window counts as seen afterwards. A pass moves this version to validated; "
                + "a fail locks the family until at least 12 new unseen months exist. No exception is possible.", 12, FundPalette.Dim);
            msg.Margin = new Thickness(0, 8, 0, 12);
            msg.LineHeight = 18;
            stack.Children.Add(msg);

            var start = HoldoutText.WindowStart(status) ?? "?";
            var end = HoldoutText.WindowEnd(status) ?? "?";
            var fullStart = HoldoutText.HoldoutStart(status) ?? start;
            stack.Children.Add(FundUi.KeyValue("Holdout window", fullStart + " … " + end, FundPalette.TextBright, 11.5));
            if (status.Mode == "unseen" || start != fullStart)
                stack.Children.Add(FundUi.KeyValue("Tested (unseen months only)", start + " … " + end, FundPalette.Risk, 11.5));

            var universe = HoldoutText.UniverseCount(status);
            var uText = universe is int n
                ? (n == 0 ? "No other family has opened this window yet." : n.ToString(CultureInfo.InvariantCulture) + (n == 1 ? " other family has" : " other families have") + " already opened this window.")
                : "Universe counter not reported by the backend.";
            var u = FundUi.Text(uText, 11.5, universe is int m && m > 0 ? FundPalette.Risk : FundPalette.Text);
            u.Margin = new Thickness(0, 6, 0, 2);
            stack.Children.Add(u);
            if (universe is int k && k > 0)
            {
                var why = FundUi.Text("Each family that looks at the same window makes a pass there a little less convincing (multiple testing on one data set).", 10.5, FundPalette.Faint);
                why.Margin = new Thickness(0, 0, 0, 4);
                stack.Children.Add(why);
            }

            var famLabel = FundUi.Text("Holdout events of this family", 10.5, FundPalette.Faint, bold: true);
            famLabel.Margin = new Thickness(0, 8, 0, 2);
            stack.Children.Add(famLabel);
            var events = HoldoutText.FamilyEventLines(status);
            var eventCount = HoldoutText.FamilyEventCount(status);
            if (events.Count > 0)
                foreach (var line in events) stack.Children.Add(FundUi.Text(line, 11, FundPalette.Text, mono: true));
            else if (eventCount is int ec)
                stack.Children.Add(FundUi.Text(ec == 0 ? "None — this family has never opened a holdout."
                                                       : "This family has opened the holdout " + ec.ToString(CultureInfo.InvariantCulture) + (ec == 1 ? " time" : " times") + " before.",
                                               11, ec == 0 ? FundPalette.Dim : FundPalette.Risk));
            else stack.Children.Add(FundUi.Text("Not reported by the backend.", 11, FundPalette.Faint));
            if (!string.IsNullOrWhiteSpace(status.SeenUntil))
                stack.Children.Add(FundUi.KeyValue("Family has seen data up to", status.SeenUntil!, status.Contaminated == true ? FundPalette.Risk : FundPalette.Text, 11));
            if (!string.IsNullOrWhiteSpace(status.HoldoutFailedAt))
                stack.Children.Add(FundUi.KeyValue("Last failed holdout", FundFormat.LocalTime(status.HoldoutFailedAt, "yyyy-MM-dd"), FundPalette.Kill, 11));

            var cLabel = FundUi.Text("Type the exact strategy name to confirm: " + strategyName, 10.5, FundPalette.Faint);
            cLabel.Margin = new Thickness(0, 14, 0, 4);
            stack.Children.Add(cLabel);
            _confirm = FundUi.Input("", mono: false);
            _confirm.MaxLength = 300;
            AutomationProperties.SetName(_confirm, "Type the exact strategy name to confirm opening the holdout");
            stack.Children.Add(_confirm);

            _error = FundUi.Text("", 11, FundPalette.Kill);
            _error.Margin = new Thickness(0, 8, 0, 0);
            _error.Visibility = Visibility.Collapsed;
            AutomationProperties.SetLiveSetting(_error, AutomationLiveSetting.Assertive);
            stack.Children.Add(_error);

            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 14, 0, 0) };
            var cancel = FundUi.MakeButton("Cancel");
            cancel.IsCancel = true;
            cancel.Click += (_, _) => { DialogResult = false; };
            _ok = FundUi.MakeButton("Open holdout", FundPalette.KillSurface, FundPalette.Kill, FundPalette.Kill);
            _ok.Margin = new Thickness(8, 0, 0, 0);
            _ok.IsEnabled = false;
            AutomationProperties.SetName(_ok, "Open the holdout now — once per strategy family");
            _ok.Click += async (_, _) => await SubmitAsync();
            _confirm.TextChanged += (_, _) => _ok.IsEnabled = _confirm.Text.Length > 0;
            buttons.Children.Add(cancel);
            buttons.Children.Add(_ok);
            stack.Children.Add(buttons);
            SetBody(stack);
            Loaded += (_, _) => _confirm.Focus();
        }

        private async Task SubmitAsync()
        {
            if (TradingUi.IsBusy(_ok)) return;
            TradingUi.SetBusy(_ok, true, "Opening…");   // fix1023 B1 (the typed name + server check stay: evidence intervention)
            _error.Visibility = Visibility.Collapsed;
            var result = await PlatformClient.OpenHoldoutAsync(_strategyId, _confirm.Text);
            TradingUi.SetBusy(_ok, false);
            if (result.Ok && !string.IsNullOrWhiteSpace(result.Value?.RunId))
            {
                RunId = result.Value!.RunId;
                DialogResult = true;
                return;
            }
            _error.Text = result.Ok
                ? "Unexpected response from the backend (POST holdout without run_id) — backend and app versions may not match."
                : result.Message;   // verbatim: wrong name (400), busy / not eligible / no passing validation (409)
            _error.Visibility = Visibility.Visible;
            _ok.IsEnabled = _confirm.Text.Length > 0;
        }
    }

    internal static class HoldoutLauncher
    {
        /// <summary>Loads the eligibility, shows the dialog, starts the holdout run and opens the report window.</summary>
        // dlgguard (2026-10-23): one at a time — queued clicks / a second shortcut return at once; the dialog itself is open-once
        public static Task<bool> RunAsync(Window? owner, string strategyId, string strategyName) => DialogGuard.RunForResultAsync("holdout-open:" + strategyId, () => RunCoreAsync(owner, strategyId, strategyName), false);

        public static async Task<bool> RunCoreAsync(Window? owner, string strategyId, string strategyName)
        {
            var status = await PlatformClient.GetHoldoutAsync(strategyId);
            if (!status.Ok || status.Value == null)
            {
                FundDialogs.Info(owner, "Open holdout", status.Ok ? "Unexpected response from the backend (holdout status)." : status.Message);
                return false;
            }
            if (!HoldoutText.CanOpen(status.Value))
            {
                FundDialogs.Info(owner, "Open holdout", HoldoutText.NotEligibleText(status.Value));
                return false;
            }
            var dialog = new OpenHoldoutDialog(owner, strategyId, strategyName, status.Value);
            if (dialog.ShowDialog() != true || dialog.RunId == null) return false;
            ValidationReportWindow.Open(owner, strategyId, strategyName, dialog.RunId);
            return true;
        }
    }

    /// <summary>
    /// "Add exception…" for a failed gate G1–G8 (SPEC §9.4): a written reason of at least 30 characters,
    /// with the warning that exceptions are permanent and shown in every report.
    /// </summary>
    internal sealed class GateExceptionDialog : FundPopupWindow
    {
        public const int MaxReasonLength = 2000;   // validation/report.MAX_REASON_CHARS
        private readonly TextBox _reason;
        private readonly TextBlock _counter;
        private readonly Button _ok;
        public string Reason => _reason.Text.Trim();

        public GateExceptionDialog(Window? owner, string gateName, string value, string threshold, string? explanation)
            : base(owner, "Add exception", 520, 420, resizable: false)
        {
            SizeToContent = SizeToContent.Height;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Add exception — " + gateName, 14, FundPalette.TextBright, bold: true));
            var msg = FundUi.Text("This gate failed (value " + value + ", threshold " + threshold + "). An exception lets the strategy count it as passed. "
                                  + "Explain why the failure is acceptable.", 12, FundPalette.Dim);
            msg.Margin = new Thickness(0, 8, 0, 6);
            msg.LineHeight = 18;
            stack.Children.Add(msg);
            if (!string.IsNullOrWhiteSpace(explanation))
            {
                var e = FundUi.Text(explanation, 11, FundPalette.Text);
                e.Margin = new Thickness(0, 0, 0, 8);
                stack.Children.Add(e);
            }
            var warn = FundUi.Text(ValidationReportView.ExceptionWarning, 12, FundPalette.Risk, bold: true);
            warn.Margin = new Thickness(0, 4, 0, 10);
            stack.Children.Add(warn);

            var label = FundUi.Text("Reason (at least " + ValidationReportView.MinExceptionReasonLength + " characters)", 10.5, FundPalette.Faint);
            label.Margin = new Thickness(0, 0, 0, 4);
            stack.Children.Add(label);
            _reason = FundUi.Input("", mono: false);
            _reason.MaxLength = MaxReasonLength;
            _reason.TextWrapping = TextWrapping.Wrap;
            _reason.AcceptsReturn = false;
            _reason.MinHeight = 64;
            AutomationProperties.SetName(_reason, "Reason for the exception, at least " + ValidationReportView.MinExceptionReasonLength + " characters");
            stack.Children.Add(_reason);
            _counter = FundUi.Text("", 10.5, FundPalette.Faint);
            _counter.Margin = new Thickness(0, 4, 0, 0);
            stack.Children.Add(_counter);

            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 14, 0, 0) };
            var cancel = FundUi.MakeButton("Cancel");
            cancel.IsCancel = true;
            cancel.Click += (_, _) => { DialogResult = false; };
            _ok = FundUi.MakeButton("Add permanent exception", FundPalette.Make(0x26D9A54B), FundPalette.Risk, FundPalette.Risk);
            _ok.Margin = new Thickness(8, 0, 0, 0);
            _ok.IsEnabled = false;
            _ok.Click += (_, _) => { if (Reason.Length >= ValidationReportView.MinExceptionReasonLength) DialogResult = true; };
            _reason.TextChanged += (_, _) => UpdateCounter();
            buttons.Children.Add(cancel);
            buttons.Children.Add(_ok);
            stack.Children.Add(buttons);
            SetBody(stack);
            UpdateCounter();
            Loaded += (_, _) => _reason.Focus();
        }

        private void UpdateCounter()
        {
            var n = Reason.Length;
            var min = ValidationReportView.MinExceptionReasonLength;
            _counter.Text = n >= min ? n.ToString(CultureInfo.InvariantCulture) + " characters" : n.ToString(CultureInfo.InvariantCulture) + " / " + min + " characters — " + (min - n) + " more needed";
            _counter.Foreground = n >= min ? FundPalette.Faint : FundPalette.Risk;
            _ok.IsEnabled = n >= min;
        }
    }

    /// <summary>
    /// Library → "⚙ Validation gates" (SPEC §9.4 settings): preset Lenient / Standard / Strict and the
    /// read-only table of the thresholds (GET/PUT /v2/settings/gates, audited by the backend). Overrides
    /// set elsewhere are kept as they are and shown; they are not edited here.
    /// </summary>
    internal sealed class GateSettingsWindow : FundPopupWindow
    {
        private static readonly string[] Presets = { "lenient", "standard", "strict" };
        private readonly List<RadioButton> _presetButtons = new();
        private readonly StackPanel _table;
        private readonly TextBlock _status;
        private readonly Button _save;
        private GateSettings? _settings;
        private bool _loading;

        public GateSettingsWindow(Window? owner)
            : base(owner, "Validation gates", 620, 600, resizable: true)
        {
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Validation gates", 14, FundPalette.TextBright, bold: true));
            var msg = FundUi.Text("Thresholds of the gates G1–G8 and H used by new validation runs. Standard is the default; Lenient lets more strategies through, Strict fewer. "
                                  + "Finished reports keep the thresholds they were evaluated with. Changes are written to the audit log.", 12, FundPalette.Dim);
            msg.Margin = new Thickness(0, 8, 0, 12);
            msg.LineHeight = 18;
            stack.Children.Add(msg);

            var presetRow = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 10) };
            foreach (var p in Presets)
            {
                var rb = new RadioButton
                {
                    GroupName = "GatePreset",
                    Content = Cap(p),
                    Tag = p,
                    Foreground = FundPalette.Text,
                    FontSize = 12,
                    Margin = new Thickness(0, 0, 18, 0),
                    IsEnabled = false,
                };
                AutomationProperties.SetName(rb, "Gate preset " + Cap(p));
                rb.Checked += (_, _) => { if (!_loading) RenderTable(); UpdateSave(); };
                _presetButtons.Add(rb);
                presetRow.Children.Add(rb);
            }
            stack.Children.Add(presetRow);

            _status = FundUi.Text("Loading…", 11, FundPalette.Faint);
            _status.Margin = new Thickness(0, 0, 0, 8);
            stack.Children.Add(_status);
            _table = new StackPanel();
            stack.Children.Add(_table);

            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 14, 0, 0) };
            var close = FundUi.MakeButton("Close");
            close.IsCancel = true;
            close.Click += (_, _) => Close();
            _save = FundUi.MakeButton("Save preset", FundPalette.Make(0x267C8CF8), FundPalette.Focus, FundPalette.TextBright);
            _save.Margin = new Thickness(8, 0, 0, 0);
            _save.IsEnabled = false;
            _save.Click += async (_, _) => await SaveAsync();
            buttons.Children.Add(close);
            buttons.Children.Add(_save);
            stack.Children.Add(buttons);
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false, Content = stack });
            Loaded += async (_, _) => await LoadAsync();
        }

        private static string Cap(string s) => s.Length == 0 ? s : char.ToUpperInvariant(s[0]) + s[1..];

        private string? SelectedPreset => _presetButtons.FirstOrDefault(b => b.IsChecked == true)?.Tag as string;

        private async Task LoadAsync()
        {
            _status.Text = "Loading…";
            _status.Foreground = FundPalette.Faint;
            var res = await PlatformClient.GetGateSettingsAsync();
            if (!res.Ok || res.Value == null)
            {
                _status.Text = res.Ok ? "Unexpected response from the backend (/v2/settings/gates)." : res.Message;
                _status.Foreground = FundPalette.Kill;
                return;
            }
            _settings = res.Value;
            _loading = true;
            var current = (_settings.Preset ?? "").ToLowerInvariant();
            foreach (var b in _presetButtons)
            {
                b.IsEnabled = true;
                b.IsChecked = (b.Tag as string) == current;
            }
            _loading = false;
            _status.Text = "Current preset: " + (string.IsNullOrWhiteSpace(_settings.Preset) ? "—" : Cap(_settings.Preset!)) + OverrideNote();
            _status.Foreground = FundPalette.Dim;
            RenderTable();
            UpdateSave();
        }

        private string OverrideNote()
        {
            var n = BtJson.Props(_settings?.Overrides).Count();
            return n == 0 ? " · no overrides" : " · " + n.ToString(CultureInfo.InvariantCulture) + (n == 1 ? " override" : " overrides") + " (kept when the preset changes)";
        }

        private void UpdateSave()
        {
            var sel = SelectedPreset;
            _save.IsEnabled = _settings != null && sel != null && !string.Equals(sel, _settings.Preset, StringComparison.OrdinalIgnoreCase);
        }

        /// <summary>Threshold table: one column per preset when the backend lists them, the effective values otherwise.</summary>
        private void RenderTable()
        {
            _table.Children.Clear();
            if (_settings == null) return;
            var sel = SelectedPreset;
            var presetBlocks = Presets.Select(p => (p, BtJson.Get(_settings.Presets, p) ?? FindPreset(p))).Where(x => x.Item2 != null).ToList();
            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var columns = presetBlocks.Count > 0 ? presetBlocks.Select(x => x.p).ToList() : new List<string> { "effective" };
            foreach (var _ in columns) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(90) });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var h0 = FundUi.Text("Threshold", 10.5, FundPalette.Faint, bold: true);
            h0.Margin = new Thickness(0, 0, 8, 4);
            grid.Children.Add(h0);
            for (var c = 0; c < columns.Count; c++)
            {
                var h = FundUi.Text(Cap(columns[c]), 10.5, columns[c] == sel ? FundPalette.Focus : FundPalette.Faint, bold: true, wrap: false);
                h.Margin = new Thickness(0, 0, 8, 4);
                Grid.SetColumn(h, c + 1);
                grid.Children.Add(h);
            }
            var keys = new List<string>();
            foreach (var (_, block) in presetBlocks) foreach (var p in BtJson.Props(block)) if (!keys.Contains(p.Name)) keys.Add(p.Name);
            foreach (var p in BtJson.Props(_settings.Effective)) if (!keys.Contains(p.Name)) keys.Add(p.Name);
            var overrides = BtJson.Props(_settings.Overrides).ToDictionary(p => p.Name, p => p.Value);
            var row = 1;
            foreach (var key in keys)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var name = FundUi.Text(key + (overrides.ContainsKey(key) ? "  (override " + ValidationReportView.ValueText(overrides[key]) + ")" : ""), 11,
                                       overrides.ContainsKey(key) ? FundPalette.Risk : FundPalette.Text, mono: true);
                name.Margin = new Thickness(0, 1, 8, 1);
                Grid.SetRow(name, row);
                grid.Children.Add(name);
                for (var c = 0; c < columns.Count; c++)
                {
                    var block = presetBlocks.Count > 0 ? presetBlocks[c].Item2 : _settings.Effective;
                    var v = ValidationReportView.ValueText(BtJson.Get(block, key));
                    var cell = FundUi.Text(v, 11, columns[c] == sel ? FundPalette.TextBright : FundPalette.Dim, mono: true, wrap: false);
                    cell.Margin = new Thickness(0, 1, 8, 1);
                    Grid.SetRow(cell, row);
                    Grid.SetColumn(cell, c + 1);
                    grid.Children.Add(cell);
                    AutomationProperties.SetName(cell, key + ", " + columns[c] + ": " + v);
                }
                row++;
            }
            if (keys.Count == 0)
            {
                _table.Children.Add(FundUi.Text("The backend lists no thresholds.", 11, FundPalette.Faint));
                return;
            }
            var panel = FundUi.Panel(grid, new Thickness(12, 8, 12, 8), new Thickness(0, 2, 0, 4));
            AutomationProperties.SetName(panel, "Gate thresholds per preset (read-only)");
            _table.Children.Add(panel);
            _table.Children.Add(FundUi.Text("Read-only. The selected preset is highlighted; overrides (amber) replace the preset value.", 10.5, FundPalette.Faint));
        }

        private JsonElement? FindPreset(string name)
        {
            // presets may be keyed with a capital letter ("Standard")
            foreach (var p in BtJson.Props(_settings?.Presets))
                if (string.Equals(p.Name, name, StringComparison.OrdinalIgnoreCase)) return p.Value;
            return null;
        }

        private async Task SaveAsync()
        {
            var sel = SelectedPreset;
            if (sel == null || _settings == null) return;
            _save.IsEnabled = false;
            var overrides = new Dictionary<string, double>();
            foreach (var p in BtJson.Props(_settings.Overrides))
                if (p.Value.ValueKind == JsonValueKind.Number) overrides[p.Name] = p.Value.GetDouble();
            var res = await PlatformClient.PutGateSettingsAsync(sel, overrides);
            if (!res.Ok)
            {
                // the backend's `error` text plus its per-field `errors` ({field: message}), verbatim
                var fields = res.Status == PlatformCallStatus.BadRequest ? FundClient.ParseErrors(res.RawBody ?? "") : Array.Empty<string>();
                _status.Text = res.Message + (fields.Count > 0 && !(fields.Count == 1 && fields[0] == res.Message) ? "\n" + string.Join("\n", fields) : "");
                _status.Foreground = FundPalette.Kill;
                UpdateSave();
                return;
            }
            await LoadAsync();
        }
    }
}
