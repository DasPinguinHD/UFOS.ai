using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Dialogs of "Portfolio & Trading" (Strategy Platform stage 7 — SPEC §17, WPF notes §17.13). Chrome: FundPopupWindow
    // (borderless, full-screen safe, Esc closes). Nothing is sent before the user confirms; every request goes through
    // PlatformClient; the backend's answer (incl. 400 field errors and 409 reasons) is shown inline, verbatim. Strategy
    // names, reasons and reject texts are plain text. PAPER TRADING ONLY.
    // =====================================================================================

    internal static class TradingUi
    {
        public static TextBlock Head(string text)
        {
            var t = FundUi.Text(text.ToUpperInvariant(), 10.5, FundPalette.Faint, bold: true);
            t.Margin = new Thickness(0, 10, 0, 4);
            return t;
        }

        public static TextBlock Label(string text)
        {
            var t = FundUi.Text(text, 10.5, FundPalette.Faint);
            t.Margin = new Thickness(0, 2, 0, 3);
            return t;
        }

        public static TextBlock Note(string text, Brush brush, bool mono = false)
        {
            var t = FundUi.Text(text, 10.5, brush, mono: mono);
            t.Margin = new Thickness(0, 2, 0, 3);
            return t;
        }

        public static TextBlock ErrorText()
        {
            var t = FundUi.Text("", 11, FundPalette.Kill);
            t.Margin = new Thickness(0, 8, 0, 0);
            t.Visibility = Visibility.Collapsed;
            AutomationProperties.SetLiveSetting(t, AutomationLiveSetting.Assertive);
            return t;
        }

        public static void ShowError(TextBlock target, string? text)
        {
            target.Text = text ?? "";
            target.Visibility = string.IsNullOrWhiteSpace(text) ? Visibility.Collapsed : Visibility.Visible;
            AutomationProperties.SetName(target, text ?? "");
        }

        public static Border Callout(string text, Brush brush, Brush surface)
        {
            var t = FundUi.Text(text, 11.5, brush);
            t.LineHeight = 17;
            var b = new Border
            {
                Background = surface,
                BorderBrush = brush,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 7, 10, 8),
                Margin = new Thickness(0, 4, 0, 8),
                Child = t,
            };
            AutomationProperties.SetName(b, text);
            return b;
        }

        public static StackPanel Buttons(out Button cancel, Button ok)
        {
            var row = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 14, 0, 0) };
            cancel = FundUi.MakeButton("Cancel");
            cancel.IsCancel = true;
            AutomationProperties.SetName(cancel, "Cancel");
            ok.Margin = new Thickness(8, 0, 0, 0);
            row.Children.Add(cancel);
            row.Children.Add(ok);
            return row;
        }

        public static Button Primary(string text) => FundUi.MakeButton(text, FundPalette.Make(0x267C8CF8), FundPalette.Focus, FundPalette.TextBright);
        public static Button Danger(string text) => FundUi.MakeButton(text, FundPalette.KillSurface, FundPalette.Kill, FundPalette.Kill);

        public static bool ValidReason(string? text)
        {
            var n = (text ?? "").Trim().Length;
            return n >= PlatformClient.TradingReasonMin && n <= PlatformClient.TradingReasonMax;
        }

        // ---- fix1023 (decision 2026-10-23, user + lead): one confirmation step, no "I understand" ticks, no typed confirm words,
        // audit notes OPTIONAL. Exceptions keep their own checks: switching LIVE trading on, and evidence interventions (holdout,
        // gate exceptions) with their mandatory reason. ----

        /// <summary>Label of the optional audit note.</summary>
        public const string NoteLabel = "Note (optional, stored in the audit log)";

        /// <summary>An optional audit note: empty, or at most <see cref="PlatformClient.TradingReasonMax"/> characters.</summary>
        public static bool ValidNote(string? text) => (text ?? "").Trim().Length <= PlatformClient.TradingReasonMax;

        public static TextBox NoteBox(string automationName)
        {
            var box = ReasonBox(automationName);
            AutomationProperties.SetHelpText(box, "Optional, up to 500 characters, stored in the audit log");
            return box;
        }

        private static readonly ConditionalWeakTable<Button, object> BusyContent = new();

        /// <summary>
        /// fix1023 B1: while a dialog's request is in flight its action button is disabled and says what is happening
        /// (e.g. "Starting…"), so a second click cannot submit twice. <paramref name="busy"/> false restores the original text;
        /// the caller then recomputes IsEnabled as usual.
        /// </summary>
        public static void SetBusy(Button button, bool busy, string busyText = "Working…")
        {
            if (busy)
            {
                if (!BusyContent.TryGetValue(button, out _)) BusyContent.Add(button, button.Content ?? "");
                button.Content = busyText;
                button.IsEnabled = false;
                AutomationProperties.SetItemStatus(button, busyText);
            }
            else if (BusyContent.TryGetValue(button, out var original))
            {
                BusyContent.Remove(button);
                button.Content = original;
                AutomationProperties.SetItemStatus(button, "");
            }
        }

        public static bool IsBusy(Button button) => BusyContent.TryGetValue(button, out _);

        public static TextBox ReasonBox(string automationName)
        {
            var box = FundUi.Input("", mono: false);
            box.MaxLength = PlatformClient.TradingReasonMax;
            box.TextWrapping = TextWrapping.Wrap;
            box.AcceptsReturn = false;
            box.MinHeight = 44;
            AutomationProperties.SetName(box, automationName);
            AutomationProperties.SetHelpText(box, "3 to 500 characters, stored in the audit log");
            return box;
        }

        public static RadioButton Radio(string group, string content, string name, bool isChecked)
        {
            var rb = new RadioButton
            {
                GroupName = group,
                Content = content,
                IsChecked = isChecked,
                Foreground = FundPalette.Text,
                FontSize = 12,
                Margin = new Thickness(0, 0, 18, 4),
                VerticalAlignment = VerticalAlignment.Center,
            };
            AutomationProperties.SetName(rb, name);
            return rb;
        }

        public static ListBox List(string name, double height)
        {
            var list = new ListBox
            {
                Background = FundPalette.Panel,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                Foreground = FundPalette.Text,
                Height = height,
                HorizontalContentAlignment = HorizontalAlignment.Stretch,
            };
            ScrollViewer.SetHorizontalScrollBarVisibility(list, ScrollBarVisibility.Disabled);
            AutomationProperties.SetName(list, name);
            return list;
        }

        public static string Inv(double v, string format = "0.##") => v.ToString(format, CultureInfo.InvariantCulture);

        public static bool TryParseNumber(string? text, out double value)
            => double.TryParse((text ?? "").Trim().Replace(",", ""), NumberStyles.Float, CultureInfo.InvariantCulture, out value)
               && !double.IsNaN(value) && !double.IsInfinity(value);
    }

    /// <summary>
    /// One confirmation step for an action, with an OPTIONAL audit note (fix1023, 2026-10-23 — was a mandatory 3..500-character
    /// reason). Returns null when cancelled, else the trimmed note ("" = none).
    /// </summary>
    internal sealed class TradingReasonDialog : FundPopupWindow
    {
        private readonly TextBox _reason;
        public string Reason => _reason.Text.Trim();

        public TradingReasonDialog(Window? owner, string title, string message, string confirmText, bool danger, string? initial = null)
            : base(owner, title, 500, 300, resizable: false)
        {
            SizeToContent = SizeToContent.Height;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text(title, 14, FundPalette.TextBright, bold: true));
            var msg = FundUi.Text(message, 12, FundPalette.Dim);
            msg.Margin = new Thickness(0, 8, 0, 10);
            msg.LineHeight = 18;
            stack.Children.Add(msg);
            stack.Children.Add(TradingUi.Label(TradingUi.NoteLabel));
            _reason = TradingUi.NoteBox("Optional note for: " + title);
            _reason.Text = initial ?? "";
            stack.Children.Add(_reason);
            var hint = TradingUi.Note("", FundPalette.Faint);
            stack.Children.Add(hint);
            var ok = danger ? TradingUi.Danger(confirmText) : TradingUi.Primary(confirmText);
            ok.IsDefault = !danger;
            AutomationProperties.SetName(ok, confirmText);
            stack.Children.Add(TradingUi.Buttons(out var cancel, ok));
            cancel.Click += (_, _) => { DialogResult = false; };
            ok.Click += (_, _) => { if (TradingUi.ValidNote(_reason.Text)) DialogResult = true; };
            void Update()
            {
                var n = _reason.Text.Trim().Length;
                ok.IsEnabled = TradingUi.ValidNote(_reason.Text);
                hint.Text = n == 0 ? "" : n.ToString(CultureInfo.InvariantCulture) + " / " + PlatformClient.TradingReasonMax.ToString(CultureInfo.InvariantCulture);
            }
            _reason.TextChanged += (_, _) => Update();
            Update();
            SetBody(stack);
            Loaded += (_, _) => _reason.Focus();
        }

        /// <summary>Shows the dialog; null = cancelled, else the trimmed note ("" = none).</summary>
        public static string? Ask(Window? owner, string title, string message, string confirmText, bool danger = false, string? initial = null)
        {
            try
            {
                var d = new TradingReasonDialog(owner, title, message, confirmText, danger, initial);
                return d.ShowDialog() == true ? d.Reason : null;
            }
            catch (Exception)
            {
                // the styled dialog failed (resources / owner): the plain form still asks (FundDialogs logs and falls back)
                var values = FundDialogs.Form(owner, title, message, new[] { ("Note (optional)", initial ?? "") }, confirmText, danger);
                if (values == null) return null;
                var note = values.Length == 0 ? "" : values[0].Trim();
                return TradingUi.ValidNote(note) ? note : null;
            }
        }
    }

    /// <summary>
    /// "Start incubation…" (SPEC §17.2): choose a validated strategy, see the backend's eligibility (reason verbatim), the
    /// calendar and the first session; choose the length (20..250 sessions, default from the backend); the dialog explains
    /// that the length becomes IMMUTABLE and the button says so ("Start 60-session incubation (length fixed)"); the note is
    /// optional (fix1023, 2026-10-23 — no "I understand" tick any more). The backend
    /// stores the length with the run (insert-only, a trigger refuses any change) — no route can change it later.
    /// </summary>
    internal sealed class IncubationStartDialog : FundPopupWindow
    {
        private readonly ListBox _strategies;
        private readonly TextBlock _eligibility;
        private readonly TextBox _length;
        private readonly TextBlock _lengthHint;
        private readonly TextBox _reason;
        private readonly TextBlock _error;
        private readonly Button _ok;
        private readonly int _min;
        private readonly int _max;
        private IncubationEligibility? _eligible;
        private string? _strategyId;
        private int _eligibilitySeq;
        private bool _busy;
        private bool _closed;
        private bool _startCheckFailed;

        public IncubationRun? Run { get; private set; }
        /// <summary>fix1023 round 3: the start response's allocation block (null when the run was confirmed after a time-out).</summary>
        public IncubationAllocation? Allocation { get; private set; }
        /// <summary>The backend answered 409 already_incubating — the dialog closed; the caller says so and refreshes.</summary>
        public bool AlreadyRunning { get; private set; }

        public IncubationStartDialog(Window? owner, IReadOnlyList<StrategySummary> validated, IncubationDefaults? defaults, string? preselectId)
            : base(owner, "Start incubation", 620, 720, resizable: true)
        {
            Closed += (_, _) => _closed = true;
            _min = defaults?.Min ?? 20;
            _max = defaults?.Max ?? 250;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Start an incubation run", 14, FundPalette.TextBright, bold: true));
            var intro = FundUi.Text(
                "Incubation trades a VALIDATED strategy on its routed account — by default the platform paper account paper-main (internal "
                + "paper broker); a strategy routed to alpaca-paper / binance-demo in Settings → Brokers → Routing starts there — with the same "
                + "code, costs and risk checks as the backtest, and compares the result with the band the holdout evidence expects. Only you can "
                + "start it. Paper / demo trading only — simulated orders, no real money.", 12, FundPalette.Dim);
            intro.Margin = new Thickness(0, 8, 0, 8);
            intro.LineHeight = 18;
            stack.Children.Add(intro);

            stack.Children.Add(TradingUi.Head("Strategy (validated only)"));
            _strategies = TradingUi.List("Validated strategies — choose one to incubate", 120);
            foreach (var s in validated)
            {
                var itemContent = new StackPanel();
                itemContent.Children.Add(FundUi.Text((s.Name ?? s.Id) + " · v" + (s.CurrentVersion?.ToString(CultureInfo.InvariantCulture) ?? "?") + " · " + s.Id, 11.5, FundPalette.Text, wrap: false));
                if (FundUi.FixtureLine(s.Fixture, s.FixtureNote, 10.5, new Thickness(0, 1, 0, 0)) is TextBlock fixture) itemContent.Children.Add(fixture);   // sandbox fixture (SPEC "Sandbox profile" S4)
                var item = new ListBoxItem
                {
                    Content = itemContent,
                    Tag = s.Id,
                    Padding = new Thickness(6, 3, 6, 3),
                };
                AutomationProperties.SetName(item, (s.Name ?? s.Id) + ", version " + (s.CurrentVersion?.ToString(CultureInfo.InvariantCulture) ?? "unknown"));
                _strategies.Items.Add(item);
                if (s.Id == preselectId) _strategies.SelectedItem = item;
            }
            stack.Children.Add(_strategies);
            if (validated.Count == 0)
                stack.Children.Add(TradingUi.Note("No validated strategy — a strategy becomes validated by passing its holdout (Library → Validation).", FundPalette.Risk));
            _eligibility = TradingUi.Note("Choose a strategy to check its eligibility.", FundPalette.Faint);
            AutomationProperties.SetLiveSetting(_eligibility, AutomationLiveSetting.Polite);
            stack.Children.Add(_eligibility);

            stack.Children.Add(TradingUi.Head("Length"));
            var lenRow = new StackPanel { Orientation = Orientation.Horizontal };
            _length = FundUi.Input((defaults?.LengthSessions ?? 60).ToString(CultureInfo.InvariantCulture));
            _length.Width = 90;
            _length.MaxLength = 3;
            AutomationProperties.SetName(_length, "Incubation length in sessions, " + _min.ToString(CultureInfo.InvariantCulture) + " to " + _max.ToString(CultureInfo.InvariantCulture));
            lenRow.Children.Add(_length);
            var unit = FundUi.Text("sessions (" + _min.ToString(CultureInfo.InvariantCulture) + "–" + _max.ToString(CultureInfo.InvariantCulture)
                                   + "; default " + (defaults?.LengthSessions ?? 60).ToString(CultureInfo.InvariantCulture) + ")", 11.5, FundPalette.Dim);
            unit.VerticalAlignment = VerticalAlignment.Center;
            unit.Margin = new Thickness(10, 0, 0, 0);
            lenRow.Children.Add(unit);
            stack.Children.Add(lenRow);
            _lengthHint = TradingUi.Note("", FundPalette.Faint);
            stack.Children.Add(_lengthHint);
            stack.Children.Add(TradingUi.Callout(
                "The length is fixed at the start and can NEVER be changed afterwards — not by you and not by the engine (it is stored "
                + "with the run; the database refuses any change). Sessions are NYSE sessions for equity/mixed universes and UTC days "
                + "for crypto-only strategies. If you want a different length later, abort the run and start a new one — a new run "
                + "chooses its length again and starts from zero.", FundPalette.Risk, FundPalette.Make(0x1FD9A54B)));

            stack.Children.Add(TradingUi.Head("Note (optional)"));
            _reason = TradingUi.NoteBox("Optional note for starting the incubation run");
            stack.Children.Add(_reason);

            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);

            _ok = TradingUi.Primary("Start incubation");
            _ok.IsEnabled = false;
            AutomationProperties.SetName(_ok, "Start the incubation run with the chosen fixed length");
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { DialogResult = false; };
            _ok.Click += async (_, _) => await SubmitAsync();

            _strategies.SelectionChanged += async (_, _) => await LoadEligibilityAsync();
            _length.TextChanged += (_, _) => Update();
            _reason.TextChanged += (_, _) => Update();

            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
            Loaded += async (_, _) =>
            {
                if (_strategies.SelectedItem != null) { await LoadEligibilityAsync(); _length.Focus(); }
                else if (_strategies.Items.Count > 0) _strategies.Focus();
            };
            Update();
        }

        private int? Length()
        {
            if (!int.TryParse(_length.Text.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var n)) return null;
            return n >= _min && n <= _max ? n : null;
        }

        private async Task LoadEligibilityAsync()
        {
            _eligible = null;
            _strategyId = (_strategies.SelectedItem as ListBoxItem)?.Tag as string;
            Update();
            if (_strategyId == null) return;
            var seq = ++_eligibilitySeq;
            _eligibility.Text = "Checking eligibility…";
            _eligibility.Foreground = FundPalette.Faint;
            var res = await PlatformClient.GetIncubationEligibilityAsync(_strategyId);
            if (seq != _eligibilitySeq) return;
            if (!res.Ok || res.Value == null)
            {
                _eligibility.Text = "Eligibility not available: " + (res.Ok ? "unexpected response from the backend." : PlatformClient.DescribeFailure(res));
                _eligibility.Foreground = FundPalette.Kill;
                Update();
                return;
            }
            _eligible = res.Value;
            var cal = _eligible.Calendar == "utc" ? "UTC days (crypto-only)" : _eligible.Calendar == "nyse" ? "NYSE sessions" : (_eligible.Calendar ?? "—");
            if (_eligible.Eligible)
            {
                _eligibility.Text = "Eligible · calendar: " + cal + " · first session if started now: " + (_eligible.StartDateIfNow ?? "—")
                                    + " · account " + (_eligible.AccountId ?? PlatformClient.MainAccountId);
                _eligibility.Foreground = FundPalette.Long;
            }
            else
            {
                _eligibility.Text = "Not eligible: " + (_eligible.Reason ?? "no reason given");   // backend text, verbatim
                _eligibility.Foreground = FundPalette.Risk;
            }
            AutomationProperties.SetName(_eligibility, _eligibility.Text);
            Update();
        }

        private void Update()
        {
            var n = Length();
            var nText = n?.ToString(CultureInfo.InvariantCulture) ?? "?";
            _lengthHint.Text = n == null
                ? "Enter a whole number from " + _min.ToString(CultureInfo.InvariantCulture) + " to " + _max.ToString(CultureInfo.InvariantCulture) + "."
                : "The run will last " + nText + " sessions" + (_eligible?.StartDateIfNow is string sd ? ", starting with the session of " + sd : "")
                  + "; the backend computes the end date from the strategy's calendar.";
            _lengthHint.Foreground = n == null ? FundPalette.Kill : FundPalette.Faint;
            if (!_busy)
            {
                _ok.Content = n == null ? "Start incubation" : "Start " + nText + "-session incubation (length fixed)";
                AutomationProperties.SetName(_ok, n == null ? "Start incubation" : "Start the incubation run with the fixed length of " + nText + " sessions");
            }
            _ok.IsEnabled = !_busy && _strategyId != null && _eligible?.Eligible == true && n != null && TradingUi.ValidNote(_reason.Text);
        }

        private async Task SubmitAsync()
        {
            var n = Length();
            if (_busy || _strategyId == null || n == null || !TradingUi.ValidNote(_reason.Text)) return;
            _busy = true;
            TradingUi.SetBusy(_ok, true, "Starting…");
            Update();
            TradingUi.ShowError(_error, null);
            var strategyId = _strategyId;
            var sentAt = DateTimeOffset.UtcNow;
            var res = await PlatformClient.StartIncubationAsync(strategyId, n.Value, _reason.Text.Trim());
            if (_closed) return;
            if (res.Ok && res.Value?.Run != null)
            {
                Run = res.Value.Run;
                Allocation = res.Value.Allocation;
                DialogResult = true;
                return;
            }
            // click-test 2026-10-23: a 409 already_incubating is not an error of this dialog — close it; the caller says so + refreshes
            if (!res.Ok && PlatformClient.ErrorCode(res) == "already_incubating")
            {
                AlreadyRunning = true;
                DialogResult = true;
                return;
            }
            // no answer / connection lost: the run may have been created — confirm via the incubation list before offering a retry
            if (res.TimedOut || res.Status == PlatformCallStatus.Offline)
            {
                var found = await FindStartedRunAsync(strategyId, sentAt);
                if (_closed) return;
                if (found != null)
                {
                    Run = found;
                    DialogResult = true;
                    return;
                }
                _busy = false;
                TradingUi.SetBusy(_ok, false);
                // confirmed "not started" → clear; the check itself failed → say so (a retry while a run is active is refused with 409)
                TradingUi.ShowError(_error, _startCheckFailed
                    ? "No answer in time, and the start could not be confirmed (the backend did not answer the check). Look at Portfolio & Trading → "
                      + "Incubation before trying again — a second start while a run is active is refused."
                    : null);
                Update();
                return;
            }
            _busy = false;
            TradingUi.SetBusy(_ok, false);
            TradingUi.ShowError(_error, res.Ok ? "Unexpected response from the backend (POST /v2/incubation without run)." : PlatformClient.DescribeFailure(res));
            Update();
        }

        /// <summary>
        /// After a time-out / lost connection (fix1023 round 3): GET /v2/incubation?strategy_id=&amp;started_after=&lt;T&gt; — T taken just
        /// before sending; the backend returns only runs created at or after T (newest first), so a non-empty list means the start went
        /// through. Up to 3 tries 3 s apart (a failed READ is retried; an empty answer is final). Null = not started.
        /// </summary>
        private async Task<IncubationRun?> FindStartedRunAsync(string strategyId, DateTimeOffset sentAt)
        {
            const string head = "No answer in time — the incubation may have been started — checking…";
            _startCheckFailed = false;
            for (var i = 1; i <= 3 && !_closed; i++)
            {
                TradingUi.ShowError(_error, head + " (" + i.ToString(CultureInfo.InvariantCulture) + "/3)");
                _error.Foreground = FundPalette.Faint;
                var list = await PlatformClient.ListIncubationAsync(strategyId, sentAt);
                if (_closed) return null;
                if (list.Ok && list.Value != null)
                {
                    _error.Foreground = FundPalette.Kill;
                    return (list.Value.Runs ?? new List<IncubationRun>()).FirstOrDefault();
                }
                await Task.Delay(3000);
            }
            _error.Foreground = FundPalette.Kill;
            _startCheckFailed = true;
            return null;
        }
    }

    /// <summary>
    /// Risk-limit / allocator settings change (SPEC §17.6): lists every change (current → new, default, bounds), warns when a
    /// post-trade limit is loosened (warning text only — no extra tick since fix1023), takes an optional audit note and saves with
    /// PUT /v2/portfolio/settings. Field errors
    /// (out of bounds, unknown key) come back verbatim and nothing is clamped.
    /// </summary>
    internal sealed class TradingSettingsDialog : FundPopupWindow
    {
        private readonly string _accountId;
        private readonly Dictionary<string, double> _changes;
        private readonly TextBox _reason;
        private readonly TextBlock _error;
        private readonly Button _ok;
        private readonly int _loosened;   // number of loosened limits (warnings shown above the button; the button text says it)

        public TradingSettings? Saved { get; private set; }

        public TradingSettingsDialog(Window? owner, string accountId, IReadOnlyList<(string Key, string Label, string Old, string New, string Default)> rows,
                                     Dictionary<string, double> changes, IReadOnlyList<string> warnings)
            : base(owner, "Change risk limits", 600, 520, resizable: true)
        {
            _accountId = accountId;
            _changes = changes;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Change risk limits · " + TradingText.AccountLabel(accountId), 14, FundPalette.TextBright, bold: true));
            var intro = FundUi.Text("These limits apply to every new order of this platform paper account from the next check on. The change is stored "
                                    + "as a new settings version (with your note, if any, in the audit log); older versions stay in the history.", 12, FundPalette.Dim);
            intro.Margin = new Thickness(0, 8, 0, 8);
            intro.LineHeight = 18;
            stack.Children.Add(intro);
            stack.Children.Add(TradingUi.Head("Changes"));
            foreach (var r in rows)
            {
                var line = FundUi.KeyValue(r.Label, r.Old + "  →  " + r.New + "   (default " + r.Default + ")", FundPalette.TextBright, 11.5);
                AutomationProperties.SetName(line, r.Label + ": from " + r.Old + " to " + r.New + ", default " + r.Default);
                stack.Children.Add(line);
            }
            foreach (var w in warnings)
                stack.Children.Add(TradingUi.Callout(w, FundPalette.Risk, FundPalette.Make(0x1FD9A54B)));
            _loosened = warnings.Count;
            stack.Children.Add(TradingUi.Head(TradingUi.NoteLabel));
            _reason = TradingUi.NoteBox("Optional note for the risk-limit change (audit log)");
            stack.Children.Add(_reason);
            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);
            var saveText = "Save " + rows.Count.ToString(CultureInfo.InvariantCulture) + (rows.Count == 1 ? " change" : " changes")
                           + (_loosened > 0 ? " — loosens " + _loosened.ToString(CultureInfo.InvariantCulture) + (_loosened == 1 ? " limit" : " limits") : "");
            _ok = _loosened > 0 ? TradingUi.Danger(saveText) : TradingUi.Primary(saveText);
            _ok.IsEnabled = CanSaveNote();
            AutomationProperties.SetName(_ok, saveText);
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { DialogResult = false; };
            _ok.Click += async (_, _) => await SubmitAsync();
            _reason.TextChanged += (_, _) => _ok.IsEnabled = CanSaveNote();
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
            Loaded += (_, _) => _reason.Focus();
        }

        private bool CanSaveNote() => !TradingUi.IsBusy(_ok) && TradingUi.ValidNote(_reason.Text);

        private async Task SubmitAsync()
        {
            if (!CanSaveNote()) return;
            TradingUi.SetBusy(_ok, true, "Saving…");
            TradingUi.ShowError(_error, null);
            var res = await PlatformClient.PutTradingSettingsAsync(_accountId, _reason.Text.Trim(), _changes);
            TradingUi.SetBusy(_ok, false);
            if (res.Ok && res.Value != null)
            {
                Saved = res.Value;
                DialogResult = true;
                return;
            }
            TradingUi.ShowError(_error, res.Ok ? "Unexpected response from the backend (PUT /v2/portfolio/settings)." : PlatformClient.DescribeFailure(res));
            _ok.IsEnabled = CanSaveNote();
        }
    }

    /// <summary>
    /// Migration of the old paper account (SPEC §17.9) — only after a dry run without blockers: explains the hard switch
    /// (the old engine becomes advisory only, its account is frozen and copied 1:1 into paper-council); one confirmation step with
    /// an optional note (fix1023 — no typed MIGRATE any more; no confirm word is sent).
    /// 409 migration_blocked lists the blockers verbatim.
    /// </summary>
    internal sealed class TradingMigrationDialog : FundPopupWindow
    {
        private readonly TextBox _reason;
        private readonly TextBlock _error;
        private readonly Button _ok;

        public TradingMigrationResult? Result { get; private set; }
        /// <summary>The `code` of the last refused / failed attempt (migration_blocked / migration_failed / …), null if none.</summary>
        public string? FailureCode { get; private set; }

        public TradingMigrationDialog(Window? owner, TradingDryRun dry)
            : base(owner, "Migrate the old paper account", 600, 560, resizable: true)
        {
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Migrate the old paper account", 14, FundPalette.TextBright, bold: true));
            var msg = FundUi.Text(
                "This is a one-way switch of the account's OWNER:\n"
                + "• the old account (Overview) is frozen and copied 1:1 into the platform account " + (dry.TargetAccountId ?? PlatformClient.CouncilAccountId)
                + " (cash ledger, positions, orders, equity curve, peak, broker parameters);\n"
                + "• its positions are held by the source \"Council of AIs\" (" + (dry.SourceId ?? "council-of-ais") + ") — you can reduce or close them with manual platform orders;\n"
                + "• from then on the old engine only analyses (agents, AI judge, recommendations); every trading path of the Overview "
                + "(autopilot, manual orders, Execute, reset) is refused with a clear message;\n"
                + "• the old tables stay as the read-only historical record (nothing is deleted);\n"
                + "• the kill switch and the dead-man switch keep covering both.", 12, FundPalette.Dim);
            msg.Margin = new Thickness(0, 8, 0, 8);
            msg.LineHeight = 18;
            stack.Children.Add(msg);
            if (!string.IsNullOrWhiteSpace(dry.Note)) stack.Children.Add(TradingUi.Note(dry.Note!, FundPalette.Faint));
            if (dry.Legacy is TradingMigrationLegacy l)
            {
                stack.Children.Add(TradingUi.Head("What will be copied (dry run)"));
                stack.Children.Add(FundUi.KeyValue("Orders · ledger rows · equity points",
                    (l.Orders?.ToString(CultureInfo.InvariantCulture) ?? "—") + " · " + (l.LedgerRows?.ToString(CultureInfo.InvariantCulture) ?? "—")
                    + " · " + (l.EquityPoints?.ToString(CultureInfo.InvariantCulture) ?? "—"), null, 11.5));
                stack.Children.Add(FundUi.KeyValue("Cash · starting capital", FundFormat.Money(l.Cash) + " · " + FundFormat.Money(l.StartingCapital), null, 11.5));
                stack.Children.Add(FundUi.KeyValue("Positions", (l.Positions?.Count ?? 0).ToString(CultureInfo.InvariantCulture)
                    + ((l.Positions?.Count ?? 0) > 0 ? " — " + string.Join(", ", l.Positions!.Select(p => (p.Symbol ?? "?") + " " + FundFormat.Qty(p.Qty))) : ""), null, 11.5));
            }
            stack.Children.Add(TradingUi.Label(TradingUi.NoteLabel));
            _reason = TradingUi.NoteBox("Optional note for the migration");
            stack.Children.Add(_reason);
            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);
            var target = dry.TargetAccountId ?? PlatformClient.CouncilAccountId;
            _ok = TradingUi.Danger("Migrate now — freeze the old account and copy it into " + target);
            AutomationProperties.SetName(_ok, "Migrate the old paper account into " + target + " now (one-way)");
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { DialogResult = false; };
            _ok.Click += async (_, _) => await SubmitAsync();
            _reason.TextChanged += (_, _) => { if (!TradingUi.IsBusy(_ok)) _ok.IsEnabled = TradingUi.ValidNote(_reason.Text); };
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
            Loaded += (_, _) => _reason.Focus();
        }

        private async Task SubmitAsync()
        {
            if (TradingUi.IsBusy(_ok) || !TradingUi.ValidNote(_reason.Text)) return;
            TradingUi.SetBusy(_ok, true, "Migrating…");
            TradingUi.ShowError(_error, "Migrating — the old account is frozen first, then copied in one transaction…");
            _error.Foreground = FundPalette.Faint;
            var res = await PlatformClient.MigrateAsync(_reason.Text.Trim());
            if (!res.Ok && (res.TimedOut || res.Status == PlatformCallStatus.Offline))
            {
                // dlgguard (2026-10-23): no answer — the migration may have run; confirm via GET /v2/portfolio/migration before a retry
                for (var i = 1; i <= 10 && IsLoaded; i++)
                {
                    TradingUi.ShowError(_error, "No answer in time — the migration may have run — checking… (" + i.ToString(CultureInfo.InvariantCulture) + "/10)");
                    var st = await PlatformClient.GetMigrationAsync();
                    if (st.Ok && st.Value?.Migration != null)
                    {
                        Result = new TradingMigrationResult { Migration = st.Value.Migration };
                        DialogResult = true;
                        return;
                    }
                    await Task.Delay(3000);
                }
                if (!IsLoaded) return;
                TradingUi.ShowError(_error, null);
            }
            TradingUi.SetBusy(_ok, false);
            _error.Foreground = FundPalette.Kill;
            if (res.Ok && res.Value?.Migration != null)
            {
                Result = res.Value;
                DialogResult = true;
                return;
            }
            if (!res.Ok && (res.TimedOut || res.Status == PlatformCallStatus.Offline))
            {
                _ok.IsEnabled = TradingUi.ValidNote(_reason.Text);   // checked: nothing was migrated — the user may try again
                return;
            }
            FailureCode = res.Ok ? "unexpected" : PlatformClient.ErrorCode(res) ?? res.Status.ToString();
            TradingUi.ShowError(_error, res.Ok ? "Unexpected response from the backend (POST /v2/portfolio/migration)." : PlatformClient.MigrationFailureText(res));
            _ok.IsEnabled = TradingUi.ValidNote(_reason.Text);
        }
    }

    /// <summary>
    /// A manual simulated paper order on a platform account (SPEC §17.7, POST /v2/oms/orders): symbol, side, quantity or USD
    /// notional, the source (sub-ledger) it books to, optional reason. One client order id (GUID) per dialog — pressing
    /// "Place" again after a time-out returns the stored order instead of sending a second one. The pre-trade checks run in
    /// the backend; a failed check is an order in state "rejected" with its checks (shown by the caller, verbatim).
    /// </summary>
    internal sealed class TradingOrderDialog : FundPopupWindow
    {
        private readonly string _accountId;
        private readonly string _clientOrderId = Guid.NewGuid().ToString("N");
        private readonly TextBox _symbol;
        private readonly RadioButton _buy, _sell, _short, _cover;
        private readonly RadioButton _byQty, _byNotional;
        private readonly TextBox _amount;
        private readonly ListBox _sources;
        private readonly TextBox _reason;
        private readonly TextBlock _error;
        private readonly Button _ok;
        private bool _closed;

        public OmsOrder? Order { get; private set; }

        /// <summary>True when a submit timed out (the order may exist) — the caller reloads the view even if the dialog is cancelled.</summary>
        public bool SubmitTimedOut { get; private set; }

        public TradingOrderDialog(Window? owner, string accountId, IReadOnlyList<TradingSource> sources, string? symbol, string? side, double? qty)
            : base(owner, "New paper order · " + accountId, 560, 640, resizable: true)
        {
            _accountId = accountId;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("New paper order · " + TradingText.AccountLabel(accountId), 14, FundPalette.TextBright, bold: true));
            var intro = FundUi.Text("A simulated market order through the platform OMS: stored first, then checked (pre-trade checks) and sent to the "
                                    + "internal paper broker. Paper trading only — no real money.", 12, FundPalette.Dim);
            intro.Margin = new Thickness(0, 8, 0, 8);
            intro.LineHeight = 18;
            stack.Children.Add(intro);

            stack.Children.Add(TradingUi.Label("Symbol (e.g. SPY, BTC-USD)"));
            _symbol = FundUi.Input(symbol ?? "");
            _symbol.MaxLength = 20;
            _symbol.CharacterCasing = CharacterCasing.Upper;
            AutomationProperties.SetName(_symbol, "Order symbol");
            stack.Children.Add(_symbol);

            stack.Children.Add(TradingUi.Label("Side"));
            var sides = new WrapPanel { Margin = new Thickness(0, 0, 0, 4) };
            _buy = TradingUi.Radio("PtSide", "Buy", "Side: buy", side == null || side == "buy");
            _sell = TradingUi.Radio("PtSide", "Sell", "Side: sell (reduce a long)", side == "sell");
            _short = TradingUi.Radio("PtSide", "Short", "Side: short", side == "short");
            _cover = TradingUi.Radio("PtSide", "Cover", "Side: cover (reduce a short)", side == "cover");
            sides.Children.Add(_buy);
            sides.Children.Add(_sell);
            sides.Children.Add(_short);
            sides.Children.Add(_cover);
            stack.Children.Add(sides);

            stack.Children.Add(TradingUi.Label("Amount"));
            var unitRow = new WrapPanel { Margin = new Thickness(0, 0, 0, 4) };
            _byQty = TradingUi.Radio("PtUnit", "Quantity", "Amount as a quantity (shares; fractional for crypto)", true);
            _byNotional = TradingUi.Radio("PtUnit", "USD notional", "Amount as a USD notional", false);
            unitRow.Children.Add(_byQty);
            unitRow.Children.Add(_byNotional);
            stack.Children.Add(unitRow);
            _amount = FundUi.Input(qty.HasValue ? FundFormat.Qty(qty).Replace(FundFormat.Minus, "-").Replace(",", "") : "");
            _amount.MaxLength = 24;
            AutomationProperties.SetName(_amount, "Order amount");
            stack.Children.Add(_amount);

            stack.Children.Add(TradingUi.Label("Books to (sub-ledger)"));
            _sources = TradingUi.List("Source the order is booked to", 96);
            var defaultSource = accountId == PlatformClient.CouncilAccountId ? "council-of-ais" : "_unallocated";
            var bookable = sources.Where(s => !string.IsNullOrEmpty(s.SourceId)).ToList();
            if (!bookable.Any(s => s.SourceId == defaultSource))
                bookable.Insert(0, new TradingSource { SourceId = defaultSource, Name = defaultSource == "_unallocated" ? "Unallocated cash" : "Council of AIs", Kind = defaultSource == "_unallocated" ? "unallocated" : "council" });
            foreach (var s in bookable)
            {
                if (accountId == PlatformClient.CouncilAccountId && s.SourceId != "council-of-ais") continue;
                var item = new ListBoxItem
                {
                    Content = FundUi.Text((s.Name ?? s.SourceId) + " · " + s.SourceId, 11.5, FundPalette.Text, wrap: false),
                    Tag = s.SourceId,
                    Padding = new Thickness(6, 2, 6, 2),
                };
                AutomationProperties.SetName(item, "Book to " + (s.Name ?? s.SourceId));
                _sources.Items.Add(item);
                if (s.SourceId == defaultSource) _sources.SelectedItem = item;
            }
            stack.Children.Add(_sources);
            stack.Children.Add(TradingUi.Note("Default: " + (defaultSource == "_unallocated" ? "Unallocated cash (paper-main)" : "Council of AIs (paper-council)")
                                              + ". Booking a manual order to a strategy changes that strategy's sub-ledger — the runtime will trade it back to its target.", FundPalette.Faint));

            stack.Children.Add(TradingUi.Label("Reason (optional, stored with the order)"));
            _reason = FundUi.Input("", mono: false);
            _reason.MaxLength = PlatformClient.TradingReasonMax;
            AutomationProperties.SetName(_reason, "Order reason, optional");
            stack.Children.Add(_reason);

            stack.Children.Add(TradingUi.Note("Client order id (idempotent): " + _clientOrderId, FundPalette.Faint, mono: true));
            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);
            _ok = TradingUi.Primary("Place paper order");
            AutomationProperties.SetName(_ok, "Place the simulated paper order");
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { DialogResult = false; };
            _ok.Click += async (_, _) => await SubmitAsync();
            Closed += (_, _) => _closed = true;
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
            Loaded += (_, _) => { if (string.IsNullOrEmpty(_symbol.Text)) _symbol.Focus(); else _amount.Focus(); };
        }

        private string Side() => _sell.IsChecked == true ? "sell" : _short.IsChecked == true ? "short" : _cover.IsChecked == true ? "cover" : "buy";

        private async Task SubmitAsync()
        {
            var symbol = _symbol.Text.Trim();
            if (symbol.Length == 0) { TradingUi.ShowError(_error, "Enter a symbol."); return; }
            if (!TradingUi.TryParseNumber(_amount.Text, out var amount) || amount <= 0) { TradingUi.ShowError(_error, "Enter a positive amount."); return; }
            var source = (_sources.SelectedItem as ListBoxItem)?.Tag as string;
            if (TradingUi.IsBusy(_ok)) return;
            TradingUi.SetBusy(_ok, true, "Placing…");   // fix1023 B1: no second submit while the request is in flight
            TradingUi.ShowError(_error, null);
            var byQty = _byQty.IsChecked == true;
            var res = await PlatformClient.PlaceOmsOrderAsync(_accountId, symbol, Side(), byQty ? amount : null, byQty ? null : amount, source,
                                                              string.IsNullOrWhiteSpace(_reason.Text) ? null : _reason.Text, _clientOrderId);
            if (_closed) return;
            if (!res.TimedOut) TradingUi.SetBusy(_ok, false);
            if (res.Ok && res.Value?.Order != null)
            {
                Order = res.Value.Order;
                DialogResult = true;
                return;
            }
            if (res.TimedOut)
            {
                // retest 2026-10-23: the order may have been placed — look it up by its client order id before offering a retry
                SubmitTimedOut = true;
                TradingUi.ShowError(_error, PlatformClient.OrderNoAnswerText + "\nLooking the order up…");
                for (var i = 1; i <= 10 && !_closed; i++)
                {
                    var look = await PlatformClient.GetOmsOrderAsync(_clientOrderId);
                    if (_closed) return;
                    if (look.Ok && look.Value?.Order != null)
                    {
                        Order = look.Value.Order;   // the result (filled / working / rejected) — the caller shows it
                        DialogResult = true;
                        return;
                    }
                    TradingUi.ShowError(_error, PlatformClient.OrderNoAnswerText + "\nNot found yet (" + i.ToString(CultureInfo.InvariantCulture) + "/10"
                                                + (look.Ok || look.HttpStatus == 404 ? "" : ": " + PlatformClient.DescribeFailure(look)) + ") — checking again…");
                    await Task.Delay(3000);
                }
                if (_closed) return;
                TradingUi.ShowError(_error, PlatformClient.OrderNoAnswerText + "\nThe order was not found within 30 s — check Orders; Place again sends the same client order id.");
                TradingUi.SetBusy(_ok, false);
                _ok.IsEnabled = true;
                return;
            }
            TradingUi.ShowError(_error, res.Ok ? "Unexpected response from the backend (POST /v2/oms/orders)." : PlatformClient.DescribeFailure(res));
            _ok.IsEnabled = true;
        }
    }
}
