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
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// Backtest report (Strategy Platform stage 3, SPEC §8.6 / §8.11) — built in code like the
    /// other Hedge Fund popups. Left: run history of the strategy (GET /v2/backtests?strategy_id=).
    /// Right: progress panel while the selected run is queued/running (phase, percent, message
    /// shown verbatim, Cancel; GET /v2/backtests/{id} every 1.5 s), then the
    /// report: equity vs benchmark (seam 2016-01-04 marked when the range crosses it), drawdown,
    /// metrics tables (daily + per-trade hit rate / profit factor, costs), signal quality (IC decay
    /// chart or the time-series value), stress results, warnings (code + message), data / licences
    /// + seam, trades summary. Backend texts (errors, warnings, progress messages, reasons) are
    /// shown verbatim; a metric the backend could not compute shows "—" and its `unavailable`
    /// reason. Hypothetical, simulated results.
    /// §11 (explain): section "Attribution" (computed by the backend, no AI — <see cref="AttributionView"/>) and
    /// "Explain with AI…" (<see cref="ExplainPanel"/>: AI-generated and labelled as such, EU AI Act Art. 50).
    /// §10 (robustness): status "waiting" (auto-resume after a connection loss) shows the backend's waiting
    /// message verbatim with Cancel; a failed run with resumable = true gets "↻ Resume" in the history and the
    /// report header (POST …/resume; a 409 reason is shown verbatim); resumed runs show "resumed from &lt;id&gt;".
    /// </summary>
    internal sealed class BacktestReportView : UserControl
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private const string DefaultSeamDate = "2016-01-04";
        private const string EarliestDataDate = "2000-01-03";   // history_from "2000" (SPEC §8.5, fix 2026-10-09)
        private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(1.5);

        private string? _strategyId;
        private string? _strategyName;
        private string? _selectedRunId;
        private BacktestCard? _card;
        private List<BacktestRunSummary> _runs = new();
        private int _cardSeq;
        private int _historySeq;
        private bool _polling;
        private bool _stopped;

        private readonly DispatcherTimer _timer;
        private readonly StackPanel _historyPanel;
        private readonly TextBlock _historyStatus;
        private readonly StackPanel _report;
        private readonly Border _progressPanel;
        private readonly TextBlock _progressPhase;
        private readonly TextBlock _progressMessage;
        private readonly Border _progressFill;
        private readonly Grid _progressTrack;
        private readonly Button _cancelButton;
        private readonly TextBlock _progressError;

        public BacktestReportView(string? strategyId, string? strategyName)
        {
            _strategyId = strategyId;
            _strategyName = strategyName;
            AutomationProperties.SetName(this, "Backtest report");

            var root = new Grid { Margin = new Thickness(14, 12, 14, 14) };
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(250) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            // ----- run history -----
            var historyStack = new StackPanel();
            var hHead = new Grid { Margin = new Thickness(0, 0, 0, 6) };
            hHead.Children.Add(FundUi.Text("Run history", 12, FundPalette.Text, bold: true));
            var refresh = FundUi.MakeButton("↻");
            refresh.Padding = new Thickness(8, 1, 8, 1);
            refresh.MinHeight = 20;
            refresh.FontSize = 11;
            refresh.HorizontalAlignment = HorizontalAlignment.Right;
            refresh.ToolTip = "Reload the run history";
            AutomationProperties.SetName(refresh, "Reload the run history");
            refresh.Click += async (_, _) => await LoadHistoryAsync();
            hHead.Children.Add(refresh);
            historyStack.Children.Add(hHead);
            _historyStatus = FundUi.Text("", 10.5, FundPalette.Faint);
            _historyStatus.Margin = new Thickness(0, 0, 0, 6);
            historyStack.Children.Add(_historyStatus);
            _historyPanel = new StackPanel();
            AutomationProperties.SetName(_historyPanel, "Backtest runs of this strategy");
            historyStack.Children.Add(_historyPanel);
            var historyScroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false, Content = historyStack };
            var historyBorder = new Border
            {
                Background = FundPalette.Panel,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(10, 10, 6, 10),
                Child = historyScroll,
            };
            root.Children.Add(historyBorder);

            // ----- right side: progress panel + report -----
            var right = new StackPanel();

            _progressPhase = FundUi.Text("", 12, FundPalette.TextBright, bold: true);
            _progressTrack = new Grid { Height = 6, Margin = new Thickness(0, 8, 0, 6) };
            _progressTrack.Children.Add(new Border { Background = FundPalette.Hairline, CornerRadius = new CornerRadius(3) });
            _progressFill = new Border { Background = FundPalette.Focus, CornerRadius = new CornerRadius(3), HorizontalAlignment = HorizontalAlignment.Left, Width = 0 };
            _progressTrack.Children.Add(_progressFill);
            _progressTrack.SizeChanged += (_, _) => UpdateProgressBar(_card?.Progress?.Percent);
            _progressMessage = FundUi.Text("", 11.5, FundPalette.Dim, mono: true);
            AutomationProperties.SetLiveSetting(_progressMessage, AutomationLiveSetting.Polite);
            _progressError = FundUi.Text("", 11, FundPalette.Kill);
            _progressError.Visibility = Visibility.Collapsed;
            _progressError.Margin = new Thickness(0, 6, 0, 0);
            _cancelButton = FundUi.MakeButton("Cancel backtest", FundPalette.KillSurface, FundPalette.Kill, FundPalette.Kill);
            _cancelButton.HorizontalAlignment = HorizontalAlignment.Left;
            _cancelButton.Margin = new Thickness(0, 10, 0, 0);
            AutomationProperties.SetName(_cancelButton, "Cancel the running backtest");
            _cancelButton.Click += async (_, _) => await CancelAsync();
            var progressStack = new StackPanel();
            progressStack.Children.Add(_progressPhase);
            progressStack.Children.Add(_progressTrack);
            progressStack.Children.Add(_progressMessage);
            progressStack.Children.Add(_progressError);
            progressStack.Children.Add(_cancelButton);
            _progressPanel = FundUi.Panel(progressStack);
            _progressPanel.Visibility = Visibility.Collapsed;
            AutomationProperties.SetName(_progressPanel, "Backtest progress");
            right.Children.Add(_progressPanel);

            _report = new StackPanel();
            right.Children.Add(_report);

            var rightScroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false, Content = right, Margin = new Thickness(12, 0, 0, 0) };
            ChartTokens.AttachPageWheel(rightScroll);   // retest 2026-10-23: one notch = the normal step everywhere on the report
            Grid.SetColumn(rightScroll, 1);
            root.Children.Add(rightScroll);
            Content = root;

            _timer = new DispatcherTimer { Interval = PollInterval };
            _timer.Tick += async (_, _) => await PollAsync();
        }

        public string? StrategyId => _strategyId;

        /// <summary>Stops polling (window closed).</summary>
        public void Stop()
        {
            _stopped = true;
            _timer.Stop();
        }

        /// <summary>Starts <see cref="ShowRunAsync"/> from a non-async caller (async void: an exception reaches the
        /// dispatcher's unhandled-exception handler instead of vanishing in a discarded task).</summary>
        public async void Begin(string? runId) => await ShowRunAsync(runId);

        /// <summary>Loads the run history and selects <paramref name="runId"/> (or the newest run).</summary>
        public async Task ShowRunAsync(string? runId)
        {
            if (runId != null)
            {
                await SelectRunAsync(runId);
                await LoadHistoryAsync();
            }
            else
            {
                await LoadHistoryAsync();
                var first = _runs.FirstOrDefault()?.Id;
                if (first != null) await SelectRunAsync(first);
                else ShowReportPlaceholder("No backtest runs for this strategy yet — use \"Run backtest…\" in the library.");
            }
        }

        // =====================================================================================
        // History
        // =====================================================================================

        private async Task LoadHistoryAsync()
        {
            if (_strategyId == null)
            {
                _historyStatus.Text = "Loading…";
                return;
            }
            var seq = ++_historySeq;
            _historyStatus.Text = "Loading…";
            var list = await PlatformClient.ListBacktestsAsync(_strategyId, 50);
            if (seq != _historySeq || _stopped) return;
            if (!list.Ok)
            {
                _historyStatus.Text = "";
                _historyPanel.Children.Clear();
                _historyPanel.Children.Add(ErrorBlock(list.Message));
                return;
            }
            _runs = list.Value?.Runs ?? new List<BacktestRunSummary>();
            if (string.IsNullOrWhiteSpace(_strategyName)) _strategyName = _runs.FirstOrDefault(r => !string.IsNullOrWhiteSpace(r.StrategyName))?.StrategyName;
            _historyStatus.Text = (_strategyName ?? _strategyId) + " · " + _runs.Count.ToString(Inv) + (_runs.Count == 1 ? " run" : " runs");
            RenderHistory();
        }

        private void RenderHistory()
        {
            _historyPanel.Children.Clear();
            if (_runs.Count == 0)
            {
                _historyPanel.Children.Add(FundUi.Text("No runs yet.", 11, FundPalette.Faint));
                return;
            }
            foreach (var run in _runs)
            {
                var id = run.Id ?? "";
                var stack = new StackPanel();
                var head = new StackPanel { Orientation = Orientation.Horizontal };
                head.Children.Add(StatusChip(run.Status));
                var when = FundUi.Text(FundFormat.LocalTime(run.CreatedAt, "yyyy-MM-dd HH:mm") + " · v" + (run.Version?.ToString(Inv) ?? "?"), 10.5, FundPalette.Dim, mono: true, wrap: false);
                when.Margin = new Thickness(6, 0, 0, 0);
                when.VerticalAlignment = VerticalAlignment.Center;
                head.Children.Add(when);
                stack.Children.Add(head);
                if (FundUi.FixtureLine(run.Fixture, run.FixtureNote, 10.5, new Thickness(0, 1, 0, 1)) is TextBlock fixtureRow) stack.Children.Add(fixtureRow);
                var range = RangeText(run.Range, run.Request);
                stack.Children.Add(Line(range, 10.5, FundPalette.Faint, mono: true));
                if (run.KeyMetrics is JsonElement km)
                {
                    stack.Children.Add(Line("Return " + FundFormat.Fraction(BtJson.Num(km, "total_return"), 1, signed: true)
                                            + " · Sharpe " + FundFormat.Num(BtJson.Num(km, "sharpe"))
                                            + " · Max DD " + FundFormat.Fraction(BtJson.Num(km, "max_drawdown")), 10.5, FundPalette.Text, mono: true));
                }
                else if (!string.IsNullOrWhiteSpace(run.Error))
                {
                    stack.Children.Add(Line(run.Error! + RunResume.ErrorKindSuffix(run.ErrorKind), 10.5, FundPalette.Kill));
                }
                var resumed = RunResume.ResumedText(run.ResumedFrom, run.ResumeCount);
                if (resumed != null) stack.Children.Add(Line(resumed, 10.5, FundPalette.Dim, mono: true));
                var button = RowButton(stack, id == _selectedRunId);
                AutomationProperties.SetName(button, "Backtest run " + id + ", " + (run.Status ?? "unknown status") + ", started " + FundFormat.LocalTime(run.CreatedAt, "yyyy-MM-dd HH:mm")
                                                     + (resumed != null ? ", " + resumed : ""));
                button.Click += async (_, _) => await SelectRunAsync(id);
                _historyPanel.Children.Add(button);
                if (id.Length > 0 && RunResume.CanResume(run.Status, run.Resumable, run.Kind))
                {
                    var resume = RunResume.MakeResumeButton(id);
                    resume.HorizontalAlignment = HorizontalAlignment.Left;
                    resume.Margin = new Thickness(0, -2, 4, 8);
                    resume.Click += async (_, _) => await ResumeRunAsync(id, resume);
                    _historyPanel.Children.Add(resume);
                }
            }
        }

        private static Button RowButton(UIElement content, bool selected)
        {
            var b = FundUi.MakeButton("");
            b.Content = content;
            b.HorizontalContentAlignment = HorizontalAlignment.Stretch;
            b.Padding = new Thickness(8, 6, 8, 6);
            b.Margin = new Thickness(0, 0, 4, 6);
            b.Background = selected ? FundPalette.Make(0x267C8CF8) : FundPalette.Make(0xFF1E1E1E);
            b.BorderBrush = selected ? FundPalette.Focus : FundPalette.PanelBorder;
            // the template's ContentPresenter centres; a left-aligned grid child keeps the text readable
            if (content is FrameworkElement fe) fe.HorizontalAlignment = HorizontalAlignment.Left;
            return b;
        }

        // =====================================================================================
        // Selected run: load, poll, cancel
        // =====================================================================================

        private async Task SelectRunAsync(string runId)
        {
            var seq = ++_cardSeq;
            _selectedRunId = runId;
            _timer.Stop();
            RenderHistory();
            ShowReportPlaceholder("Loading…");
            _progressPanel.Visibility = Visibility.Collapsed;
            var res = await PlatformClient.GetBacktestAsync(runId);
            if (seq != _cardSeq || _stopped) return;
            if (!res.Ok || res.Value == null)
            {
                _report.Children.Clear();
                _report.Children.Add(ErrorBlock(res.Ok ? "Unexpected response from the backend (backtest detail)." : res.Message));
                return;
            }
            var historyMissing = _strategyId == null;
            ApplyCard(res.Value);
            if (historyMissing && _strategyId != null) await LoadHistoryAsync();
        }

        private void ApplyCard(BacktestCard card)
        {
            _card = card;
            if (_strategyId == null && !string.IsNullOrWhiteSpace(card.StrategyId)) _strategyId = card.StrategyId;
            if (string.IsNullOrWhiteSpace(_strategyName) && !string.IsNullOrWhiteSpace(card.StrategyName)) _strategyName = card.StrategyName;
            if (BacktestStatus.IsActive(card.Status))
            {
                ShowProgress(card);
                ShowReportPlaceholder("The report appears here when the run has finished.");
                if (!_stopped) _timer.Start();
            }
            else
            {
                _timer.Stop();
                _progressPanel.Visibility = Visibility.Collapsed;
                RenderReport(card);
            }
        }

        private async Task PollAsync()
        {
            if (_polling || _stopped || _selectedRunId == null) return;
            _polling = true;
            try
            {
                var seq = _cardSeq;
                var runId = _selectedRunId;
                var res = await PlatformClient.GetBacktestAsync(runId);
                if (seq != _cardSeq || _stopped) return;
                if (!res.Ok || res.Value == null)
                {
                    // keep polling: the last progress stays visible, the problem is shown verbatim
                    _progressError.Text = res.Ok ? "Unexpected response from the backend (backtest detail)." : res.Message;
                    _progressError.Visibility = Visibility.Visible;
                    return;
                }
                _progressError.Visibility = Visibility.Collapsed;
                ApplyCard(res.Value);
                // the timer only runs while the run is active: a finished status here is the transition
                if (!BacktestStatus.IsActive(res.Value.Status)) await LoadHistoryAsync();
            }
            finally
            {
                _polling = false;
            }
        }

        private void ShowProgress(BacktestCard card)
        {
            var p = card.Progress;
            _progressPanel.Visibility = Visibility.Visible;
            var phase = p?.Phase ?? card.Status ?? "running";
            // SPEC §10.3: "waiting" = connection lost, auto-resume probing; the backend's message
            // ("Connection lost — waiting for Alpaca (attempt n/5)…") is shown verbatim, Cancel stays available.
            var waiting = RunResume.IsWaiting(card.Status, p?.Phase);
            _progressPhase.Text = waiting
                ? "Backtest waiting — connection lost, resumes automatically when it is back"
                : "Backtest " + (card.Status ?? "running") + " — phase: " + PhaseLabel(phase)
                  + (p?.Percent is double pct ? " · " + pct.ToString("0", Inv) + " %" : "");
            _progressPhase.Foreground = waiting ? FundPalette.Risk : FundPalette.TextBright;
            _progressMessage.Text = string.IsNullOrWhiteSpace(p?.Message)
                ? (waiting ? "Waiting for the connection (the backend sent no progress message)." : "—")
                : p!.Message!;
            _progressMessage.Foreground = waiting ? FundPalette.Risk : FundPalette.Dim;
            AutomationProperties.SetName(_progressPanel, "Backtest progress: " + _progressPhase.Text + ". " + _progressMessage.Text);
            UpdateProgressBar(p?.Percent);
        }

        private static string PhaseLabel(string phase) => phase switch
        {
            "queued" => "queued",
            "data" => "loading data (incl. seam check)",
            "simulate" => "simulating",
            "metrics" => "computing metrics",
            "stress" => "stress tests",
            "saving" => "saving the run card",
            _ => phase,
        };

        private void UpdateProgressBar(double? percent)
        {
            var w = _progressTrack.ActualWidth;
            var p = percent is double v && !double.IsNaN(v) ? Math.Max(0, Math.Min(100, v)) : 0;
            _progressFill.Width = w > 0 ? w * p / 100.0 : 0;
        }

        private async Task CancelAsync()
        {
            var runId = _selectedRunId;
            if (runId == null) return;
            if (!FundDialogs.Confirm(Window.GetWindow(this), "Cancel backtest", "Cancel the running backtest? The run card is kept with status \"cancelled\".", "Cancel backtest", danger: true))
                return;
            _cancelButton.IsEnabled = false;
            var res = await PlatformClient.CancelBacktestAsync(runId);
            _cancelButton.IsEnabled = true;
            if (!res.Ok)
            {
                _progressError.Text = res.Message;
                _progressError.Visibility = Visibility.Visible;
                return;
            }
            _progressMessage.Text = "cancelling…";
            await PollAsync();
        }

        /// <summary>POST /v2/backtests/{id}/resume → shows the NEW run (resumed_from = id). A refusal (409) is shown verbatim.</summary>
        private async Task ResumeRunAsync(string runId, Button button)
        {
            button.IsEnabled = false;
            var newId = await RunResume.ResumeAsync(Window.GetWindow(this), runId, validation: false);
            if (_stopped) return;
            button.IsEnabled = true;
            if (newId == null) return;
            await SelectRunAsync(newId);
            await LoadHistoryAsync();
        }

        // =====================================================================================
        // Report
        // =====================================================================================

        private void ShowReportPlaceholder(string text)
        {
            _report.Children.Clear();
            if (_card != null && BacktestStatus.IsActive(_card.Status)) _report.Children.Add(Header(_card));
            _report.Children.Add(FundUi.Text(text, 11.5, FundPalette.Faint));
        }

        private void RenderReport(BacktestCard card)
        {
            _report.Children.Clear();
            _report.Children.Add(Header(card));

            if (card.Status != "done")
            {
                var text = card.Status switch
                {
                    "failed" => "The backtest failed: " + (string.IsNullOrWhiteSpace(card.Error) ? "(no message)" : card.Error) + RunResume.ErrorKindSuffix(card.ErrorKind),
                    "cancelled" => string.IsNullOrWhiteSpace(card.Error) ? "The backtest was cancelled." : card.Error!,
                    _ => "Status: " + (card.Status ?? "unknown") + (string.IsNullOrWhiteSpace(card.Error) ? "" : " — " + card.Error),
                };
                _report.Children.Add(ErrorBlock(text));
                return;
            }
            _report.Children.Add(ResearchLinks(card)); // MR8: "Open in Market Research" per symbol

            var metrics = card.Metrics;
            var unavailable = BtJson.Get(metrics, "unavailable");
            var seamDate = string.IsNullOrWhiteSpace(card.SeamDate) ? DefaultSeamDate : card.SeamDate!;
            var start = BtJson.Str(card.Range, "start");
            var end = BtJson.Str(card.Range, "end");
            var crossesSeam = string.CompareOrdinal(start ?? "", seamDate) < 0 && string.CompareOrdinal(end ?? "", seamDate) > 0;

            // ----- charts -----
            Section("Equity vs benchmark");
            var dates = (card.Curve?.Dates ?? new List<string>()).Select(ParseDate).ToList();
            var equity = card.Curve?.Equity ?? new List<double?>();
            var bench = card.Curve?.Benchmark ?? new List<double?>();
            var benchSymbol = BtJson.Str(metrics, "benchmark", "symbol") ?? BtJson.Str(card.CostModel, "benchmark") ?? BtJson.Str(card.Request, "benchmark") ?? "benchmark";
            var capital = BtJson.Num(card.Request, "capital") ?? BtJson.Num(metrics, "performance", "start_equity");
            DateTime? seam = crossesSeam ? ParseDate(seamDate) : null;
            var xMin = dates.FirstOrDefault(d => d.HasValue);
            var xMax = dates.LastOrDefault(d => d.HasValue);
            var cursorGroup = new ChartCursorGroup();
            var eqChart = new BacktestLineChart(drawdown: false) { Height = 230, CursorGroup = cursorGroup };
            eqChart.SetData(Zip(dates, equity), "Strategy", Zip(dates, bench), benchSymbol, capital, seam,
                            "seam " + seamDate + " · Tiingo | Alpaca", "No equity curve in this run card.", xMin, xMax,
                            "Equity chart: strategy equity against " + benchSymbol + " buy-and-hold"
                            + (crossesSeam ? ", data seam on " + seamDate + " marked" : ""));
            _report.Children.Add(FundUi.Panel(eqChart, new Thickness(8, 8, 8, 6), new Thickness(0, 0, 0, 4)));
            _report.Children.Add(Line("Blue: strategy equity · grey dashed: " + benchSymbol + " buy-and-hold (same costs and dividends) · hairline: starting capital · hover or focus a chart (arrow keys) for values"
                                      + (crossesSeam ? " · amber: data seam " + seamDate + " (before: Tiingo, scaled at the seam; from it: Alpaca)" : ""), 10.5, FundPalette.Faint));

            Section("Drawdown");
            var ddChart = new BacktestLineChart(drawdown: true) { Height = 130, CursorGroup = cursorGroup };
            ddChart.SetTooltipExtra("Equity", Zip(dates, equity));
            ddChart.SetData(Drawdown(dates, equity), "Drawdown", null, null, null, seam, "seam", "No equity curve in this run card.", xMin, xMax,
                            "Drawdown chart: percent below the running equity peak");
            _report.Children.Add(FundUi.Panel(ddChart, new Thickness(8, 8, 8, 6), new Thickness(0, 0, 0, 4)));
            _report.Children.Add(Line("Percent below the running peak of the strategy equity (computed from the curve; days without a valuation are skipped).", 10.5, FundPalette.Faint));

            // ----- metrics -----
            Section("Metrics");
            var perf = BtJson.Get(metrics, "performance");
            var grid = new WrapPanel();
            grid.Children.Add(MetricGroup("Performance", unavailable, new[]
            {
                M("Total return", "performance", "total_return", perf, Fmt.Fraction),
                M("CAGR", "performance", "cagr", perf, Fmt.Fraction),
                M("Volatility (ann.)", "performance", "volatility", perf, Fmt.Fraction),
                M("Sharpe (vs 0 %)", "performance", "sharpe", perf, Fmt.Ratio),
                M("Sortino", "performance", "sortino", perf, Fmt.Ratio),
                M("Calmar", "performance", "calmar", perf, Fmt.Ratio),
                M("Max drawdown", "performance", "max_drawdown", perf, Fmt.Fraction),
                M("Max DD duration (days)", "performance", "max_drawdown_duration_days", perf, Fmt.Int),
                M("Best day", "performance", "best_day", perf, Fmt.Fraction),
                M("Worst day", "performance", "worst_day", perf, Fmt.Fraction),
                M("End equity", "performance", "end_equity", perf, Fmt.Money),
            }));
            grid.Children.Add(MetricGroup("Hit rate & profit factor", unavailable, new[]
            {
                M("Hit rate (daily)", "performance", "hit_rate_daily", perf, Fmt.Fraction),
                M("Profit factor (daily)", "performance", "profit_factor_daily", perf, Fmt.Ratio),
                M("Hit rate (per trade)", "performance", "hit_rate_trades", perf, Fmt.Fraction),
                M("Profit factor (per trade)", "performance", "profit_factor_trades", perf, Fmt.Ratio),
                M("Avg. trade P&L", "performance", "avg_trade_pnl", perf, Fmt.Money),
                M("Closed trades", "performance", "trades_closed", perf, Fmt.Int),
                M("Open at end (excluded)", "performance", "trades_open", perf, Fmt.Int),
            }));
            var expo = BtJson.Get(metrics, "exposure");
            var bm = BtJson.Get(metrics, "benchmark");
            grid.Children.Add(MetricGroup("Exposure & benchmark (" + benchSymbol + ")", unavailable, new[]
            {
                M("Avg. gross exposure", "exposure", "avg_gross_exposure", expo, Fmt.Fraction),
                M("Avg. net exposure", "exposure", "avg_net_exposure", expo, Fmt.Fraction),
                M("Turnover (one-sided / yr)", "exposure", "turnover_annual", expo, Fmt.Times),
                M("Benchmark total return", "benchmark", "total_return", bm, Fmt.Fraction),
                M("Benchmark CAGR", "benchmark", "cagr", bm, Fmt.Fraction),
                M("Benchmark max drawdown", "benchmark", "max_drawdown", bm, Fmt.Fraction),
                M("Benchmark Sharpe", "benchmark", "sharpe", bm, Fmt.Ratio),
                M("Beta", "benchmark", "beta", bm, Fmt.Ratio),
                M("Alpha (ann., vs 0 %)", "benchmark", "alpha_annual", bm, Fmt.Fraction),
                M("Correlation", "benchmark", "correlation", bm, Fmt.Ratio),
            }));
            var costs = BtJson.Get(metrics, "costs");
            grid.Children.Add(MetricGroup("Costs", unavailable, new[]
            {
                M("Fees", "costs", "fees", costs, Fmt.Money),
                M("Slippage", "costs", "slippage", costs, Fmt.Money),
                M("Borrow", "costs", "borrow", costs, Fmt.Money),
                M("Funding", "costs", "funding", costs, Fmt.Money),
                M("Liquidation fees", "costs", "liquidation_fees", costs, Fmt.Money),
                M("Total costs", "costs", "total", costs, Fmt.Money),
                M("Costs / gross P&L", "costs", "pct_of_gross_pnl", costs, Fmt.Percent),
                M("Gross P&L (before costs)", "costs", "gross_pnl", costs, Fmt.Money),
                M("Net P&L", "costs", "net_pnl", costs, Fmt.Money),
                M("Dividends received", "costs", "dividends_received", costs, Fmt.Money),
                M("Dividends paid (shorts)", "costs", "dividends_paid_short", costs, Fmt.Money),
            }));
            var cap = BtJson.Get(metrics, "capacity");
            if (cap != null)
            {
                // live click-test 2026-10-22: at_most is set only when the 1 % limit already binds at the tested capital (backtest/metrics.py
                // capacity); otherwise the row says why it is empty instead of a bare dash
                var capUsd = BtJson.Num(cap, "capacity_usd");
                var capReason = BtJson.Str(cap, "reason");
                Func<double?, string> atMostFormat = v => v is double
                    ? FundFormat.Money(v)
                    : FundFormat.Dash + (capUsd != null ? " (not binding: the 1 % limit cut too few orders at the tested capital)"
                                         : capReason != null ? " (see the note below)" : " (not reported)");
                grid.Children.Add(MetricGroup("Capacity", unavailable, new[]
                {
                    M("Capacity estimate", "capacity", "capacity_usd", cap, Fmt.Money),
                    M("At most", "capacity", "at_most", cap, atMostFormat),
                    M("Orders", "capacity", "orders", cap, Fmt.Int),
                    M("Orders cut at the 1 % limit", "capacity", "orders_at_limit", cap, Fmt.Int),
                    M("Orders without volume", "capacity", "orders_without_volume", cap, Fmt.Int),
                }, BtJson.Str(cap, "reason")));
            }
            _report.Children.Add(grid);

            RenderAttribution(card);
            RenderSignalQuality(BtJson.Get(metrics, "signal_quality"));
            RenderStress(card.Stress);
            RenderWarnings(card.Warnings);
            RenderData(card, seamDate, crossesSeam);
            RenderTrades(card.Trades);

            var note = FundUi.Text("Hypothetical, simulated results on historical data — they do not indicate future results. Static universe: survivorship bias applies.", 10.5, FundPalette.Faint);
            note.FontStyle = FontStyles.Italic;
            note.Margin = new Thickness(0, 14, 0, 0);
            _report.Children.Add(note);
        }

        /// <summary>SPEC §11.3: the Attribution section (layer 1, computed, no AI) with "Explain with AI…" (layer 2) in its
        /// header; the AI panel (options, stored explanations) follows the section. Runs without an attribution show the
        /// backend's reason (§11.4) and the button is disabled with it.</summary>
        private void RenderAttribution(BacktestCard card)
        {
            var explain = new ExplainPanel(validation: false, card.Id, card.Status, card.Attribution != null, card.AttributionUnavailable);
            _report.Children.Add(new AttributionView(card.Attribution, card.AttributionUnavailable, "this backtest run", explain.TriggerButton));
            _report.Children.Add(explain);
        }

        private FrameworkElement Header(BacktestCard card)
        {
            var stack = new StackPanel { Margin = new Thickness(0, 0, 0, 6) };
            var title = new StackPanel { Orientation = Orientation.Horizontal };
            title.Children.Add(FundUi.Text((card.StrategyName ?? _strategyName ?? card.StrategyId ?? "Strategy") + " · v" + (card.Version?.ToString(Inv) ?? "?"), 15, FundPalette.TextBright, bold: true, wrap: false));
            var chip = StatusChip(card.Status);
            chip.Margin = new Thickness(10, 2, 0, 0);
            title.Children.Add(chip);
            if (!string.IsNullOrWhiteSpace(card.Id) && RunResume.CanResume(card.Status, card.Resumable, card.Kind))
            {
                var runId = card.Id!;
                var resume = RunResume.MakeResumeButton(runId);
                resume.Margin = new Thickness(12, 0, 0, 0);
                resume.VerticalAlignment = VerticalAlignment.Center;
                resume.Click += async (_, _) => await ResumeRunAsync(runId, resume);
                title.Children.Add(resume);
            }
            stack.Children.Add(title);
            if (FundUi.FixtureLine(card.Fixture, card.FixtureNote, 12.5, new Thickness(0, 4, 0, 4)) is TextBlock fixture) stack.Children.Add(fixture);
            var req = card.Request;
            var reqText = "Requested: " + (BtJson.Str(req, "start")
                                           ?? "default start (data from " + (BtJson.Str(req, "history_from") == "2000" ? EarliestDataDate : DefaultSeamDate)
                                              + ", trading after the warm-up)") + " … " + (BtJson.Str(req, "end") ?? "last complete session")
                          + " · benchmark " + (BtJson.Str(req, "benchmark") ?? "spec / SPY")
                          + " · capital " + (BtJson.Num(req, "capital") is double c ? FundFormat.Money(c, decimals: 0) : "$100,000 (default)")
                          + " · stress " + (BtJson.Bool(req, "stress") == true ? "on" : "off")
                          + (card.AutoResume == true ? " · auto-resume on (max " + RunResume.MaxAutoResumes.ToString(Inv) + ")" : "");
            stack.Children.Add(Line(reqText, 11, FundPalette.Dim, mono: true));
            if (card.Range != null) stack.Children.Add(Line("Simulated: " + RangeText(card.Range, null), 11, FundPalette.Dim, mono: true));
            // Stage 4 (SPEC §9.8): range.holdout = {universe_kind, start, end, training_end, access}
            if (BtJson.Get(card.Range, "holdout") is JsonElement hl)
                stack.Children.Add(Line("Locked holdout window " + (BtJson.Str(hl, "start") ?? "?") + " … " + (BtJson.Str(hl, "end") ?? "?")
                                        + " · training end " + (BtJson.Str(hl, "training_end") ?? "?")
                                        + (BtJson.Bool(hl, "access") == true ? " · holdout access (holdout run)" : ""), 10.5, FundPalette.Faint, mono: true));
            stack.Children.Add(Line("Run " + (card.Id ?? "?") + " · created " + FundFormat.LocalTime(card.CreatedAt, "yyyy-MM-dd HH:mm")
                                    + (card.FinishedAt != null ? " · finished " + FundFormat.LocalTime(card.FinishedAt, "yyyy-MM-dd HH:mm") : "")
                                    + " · engine " + (card.EngineVersion ?? "—") + " · seed " + (card.Seed?.ToString(Inv) ?? "—")
                                    + " · content " + Short(card.ContentHash), 10.5, FundPalette.Faint, mono: true));
            if (RunResume.ResumedText(card.ResumedFrom, card.ResumeCount) is string resumed)
                stack.Children.Add(Line(char.ToUpperInvariant(resumed[0]) + resumed[1..], 10.5, FundPalette.Dim, mono: true));
            return stack;
        }

        private void RenderSignalQuality(JsonElement? sq)
        {
            Section("Signal quality");
            if (sq == null)
            {
                _report.Children.Add(Line("Not computed for this run.", 11, FundPalette.Faint));
                return;
            }
            var mode = BtJson.Str(sq, "mode");
            if (mode == "cross_sectional")
            {
                var bars = BtJson.Array(sq, "decay")
                    .Select(d => ("lag " + (BtJson.Str(d, "lag") ?? "?"), BtJson.Num(d, "mean_ic")))
                    .ToList();
                var chart = new IcDecayChart { Height = 130 };
                chart.SetData(bars);
                AutomationProperties.SetName(chart, "IC decay chart: mean rank IC of the signal against the 1-day return at lags "
                                                    + string.Join(", ", bars.Select(b => b.Item1 + " = " + (b.Item2 is double v ? v.ToString("0.000", Inv) : "not available"))));
                _report.Children.Add(FundUi.Panel(chart, new Thickness(8, 8, 8, 6), new Thickness(0, 0, 0, 4)));
                var hl = BtJson.Num(sq, "half_life_days");
                _report.Children.Add(Line("Cross-sectional daily Spearman rank IC (" + (BtJson.Str(sq, "symbols") ?? "?") + " symbols) · IC decay half-life: "
                                          + (hl is double h ? h.ToString("0.0", Inv) + " days" : "— " + (BtJson.Str(sq, "half_life_reason") ?? "")), 10.5, FundPalette.Faint));
                var rows = new List<string[]>();
                foreach (var hz in BtJson.Array(sq, "horizons"))
                {
                    rows.Add(new[]
                    {
                        (BtJson.Str(hz, "horizon") ?? "?") + " d",
                        Fmt.Ic(BtJson.Num(hz, "mean_ic")),
                        Fmt.Ic(BtJson.Num(hz, "icir")),
                        FundFormat.Num(BtJson.Num(hz, "t_stat")),
                        BtJson.Str(hz, "days") ?? "—",
                        BtJson.Str(hz, "reason") ?? "",
                    });
                }
                if (rows.Count > 0)
                    _report.Children.Add(Table(new[] { "Horizon", "Mean IC", "ICIR (daily)", "t-stat", "Days", "Note" },
                                               new[] { 70.0, 80, 90, 70, 60, -1 }, rows, "Rank IC by forward-return horizon"));
            }
            else if (mode == "time_series")
            {
                var share = BtJson.Num(sq, "positive_share");
                _report.Children.Add(FundUi.KeyValue("Positive share of rolling 13-week windows", share is double s ? FundFormat.Fraction(s) : "—"));
                _report.Children.Add(FundUi.KeyValue("Mean correlation (signal vs next-day return)", Fmt.Ic(BtJson.Num(sq, "mean_corr"))));
                _report.Children.Add(FundUi.KeyValue("Windows · window length", (BtJson.Str(sq, "windows") ?? "—") + " · " + (BtJson.Str(sq, "window_days") ?? "—") + " days"));
                if (BtJson.Str(sq, "reason") is { Length: > 0 } why) _report.Children.Add(Line(why, 11, FundPalette.Risk));
                _report.Children.Add(Line("Time-series mode (fewer than 5 symbols): no cross-sectional IC.", 10.5, FundPalette.Faint));
            }
            else
            {
                _report.Children.Add(Line("Not available: " + (BtJson.Str(sq, "reason") ?? "no reason given"), 11, FundPalette.Risk));
            }
        }

        private void RenderStress(JsonElement? stress)
        {
            Section("Stress tests");
            if (stress == null)
            {
                _report.Children.Add(Line("Not requested for this run.", 11, FundPalette.Faint));
                return;
            }
            var rows = new List<string[]>();
            var brushes = new List<Brush>();
            var details = new List<string>();   // per scenario: code → symbols (expander below the table, never one line per warning)
            JsonElement? mc = null;
            foreach (var sc in BtJson.Array(stress, "scenarios"))
            {
                if (BtJson.Str(sc, "kind") == "monte_carlo" || BtJson.Str(sc, "key") == "monte_carlo") { mc = sc; continue; }
                var status = BtJson.Str(sc, "status") ?? "—";
                var win = BtJson.Str(sc, "window", "start") is string ws ? ws + " … " + (BtJson.Str(sc, "window", "end") ?? "?") : "—";
                if (status != "done")
                {
                    rows.Add(new[] { BtJson.Str(sc, "label") ?? BtJson.Str(sc, "key") ?? "?", win, BtJson.Str(sc, "reason") ?? status, "", "", "", "" });
                    brushes.Add(status == "failed" ? FundPalette.Kill : FundPalette.Faint);
                    continue;
                }
                var m = BtJson.Get(sc, "metrics");
                var delta = BtJson.Get(m, "delta_vs_base");
                rows.Add(new[]
                {
                    BtJson.Str(sc, "label") ?? BtJson.Str(sc, "key") ?? "?",
                    win,
                    "done" + StressStatusText(sc, details),
                    FundFormat.Fraction(BtJson.Num(m, "strategy", "total_return"), 1, signed: true)
                        + (delta != null ? " (Δ " + FundFormat.Fraction(BtJson.Num(delta, "total_return"), 1, signed: true) + ")" : ""),
                    FundFormat.Fraction(BtJson.Num(m, "strategy", "max_drawdown")),
                    FundFormat.Num(BtJson.Num(m, "strategy", "sharpe")),
                    FundFormat.Fraction(BtJson.Num(m, "benchmark", "total_return"), 1, signed: true),
                });
                brushes.Add(FundPalette.Text);
            }
            if (rows.Count > 0)
                _report.Children.Add(Table(new[] { "Scenario", "Window", "Status", "Return", "Max DD", "Sharpe", "Benchmark" },
                                           new[] { 150.0, 170, -1, 120, 70, 60, 80 }, rows, "Stress scenarios", brushes));
            if (details.Count > 0)
            {
                var body = new StackPanel();
                foreach (var d in details)
                {
                    var t = FundUi.Text(d, 10.5, FundPalette.Dim, mono: true);
                    t.Margin = new Thickness(0, 1, 0, 3);
                    body.Children.Add(t);
                }
                var exp = new Expander
                {
                    Header = FundUi.Text("Stress warnings — symbols per code (" + details.Count.ToString(Inv) + ")", 10.5, FundPalette.Faint),
                    Content = body,
                    IsExpanded = false,
                    Margin = new Thickness(0, 0, 0, 6),
                };
                AutomationProperties.SetName(exp, "Stress warnings: symbols per warning code");
                _report.Children.Add(exp);
            }
            if (mc is JsonElement mcs)
            {
                var status = BtJson.Str(mcs, "status");
                var label = "Monte-Carlo bootstrap (" + (BtJson.Str(mcs, "paths") ?? "1000") + " paths, block " + (BtJson.Str(mcs, "block_length") ?? "—")
                            + ", seed " + (BtJson.Str(mcs, "seed") ?? BtJson.Str(stress, "seed") ?? "—") + ")";
                if (status == "done")
                {
                    var m = BtJson.Get(mcs, "metrics");
                    var mrows = new List<string[]>
                    {
                        new[] { "Max drawdown", FundFormat.Fraction(BtJson.Num(m, "max_drawdown", "p5")), FundFormat.Fraction(BtJson.Num(m, "max_drawdown", "p50")), FundFormat.Fraction(BtJson.Num(m, "max_drawdown", "p95")) },
                        new[] { "CAGR", FundFormat.Fraction(BtJson.Num(m, "cagr", "p5"), 1, signed: true), FundFormat.Fraction(BtJson.Num(m, "cagr", "p50"), 1, signed: true), FundFormat.Fraction(BtJson.Num(m, "cagr", "p95"), 1, signed: true) },
                    };
                    _report.Children.Add(Line(label, 11, FundPalette.Dim));
                    _report.Children.Add(Table(new[] { "Metric", "5 %", "50 %", "95 %" }, new[] { 140.0, 90, 90, 90 }, mrows, "Monte-Carlo percentiles"));
                }
                else
                {
                    _report.Children.Add(Line(label + ": " + (BtJson.Str(mcs, "reason") ?? status ?? "—"), 11, status == "failed" ? FundPalette.Kill : FundPalette.Faint));
                }
            }
            if (rows.Count == 0 && mc == null) _report.Children.Add(Line("The stress block has no scenarios.", 11, FundPalette.Faint));
        }

        /// <summary>
        /// Live click-test 2026-10-22: the status of a done stress scenario. The backend's short summary text when it sends one
        /// (warning_summary.text, backtest/stress.py); without the field: "warning summary not available (backend too old)".
        /// The symbols go to <paramref name="details"/> (one wrapped line per code) — never hundreds of lines in the table.
        /// </summary>
        private static string StressStatusText(JsonElement sc, List<string> details)
        {
            var label = BtJson.Str(sc, "label") ?? BtJson.Str(sc, "key") ?? "?";
            if (BtJson.Str(sc, "warning_summary", "text") is { Length: > 0 } shortText)
            {
                // backend (backtest/stress.py warning_summary): {total, counts {code: n}, symbols {code: [symbols]}, text}
                foreach (var p in BtJson.Props(sc, "warning_summary", "symbols"))
                {
                    var symbols = BtJson.Array(p.Value).Select(x => BtJson.Str(x) ?? "").Where(x => x.Length > 0).ToList();
                    if (symbols.Count > 0) details.Add(label + " · " + p.Name + " (" + symbols.Count.ToString(Inv) + "): " + string.Join(", ", symbols));
                }
                return " · " + shortText;
            }
            // review 2026-10-22: no client-side grouping of the raw warnings — the summary comes from the backend only
            if (BtJson.Get(sc, "warning_summary") == null) return " · warning summary not available (backend too old)";
            return "";   // warning_summary present with an empty text: no warnings
        }

        private void RenderWarnings(List<BacktestWarning>? warnings)
        {
            Section("Warnings");
            if (warnings == null || warnings.Count == 0)
            {
                _report.Children.Add(Line("None.", 11, FundPalette.Faint));
                return;
            }
            foreach (var w in warnings)
            {
                var text = (w.Code ?? "warning") + " — " + (w.Message ?? "") + (w.Count is int n && n > 1 ? "  (×" + n.ToString(Inv) + ")" : "");
                var t = Line(text, 11, w.Code == "survivorship" ? FundPalette.Dim : FundPalette.Risk);
                AutomationProperties.SetName(t, "Warning: " + text);
                _report.Children.Add(t);
            }
        }

        private void RenderData(BacktestCard card, string seamDate, bool crossesSeam)
        {
            Section("Data, licences & seam");
            var rng = card.Range;
            if (BtJson.Get(rng, "end_limited_by") is JsonElement lim)
                _report.Children.Add(Line("End limited by " + (BtJson.Str(lim, "symbol") ?? "?") + " (" + (BtJson.Str(lim, "source") ?? "?") + ") — last data " + (BtJson.Str(lim, "date") ?? "?"), 11, FundPalette.Risk));
            _report.Children.Add(Line("Calendar " + (BtJson.Str(rng, "calendar") ?? "—") + " · data loaded from " + (BtJson.Str(rng, "load_start") ?? "—")
                                      + " (first data " + (BtJson.Str(rng, "first_data") ?? "—") + ", warm-up " + (BtJson.Str(rng, "warmup_sessions") ?? "—") + " sessions) · execution "
                                      + (BtJson.Str(rng, "timing") ?? "—"), 11, FundPalette.Dim, mono: true));
            _report.Children.Add(Line(crossesSeam
                ? "The range crosses the data seam " + seamDate + ": daily prices before it come from Tiingo, scaled by a constant factor after an overlap check against Alpaca (60 sessions); from the seam on, Alpaca (SIP history). A symbol whose check failed has no pre-seam data (warning seam_mismatch)."
                : "Seam " + seamDate + ": this range does not cross it — one price source per symbol.", 10.5, FundPalette.Faint));

            var rows = new List<string[]>();
            var brushes = new List<Brush>();
            foreach (var d in BtJson.Array(card.Data))
            {
                var ranges = string.Join("; ", BtJson.Array(d, "prices", "ranges").Select(r =>
                    (BtJson.Str(r, "source") ?? "?") + " " + (BtJson.Str(r, "first") ?? "?") + "…" + (BtJson.Str(r, "last") ?? "?") + " (" + (BtJson.Str(r, "rows") ?? "?") + ")"));
                var vol = BtJson.Get(d, "volume") is JsonElement v ? (BtJson.Str(v, "source") ?? "?") + " (" + (BtJson.Str(v, "rows") ?? "?") + ")" : "—";
                var seams = string.Join("; ", BtJson.Array(d, "seam").Select(sm =>
                    (BtJson.Str(sm, "adjustment") ?? "?") + ": " + (BtJson.Bool(sm, "ok") == true ? "ok" : BtJson.Bool(sm, "ok") == false ? "FAILED" : "?")
                    + (BtJson.Num(sm, "factor") is double f ? " ×" + f.ToString("0.######", Inv) : "")));
                var excluded = BtJson.Bool(d, "excluded") == true;
                rows.Add(new[]
                {
                    BtJson.Str(d, "symbol") ?? "?",
                    (BtJson.Str(d, "role") ?? "—") + (excluded ? " · excluded" : ""),
                    ranges.Length > 0 ? ranges : "—",
                    vol,
                    BtJson.Str(d, "dividends") ?? "—",
                    seams.Length > 0 ? seams : "—",
                });
                brushes.Add(excluded || seams.Contains("FAILED") ? FundPalette.Risk : FundPalette.Text);
            }
            if (rows.Count > 0)
                _report.Children.Add(Table(new[] { "Symbol", "Role", "Prices (source first…last (rows))", "Volume", "Dividends", "Seam check" },
                                           new[] { 80.0, 120, -1, 110, 70, 170 }, rows, "Data per symbol", brushes));
            else
                _report.Children.Add(Line("No data snapshot in this run card.", 11, FundPalette.Faint));

            var lic = BtJson.Props(card.Licences).ToList();
            if (lic.Count > 0)
            {
                var lrows = lic.Select(p => new[] { p.Name, BtJson.Str(p.Value, "licence") ?? FundFormat.Plain(p.Value), BtJson.Str(p.Value, "note") ?? "" }).ToList();
                _report.Children.Add(Table(new[] { "Source", "Licence", "Note" }, new[] { 110.0, 130, -1 }, lrows, "Data licences"));
            }
            var cm = card.CostModel;
            if (cm != null)
            {
                _report.Children.Add(Line("Cost model: " + (BtJson.Str(cm, "costs", "model") ?? "—") + " · equity fees " + (BtJson.Str(cm, "costs", "fees_bps") ?? "—")
                                          + " bps · crypto fees " + (BtJson.Str(cm, "costs", "crypto_fees_bps") ?? "—") + " bps (half fee, half slippage per fill)", 10.5, FundPalette.Faint, mono: true));
            }
        }

        private void RenderTrades(JsonElement? trades)
        {
            if (trades == null) return;
            Section("Trades");
            var status = string.Join(", ", BtJson.Props(trades, "orders_by_status").Select(p => p.Name + " " + FundFormat.Plain(p.Value)));
            _report.Children.Add(Line("Fills " + (BtJson.Str(trades, "fills") ?? "—") + " · orders " + (BtJson.Str(trades, "orders") ?? "—")
                                      + (status.Length > 0 ? " (" + status + ")" : "") + " · margin events " + (BtJson.Str(trades, "margin_events") ?? "—")
                                      + " · first fill " + (BtJson.Str(trades, "first_fill") ?? "—") + " · last fill " + (BtJson.Str(trades, "last_fill") ?? "—"), 11, FundPalette.Dim, mono: true));
        }

        // =====================================================================================
        // Small builders
        // =====================================================================================

        private sealed record MetricRow(string Label, string Group, string Key, JsonElement? Block, Func<double?, string> Format);

        private static MetricRow M(string label, string group, string key, JsonElement? block, Func<double?, string> format)
            => new(label, group, key, block, format);

        private static class Fmt
        {
            public static readonly Func<double?, string> Fraction = v => FundFormat.Fraction(v, 2);
            public static readonly Func<double?, string> Percent = v => FundFormat.Pct(v, 1);   // already in percent units
            public static readonly Func<double?, string> Ratio = v => FundFormat.Num(v, 2);
            public static readonly Func<double?, string> Int = v => FundFormat.Num(v, 0);
            public static readonly Func<double?, string> Money = v => FundFormat.Money(v);
            public static readonly Func<double?, string> Times = v => v is double x ? FundFormat.Num(x, 2) + "×" : FundFormat.Dash;
            public static string Ic(double? v) => v is double x && !double.IsNaN(x) ? x.ToString("0.0000", Inv) : FundFormat.Dash;
        }

        private static Border MetricGroup(string title, JsonElement? unavailable, IEnumerable<MetricRow> rows, string? note = null)
        {
            var stack = new StackPanel();
            var t = FundUi.Text(title, 11, FundPalette.Text, bold: true);
            t.Margin = new Thickness(0, 0, 0, 6);
            stack.Children.Add(t);
            foreach (var row in rows)
            {
                var value = BtJson.Num(row.Block, row.Key);
                var reason = value == null ? BtJson.Str(unavailable, row.Group + "." + row.Key) : null;
                var kv = FundUi.KeyValue(row.Label, row.Format(value), value == null ? FundPalette.Faint : FundPalette.Text, 11);
                AutomationProperties.SetName(kv, row.Label + ": " + row.Format(value) + (reason != null ? " (not available: " + reason + ")" : ""));
                stack.Children.Add(kv);
                if (reason != null)
                {
                    var r = FundUi.Text("not available: " + reason, 10, FundPalette.Faint);
                    r.Margin = new Thickness(8, 0, 0, 3);
                    stack.Children.Add(r);
                }
            }
            if (!string.IsNullOrWhiteSpace(note))
            {
                var n = FundUi.Text(note, 10, FundPalette.Faint);
                n.Margin = new Thickness(0, 4, 0, 0);
                stack.Children.Add(n);
            }
            var panel = FundUi.Panel(stack, new Thickness(12, 10, 12, 10), new Thickness(0, 0, 10, 10));
            panel.Width = 320;
            panel.VerticalAlignment = VerticalAlignment.Top;
            AutomationProperties.SetName(panel, title);
            return panel;
        }

        /// <summary>Plain table: header row + text rows; width -1 = star column. All cells are plain text.</summary>
        private static Border Table(string[] headers, double[] widths, List<string[]> rows, string name, List<Brush>? rowBrushes = null)
        {
            var grid = new Grid();
            foreach (var w in widths)
                grid.ColumnDefinitions.Add(new ColumnDefinition { Width = w < 0 ? new GridLength(1, GridUnitType.Star) : new GridLength(w) });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            for (var c = 0; c < headers.Length && c < widths.Length; c++)
            {
                var h = FundUi.Text(headers[c], 10.5, FundPalette.Faint, bold: true);
                h.Margin = new Thickness(0, 0, 8, 4);
                Grid.SetColumn(h, c);
                grid.Children.Add(h);
            }
            for (var r = 0; r < rows.Count; r++)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var brush = rowBrushes != null && r < rowBrushes.Count ? rowBrushes[r] : FundPalette.Text;
                for (var c = 0; c < rows[r].Length && c < widths.Length; c++)
                {
                    var cell = FundUi.Text(rows[r][c], 11, c == 0 ? FundPalette.TextBright : brush, mono: c > 0);
                    cell.Margin = new Thickness(0, 2, 8, 2);
                    Grid.SetRow(cell, r + 1);
                    Grid.SetColumn(cell, c);
                    grid.Children.Add(cell);
                }
            }
            var panel = FundUi.Panel(grid, new Thickness(12, 8, 12, 8), new Thickness(0, 2, 0, 8));
            AutomationProperties.SetName(panel, name);
            return panel;
        }

        private void Section(string title)
        {
            var t = FundUi.Text(title, 12, FundPalette.Text, bold: true);
            t.Margin = new Thickness(0, 14, 0, 6);
            _report.Children.Add(t);
        }

        /// <summary>
        /// MR8: one button per symbol of the run → the Hedge Fund window's Research section with that symbol and the run's
        /// entries/exits, best/worst periods and drawdowns as chart markers (read-only; older run cards without stored fills
        /// show the backend's "unavailable" reason there).
        /// </summary>
        private UIElement ResearchLinks(BacktestCard card)
        {
            var panel = new WrapPanel { Margin = new Thickness(0, 8, 0, 8) };
            panel.Children.Add(Line("Open in Market Research:", 11, FundPalette.Dim));
            var runId = _selectedRunId;
            var symbols = BtJson.Array(card.Data)
                .Where(d => BtJson.Bool(d, "excluded") != true)
                .Select(d => BtJson.Str(d, "symbol"))
                .Where(s => !string.IsNullOrWhiteSpace(s))
                .Select(s => s!.Trim().ToUpperInvariant())
                .Distinct()
                .Take(40)
                .ToList();
            if (symbols.Count == 0)
            {
                panel.Children.Add(Line(" no symbols in this run card's data snapshot.", 11, FundPalette.Faint));
                return panel;
            }
            foreach (var symbol in symbols)
            {
                var button = FundUi.MakeButton(symbol);
                button.Margin = new Thickness(6, 0, 0, 4);
                button.Padding = new Thickness(8, 2, 8, 2);
                button.ToolTip = "Open " + symbol + " in Market Research with this run's entries/exits, best/worst periods and drawdowns on the chart." +
                                 " Older run cards (before 2026-10-17) have no stored entries/exits — Research says so.";
                AutomationProperties.SetName(button, "Open " + symbol + " in Market Research with the backtest markers");
                button.Click += (_, _) =>
                {
                    var hedgeFund = FindHedgeFundWindow(Window.GetWindow(this));
                    if (hedgeFund == null)
                    {
                        UFOS.ai.Logging.AppLog.Warn("HedgeFund", "Open in Market Research: no Hedge Fund window found for the backtest report.");
                        return;
                    }
                    hedgeFund.OpenInResearch(symbol, runId);
                };
                panel.Children.Add(button);
            }
            return panel;
        }

        private static HedgeFundWindow? FindHedgeFundWindow(Window? window)
        {
            for (var w = window; w != null; w = w.Owner)
                if (w is HedgeFundWindow hf) return hf;
            return Application.Current?.Windows.OfType<HedgeFundWindow>().FirstOrDefault();
        }

        private static TextBlock Line(string text, double size, Brush brush, bool mono = false)
        {
            var t = FundUi.Text(text, size, brush, mono: mono);
            t.Margin = new Thickness(0, 1, 0, 2);
            return t;
        }

        private static Border StatusChip(string? status)
        {
            var brush = status switch
            {
                "done" => FundPalette.Long,
                "failed" => FundPalette.Kill,
                "cancelled" => FundPalette.Neutral,
                "running" or "queued" => FundPalette.Focus,
                RunResume.WaitingStatus => FundPalette.Risk,
                _ => FundPalette.Dim,
            };
            var text = FundUi.Text(status ?? "unknown", 9.5, brush, bold: true, wrap: false);
            return new Border
            {
                BorderBrush = brush,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(999),
                Padding = new Thickness(6, 0, 6, 1),
                VerticalAlignment = VerticalAlignment.Center,
                Child = text,
            };
        }

        private static Border ErrorBlock(string message)
        {
            var text = FundUi.Text(message, 11.5, FundPalette.Make(0xFFF4A197));
            return new Border
            {
                Background = FundPalette.KillSurface,
                BorderBrush = FundPalette.Kill,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 6, 10, 6),
                Margin = new Thickness(0, 4, 0, 8),
                Child = text,
            };
        }

        private static string RangeText(JsonElement? range, JsonElement? request)
        {
            var start = BtJson.Str(range, "start") ?? BtJson.Str(request, "start");
            var end = BtJson.Str(range, "end") ?? BtJson.Str(request, "end");
            if (start == null && end == null) return "range pending";
            return (start ?? "default") + " … " + (end ?? "last session")
                   + (BtJson.Str(range, "calendar") is string cal ? " · " + cal : "");
        }

        private static DateTime? ParseDate(string? iso)
            => !string.IsNullOrWhiteSpace(iso) && iso!.Length >= 10 &&
               DateTime.TryParseExact(iso[..10], "yyyy-MM-dd", Inv, DateTimeStyles.None, out var d) ? d : null;

        private static IEnumerable<(DateTime X, double? Y)> Zip(List<DateTime?> dates, List<double?> values)
        {
            var n = Math.Min(dates.Count, values.Count);
            for (var i = 0; i < n; i++)
                if (dates[i] is DateTime d) yield return (d, values[i]);
        }

        /// <summary>Percent below the running peak (≤ 0); days without a valuation are skipped.</summary>
        private static IEnumerable<(DateTime X, double? Y)> Drawdown(List<DateTime?> dates, List<double?> equity)
        {
            double peak = 0;
            var n = Math.Min(dates.Count, equity.Count);
            for (var i = 0; i < n; i++)
            {
                if (dates[i] is not DateTime d || equity[i] is not double e || double.IsNaN(e) || e <= 0) continue;
                if (e > peak) peak = e;
                yield return (d, (e / peak - 1.0) * 100.0);
            }
        }

        private static string Short(string? hash) => string.IsNullOrEmpty(hash) ? "—" : hash!.Length > 12 ? hash[..12] : hash;
    }

    /// <summary>
    /// Window hosting <see cref="BacktestReportView"/> (FundPopupWindow chrome: borderless, resizable,
    /// drag-to-restore, square corners when maximised). One window per strategy: opening a run of a
    /// strategy whose report is already open selects that run there.
    /// </summary>
    internal sealed class BacktestReportWindow : FundPopupWindow
    {
        private static readonly List<BacktestReportWindow> OpenWindows = new();
        private readonly BacktestReportView _view;

        private BacktestReportWindow(Window? owner, string? strategyId, string? strategyName)
            : base(owner, "Backtest report" + (string.IsNullOrWhiteSpace(strategyName) ? "" : " — " + strategyName), 1180, 820, resizable: true)
        {
            _view = new BacktestReportView(strategyId, strategyName);
            SetBody(_view);
            StartMaximized();   // user request 2026-10-23: report windows open maximized
            Closed += (_, _) =>
            {
                _view.Stop();
                OpenWindows.Remove(this);
            };
        }

        public static void Open(Window? owner, string? strategyId, string? strategyName, string? runId)
        {
            var existing = strategyId == null ? null : OpenWindows.FirstOrDefault(w => w._view.StrategyId == strategyId);
            if (existing != null)
            {
                if (existing.WindowState == WindowState.Minimized) existing.WindowState = WindowState.Normal;
                existing.Activate();
                existing._view.Begin(runId);
                return;
            }
            var window = new BacktestReportWindow(owner, strategyId, strategyName);
            OpenWindows.Add(window);
            window.Show();
            window._view.Begin(runId);
        }
    }
}
