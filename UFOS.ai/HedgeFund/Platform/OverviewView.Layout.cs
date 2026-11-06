using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.HedgeFund.Ui;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>Stage 11 Overview — the layout (built once; the render methods in OverviewView.Sections.cs fill it).</summary>
    internal sealed partial class OverviewView
    {
        private ScrollViewer _scroll = null!;
        private Grid _content = null!;
        private AttentionStrip _attention = null!;
        private Card _marketCard = null!;
        private Card _systemCard = null!;
        private Card _activityCard = null!;
        private Grid _strategiesGrid = null!;
        private const double ChartMinHeight = 160;
        private const double ActivityMinHeight = 180;

        // market band
        private Border _tickerHost = null!;
        private UniformGrid _tickerRow = null!;
        private TextBlock _marketProblem = null!;
        private readonly List<TickerCell> _cells = new();
        private TextBlock _osMeta = null!;
        private Grid _osGrid = null!;
        private readonly List<Border> _osPanels = new();
        // SPY GEX
        private TextBlock _gexRegime = null!;
        private System.Windows.Shapes.Ellipse _gexDot = null!;
        private GexBarChart _gexChart = null!;
        private TextBlock _gexCaption = null!;
        private TextBlock _gexProblemText = null!;
        // QQQ
        private TextBlock _qqqAgree = null!;
        private System.Windows.Shapes.Ellipse _qqqDot = null!;
        private StackPanel _qqqRows = null!;
        // expected move
        private TextBlock _emTitle = null!;
        private TextBlock _emValue = null!;
        private MoveRangeBar _emBar = null!;
        private TextBlock _emCaption = null!;
        private TextBlock _emNext = null!;
        // VIX
        private TextBlock _vixLabel = null!;
        private TermCurve _vixCurve = null!;
        private TextBlock _vixCaption = null!;

        // strategies card
        private Grid _main = null!;
        private Card _strategiesCard = null!;
        private Grid _rightColumn = null!;
        private TextBlock _accountLabel = null!;
        private TextBlock _equityValue = null!;
        private TextBlock _equityReason = null!;
        private TextBlock _accountSource = null!;
        private TextBlock _todayValue = null!;
        private TextBlock _totalValue = null!;
        private readonly Dictionary<int, Button> _rangeButtons = new();
        private StatChip _chipVar = null!, _chipEs = null!, _chipExposure = null!, _chipDd = null!, _chipVol = null!;
        private TextBlock _riskMeta = null!;
        private Border _chartHost = null!;
        private readonly EquityChart _equityChart = new();
        private TextBlock _chartCaption = null!;
        private UniformGrid _tiles = null!;
        private TextBlock _tilesProblem = null!;

        // right column
        private KeyValueRow _loopRow = null!, _validationRow = null!, _archiveRow = null!, _aiRow = null!, _connRow = null!;
        private TextBlock _systemMeta = null!;
        private StackPanel _activityList = null!;
        private TextBlock _activityProblemText = null!;

        private void BuildLayout()
        {
            _content = new Grid { Margin = new Thickness(0, 0, 4, 0) };
            _content.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            _content.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            _content.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });

            _attention = new AttentionStrip { Margin = new Thickness(0, 0, 0, HfTokens.Gap) };
            _content.Children.Add(_attention);

            var market = BuildMarketCard();
            _marketCard = market;
            market.Margin = new Thickness(0, 0, 0, HfTokens.Gap);
            Grid.SetRow(market, 1);
            _content.Children.Add(market);

            _main = new Grid();
            Grid.SetRow(_main, 2);
            _strategiesCard = BuildStrategiesCard();
            _rightColumn = BuildRightColumn();
            _main.Children.Add(_strategiesCard);
            _main.Children.Add(_rightColumn);
            _content.Children.Add(_main);

            _scroll = new ScrollViewer
            {
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
                Focusable = false,
                Content = _content,
            };
            // Fill the window height (review 2026-11-06): in the wide layout the scroll viewer does NOT scroll vertically while everything
            // fits, so the star row (equity chart, activity list) gets the rest of the height and the activity list scrolls inside its card.
            // It scrolls (Auto) on actual overflow and always in the narrow layout — see UpdateScrollMode.
            _scroll.SizeChanged += (_, _) => UpdateScrollMode();
            _attention.SizeChanged += (_, _) => UpdateScrollMode();
            market.SizeChanged += (_, _) => UpdateScrollMode();
            _chartHost.SizeChanged += (_, _) => UpdateScrollMode();
            _activityCard.SizeChanged += (_, _) => UpdateScrollMode();
            _strategiesCard.SizeChanged += (_, _) => UpdateScrollMode();
            _scroll.ScrollChanged += (_, e) => { if (ReferenceEquals(e.OriginalSource, _scroll) && e.ViewportHeightChange != 0) SyncMinHeight(); };
            Content = _scroll;
            AutomationProperties.SetName(this, "Overview");
            ApplyLayout(narrow: false);
        }

        /// <summary>
        /// Scroll mode decided on ACTUAL overflow (review 2026-11-06), with no fixed height guess:
        /// - Disabled (wide, fits): overflow = the star row of the strategies card (equity chart) or of the right column (activity card)
        ///   got less than the chart / card minimum (RowDefinition.ActualHeight — an element's own ActualHeight never drops below its
        ///   MinHeight, it is clipped instead) → Auto.
        /// - Auto: measured with infinite height, the DesiredSizes are the natural ones (incl. margins); when attention strip + market band +
        ///   max(strategies card, System card + activity minimum) fits the viewport → Disabled.
        /// Both tests describe the same threshold (the chart's MinHeight is what the strategies card's natural height contains), with 1 px
        /// tolerance either way, so the mode does not oscillate. Narrow layout: always Auto. Also run on every 5 s tick (late reflows).
        /// </summary>
        private void UpdateScrollMode()
        {
            var viewport = _scroll.ActualHeight;
            if (viewport <= 0 || double.IsNaN(viewport)) return;
            ScrollBarVisibility mode;
            if (_narrow) mode = ScrollBarVisibility.Auto;
            else if (_scroll.VerticalScrollBarVisibility == ScrollBarVisibility.Disabled)
            {
                var squeezed = (_strategiesGrid.IsLoaded && _strategiesGrid.RowDefinitions[3].ActualHeight < ChartMinHeight - 1)
                               || (_rightColumn.IsLoaded && _rightColumn.RowDefinitions[1].ActualHeight < ActivityMinHeight - 1);
                mode = squeezed ? ScrollBarVisibility.Auto : ScrollBarVisibility.Disabled;
            }
            else
            {
                var main = Math.Max(_strategiesCard.DesiredSize.Height, _systemCard.DesiredSize.Height + ActivityMinHeight);
                var natural = _attention.DesiredSize.Height + _marketCard.DesiredSize.Height + main;
                mode = natural > 0 && natural <= viewport - 1 ? ScrollBarVisibility.Disabled : ScrollBarVisibility.Auto;
            }
            if (_scroll.VerticalScrollBarVisibility != mode) _scroll.VerticalScrollBarVisibility = mode;
            SyncMinHeight();
        }

        private void SyncMinHeight()
        {
            var h = _scroll.ViewportHeight;
            if (h > 0 && !double.IsNaN(h) && Math.Abs(_content.MinHeight - h) > 0.5) _content.MinHeight = h;
        }

        // ---------------------------------------------------------------- market band

        private Card BuildMarketCard()
        {
            var stack = new StackPanel();
            _tickerRow = new UniformGrid { Columns = 8 };
            _marketProblem = HfUi.Text("", 12.5, HfTokens.TextDim, wrap: true);
            _marketProblem.Visibility = Visibility.Collapsed;
            var tickerStack = new StackPanel();
            tickerStack.Children.Add(_tickerRow);
            tickerStack.Children.Add(_marketProblem);
            _tickerHost = new Border { Child = tickerStack, Background = HfTokens.CardBg };
            // no "Global Monitoring →" link: the Hedge Fund module has no hook to open that window (contract §4: no dead links)
            var fs = FundUi.FullScreenButton(_tickerHost, "Market", null, _tickerHost);
            stack.Children.Add(new SectionHeader("Market", fs));
            stack.Children.Add(_tickerHost);

            var divider = new Border { Height = 1, Background = HfTokens.Divider, Margin = new Thickness(0, 12, 0, 12) };
            stack.Children.Add(divider);
            _osMeta = HfUi.Text("", 12, HfTokens.TextDim);   // "provisional" in neutral grey (review 2026-11-06)
            stack.Children.Add(new SectionHeader("Options structure", _osMeta, 13));

            _osGrid = new Grid();
            _osPanels.Add(BuildGexPanel());
            _osPanels.Add(BuildQqqPanel());
            _osPanels.Add(BuildEmPanel());
            _osPanels.Add(BuildVixPanel());
            foreach (var p in _osPanels) _osGrid.Children.Add(p);
            stack.Children.Add(_osGrid);
            return new Card(stack, new Thickness(16, 12, 16, 16));
        }

        private static Border Inset(UIElement child)
            => new()
            {
                Background = HfTokens.InsetBg,
                BorderBrush = HfTokens.InsetBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(10),
                Padding = new Thickness(12, 10, 12, 10),
                Child = child,
                MinHeight = 170,
            };

        /// <summary>A panel heading: title left; right: optional dot + state text, optional ⛶.</summary>
        private static DockPanel PanelHeader(string title, UIElement? state, Button? fullScreen)
        {
            var row = new DockPanel { LastChildFill = true, Margin = new Thickness(0, 0, 0, 6) };
            if (fullScreen != null)
            {
                DockPanel.SetDock(fullScreen, Dock.Right);
                row.Children.Add(fullScreen);
            }
            if (state != null)
            {
                DockPanel.SetDock(state, Dock.Right);
                row.Children.Add(state);
            }
            row.Children.Add(HfUi.Text(title, 12.5, HfTokens.TextMuted));
            return row;
        }

        private StackPanel DotText(out System.Windows.Shapes.Ellipse dot, out TextBlock text)
        {
            var sp = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(8, 0, 0, 0) };
            dot = HfUi.Dot(DotKind.None);
            text = HfUi.Text("", 13, HfTokens.Text, bold: true);
            sp.Children.Add(dot);
            sp.Children.Add(text);
            return sp;
        }

        private Border BuildGexPanel()
        {
            var dock = new DockPanel { LastChildFill = true };
            var state = DotText(out _gexDot, out _gexRegime);
            var panel = Inset(dock);
            var fs = FundUi.FullScreenButton(panel, "SPY gamma exposure by strike", () => _gexCaption.Text, panel);
            var head = PanelHeader("SPY gamma exposure by strike", state, fs);
            DockPanel.SetDock(head, Dock.Top);
            dock.Children.Add(head);
            _gexCaption = HfUi.Text("", 12, HfTokens.TextDim, wrap: true);
            _gexCaption.Margin = new Thickness(0, 6, 0, 0);
            DockPanel.SetDock(_gexCaption, Dock.Bottom);
            dock.Children.Add(_gexCaption);
            _gexProblemText = HfUi.Text("", 12, HfTokens.TextDim, wrap: true);
            _gexProblemText.Visibility = Visibility.Collapsed;
            DockPanel.SetDock(_gexProblemText, Dock.Top);
            dock.Children.Add(_gexProblemText);
            _gexChart = new GexBarChart { MinHeight = 110 };
            dock.Children.Add(_gexChart);
            return panel;
        }

        private Border BuildQqqPanel()
        {
            var stack = new StackPanel();
            var state = DotText(out _qqqDot, out _qqqAgree);
            stack.Children.Add(PanelHeader("QQQ gamma", state, null));
            _qqqRows = new StackPanel();
            stack.Children.Add(_qqqRows);
            return Inset(stack);
        }

        private Border BuildEmPanel()
        {
            var stack = new StackPanel();
            var panel = Inset(stack);
            var fs = FundUi.FullScreenButton(panel, "Expected move SPY", () => _emValue.Text + " · " + _emCaption.Text, panel);
            var head = PanelHeader("Expected move · SPY", null, fs);
            _emTitle = (TextBlock)head.Children[head.Children.Count - 1];
            stack.Children.Add(head);
            _emValue = HfUi.Text("", 22, HfTokens.TextValue, mono: true);
            _emValue.Margin = new Thickness(0, 4, 0, 6);
            stack.Children.Add(_emValue);
            _emBar = new MoveRangeBar();
            stack.Children.Add(_emBar);
            _emCaption = HfUi.Text("", 12, HfTokens.TextDim, wrap: true);
            _emCaption.Margin = new Thickness(0, 4, 0, 0);
            stack.Children.Add(_emCaption);
            _emNext = HfUi.Text("", 12, HfTokens.TextDim, wrap: true);
            _emNext.Margin = new Thickness(0, 6, 0, 0);
            stack.Children.Add(_emNext);
            return panel;
        }

        private Border BuildVixPanel()
        {
            var dock = new DockPanel { LastChildFill = true };
            var panel = Inset(dock);
            _vixLabel = HfUi.Text("", 13, HfTokens.Text, bold: true);
            _vixLabel.Margin = new Thickness(8, 0, 0, 0);
            var fs = FundUi.FullScreenButton(panel, "VIX term structure", () => _vixCaption.Text, panel);
            var head = PanelHeader("VIX term structure", _vixLabel, fs);
            DockPanel.SetDock(head, Dock.Top);
            dock.Children.Add(head);
            _vixCaption = HfUi.Text("", 12, HfTokens.TextDim, wrap: true);
            _vixCaption.Margin = new Thickness(0, 6, 0, 0);
            DockPanel.SetDock(_vixCaption, Dock.Bottom);
            dock.Children.Add(_vixCaption);
            _vixCurve = new TermCurve { MinHeight = 90 };
            dock.Children.Add(_vixCurve);
            return panel;
        }

        // ---------------------------------------------------------------- strategies card

        private Card BuildStrategiesCard()
        {
            var grid = new Grid();
            _strategiesGrid = grid;
            for (var i = 0; i < 6; i++)
                grid.RowDefinitions.Add(new RowDefinition { Height = i == 3 ? new GridLength(1, GridUnitType.Star) : GridLength.Auto });

            _accountLabel = HfUi.Text("", 12, HfTokens.TextDim);
            grid.Children.Add(new SectionHeader("Strategies", _accountLabel));

            // equity · today · total · range
            var eq = new Grid { Margin = new Thickness(0, 0, 0, 10) };
            eq.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            eq.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            eq.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            eq.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            eq.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var eqStack = new StackPanel { Margin = new Thickness(0, 0, 24, 0) };
            eqStack.Children.Add(HfUi.Text("Total paper equity", 12.5, HfTokens.TextDim));
            _equityValue = HfUi.Text("", HfTokens.HeroSize, HfTokens.TextValue, bold: true, mono: true);
            eqStack.Children.Add(_equityValue);
            _equityReason = HfUi.Text("", 12, HfTokens.TextDim, wrap: true);
            _equityReason.MaxWidth = 520;
            _equityReason.Visibility = Visibility.Collapsed;
            eqStack.Children.Add(_equityReason);
            _accountSource = HfUi.Text("", 11, HfTokens.TextFaint);
            eqStack.Children.Add(_accountSource);
            eq.Children.Add(eqStack);
            var today = LabelValue("Today", out _todayValue);
            Grid.SetColumn(today, 1);
            eq.Children.Add(today);
            var total = LabelValue("Total", out _totalValue);
            Grid.SetColumn(total, 2);
            eq.Children.Add(total);
            var ranges = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Bottom };
            foreach (var (label, days, name) in new[] { ("1M", 30, "one month"), ("3M", 91, "three months"), ("All", 0, "the whole history") })
            {
                var b = FundUi.MakeButton(label);
                b.Margin = new Thickness(6, 0, 0, 0);
                b.Padding = new Thickness(12, 5, 12, 5);
                AutomationProperties.SetName(b, "Equity curve range: " + name);
                var d = days;
                b.Click += (_, _) => { _rangeDays = d; UpdateRangeButtons(); SafeRender(RenderEquityChart); };
                _rangeButtons[days] = b;
                ranges.Children.Add(b);
            }
            Grid.SetColumn(ranges, 4);
            eq.Children.Add(ranges);
            Grid.SetRow(eq, 1);
            grid.Children.Add(eq);
            UpdateRangeButtons();

            // risk row (GET /v2/overview/risk)
            var risk = new WrapPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 0, 0, 4) };
            _chipVar = new StatChip("VaR 99 % 1d");
            _chipEs = new StatChip("ES 97.5 % 1d");
            _chipExposure = new StatChip("Exposure");
            _chipDd = new StatChip("Max DD");
            _chipVol = new StatChip("Vol 20d");
            foreach (var c in new[] { _chipVar, _chipEs, _chipExposure, _chipDd, _chipVol }) { c.MaxWidth = 260; risk.Children.Add(c); }
            var riskLink = HfUi.Link("Risk Lab →", () => Navigate(OverviewTargets.RiskLab), "Open the Risk Lab");
            riskLink.Margin = new Thickness(4, 6, 0, 8);
            riskLink.VerticalAlignment = VerticalAlignment.Top;
            risk.Children.Add(riskLink);
            var riskStack = new StackPanel();
            riskStack.Children.Add(risk);
            _riskMeta = HfUi.Text("", 11, HfTokens.TextFaint);
            _riskMeta.Margin = new Thickness(0, 0, 0, 8);
            riskStack.Children.Add(_riskMeta);
            Grid.SetRow(riskStack, 2);
            grid.Children.Add(riskStack);

            // equity curve vs SPY (Fund/EquityChart, ⛶ + F11)
            var chartGrid = new Grid();
            _chartHost = new Border { Child = _equityChart, MinHeight = ChartMinHeight, Background = HfTokens.CardBg };
            chartGrid.Children.Add(_chartHost);
            var fs = FundUi.FullScreenButton(_chartHost, "Equity curve vs SPY", () => _accountLabel.Text + " · " + _equityValue.Text, chartGrid);
            fs.HorizontalAlignment = HorizontalAlignment.Right;
            fs.VerticalAlignment = VerticalAlignment.Top;
            fs.Margin = new Thickness(0, 0, 0, 0);
            chartGrid.Children.Add(fs);
            Grid.SetRow(chartGrid, 3);
            grid.Children.Add(chartGrid);
            AutomationProperties.SetName(_equityChart, "Equity curve of the active paper account versus SPY");

            _chartCaption = HfUi.Text("", 11.5, HfTokens.TextFaint, wrap: true);
            _chartCaption.Margin = new Thickness(0, 6, 0, 10);
            Grid.SetRow(_chartCaption, 4);
            grid.Children.Add(_chartCaption);

            // strategy tiles (platform sources) + incubation + "All strategies →"
            var tilesStack = new StackPanel();
            _tiles = new UniformGrid { Rows = 1 };
            tilesStack.Children.Add(_tiles);
            _tilesProblem = HfUi.Text("", 12, HfTokens.TextDim, wrap: true);
            _tilesProblem.Visibility = Visibility.Collapsed;
            tilesStack.Children.Add(_tilesProblem);
            var all = HfUi.Link("All strategies →", () => Navigate(OverviewTargets.Library), "Open the strategy library");
            all.HorizontalAlignment = HorizontalAlignment.Right;
            all.Margin = new Thickness(0, 6, 0, 0);
            tilesStack.Children.Add(all);
            Grid.SetRow(tilesStack, 5);
            grid.Children.Add(tilesStack);

            return new Card(grid);
        }

        private static StackPanel LabelValue(string label, out TextBlock value)
        {
            var sp = new StackPanel { Margin = new Thickness(0, 0, 24, 0), VerticalAlignment = VerticalAlignment.Bottom };
            sp.Children.Add(HfUi.Text(label, 12.5, HfTokens.TextDim));
            value = HfUi.Text("", 18, HfTokens.TextValue, mono: true);
            value.Margin = new Thickness(0, 2, 0, 4);
            sp.Children.Add(value);
            return sp;
        }

        private void UpdateRangeButtons()
        {
            foreach (var (days, b) in _rangeButtons)
            {
                var on = days == _rangeDays;
                b.Background = on ? HfTokens.ButtonSelectedBg : HfTokens.ButtonBg;
                b.BorderBrush = on ? FundPalette.Focus : HfTokens.ButtonBorder;
                b.Foreground = on ? HfTokens.TextTitle : FundPalette.Text;
                AutomationProperties.SetItemStatus(b, on ? "selected" : "");
            }
        }

        // ---------------------------------------------------------------- right column

        private Grid BuildRightColumn()
        {
            var col = new Grid();
            col.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            col.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });

            var sys = new StackPanel();
            _systemMeta = HfUi.Text("", 11, HfTokens.TextFaint);
            sys.Children.Add(new SectionHeader("System", _systemMeta));
            _loopRow = new KeyValueRow("Loop");
            _validationRow = new KeyValueRow("Validation & backtests");
            _archiveRow = new KeyValueRow("Options archive");
            _aiRow = new KeyValueRow("AI budget");
            _connRow = new KeyValueRow("Connections", divider: false);
            foreach (var r in new[] { _loopRow, _validationRow, _archiveRow, _aiRow, _connRow }) sys.Children.Add(r);
            var sysCard = new Card(sys) { Margin = new Thickness(0, 0, 0, HfTokens.Gap) };
            _systemCard = sysCard;
            col.Children.Add(sysCard);

            var dock = new DockPanel { LastChildFill = true };
            var allLink = HfUi.Link("All →", () => Navigate(OverviewTargets.Alerts), "Open Monitoring, Alerts");
            var head = new SectionHeader("Recent activity", allLink);
            DockPanel.SetDock(head, Dock.Top);
            dock.Children.Add(head);
            _activityProblemText = HfUi.Text("", 12, HfTokens.TextDim, wrap: true);
            _activityProblemText.Visibility = Visibility.Collapsed;
            _activityProblemText.Margin = new Thickness(0, 0, 0, 6);
            DockPanel.SetDock(_activityProblemText, Dock.Top);
            dock.Children.Add(_activityProblemText);
            _activityList = new StackPanel();
            var sv = new ScrollViewer
            {
                VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled,
                Content = _activityList,
                Focusable = false,
            };
            dock.Children.Add(sv);
            var actCard = new Card(dock) { MinHeight = ActivityMinHeight };
            _activityCard = actCard;
            Grid.SetRow(actCard, 1);
            col.Children.Add(actCard);
            return col;
        }

        // ---------------------------------------------------------------- responsive layout

        private void ApplyResponsive()
        {
            var w = Window.GetWindow(this)?.ActualWidth ?? ActualWidth;
            if (w <= 0) return;
            var narrow = w < NarrowWindowWidth;
            if (_layoutApplied && narrow == _narrow) return;
            ApplyLayout(narrow);
        }

        private void ApplyLayout(bool narrow)
        {
            _narrow = narrow;
            _layoutApplied = true;
            if (_scroll != null) UpdateScrollMode();
            _tickerRow.Columns = narrow ? 4 : 8;
            for (var i = 0; i < _cells.Count; i++)
                _cells[i].BorderThickness = new Thickness(0, 0, (i + 1) % _tickerRow.Columns == 0 ? 0 : 1, 0);

            // options structure: 4 panels in a row (GEX wider), or 2 × 2
            _osGrid.ColumnDefinitions.Clear();
            _osGrid.RowDefinitions.Clear();
            var cols = narrow ? 2 : 4;
            for (var c = 0; c < cols; c++)
                _osGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(!narrow && c == 0 ? 1.4 : 1, GridUnitType.Star) });
            var rows = (_osPanels.Count + cols - 1) / cols;
            for (var r = 0; r < rows; r++) _osGrid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            for (var i = 0; i < _osPanels.Count; i++)
            {
                var p = _osPanels[i];
                Grid.SetColumn(p, i % cols);
                Grid.SetRow(p, i / cols);
                var lastCol = i % cols == cols - 1;
                p.Margin = new Thickness(0, i / cols > 0 ? 12 : 0, lastCol ? 0 : 12, 0);
            }

            // main row: strategies | right column (wide) or stacked (narrow)
            _main.ColumnDefinitions.Clear();
            _main.RowDefinitions.Clear();
            if (narrow)
            {
                _main.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
                _main.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                Grid.SetColumn(_strategiesCard, 0);
                Grid.SetRow(_strategiesCard, 0);
                Grid.SetColumn(_rightColumn, 0);
                Grid.SetRow(_rightColumn, 1);
                _rightColumn.Margin = new Thickness(0, HfTokens.Gap, 0, 0);
            }
            else
            {
                _main.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                _main.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(360) });
                Grid.SetColumn(_strategiesCard, 0);
                Grid.SetRow(_strategiesCard, 0);
                Grid.SetColumn(_rightColumn, 1);
                Grid.SetRow(_rightColumn, 0);
                _rightColumn.Margin = new Thickness(HfTokens.Gap, 0, 0, 0);
            }
        }
    }
}
