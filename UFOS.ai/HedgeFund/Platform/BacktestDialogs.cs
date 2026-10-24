using System;
using System.Globalization;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// "Run backtest…" dialog (Strategy Platform stage 3, SPEC §8.5/§8.6): range, benchmark,
    /// capital, stress tests on/off. Default: data loaded from the seam 2016-01-04 (first Alpaca day,
    /// user decision 2026-10-03), trading after the strategy's warm-up. The toggle "From 2000" sends
    /// history_from = "2000" (fix 2026-10-09): data loaded from 2000-01-03, trading after the warm-up —
    /// same semantics as the default, earlier data (needs the Tiingo key; without it the backend
    /// answers 503 with its own message — shown verbatim). A typed start date is an explicit first
    /// trading day (its warm-up is loaded before it) and is sent as such, with or without the toggle.
    /// Inputs are checked here for format only; the backend validates the rest.
    /// Stage 4 (SPEC §9.1/§9.7): the default end is the training end — the session before the locked holdout
    /// window (GET /v2/strategies/{id}/holdout, shown with the window start). The request is sent from the
    /// dialog: a 400 (e.g. an end date inside the holdout window) keeps the dialog open and shows the
    /// backend's message verbatim.
    /// §10 (robustness): checkbox "If the connection drops: wait and resume automatically (max 5 times)" → auto_resume.
    /// Chrome: FundPopupWindow (borderless, drag-to-restore, square corners when maximised).
    /// </summary>
    internal sealed class RunBacktestDialog : FundPopupWindow
    {
        public const string SeamDate = "2016-01-04";
        public const string EarliestStart = "2000-01-03";
        public const double DefaultCapital = 100_000;
        public const double MinCapital = 1_000;
        public const double MaxCapital = 100_000_000;
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        private readonly string _strategyId;
        private readonly int? _version;
        private readonly TextBox _start;
        private readonly TextBox _end;
        private readonly TextBox _benchmark;
        private readonly TextBox _capital;
        private readonly CheckBox _from2000;
        private readonly CheckBox _stress;
        private readonly CheckBox _autoResume;
        private readonly TextBlock _error;
        private readonly Button _ok;
        private readonly Button _cancel;
        private readonly Func<BacktestRequest, Task<PlatformResult<BacktestStarted>>> _submit;
        private readonly TextBlock _endLabel;
        private readonly TextBlock _windowText;
        private bool _submitting;
        private bool _closed;

        public BacktestRequest? Request { get; private set; }
        /// <summary>Result of POST /v2/backtests for every outcome except a 400 (which stays in the dialog).</summary>
        public PlatformResult<BacktestStarted>? StartResult { get; private set; }

        /// <summary>
        /// Live click-test fix 2026-10-18: the dialog opens AT ONCE. With <paramref name="loadHoldout"/> it reads the holdout window
        /// itself after it is shown (GET /v2/strategies/{id}/holdout, "Loading…" until then) instead of the launcher awaiting that
        /// request before any window existed (no feedback, button still enabled).
        /// </summary>
        public RunBacktestDialog(Window? owner, string strategyId, string strategyName, int? version, string? specBenchmark,
                                 HoldoutStatus? holdout, string? holdoutError,
                                 Func<BacktestRequest, Task<PlatformResult<BacktestStarted>>> submit, bool loadHoldout = false)
            : base(owner, "Run backtest", 500, 600, resizable: false)
        {
            _strategyId = strategyId;
            _version = version;
            _submit = submit;
            SizeToContent = SizeToContent.Height;

            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Run backtest", 14, FundPalette.TextBright, bold: true));
            var msg = FundUi.Text(strategyName + (version is int v ? " · v" + v.ToString(Inv) : "")
                                  + "\nDaily bars, fills through the paper broker (fees, slippage, borrow, dividends as cash), orders capped at 1 % of the "
                                  + "20-day average dollar volume. No AI is used.", 12, FundPalette.Dim);
            msg.Margin = new Thickness(0, 8, 0, 12);
            msg.LineHeight = 18;
            stack.Children.Add(msg);

            _start = AddField(stack, "Start date (optional, YYYY-MM-DD — empty = first session after the warm-up)", "");
            _from2000 = new CheckBox
            {
                Content = "From 2000 (incl. 2001/2008 crises — Tiingo key needed only for data before 2016)",
                Foreground = FundPalette.Text,
                FontSize = 11.5,
                Margin = new Thickness(0, -4, 0, 12),
                ToolTip = "Loads data from " + EarliestStart + " instead of " + SeamDate + " and trades after the strategy's warm-up. "
                          + "Prices before " + SeamDate + " come from Tiingo (second data source, joined at the seam with an overlap check); "
                          + "from " + SeamDate + " on, Alpaca SIP history. The Tiingo key is needed only for backtests before 2016 — "
                          + "set it in Hedge Fund → Keys & data.",
            };
            AutomationProperties.SetName(_from2000, "Start in 2000, including the 2001 and 2008 crises; needs the Tiingo key");
            stack.Children.Add(_from2000);

            _end = AddField(stack, "End date (optional)", "", out _endLabel);
            _windowText = FundUi.Text("", 10.5, FundPalette.Dim);
            _windowText.Margin = new Thickness(0, -4, 0, 12);
            stack.Children.Add(_windowText);
            if (loadHoldout) ShowHoldoutLoading();
            else ApplyHoldout(holdout, holdoutError);
            var bench = string.IsNullOrWhiteSpace(specBenchmark) ? "SPY" : specBenchmark!.Trim().ToUpperInvariant();
            _benchmark = AddField(stack, "Benchmark symbol (buy-and-hold, same costs and dividends)", bench);
            _capital = AddField(stack, "Starting capital (USD, 1,000 – 100,000,000)", DefaultCapital.ToString("0", Inv));

            _stress = new CheckBox
            {
                Content = "Stress tests (crisis episodes, costs ×2, slippage ×3, Monte-Carlo)",
                Foreground = FundPalette.Text,
                FontSize = 11.5,
                IsChecked = true,
                Margin = new Thickness(0, 0, 0, 12),
                ToolTip = "Episodes without enough data are listed as \"not available\". Adds several engine runs — takes longer.",
            };
            AutomationProperties.SetName(_stress, "Run stress tests");
            stack.Children.Add(_stress);

            // SPEC §10.3/§10.4: auto_resume (default off) — on a connection error the run waits and resumes, max 5 times.
            _autoResume = RunResume.MakeAutoResumeCheckBox();
            stack.Children.Add(_autoResume);

            _error = FundUi.Text("", 11, FundPalette.Kill);
            _error.Margin = new Thickness(0, 0, 0, 10);
            _error.Visibility = Visibility.Collapsed;
            stack.Children.Add(_error);

            var note = FundUi.Text("Hypothetical, simulated results — they do not indicate future results.", 10.5, FundPalette.Faint);
            note.FontStyle = FontStyles.Italic;
            note.Margin = new Thickness(0, 0, 0, 12);
            stack.Children.Add(note);

            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
            _cancel = FundUi.MakeButton("Cancel");
            _cancel.IsCancel = true;
            _cancel.Click += (_, _) => { if (!_submitting) DialogResult = false; };
            _ok = FundUi.MakeButton("Run backtest", FundPalette.Make(0x267C8CF8), FundPalette.Focus, FundPalette.TextBright);
            _ok.IsDefault = true;
            _ok.Margin = new Thickness(8, 0, 0, 0);
            _ok.Click += Ok_Click;
            buttons.Children.Add(_cancel);
            buttons.Children.Add(_ok);
            stack.Children.Add(buttons);
            SetBody(stack);

            Loaded += async (_, _) =>
            {
                _start.Focus();
                _start.SelectAll();
                if (loadHoldout) await LoadHoldoutAsync();
            };
            // No close while the request is in flight (its result decides what happens next).
            Closing += (_, e) => { if (_submitting) e.Cancel = true; };
            Closed += (_, _) => _closed = true;
        }

        private void ShowHoldoutLoading()
        {
            _endLabel.Text = "End date (optional — empty = default end before the holdout window)";
            _windowText.Text = "Loading the locked holdout window…";
            _windowText.Foreground = FundPalette.Faint;
            AutomationProperties.SetName(_windowText, _windowText.Text);
            AutomationProperties.SetName(_end, _endLabel.Text);
        }

        private async Task LoadHoldoutAsync()
        {
            PlatformResult<HoldoutStatus> res;
            try
            {
                res = await PlatformClient.GetHoldoutAsync(_strategyId);
            }
            catch (Exception ex)
            {
                if (!_closed) ApplyHoldout(null, ex.Message);
                return;
            }
            if (_closed) return;
            if (res.Ok && res.Value != null) ApplyHoldout(res.Value, null);
            else ApplyHoldout(null, res.Ok ? "unexpected response from the backend" : res.Message);
        }

        /// <summary>Default end + locked window text from GET /v2/strategies/{id}/holdout; a failure is shown verbatim.</summary>
        private void ApplyHoldout(HoldoutStatus? holdout, string? holdoutError)
        {
            var holdoutStart = HoldoutText.HoldoutStart(holdout);
            var trainingEnd = HoldoutText.TrainingEnd(holdout);
            var defaultEnd = trainingEnd ?? (holdoutStart != null ? "the last session before " + holdoutStart : null);
            _endLabel.Text = "End date (optional — empty = " + (defaultEnd != null ? "default end " + defaultEnd : "default end before the holdout window") + ")";
            if (holdout != null)
            {
                _windowText.Text = "Locked holdout window: " + (holdoutStart ?? "?") + " … " + (HoldoutText.WindowEnd(holdout) ?? "?")
                                   + ". Backtests end before it; a start or end date on or after " + (holdoutStart ?? "?")
                                   + " is refused — the holdout is opened once per strategy family via \"Open holdout\".";
                _windowText.Foreground = FundPalette.Dim;
            }
            else
            {
                _windowText.Text = "Holdout window not loaded" + (holdoutError != null ? ": " + holdoutError : ".")
                                   + " The backend still refuses dates inside it.";
                _windowText.Foreground = FundPalette.Risk;
            }
            AutomationProperties.SetName(_windowText, _windowText.Text);
            AutomationProperties.SetName(_end, _endLabel.Text);
        }

        private static TextBox AddField(StackPanel stack, string label, string initial) => AddField(stack, label, initial, out _);

        private static TextBox AddField(StackPanel stack, string label, string initial, out TextBlock labelBlock)
        {
            var l = FundUi.Text(label, 10.5, FundPalette.Faint);
            labelBlock = l;
            l.Margin = new Thickness(0, 0, 0, 4);
            stack.Children.Add(l);
            var box = FundUi.Input(initial);
            box.Margin = new Thickness(0, 0, 0, 10);
            AutomationProperties.SetName(box, label);
            stack.Children.Add(box);
            return box;
        }

        private async void Ok_Click(object sender, RoutedEventArgs e)
        {
            if (_submitting) return;
            var problem = Build(out var request);
            if (problem != null || request == null)
            {
                _error.Text = problem ?? "The request could not be built.";
                _error.Visibility = Visibility.Visible;
                return;
            }
            _submitting = true;
            _ok.IsEnabled = false;
            _cancel.IsEnabled = false;
            _error.Visibility = Visibility.Collapsed;
            PlatformResult<BacktestStarted> result;
            try
            {
                result = await _submit(request);
            }
            finally
            {
                _submitting = false;
                _ok.IsEnabled = true;
                _cancel.IsEnabled = true;
            }
            if (result.Status == PlatformCallStatus.BadRequest)
            {
                // e.g. "This end date lies inside the locked holdout window (from …)" — verbatim, dialog stays open
                _error.Text = result.Message;
                _error.Visibility = Visibility.Visible;
                return;
            }
            Request = request;
            StartResult = result;
            DialogResult = true;
        }

        private string? Build(out BacktestRequest? request)
        {
            request = null;
            var startText = (_start.Text ?? "").Trim();
            DateTime? start = null;
            if (startText.Length > 0)
            {
                if (!DateTime.TryParseExact(startText, "yyyy-MM-dd", Inv, DateTimeStyles.None, out var s))
                    return "Start date must be empty or a date in the form YYYY-MM-DD.";
                if (s < DateTime.ParseExact(EarliestStart, "yyyy-MM-dd", Inv))
                    return "Start date must be on or after " + EarliestStart + ".";
                start = s;
            }
            var endText = (_end.Text ?? "").Trim();
            if (endText.Length > 0)
            {
                if (!DateTime.TryParseExact(endText, "yyyy-MM-dd", Inv, DateTimeStyles.None, out var end))
                    return "End date must be empty or a date in the form YYYY-MM-DD.";
                if (start is DateTime st && end <= st) return "End date must lie after the start date.";
            }
            var bench = (_benchmark.Text ?? "").Trim().ToUpperInvariant();
            if (!FundAssets.IsValidSymbol(bench)) return "Benchmark must be a valid symbol (e.g. SPY, QQQ, BTC-USD).";
            var capText = new string((_capital.Text ?? "").Where(c => c != ',' && c != '_' && c != ' ').ToArray());
            if (!double.TryParse(capText, NumberStyles.Float, Inv, out var capital) || double.IsNaN(capital) || double.IsInfinity(capital))
                return "Starting capital must be a number.";
            if (capital < MinCapital || capital > MaxCapital) return "Starting capital must be between 1,000 and 100,000,000.";
            request = new BacktestRequest
            {
                StrategyId = _strategyId,
                Version = _version,
                // Empty start = the engine's default path: load from history_from (2016-01-04, or 2000-01-03 with
                // the toggle) and trade after the warm-up. A typed date is an explicit first trading day.
                Start = startText.Length > 0 ? startText : null,
                HistoryFrom = _from2000.IsChecked == true ? "2000" : null,
                End = endText.Length > 0 ? endText : null,
                Benchmark = bench,
                Capital = capital,
                Stress = _stress.IsChecked == true,
                AutoResume = _autoResume.IsChecked == true,
            };
            return null;
        }
    }

    /// <summary>Run dialog → POST /v2/backtests → report window (progress panel); 409 handling.</summary>
    internal static class BacktestLauncher
    {
        public const string AlreadyRunning = "A backtest is already running.";

        /// <summary>Shows the dialog and starts the run. True when a run was started (or the running one opened).</summary>
        // dlgguard (2026-10-23): one at a time — queued clicks / a second shortcut return at once; the dialog itself is open-once
        public static Task<bool> RunAsync(Window? owner, string strategyId, string strategyName, int? version, string? specBenchmark) => DialogGuard.RunForResultAsync("backtest-run:" + strategyId, () => RunCoreAsync(owner, strategyId, strategyName, version, specBenchmark), false);

        public static async Task<bool> RunCoreAsync(Window? owner, string strategyId, string strategyName, int? version, string? specBenchmark)
        {
            // Live click-test fix 2026-10-18: the dialog opens at once (like Validate…) and loads the holdout window (SPEC §9.1)
            // itself; a failure is shown in the dialog, verbatim. Before, this method awaited GET …/holdout before any window existed.
            var dialog = new RunBacktestDialog(owner, strategyId, strategyName, version, specBenchmark, null, null,
                                               PlatformClient.StartBacktestAsync, loadHoldout: true);
            if (dialog.ShowDialog() != true || dialog.Request == null || dialog.StartResult == null) return false;

            var result = dialog.StartResult;
            if (result.Ok && !string.IsNullOrWhiteSpace(result.Value?.RunId))
            {
                BacktestReportWindow.Open(owner, strategyId, strategyName, result.Value!.RunId);
                return true;
            }
            if (result.Ok)
            {
                FundDialogs.Info(owner, "Backtest", "Unexpected response from the backend (POST /v2/backtests without run_id) — backend and app versions may not match.");
                return false;
            }
            if (result.Status == PlatformCallStatus.Conflict)
            {
                var runId = PlatformClient.ReadConflictRunId(result);
                string? lookupProblem = null;
                if (runId == null)
                {
                    var list = await PlatformClient.ListBacktestsAsync(null, 20);
                    if (list.Ok) runId = list.Value?.Runs?.FirstOrDefault(r => BacktestStatus.IsActive(r.Status))?.Id;
                    else lookupProblem = list.Message;
                }
                // The backend's text is shown as sent; the fixed sentence leads when the backend says something else.
                var text = string.Equals(result.Message.Trim(), AlreadyRunning, StringComparison.Ordinal)
                    ? AlreadyRunning
                    : AlreadyRunning + "\n\nBackend: " + result.Message;
                if (runId != null)
                {
                    if (!FundDialogs.Confirm(owner, "Backtest", text, "Open running backtest")) return false;
                    BacktestReportWindow.Open(owner, null, null, runId);
                    return true;
                }
                FundDialogs.Info(owner, "Backtest", text + (lookupProblem != null ? "\n\nThe running backtest could not be looked up: " + lookupProblem : ""));
                return false;
            }
            FundDialogs.Info(owner, "Backtest not started", result.Message);
            return false;
        }
    }
}
