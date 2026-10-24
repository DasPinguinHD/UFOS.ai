using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// Hedge Fund → "Shadow Account" (Strategy Platform stage 9 — SPEC §19.6–§19.8, exact JSON §19.11, backend notes §19.12, WPF
    /// notes §19.13). Your OWN broker statements (Trade Republic, Scalable Capital, IBKR Flex, or any CSV via a saved column
    /// mapping) are imported by the LOCAL backend — "Your statements stay on this computer — nothing is uploaded; an AI
    /// commentary sends only numbers." Sections (Ctrl+1…5): Overview (account, imports, FIFO positions, unmatched sales,
    /// valuation prices, classification), Transactions (normalised, paged), Diagnostics (disposition effect PGR/PLR, turnover,
    /// holding periods, concentration, trading after losses, timing vs a benchmark — each block may be "unavailable" with the
    /// reason), Shadow portfolios (rule portfolios on exactly your cash flows; parity shown) and Export &amp; AI (self-contained
    /// audit report saved as a local file; optional labelled AI commentary, numbers only). Every text that comes from an imported
    /// file (names, raw types, ISINs) is plain TextBlock.Text — nothing is evaluated (formula-injection safe; the backend
    /// neutralises formula-looking cells on every export). No polling: the data only changes through your actions.
    /// </summary>
    internal sealed class ShadowAccountView : UserControl
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        public const string LocalNotice = "Your statements stay on this computer — nothing is uploaded; an AI commentary sends only numbers.";
        private static readonly Regex SymbolRe = new(@"^[A-Z][A-Z0-9.\-]{0,9}$|^[A-Z0-9]{2,10}-USD$", RegexOptions.Compiled);

        private const string SecOverview = "overview";
        private const string SecTransactions = "transactions";
        private const string SecDiagnostics = "diagnostics";
        private const string SecShadows = "shadows";
        private const string SecExport = "export";

        private readonly ComboBox _accountBox;
        private readonly TextBlock _status;
        private readonly List<(RadioButton Button, string Key)> _tabs = new();
        private readonly ScrollViewer _scroll;
        private readonly StackPanel _overviewPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _txnPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _diagPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _shadowPanel = new() { Margin = new Thickness(0, 0, 10, 10) };
        private readonly StackPanel _exportPanel = new() { Margin = new Thickness(0, 0, 10, 10) };

        private ShTemplates? _templates;
        private int? _fxYear;
        private ShFx? _fx;
        private string? _fxError;
        private List<ShProfile> _profiles = new();
        private List<ShAccount> _accounts = new();
        private string? _accountId;
        private ShAccountDetail? _detail;
        private string? _detailError;
        private ShTxnList? _txns;
        private string? _txnError;
        private int _txnOffset;
        private const int TxnPage = 200;
        private JsonElement? _diag;
        private string? _diagError;
        private string _benchmark = "SPY";
        private readonly List<(string Type, string Param)> _rules = new() { ("buy_and_hold", "SPY") };
        private JsonElement? _shadowResult;
        private string? _shadowError;
        private string _reportFormat = "html";
        private string? _exportMessage;
        private MonCommentaryPanel? _commentary;
        private string _section = SecOverview;
        private bool _busy;
        private bool _activated;
        private bool _stopped;
        private bool _filling;

        public ShadowAccountView()
        {
            AutomationProperties.SetName(this, "Shadow Account — your own broker statements, analysed locally");
            Focusable = false;
            var root = new Grid();
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });

            var head = new StackPanel();
            var titleRow = new WrapPanel();
            titleRow.Children.Add(FundUi.Text("Shadow Account", 14, FundPalette.TextBright, bold: true, wrap: false));
            var local = MonUi.Chip("DATA STAYS ON THIS PC", FundPalette.Long);
            local.Margin = new Thickness(12, 2, 0, 0);
            titleRow.Children.Add(local);
            head.Children.Add(titleRow);
            var notice = MonUi.Line(LocalNotice + " Hypothetical comparisons for your own review. Own accounts only.", 11, FundPalette.Text);
            AutomationProperties.SetName(notice, LocalNotice);
            head.Children.Add(notice);
            var row = new WrapPanel { Margin = new Thickness(0, 8, 0, 0) };
            row.Children.Add(new TextBlock { Text = "Account  ", Foreground = FundPalette.Faint, FontSize = 11, VerticalAlignment = VerticalAlignment.Center });
            _accountBox = new ComboBox { MinWidth = 280, FontSize = 12, MinHeight = 24, Margin = new Thickness(0, 0, 10, 0) };
            AutomationProperties.SetName(_accountBox, "Shadow account");
            _accountBox.SelectionChanged += async (_, _) =>
            {
                if (_filling || _accountBox.SelectedItem is not ComboBoxItem { Tag: string id } || id == _accountId) return;
                _accountId = id;
                ResetAccountData();
                await LoadAccountAsync();
            };
            row.Children.Add(_accountBox);
            var create = MonUi.Small("New Shadow account…", "Create a new Shadow account");
            create.Click += async (_, _) => await CreateAccountAsync();
            row.Children.Add(create);
            var import = TradingUi.Primary("Import statement…");
            import.Padding = new Thickness(10, 2, 10, 2);
            import.Margin = new Thickness(0, 0, 8, 0);
            AutomationProperties.SetName(import, "Import a broker statement into the selected Shadow account (Ctrl+O)");
            import.Click += async (_, _) => await ImportAsync();
            row.Children.Add(import);
            var refresh = MonUi.Small("↻ Refresh", "Reload the Shadow account (F5)");
            refresh.Click += async (_, _) => await RefreshAsync();
            row.Children.Add(refresh);
            head.Children.Add(row);
            _status = MonUi.Line("", 10.5, FundPalette.Dim);
            _status.Visibility = Visibility.Collapsed;
            AutomationProperties.SetLiveSetting(_status, AutomationLiveSetting.Polite);
            head.Children.Add(_status);
            var headPanel = FundUi.Panel(head, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 10, 8));
            Grid.SetRow(headPanel, 0);
            root.Children.Add(headPanel);

            var tabs = new WrapPanel { Margin = new Thickness(0, 0, 10, 8) };
            KeyboardNavigation.SetDirectionalNavigation(tabs, KeyboardNavigationMode.Cycle);
            KeyboardNavigation.SetTabNavigation(tabs, KeyboardNavigationMode.Once);
            AutomationProperties.SetName(tabs, "Shadow Account sections");
            AddTab(tabs, SecOverview, "Overview", "Overview: account, imports, positions, valuation (Ctrl+1)", "Ctrl+1");
            AddTab(tabs, SecTransactions, "Transactions", "Transactions: normalised, paged (Ctrl+2)", "Ctrl+2");
            AddTab(tabs, SecDiagnostics, "Diagnostics", "Diagnostics: disposition effect, turnover, holding periods, concentration, trading after losses, timing (Ctrl+3)", "Ctrl+3");
            AddTab(tabs, SecShadows, "Shadow portfolios", "Shadow portfolios: rule portfolios on the same cash flows (Ctrl+4)", "Ctrl+4");
            AddTab(tabs, SecExport, "Export & AI", "Export the audit report, optional AI commentary (Ctrl+5)", "Ctrl+5");
            Grid.SetRow(tabs, 1);
            root.Children.Add(tabs);

            _scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false, Content = _overviewPanel };
            Grid.SetRow(_scroll, 2);
            root.Children.Add(_scroll);
            Content = root;
            _overviewPanel.Children.Add(MonUi.Line("Loading…", 11.5, FundPalette.Faint));
            AutomationProperties.SetName(_overviewPanel, "Shadow account overview");
            AutomationProperties.SetName(_txnPanel, "Transactions");
            AutomationProperties.SetName(_diagPanel, "Behaviour diagnostics");
            AutomationProperties.SetName(_shadowPanel, "Shadow portfolios");
            AutomationProperties.SetName(_exportPanel, "Export and AI commentary");
            PreviewKeyDown += async (_, e) => await OnKeyAsync(e);
        }

        // =====================================================================================
        // Lifecycle
        // =====================================================================================

        public async Task ActivateAsync()
        {
            if (_stopped) return;
            if (!_activated)
            {
                _activated = true;
                await RefreshAsync();
            }
        }

        /// <summary>Window closed: nothing polls; any late answer is ignored.</summary>
        public void Shutdown() => _stopped = true;

        private Window? Owner => Window.GetWindow(this);

        private async Task OnKeyAsync(KeyEventArgs e)
        {
            var ctrl = (Keyboard.Modifiers & ModifierKeys.Control) == ModifierKeys.Control;
            if (e.Key == Key.F5) { e.Handled = true; await RefreshAsync(); return; }
            if (!ctrl) return;
            var index = e.Key switch
            {
                Key.D1 or Key.NumPad1 => 0,
                Key.D2 or Key.NumPad2 => 1,
                Key.D3 or Key.NumPad3 => 2,
                Key.D4 or Key.NumPad4 => 3,
                Key.D5 or Key.NumPad5 => 4,
                _ => -1,
            };
            if (index >= 0 && index < _tabs.Count)
            {
                e.Handled = true;
                _tabs[index].Button.IsChecked = true;
                _tabs[index].Button.Focus();
            }
            else if (e.Key == Key.O)
            {
                e.Handled = true;
                await ImportAsync();
            }
        }

        private void SetStatus(string? text, Brush? brush = null)
        {
            _status.Text = text ?? "";
            _status.Foreground = brush ?? FundPalette.Dim;
            _status.Visibility = string.IsNullOrWhiteSpace(text) ? Visibility.Collapsed : Visibility.Visible;
            AutomationProperties.SetName(_status, text ?? "");
        }

        private void ResetAccountData()
        {
            _detail = null;
            _detailError = null;
            _txns = null;
            _txnError = null;
            _txnOffset = 0;
            _diag = null;
            _diagError = null;
            _shadowResult = null;
            _shadowError = null;
            _exportMessage = null;
            _commentary = null;
        }

        private async Task RefreshAsync()
        {
            if (_stopped) return;
            var t = await PlatformClient.GetShadowTemplatesAsync();
            if (_stopped) return;
            if (t.Ok && t.Value != null) _templates = t.Value;
            var p = await PlatformClient.GetShadowProfilesAsync();
            if (p.Ok && p.Value?.Profiles != null) _profiles = p.Value.Profiles;
            var a = await PlatformClient.GetShadowAccountsAsync();
            if (_stopped) return;
            if (!a.Ok || a.Value == null)
            {
                var msg = !a.Ok && a.HttpStatus == 404 && PlatformClient.ErrorCode(a) == null
                    ? "The running backend has no Shadow Account routes (older than stage 9) — restart it from the Hedge Fund window."
                    : a.Ok ? "Unexpected response from the backend (/v2/shadow/accounts)." : PlatformClient.DescribeFailure(a);
                SetStatus(msg, FundPalette.Kill);
                if (_accounts.Count == 0) { _overviewPanel.Children.Clear(); _overviewPanel.Children.Add(MonUi.ErrorBlock(msg)); }
                return;
            }
            _accounts = a.Value.Accounts ?? new List<ShAccount>();
            if (_accountId == null || !_accounts.Any(x => x.Id == _accountId)) _accountId = _accounts.FirstOrDefault()?.Id;
            FillAccountBox();
            ResetAccountData();
            await LoadAccountAsync();
        }

        private void FillAccountBox()
        {
            _filling = true;
            try
            {
                _accountBox.Items.Clear();
                foreach (var acc in _accounts)
                {
                    var text = acc.DisplayName + " · " + (acc.BaseCurrency ?? "?") + " · " + MonUi.Int(acc.Transactions) + " transactions";
                    var item = new ComboBoxItem { Content = text, Tag = acc.Id };
                    AutomationProperties.SetName(item, text);
                    _accountBox.Items.Add(item);
                    if (acc.Id == _accountId) _accountBox.SelectedItem = item;
                }
                _accountBox.IsEnabled = _accounts.Count > 0;
            }
            finally { _filling = false; }
        }

        private async Task LoadAccountAsync()
        {
            if (_accountId == null) { RenderSection(); return; }
            var res = await PlatformClient.GetShadowAccountAsync(_accountId);
            if (_stopped) return;
            if (res.Ok && res.Value != null) { _detail = res.Value; _detailError = null; }
            else _detailError = res.Ok ? "Unexpected response from the backend (Shadow account)." : PlatformClient.DescribeFailure(res);
            await LoadSectionDataAsync();
            RenderSection();
        }

        private async Task LoadSectionDataAsync()
        {
            if (_accountId == null) return;
            if (_section == SecTransactions && _txns == null) await LoadTransactionsAsync();
            else if (_section == SecDiagnostics && _diag == null && _diagError == null) await LoadDiagnosticsAsync();
        }

        private async Task LoadTransactionsAsync()
        {
            if (_accountId == null) return;
            var res = await PlatformClient.GetShadowTransactionsAsync(_accountId, TxnPage, _txnOffset);
            if (_stopped) return;
            if (res.Ok && res.Value != null) { _txns = res.Value; _txnError = null; }
            else _txnError = res.Ok ? "Unexpected response from the backend (transactions)." : PlatformClient.DescribeFailure(res);
        }

        private async Task LoadDiagnosticsAsync()
        {
            if (_accountId == null) return;
            SetStatus("Computing the diagnostics (reads daily closes from the data layer)…", FundPalette.Faint);
            var res = await PlatformClient.GetShadowDiagnosticsAsync(_accountId, _benchmark);
            if (_stopped) return;
            if (res.Ok && res.Value.ValueKind == JsonValueKind.Object) { _diag = res.Value; _diagError = null; SetStatus(null); }
            else { _diag = null; _diagError = res.Ok ? "Unexpected response from the backend (diagnostics)." : PlatformClient.DescribeFailure(res); SetStatus(null); }
        }

        // =====================================================================================
        // Sections
        // =====================================================================================

        private void AddTab(Panel host, string key, string text, string name, string accelerator)
        {
            var rb = new RadioButton
            {
                GroupName = "ShadowSections",
                Content = text,
                Style = MonTabs.Style,
                IsChecked = key == SecOverview,
                Margin = new Thickness(0, 0, 6, 0),
                Padding = new Thickness(12, 4, 12, 5),
                FontSize = 11.5,
                Cursor = Cursors.Hand,
            };
            AutomationProperties.SetName(rb, name);
            AutomationProperties.SetAcceleratorKey(rb, accelerator);
            rb.Checked += async (_, _) =>
            {
                _section = key;
                _scroll.Content = PanelOf(key);
                _scroll.ScrollToTop();
                if (_activated) { await LoadSectionDataAsync(); RenderSection(); }
            };
            _tabs.Add((rb, key));
            host.Children.Add(rb);
        }

        private StackPanel PanelOf(string key) => key switch
        {
            SecTransactions => _txnPanel,
            SecDiagnostics => _diagPanel,
            SecShadows => _shadowPanel,
            SecExport => _exportPanel,
            _ => _overviewPanel,
        };

        private void RenderSection()
        {
            var panel = PanelOf(_section);
            panel.Children.Clear();
            if (_accountId == null)
            {
                panel.Children.Add(MonUi.Line("No Shadow account yet. Create one (\"New Shadow account…\"), then import a statement of YOUR OWN broker account. "
                                              + "The file is read by the backend on this computer and stored in its local database — nothing is uploaded.", 11.5, FundPalette.Dim));
                return;
            }
            switch (_section)
            {
                case SecTransactions: RenderTransactions(panel); break;
                case SecDiagnostics: RenderDiagnostics(panel); break;
                case SecShadows: RenderShadows(panel); break;
                case SecExport: RenderExport(panel); break;
                default: RenderOverview(panel); break;
            }
        }

        private void RenderOverview(StackPanel panel)
        {
            if (_detailError != null) { panel.Children.Add(MonUi.ErrorBlock(_detailError)); return; }
            var d = _detail;
            if (d?.Account == null) { panel.Children.Add(MonUi.Line("Loading…", 11, FundPalette.Faint)); return; }
            var a = d.Account;
            var info = new StackPanel();
            info.Children.Add(FundUi.Text(a.Label ?? "(no label)", 12.5, FundPalette.TextBright, bold: true));
            info.Children.Add(MonUi.Pairs(new[]
            {
                ("Id", a.Id ?? "—"),
                ("Base currency", a.BaseCurrency ?? "—"),
                ("Broker country", a.BrokerCountry ?? "—"),
                ("Created", TradingText.Ts(a.CreatedAt)),
                ("Imports / transactions", MonUi.Int(a.Imports) + " / " + MonUi.Int(a.Transactions)),
                ("Period", (a.FirstDate ?? "—") + " … " + (a.LastDate ?? "—")),
                ("Templates used", (a.Templates?.Count ?? 0) > 0 ? string.Join(", ", a.Templates!) : "—"),
            }));
            panel.Children.Add(FundUi.Panel(info, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10)));
            panel.Children.Add(FxPanel(a));

            panel.Children.Add(MonUi.Head("Imports"));
            var imports = d.Imports ?? new List<ShImportSummary>();
            panel.Children.Add(MonUi.Table(new[] { "Imported", "File", "Template", "Rows", "Accepted", "Rejected", "Duplicates", "Skipped", "sha256" },
                imports.Select(i => (IReadOnlyList<string>)new[]
                {
                    TradingText.Ts(i.CreatedAt), i.FileName ?? "—", (i.Template ?? "—") + (i.ProfileId != null ? " (" + i.ProfileId + ")" : ""), MonUi.Int(i.Rows),
                    MonUi.Int(i.Accepted), MonUi.Int(i.Rejected), MonUi.Int(i.Duplicates), MonUi.Int(i.Skipped), Short(i.Sha256),
                }), "Imports", new double[] { 120, 0, 120, 50, 60, 60, 70, 55, 90 }));
            if (imports.Count > 0)
            {
                var removeRow = new WrapPanel { Margin = new Thickness(0, 0, 0, 6) };
                foreach (var imp in imports.Where(x => !string.IsNullOrEmpty(x.Id)))
                {
                    var target = imp;
                    var rm = MonUi.Small("Remove " + (imp.FileName ?? imp.Id) + "…", "Remove the import " + (imp.FileName ?? "") + " (" + imp.Id + ") and its transactions (asks for confirmation)");
                    rm.Margin = new Thickness(0, 0, 8, 4);
                    rm.Click += async (_, _) => await RemoveImportAsync(target);
                    removeRow.Children.Add(rm);
                }
                panel.Children.Add(removeRow);
            }

            var positions = d.Positions ?? new List<ShPosition>();
            panel.Children.Add(MonUi.Head("Positions (FIFO, cost in the instrument's currency)"));
            panel.Children.Add(MonUi.Table(new[] { "Key", "Symbol", "Name", "Class", "Ccy", "Qty", "Cost", "Avg cost" },
                positions.Select(p => (IReadOnlyList<string>)new[]
                {
                    p.Key ?? "—", p.Symbol ?? "—", p.Name ?? "—", p.AssetClass ?? "unclassified", p.Currency ?? "—", FundFormat.Qty(p.Qty), MonUi.Num(p.Cost), MonUi.Num(p.AvgCost, 4),
                }), "Positions", new double[] { 120, 70, 0, 80, 40, 80, 90, 80 }));
            var buttons = new WrapPanel { Margin = new Thickness(0, 0, 0, 6) };
            var val = MonUi.Small("Set valuation prices…", "Set your own valuation prices for the positions (needed for instruments without market data, e.g. ISIN only)");
            val.IsEnabled = positions.Count > 0;
            val.Click += async (_, _) => await SetValuationAsync();
            buttons.Children.Add(val);
            // §20.10/§20.11: ISIN → a listing the user picks (EODHD candidates or a US ticker) — global, audited
            var map = MonUi.Small("Symbol mapping…", "Map ISINs to a listing you pick (EODHD listing or US ticker), list and remove mappings, EODHD daily calls");
            map.Click += async (_, _) => await OpenSymbolMappingAsync();
            buttons.Children.Add(map);
            var cls = MonUi.Small("Classify an instrument…", "Classify an instrument (share, ETF, fund, bond, crypto, derivative, other) for the diagnostics and price lookups");
            cls.IsEnabled = positions.Count > 0;
            cls.Click += async (_, _) => await ClassifyAsync();
            buttons.Children.Add(cls);
            panel.Children.Add(buttons);
            var v = d.Valuation;
            panel.Children.Add(MonUi.Line(v == null
                ? "No valuation of your own yet — instruments without market data (ISIN only) stay unpriced (never a stand-in price)."
                : "Your valuation as of " + (v.AsOf ?? "?") + " (" + MonUi.Int(v.Prices?.Count) + " prices, saved " + TradingText.Ts(v.Ts) + ") — it wins over market data on its day.",
                10.5, v == null ? FundPalette.Risk : FundPalette.Dim));
            var unmatched = d.Unmatched ?? new List<ShUnmatched>();
            if (unmatched.Count > 0)
            {
                panel.Children.Add(MonUi.Head("Sales without an earlier purchase (no invented cost basis)"));
                panel.Children.Add(MonUi.Line("Import the older statements — these quantities have no purchase in the data, so they have no cost basis and are left out of the statistics.", 10.5, FundPalette.Risk));
                panel.Children.Add(MonUi.Table(new[] { "Date", "Key", "Quantity" }, unmatched.Select(u => (IReadOnlyList<string>)new[] { u.Date ?? "—", u.Key ?? "—", FundFormat.Qty(u.Qty) }),
                                               "Unmatched sales", new double[] { 100, 160, 0 }));
            }
            panel.Children.Add(MonUi.Line("All texts above come from your files and are shown as plain text — nothing is evaluated.", 10, FundPalette.Faint));
        }

        // ----- FX rates (§19.15: EUR/USD for the Shadow conversions only — behaviour analysis, not tax data) -----

        /// <summary>Years of the account's transactions (from its first / last date) — empty when it has none.</summary>
        private static List<int> AccountYears(ShAccount a)
        {
            var years = new List<int>();
            if (a.FirstDate is string f && f.Length >= 4 && int.TryParse(f.Substring(0, 4), NumberStyles.Integer, Inv, out var y0)
                && a.LastDate is string l && l.Length >= 4 && int.TryParse(l.Substring(0, 4), NumberStyles.Integer, Inv, out var y1))
                for (var y = Math.Max(y1, y0); y >= Math.Min(y0, y1) && years.Count < 50; y--) years.Add(y);
            return years;
        }

        private Border FxPanel(ShAccount a)
        {
            var s = new StackPanel();
            s.Children.Add(FundUi.Text("FX rates (EUR/USD)", 12, FundPalette.TextBright, bold: true));
            s.Children.Add(MonUi.Line("FRED DEXUSEU, exact dates only — used to convert between EUR and USD in the diagnostics and the shadow portfolios. A transaction "
                                      + "without a fixing on its date is left out (with a warning) — never converted with another day's rate.", 10.5, FundPalette.Faint));
            var years = AccountYears(a);
            if (years.Count == 0)
            {
                s.Children.Add(MonUi.Line("No transactions yet — nothing to convert.", 10.5, FundPalette.Dim));
                return FundUi.Panel(s, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10));
            }
            if (_fxYear == null || !years.Contains(_fxYear.Value)) { _fxYear = null; _fx = null; _fxError = null; }
            var row = new WrapPanel { Margin = new Thickness(0, 4, 0, 2) };
            row.Children.Add(new TextBlock { Text = "Year  ", Foreground = FundPalette.Faint, FontSize = 11, VerticalAlignment = VerticalAlignment.Center });
            var box = new ComboBox { FontSize = 12, MinHeight = 24, MinWidth = 90, Margin = new Thickness(0, 0, 8, 0) };
            AutomationProperties.SetName(box, "Year of the FX rates");
            foreach (var y in years)
            {
                var item = new ComboBoxItem { Content = y.ToString(Inv), Tag = y };
                box.Items.Add(item);
                if (y == _fxYear) box.SelectedItem = item;
            }
            box.SelectionChanged += async (_, _) =>
            {
                if (box.SelectedItem is not ComboBoxItem { Tag: int y } || y == _fxYear) return;
                _fxYear = y;
                await LoadFxAsync(refresh: false);
            };
            row.Children.Add(box);
            var load = MonUi.Small("Load FX rates", "Load the FRED DEXUSEU rates of the selected year (needs the FRED key)");
            load.IsEnabled = _fxYear != null;
            load.Click += async (_, _) => await LoadFxAsync(refresh: true);
            row.Children.Add(load);
            s.Children.Add(row);
            if (_fxYear == null) s.Children.Add(MonUi.Line("Choose a year to see the stored fixings.", 10.5, FundPalette.Faint));
            else if (_fxError != null) s.Children.Add(MonUi.ErrorBlock(_fxError));
            else if (_fx != null)
                s.Children.Add(MonUi.Line((_fx.Series ?? "series not reported") + " " + (_fx.Year?.ToString(Inv) ?? "?") + ": " + MonUi.Int(_fx.Observations) + " fixings stored"
                                          + (_fx.First != null ? " (" + _fx.First + " … " + (_fx.Last ?? "?") + ")" : "")
                                          + " · last loaded " + (_fx.FetchedAt != null ? TradingText.Ts(_fx.FetchedAt) : "never")
                                          + (_fx.Added is int added ? " · " + added.ToString(Inv) + " added now" : ""), 10.5,
                                          (_fx.Observations ?? 0) == 0 ? FundPalette.Risk : FundPalette.Dim, mono: true));
            var b = FundUi.Panel(s, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10));
            AutomationProperties.SetName(b, "FX rates for the Shadow conversions");
            return b;
        }

        private async Task LoadFxAsync(bool refresh)
        {
            if (_busy || _fxYear is not int year) return;
            _busy = true;
            if (refresh) SetStatus("Loading FRED DEXUSEU for " + year.ToString(Inv) + " — this can take a while…", FundPalette.Faint);
            try
            {
                var res = refresh ? await PlatformClient.RefreshShadowFxAsync(year) : await PlatformClient.GetShadowFxAsync(year);
                if (_stopped) return;
                if (res.Ok && res.Value != null) { _fx = res.Value; _fxError = null; }
                else { _fx = null; _fxError = "FX rates not available (" + (res.Ok ? "unexpected response from the backend" : PlatformClient.DescribeFailure(res)) + ")"; }
                if (refresh && res.Ok) { _diag = null; _diagError = null; _shadowResult = null; }
            }
            finally
            {
                _busy = false;
                if (refresh) SetStatus(null);
            }
            RenderSection();
        }

        private static string Short(string? sha) => string.IsNullOrEmpty(sha) ? "—" : sha.Length > 12 ? sha.Substring(0, 12) + "…" : sha;

        private void RenderTransactions(StackPanel panel)
        {
            if (_txnError != null) panel.Children.Add(MonUi.ErrorBlock(_txnError));
            var list = _txns?.Transactions ?? new List<ShTxn>();
            var total = _txns?.Total ?? 0;
            var nav = new WrapPanel { Margin = new Thickness(0, 0, 0, 6) };
            nav.Children.Add(new TextBlock
            {
                Text = total == 0 ? "No transactions.  " : (_txnOffset + 1).ToString("N0", Inv) + "–" + (_txnOffset + list.Count).ToString("N0", Inv) + " of " + total.ToString("N0", Inv) + " (date order)  ",
                Foreground = FundPalette.Dim, FontSize = 11, VerticalAlignment = VerticalAlignment.Center,
            });
            var prev = MonUi.Small("◂ Previous", "Previous page of transactions");
            prev.IsEnabled = _txnOffset > 0;
            prev.Click += async (_, _) => { _txnOffset = Math.Max(0, _txnOffset - TxnPage); await LoadTransactionsAsync(); RenderSection(); };
            nav.Children.Add(prev);
            var next = MonUi.Small("Next ▸", "Next page of transactions");
            next.IsEnabled = _txnOffset + list.Count < total;
            next.Click += async (_, _) => { _txnOffset += TxnPage; await LoadTransactionsAsync(); RenderSection(); };
            nav.Children.Add(next);
            panel.Children.Add(nav);
            panel.Children.Add(MonUi.Table(new[] { "Date", "Kind", "Instrument", "Qty", "Price", "Ccy", "Gross", "Fee", "Tax", "Net", "Original (FX)", "Raw type", "Note / id", "Row" },
                list.Select(t => (IReadOnlyList<string>)new[]
                {
                    t.Date ?? "—", ShText.Kind(t.Kind), (t.Key ?? "") + (string.IsNullOrWhiteSpace(t.Name) ? "" : " · " + t.Name), FundFormat.Qty(t.Qty), MonUi.Num(t.Price, 4),
                    t.Currency ?? "—", MonUi.Num(t.Gross), MonUi.Num(t.Fee), MonUi.Num(t.Tax), MonUi.Num(t.Net), ShText.Original(t), t.RawType ?? "—",
                    ShText.NoteAndId(t), t.Row?.ToString(Inv) ?? "—",
                }), "Transactions", new double[] { 82, 90, 0, 64, 64, 36, 70, 50, 50, 72, 100, 90, 140, 36 }));
            panel.Children.Add(MonUi.Line("Staking rewards open a lot at their market value at receipt; tax refunds and pending private-market orders (pre-payments, not "
                                          + "positions) are listed as their own kinds; an unpaired corporate action changes nothing until its partner row is imported. "
                                          + "Original (FX) = the amount, currency and FX rate as reported by the broker (never recomputed). Transfer counterparties are not imported.",
                                          10, FundPalette.Faint));
            panel.Children.Add(MonUi.Line("Net = signed cash effect (buy −(gross+fee); sell +(gross−fee−tax); dividend/interest +(gross−tax); withholding −tax; fee −fee). Plain text from your files.",
                                          10, FundPalette.Faint));
        }

        // ----- diagnostics -----

        private void RenderDiagnostics(StackPanel panel)
        {
            var row = new WrapPanel { Margin = new Thickness(0, 0, 0, 6) };
            row.Children.Add(new TextBlock { Text = "Benchmark (timing)  ", Foreground = FundPalette.Faint, FontSize = 11, VerticalAlignment = VerticalAlignment.Center });
            var bench = FundUi.Input(_benchmark);
            bench.Width = 90;
            bench.MaxLength = 14;
            bench.Margin = new Thickness(0, 0, 8, 0);
            AutomationProperties.SetName(bench, "Benchmark ticker for the timing block, e.g. SPY");
            row.Children.Add(bench);
            var run = MonUi.Small("Compute", "Compute the behaviour diagnostics");
            run.Click += async (_, _) =>
            {
                var b = bench.Text.Trim().ToUpperInvariant();
                if (!SymbolRe.IsMatch(b)) { SetStatus("The benchmark must be a ticker such as SPY.", FundPalette.Kill); return; }
                _benchmark = b;
                await LoadDiagnosticsAsync();
                RenderSection();
            };
            row.Children.Add(run);
            panel.Children.Add(row);
            if (_diagError != null) { panel.Children.Add(MonUi.ErrorBlock(_diagError)); return; }
            if (_diag is not JsonElement dg) { panel.Children.Add(MonUi.Line("Press Compute.", 11, FundPalette.Faint)); return; }
            var cur = ShJson.Str(dg, "base_currency") ?? "";
            var top = ShJson.Str(dg, "unavailable");
            var period = ShJson.Get(dg, "period");
            panel.Children.Add(MonUi.Line("Period " + (ShJson.Str(period, "first") ?? "—") + " … " + (ShJson.Str(period, "last") ?? "—") + " (" + Num0(ShJson.Num(period, "days")) + " days) · base currency " + cur,
                                          10.5, FundPalette.Faint, mono: true));
            if (top != null) panel.Children.Add(MonUi.Callout("Diagnostics unavailable: " + top, FundPalette.Risk, MonUi.RiskSurface));
            foreach (var w in ShJson.Strings(dg, "warnings")) panel.Children.Add(MonUi.Line("⚠ " + w, 10.5, FundPalette.Risk));

            var cov = ShJson.Get(dg, "coverage");
            if (cov != null)
            {
                var unpriced = ShJson.Strings(cov, "unpriced");
                panel.Children.Add(Block("Price coverage", null, ShJson.Unavailable(cov), new[]
                {
                    ("Instruments / priced", Num0(ShJson.Num(cov, "instruments")) + " / " + Num0(ShJson.Num(cov, "priced"))),
                    ("Unpriced (no stand-in)", unpriced.Count == 0 ? "none" : string.Join(", ", unpriced)),
                    ("Price source", ShJson.Str(cov, "price_source") ?? "—"),
                }));
                // §20.10/§20.11: the source per priced instrument — the backend's text verbatim (e.g. "EODHD close of SAP.XETRA (EUR, your listing)")
                var srcs = ShJson.Arr(cov, "price_sources");
                if (srcs.Count > 0)
                {
                    panel.Children.Add(MonUi.Head("Price source per instrument (" + srcs.Count.ToString(Inv) + ")"));
                    foreach (var ps in srcs)
                        panel.Children.Add(MonUi.Line("• " + (ShJson.Str(ps, "key") ?? "?") + (ShJson.Str(ps, "symbol") is string sy ? " (" + sy + ")" : "") + " — "
                                                      + (ShJson.Str(ps, "price_source") ?? "source text not reported"), 10.5, FundPalette.Dim));
                }
            }
            // §19.14: instruments left out of the diagnostics ("not available: no price source for …") — listed verbatim
            var excluded = ShJson.Arr(dg, "excluded").Concat(ShJson.Arr(cov, "excluded")).Select(ExcludedText).Where(x => x.Length > 0).Distinct().ToList();
            if (excluded.Count > 0)
            {
                panel.Children.Add(MonUi.Head("Excluded instruments — not priced, not counted (" + excluded.Count.ToString(Inv) + ")"));
                foreach (var ex in excluded) panel.Children.Add(MonUi.Line("• " + ex, 10.5, FundPalette.Risk));
            }
            var di = ShJson.Get(dg, "disposition");
            panel.Children.Add(Block("Disposition effect (Odean 1998)",
                "On every day with a sale: each held position is a realised gain/loss (sold that day) or a paper gain/loss (held). PGR = share of gains realised, "
                + "PLR = share of losses realised. DE = PGR − PLR > 0 means winners are sold faster than losers.", ShJson.Unavailable(di), new[]
                {
                    ("PGR / PLR", MonUi.Frac(ShJson.Num(di, "pgr"), 1) + " / " + MonUi.Frac(ShJson.Num(di, "plr"), 1)),
                    ("DE = PGR − PLR", MonUi.Frac(ShJson.Num(di, "de"), 1, signed: true) + "   ·   t-stat " + MonUi.Num(ShJson.Num(di, "t_stat"))),
                    ("Realised gains / losses", Num0(ShJson.Num(di, "realized_gains")) + " / " + Num0(ShJson.Num(di, "realized_losses"))),
                    ("Paper gains / losses", Num0(ShJson.Num(di, "paper_gains")) + " / " + Num0(ShJson.Num(di, "paper_losses"))),
                    ("Sell days · unpriced cases", Num0(ShJson.Num(di, "sell_days")) + " · " + Num0(ShJson.Num(di, "unpriced_cases"))),
                }));
            // §19.14 H2: disposition.excluded_instruments = the NUMBER of instruments not counted (their reasons: "Excluded instruments" above)
            if (ShJson.Unavailable(di) == null)
            {
                var notCounted = ShJson.Num(di, "excluded_instruments");
                panel.Children.Add(MonUi.Line(notCounted is double nc
                        ? "Instruments not counted in the disposition effect: " + Num0(nc) + (nc > 0 ? " — the reasons are listed under \"Excluded instruments\" above." : ".")
                        : "Instruments not counted in the disposition effect: not available (the backend did not report excluded_instruments).",
                    10.5, notCounted is double n2 && n2 == 0 ? FundPalette.Faint : FundPalette.Risk));
            }
            var tu = ShJson.Get(dg, "turnover");
            panel.Children.Add(Block("Overtrading / turnover", "Cost-valued (needs no prices): turnover = (volume / 2) / average cost basis, annualised.", ShJson.Unavailable(tu), new[]
            {
                ("Trades (buys / sells)", Num0(ShJson.Num(tu, "trades")) + " (" + Num0(ShJson.Num(tu, "buys")) + " / " + Num0(ShJson.Num(tu, "sells")) + ") · " + MonUi.Num(ShJson.Num(tu, "trades_per_month"), 1) + " per month"),
                ("Traded volume", MonUi.Money(ShJson.Num(tu, "volume"), cur)),
                ("Average cost basis", MonUi.Money(ShJson.Num(tu, "avg_cost_basis"), cur)),
                ("Annual turnover", ShJson.Num(tu, "turnover_annual") is double ta ? MonUi.Num(ta, 2) + " ×" : FundFormat.Dash),
                ("Fees", MonUi.Money(ShJson.Num(tu, "fees"), cur) + " · " + FundFormat.Pct(ShJson.Num(tu, "fee_share_pct"), 2) + " of volume · drag " + FundFormat.Pct(ShJson.Num(tu, "fee_drag_annual_pct"), 2) + " / year"),
                ("Round trips < 30 days", Num0(ShJson.Num(tu, "round_trips_under_30d"))),
            }));
            var ho = ShJson.Get(dg, "holding");
            panel.Children.Add(Block("Holding periods (closed FIFO lot parts, quantity-weighted)", "A disposition sign: losers held longer than winners.", ShJson.Unavailable(ho), new[]
            {
                ("Closed lot parts", Num0(ShJson.Num(ho, "closed_parts"))),
                ("Median / mean", Days(ShJson.Num(ho, "median_days")) + " / " + Days(ShJson.Num(ho, "mean_days"))),
                ("25 % / 75 %", Days(ShJson.Num(ho, "p25_days")) + " / " + Days(ShJson.Num(ho, "p75_days"))),
                ("Winners / losers (median)", Days(ShJson.Num(ho, "winners_median_days")) + " / " + Days(ShJson.Num(ho, "losers_median_days"))),
            }));
            var co = ShJson.Get(dg, "concentration");
            var now = ShJson.Get(co, "current");
            var avg = ShJson.Get(co, "average");
            panel.Children.Add(Block("Concentration (" + (ShJson.Str(co, "basis") ?? "cost") + "-valued weights)", "HHI = Σ w²; effective N = 1 / HHI.", ShJson.Unavailable(co), new[]
            {
                ("Now: HHI · effective N", MonUi.Num(ShJson.Num(now, "hhi"), 3) + " · " + MonUi.Num(ShJson.Num(now, "effective_n"), 1)),
                ("Now: largest / top 3", FundFormat.Pct(ShJson.Num(now, "top1_pct"), 1) + " / " + FundFormat.Pct(ShJson.Num(now, "top3_pct"), 1)),
                ("Average over days held: HHI · eff. N", MonUi.Num(ShJson.Num(avg, "hhi"), 3) + " · " + MonUi.Num(ShJson.Num(avg, "effective_n"), 1)),
                ("Average: largest / top 3", FundFormat.Pct(ShJson.Num(avg, "top1_pct"), 1) + " / " + FundFormat.Pct(ShJson.Num(avg, "top3_pct"), 1)),
                ("Positions", Num0(ShJson.Num(co, "positions"))),
            }));
            var al = ShJson.Get(dg, "after_losses");
            panel.Children.Add(Block("Trading after losses", "Buys within " + Num0(ShJson.Num(al, "window_days")) + " calendar days after a sale that realised a loss vs a gain.", ShJson.Unavailable(al), new[]
            {
                ("Loss sells / gain sells", Num0(ShJson.Num(al, "loss_sells")) + " / " + Num0(ShJson.Num(al, "gain_sells"))),
                ("Buys after a loss / after a gain (avg)", MonUi.Num(ShJson.Num(al, "buys_after_loss_avg")) + " / " + MonUi.Num(ShJson.Num(al, "buys_after_gain_avg")) + " · ratio " + MonUi.Num(ShJson.Num(al, "ratio"))),
                ("Buy size after losses / overall (avg)", MonUi.Money(ShJson.Num(al, "size_after_loss_avg"), cur) + " / " + MonUi.Money(ShJson.Num(al, "size_avg"), cur)),
            }));
            var ti = ShJson.Get(dg, "timing");
            panel.Children.Add(Block("Timing vs the market (" + (ShJson.Str(ti, "benchmark") ?? _benchmark) + ")",
                "Benchmark return over the next " + Num0(ShJson.Num(ti, "horizon_sessions")) + " sessions after your buys / sells vs the unconditional average over the same period.", ShJson.Unavailable(ti), new[]
            {
                ("Buys / sells", Num0(ShJson.Num(ti, "buys")) + " / " + Num0(ShJson.Num(ti, "sells"))),
                ("After buys", FundFormat.Pct(ShJson.Num(ti, "buy_fwd_avg_pct"), 2, signed: true)),
                ("After sells", FundFormat.Pct(ShJson.Num(ti, "sell_fwd_avg_pct"), 2, signed: true)),
                ("Unconditional", FundFormat.Pct(ShJson.Num(ti, "uncond_fwd_avg_pct"), 2, signed: true)),
            }));
            panel.Children.Add(MonUi.Line("Deterministic statistics on your imported transactions — descriptive, for your own review.", 10, FundPalette.Faint));
        }

        private static Border Block(string title, string? explain, string? unavailable, IEnumerable<(string, string)> rows)
        {
            var s = new StackPanel();
            s.Children.Add(FundUi.Text(title, 12, FundPalette.TextBright, bold: true));
            if (!string.IsNullOrWhiteSpace(explain)) s.Children.Add(MonUi.Line(explain, 10.5, FundPalette.Faint));
            if (unavailable != null) s.Children.Add(MonUi.Line("Unavailable: " + unavailable, 11, FundPalette.Risk));
            else s.Children.Add(MonUi.Pairs(rows));
            var b = FundUi.Panel(s, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 8));
            AutomationProperties.SetName(b, title + (unavailable != null ? ", unavailable: " + unavailable : ""));
            return b;
        }

        /// <summary>One excluded instrument {key, name, reason} as "key · name — reason" (plain text from the backend / your file).</summary>
        private static string ExcludedText(JsonElement e)
        {
            if (e.ValueKind != JsonValueKind.Object) return FundFormat.Plain(e);
            var key = ShJson.Str(e, "key") ?? "?";
            var name = ShJson.Str(e, "name");
            var reason = ShJson.Str(e, "reason") ?? "";
            return key + (string.IsNullOrWhiteSpace(name) ? "" : " · " + name) + (reason.Length > 0 ? " — " + reason : "");
        }

        private static string Num0(double? v) => v is double d ? d.ToString("N0", Inv) : FundFormat.Dash;

        private static string Days(double? v) => v is double d ? d.ToString("0.#", Inv) + " d" : FundFormat.Dash;

        // ----- shadow portfolios -----

        private void RenderShadows(StackPanel panel)
        {
            panel.Children.Add(MonUi.Line("Same cash flows: every rule portfolio receives exactly your dated investment flows (buys + fees in, sale proceeds out, per day, in the "
                                          + "account's base currency) at the day's close — the comparison is fair by construction, and the parity (Σ and every date) is checked "
                                          + "and shown per rule. Fractional units, no costs, no taxes. USD rule returns on EUR flows include the FX effect (FRED DEXUSEU).",
                                          10.5, FundPalette.Faint));
            var editor = new StackPanel();
            editor.Children.Add(FundUi.Text("Rules (1–5)", 11.5, FundPalette.Text, bold: true));
            for (var i = 0; i < _rules.Count; i++)
            {
                var index = i;
                var row = new Grid { Margin = new Thickness(0, 3, 0, 3) };
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(220) });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                var type = new ComboBox { FontSize = 12, MinHeight = 24, Margin = new Thickness(0, 0, 8, 0) };
                foreach (var (id, label) in new[] { ("buy_and_hold", "Buy & hold (ticker)"), ("monthly_rebalance", "Monthly rebalance (weights)"), ("strategy", "Strategy backtest (strategy id)") })
                {
                    var item = new ComboBoxItem { Content = label, Tag = id };
                    type.Items.Add(item);
                    if (id == _rules[i].Type) type.SelectedItem = item;
                }
                AutomationProperties.SetName(type, "Rule " + (i + 1).ToString(Inv) + " type");
                var param = FundUi.Input(_rules[i].Param);
                param.MaxLength = 200;
                AutomationProperties.SetName(param, "Rule " + (i + 1).ToString(Inv) + " parameter: a ticker, weights like SPY=0.6, AGG=0.4, or a strategy id");
                param.ToolTip = "Buy & hold: a ticker (SPY). Monthly rebalance: SPY=0.6, AGG=0.4 (sum 1, ≤ 10 tickers, first session of each month). Strategy: its id (s-…) — the latest done backtest card of its current version must cover the period.";
                type.SelectionChanged += (_, _) => { if (type.SelectedItem is ComboBoxItem { Tag: string t }) _rules[index] = (t, param.Text); };
                param.TextChanged += (_, _) => _rules[index] = (_rules[index].Type, param.Text);
                Grid.SetColumn(param, 1);
                var remove = MonUi.Small("✕", "Remove rule " + (i + 1).ToString(Inv));
                remove.Margin = new Thickness(8, 0, 0, 0);
                remove.IsEnabled = _rules.Count > 1;
                remove.Click += (_, _) => { _rules.RemoveAt(index); RenderSection(); };
                Grid.SetColumn(remove, 2);
                row.Children.Add(type);
                row.Children.Add(param);
                row.Children.Add(remove);
                editor.Children.Add(row);
            }
            var buttons = new WrapPanel { Margin = new Thickness(0, 6, 0, 0) };
            var add = MonUi.Small("+ Add rule", "Add a rule");
            add.IsEnabled = _rules.Count < 5;
            add.Click += (_, _) => { if (_rules.Count < 5) { _rules.Add(("buy_and_hold", "")); RenderSection(); } };
            buttons.Children.Add(add);
            var compare = TradingUi.Primary("Compare");
            AutomationProperties.SetName(compare, "Compare your account with the rule portfolios on the same cash flows");
            compare.Click += async (_, _) => await RunShadowsAsync();
            buttons.Children.Add(compare);
            editor.Children.Add(buttons);
            panel.Children.Add(FundUi.Panel(editor, new Thickness(14, 10, 14, 10), new Thickness(0, 4, 0, 10)));

            if (_shadowError != null) panel.Children.Add(MonUi.ErrorBlock(_shadowError));
            if (_shadowResult is not JsonElement r) return;
            var cur = ShJson.Str(r, "base_currency") ?? "";
            var period = ShJson.Get(r, "period");
            var flows = ShJson.Get(r, "flows");
            var user = ShJson.Get(r, "user");
            var res = new StackPanel();
            res.Children.Add(FundUi.Text("Comparison " + (ShJson.Str(period, "first") ?? "—") + " … " + (ShJson.Str(period, "last") ?? "—"), 12.5, FundPalette.TextBright, bold: true));
            res.Children.Add(MonUi.Line("Cash flows: " + Num0(ShJson.Num(flows, "count")) + " dated flows · in " + MonUi.Money(ShJson.Num(flows, "sum_in"), cur)
                                        + " · out " + MonUi.Money(ShJson.Num(flows, "sum_out"), cur), 10.5, FundPalette.Dim, mono: true));
            var rows = new List<IReadOnlyList<string>>
            {
                new[]
                {
                    "You (your account)",
                    // §19.16 review fixes: end_value null with user.message (e.g. "no trades imported") — the message verbatim, never 0.00
                    ShJson.Bool(user, "valued") == false || ShJson.Num(user, "end_value") == null ? "—" : MonUi.Money(ShJson.Num(user, "end_value"), cur),
                    MonUi.Money(ShJson.Num(user, "pnl"), cur), MwrText(ShJson.Num(user, "mwr_annual")), "—", "—",
                    ShJson.Str(user, "message") is string um && um.Length > 0
                        ? um
                        : ShJson.Bool(user, "valued") == false || ShJson.Num(user, "end_value") == null ? "not available (no reason reported)" : "",
                },
            };
            foreach (var s in ShJson.Arr(r, "shadows"))
            {
                var label = ShJson.Str(s, "label") ?? "rule";
                var un = ShJson.Str(s, "unavailable");
                if (un != null) { rows.Add(new[] { label, "—", "—", "—", "—", "—", "unavailable: " + un }); continue; }
                var parity = ShJson.Get(s, "parity");
                var parityText = ShJson.Bool(parity, "flows_equal") == true
                    ? "same flows ✓ (" + Num0(ShJson.Num(parity, "count")) + ")"
                    : "FLOWS DIFFER — max diff " + MonUi.Num(ShJson.Num(parity, "max_abs_diff"), 4);
                var cashMin = ShJson.Num(s, "cash_min");
                rows.Add(new[]
                {
                    label, MonUi.Money(ShJson.Num(s, "end_value"), cur), MonUi.Money(ShJson.Num(s, "pnl"), cur), MwrText(ShJson.Num(s, "mwr_annual")),
                    MonUi.Frac(ShJson.Num(s, "twr"), 2, signed: true), parityText,
                    string.Join(" · ", new[]
                    {
                        cashMin is double cm && cm < 0 ? "cash below 0 (financed at 0 %): min " + MonUi.Money(cm, cur) : "",
                        ShJson.Str(s, "conversion") ?? "",   // §20.11: "simulated in USD — converted at the FRED rate of each flow date"
                    }.Where(x => x.Length > 0)),
                });
            }
            res.Children.Add(MonUi.Table(new[] { "Portfolio", "End value", "P&L", "MWR / year", "TWR", "Cash-flow parity", "Note" }, rows, "Shadow comparison",
                                         new double[] { 0, 110, 110, 80, 70, 150, 0 }));
            if (ShJson.Bool(user, "valued") == false)
                res.Children.Add(MonUi.Line("Your end value needs a price for every position (market data or your valuation in Overview → Set valuation prices…). The shadows are shown anyway.", 10.5, FundPalette.Risk));
            foreach (var w in ShJson.Strings(r, "warnings")) res.Children.Add(MonUi.Line("⚠ " + w, 10.5, FundPalette.Risk));
            res.Children.Add(MonUi.Line(ShJson.Str(r, "notice") ?? "Notice not available (the backend sent none with this comparison).", 10.5, FundPalette.Faint));
            res.Children.Add(MonUi.Line("MWR = money-weighted return (XIRR, annualised) on the identical flows; \"—\" when no annualised root exists (e.g. only a few days of history) — never a stand-in.",
                                        10, FundPalette.Faint));
            panel.Children.Add(FundUi.Panel(res, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10)));
        }

        private static string MwrText(double? v) => v is double d ? MonUi.Frac(d, 2, signed: true) : FundFormat.Dash;

        /// <summary>The rule editor as request objects, or an error text (checked again by the backend).</summary>
        private (List<Dictionary<string, object?>>? Rules, string? Error) BuildRules()
        {
            var rules = new List<Dictionary<string, object?>>();
            for (var i = 0; i < _rules.Count; i++)
            {
                var (type, param) = _rules[i];
                var p = (param ?? "").Trim();
                var n = "Rule " + (i + 1).ToString(Inv) + ": ";
                switch (type)
                {
                    case "buy_and_hold":
                        var sym = p.ToUpperInvariant();
                        if (!SymbolRe.IsMatch(sym)) return (null, n + "a ticker such as SPY.");
                        rules.Add(new Dictionary<string, object?> { ["type"] = type, ["symbol"] = sym });
                        break;
                    case "monthly_rebalance":
                        var weights = new Dictionary<string, object?>();
                        double sum = 0;
                        foreach (var part in p.Split(new[] { ',', ';' }, StringSplitOptions.RemoveEmptyEntries))
                        {
                            var kv = part.Split('=');
                            if (kv.Length != 2) return (null, n + "weights as SPY=0.6, AGG=0.4.");
                            var k = kv[0].Trim().ToUpperInvariant();
                            if (!SymbolRe.IsMatch(k)) return (null, n + "\"" + kv[0].Trim() + "\" is not a ticker.");
                            if (!double.TryParse(kv[1].Trim(), NumberStyles.Float, Inv, out var w) || w <= 0 || w > 1) return (null, n + "each weight in (0, 1], with a dot as decimal separator.");
                            weights[k] = w;
                            sum += w;
                        }
                        if (weights.Count < 1 || weights.Count > 10) return (null, n + "1 to 10 tickers.");
                        if (Math.Abs(sum - 1.0) > 1e-6) return (null, n + "the weights must sum to 1 (now " + sum.ToString("0.####", Inv) + ").");
                        rules.Add(new Dictionary<string, object?> { ["type"] = type, ["weights"] = weights });
                        break;
                    case "strategy":
                        if (p.Length < 1 || p.Length > 64) return (null, n + "a strategy id (s-…).");
                        rules.Add(new Dictionary<string, object?> { ["type"] = type, ["strategy_id"] = p });
                        break;
                    default:
                        return (null, n + "choose a rule type.");
                }
            }
            if (rules.Count == 0) return (null, "Add at least one rule.");
            return (rules, null);
        }

        private async Task RunShadowsAsync()
        {
            if (_busy || _accountId == null) return;
            var (rules, error) = BuildRules();
            if (rules == null) { _shadowError = error; RenderSection(); return; }
            _busy = true;
            SetStatus("Comparing (reads daily closes from the data layer)…", FundPalette.Faint);
            try
            {
                var res = await PlatformClient.RunShadowPortfoliosAsync(_accountId, rules);
                if (_stopped) return;
                if (res.Ok && res.Value.ValueKind == JsonValueKind.Object) { _shadowResult = res.Value; _shadowError = null; }
                else { _shadowResult = null; _shadowError = res.Ok ? "Unexpected response from the backend (shadows)." : PlatformClient.DescribeFailure(res); }
            }
            finally { _busy = false; SetStatus(null); }
            RenderSection();
        }

        // ----- export & AI -----

        private void RenderExport(StackPanel panel)
        {
            var ex = new StackPanel();
            ex.Children.Add(FundUi.Text("Audit report (local file)", 12, FundPalette.TextBright, bold: true));
            ex.Children.Add(MonUi.Line("Account, imports (file name, sha256, template, counts), period, positions, diagnostics, the shadow comparison (the rules of the Shadow "
                                       + "portfolios tab), method notes and limits. HTML: self-contained (inline CSS, no script, no external resource, every text escaped). "
                                       + "The backend builds it on this computer; you choose where to save it. Benchmark: " + _benchmark + ".", 10.5, FundPalette.Faint));
            var row = new WrapPanel { Margin = new Thickness(0, 4, 0, 4) };
            var group = "fmt" + GetHashCode().ToString(Inv);
            var html = TradingUi.Radio(group, "HTML", "Report format HTML", _reportFormat == "html");
            var json = TradingUi.Radio(group, "JSON", "Report format JSON", _reportFormat == "json");
            html.Checked += (_, _) => _reportFormat = "html";
            json.Checked += (_, _) => _reportFormat = "json";
            row.Children.Add(html);
            row.Children.Add(json);
            var export = TradingUi.Primary("Export audit report…");
            AutomationProperties.SetName(export, "Export the audit report and save it to a local file");
            export.Click += async (_, _) => await ExportReportAsync();
            row.Children.Add(export);
            ex.Children.Add(row);
            if (_exportMessage != null) ex.Children.Add(MonUi.Line(_exportMessage, 10.5, _exportMessage.StartsWith("Saved", StringComparison.Ordinal) ? FundPalette.Long : FundPalette.Kill));
            panel.Children.Add(FundUi.Panel(ex, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10)));

            var accountId = _accountId!;
            _commentary ??= new MonCommentaryPanel("Shadow Account diagnostics and shadows",
                "It receives ONLY the diagnostics' fact table (positions as \"position 1..n\"; no names, ISINs, symbols or file names). Your statements never leave this computer.",
                (model, language) => PlatformClient.CreateShadowCommentaryAsync(accountId, model, language, _benchmark));
            if (_commentary.Parent is Panel old) old.Children.Remove(_commentary);
            if (_commentary.TriggerButton.Parent is Panel oldRow) oldRow.Children.Remove(_commentary.TriggerButton);
            var ai = new StackPanel();
            var aiRow = new StackPanel { Orientation = Orientation.Horizontal };
            aiRow.Children.Add(_commentary.TriggerButton);
            aiRow.Children.Add(new TextBlock { Text = "  optional · labelled AI output · costs money (budget \"reports\")", FontSize = 10.5, Foreground = FundPalette.Faint, VerticalAlignment = VerticalAlignment.Center });
            ai.Children.Add(aiRow);
            ai.Children.Add(_commentary);
            panel.Children.Add(FundUi.Panel(ai, new Thickness(14, 10, 14, 10), new Thickness(0, 0, 0, 10)));
        }

        // dlgguard (2026-10-23): one at a time — queued clicks / a second shortcut return at once; the dialog itself is open-once
        private Task ExportReportAsync() => DialogGuard.RunAsync("shadow-export-report", () => ExportReportCoreAsync());

        private async Task ExportReportCoreAsync()
        {
            if (_busy || _accountId == null) return;
            var (rules, _) = BuildRules();
            _busy = true;
            SetStatus("Building the audit report…", FundPalette.Faint);
            try
            {
                var res = await PlatformClient.ExportShadowReportAsync(_accountId, _reportFormat, rules, _benchmark);
                if (_stopped) return;
                if (!res.Ok || res.Value?.Content == null)
                {
                    _exportMessage = res.Ok ? "Unexpected response from the backend (report)." : PlatformClient.DescribeFailure(res);
                    return;
                }
                var ext = _reportFormat == "json" ? ".json" : ".html";
                var dialog = new Microsoft.Win32.SaveFileDialog
                {
                    Title = "Save the Shadow Account audit report",
                    Filter = _reportFormat == "json" ? "JSON (*.json)|*.json" : "HTML (*.html)|*.html",
                    DefaultExt = ext,
                    AddExtension = true,
                    OverwritePrompt = true,
                    FileName = MonitoringView.SafeFileName(res.Value.FileName, "shadow-audit-report" + ext),
                };
                var owner = Owner;
                var ok = owner != null ? dialog.ShowDialog(owner) : dialog.ShowDialog();
                if (ok != true) { _exportMessage = "Built but not saved (cancelled)."; return; }
                try
                {
                    var bytes = new UTF8Encoding(false).GetBytes(res.Value.Content);
                    File.WriteAllBytes(dialog.FileName, bytes);
                    var sha = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
                    _exportMessage = "Saved " + dialog.FileName + " · sha256 " + Short(sha)
                                     + (string.Equals(sha, res.Value.Sha256, StringComparison.OrdinalIgnoreCase) ? " (matches the backend ✓)" : " (differs from the backend's " + Short(res.Value.Sha256) + ")")
                                     + " · local file only.";
                }
                catch (Exception e) when (e is IOException || e is UnauthorizedAccessException || e is System.Security.SecurityException)
                {
                    _exportMessage = "The file could not be written: " + e.Message;
                }
            }
            finally
            {
                _busy = false;
                SetStatus(null);
                RenderSection();
            }
        }

        // =====================================================================================
        // Actions: create account, import, valuation, classification
        // =====================================================================================

        private async Task CreateAccountAsync()
        {
            var dialog = new ShadowAccountDialog(Owner);
            if (dialog.ShowDialog() != true || dialog.Created?.Id == null) return;
            _accountId = dialog.Created.Id;
            SetStatus("Shadow account " + (dialog.Created.Label ?? "") + " created — now import a statement.", FundPalette.Long);
            await RefreshAsync();
        }

        // dlgguard (2026-10-23): one at a time — queued clicks / a second shortcut return at once; the dialog itself is open-once
        private Task ImportAsync() => DialogGuard.RunAsync("shadow-import", () => ImportCoreAsync());

        private async Task ImportCoreAsync()
        {
            if (_busy) return;
            if (_accountId == null)
            {
                SetStatus("Create a Shadow account first (\"New Shadow account…\").", FundPalette.Risk);
                return;
            }
            if (_templates == null)
            {
                var t = await PlatformClient.GetShadowTemplatesAsync();
                if (!t.Ok || t.Value == null) { SetStatus("The import templates could not be loaded: " + PlatformClient.DescribeFailure(t), FundPalette.Kill); return; }
                _templates = t.Value;
            }
            var label = _accounts.FirstOrDefault(a => a.Id == _accountId)?.DisplayName ?? _accountId;
            var wizard = new ShadowImportWizard(Owner, _accountId, label, _templates, _profiles);
            wizard.ShowDialog();
            if (wizard.ProfilesChanged)
            {
                var p = await PlatformClient.GetShadowProfilesAsync();
                if (p.Ok && p.Value?.Profiles != null) _profiles = p.Value.Profiles;
            }
            if (wizard.Imported != null)
            {
                SetStatus("Imported " + (wizard.Imported.FileName ?? "the file") + ": " + MonUi.Int(wizard.Imported.Accepted) + " accepted, " + MonUi.Int(wizard.Imported.Duplicates)
                          + " duplicates, " + MonUi.Int(wizard.Imported.Rejected) + " rejected, " + MonUi.Int(wizard.Imported.Skipped) + " skipped.", FundPalette.Long);
                await RefreshAsync();
            }
        }

        /// <summary>§19.14: remove ONE import batch (database rows only — your file is untouched); one confirmation step, optional note (fix1023).</summary>
        // dlgguard (2026-10-23): one at a time — queued clicks / a second shortcut return at once; the dialog itself is open-once
        private Task RemoveImportAsync(ShImportSummary imp) => DialogGuard.RunAsync("shadow-remove-import:" + imp.Id, () => RemoveImportCoreAsync(imp));

        private async Task RemoveImportCoreAsync(ShImportSummary imp)
        {
            if (_busy || _accountId == null || string.IsNullOrEmpty(imp.Id)) return;
            var accountId = _accountId;
            var importId = imp.Id!;
            var removed = 0;
            var message = "Removes the import " + (imp.FileName ?? "") + " (" + importId + ", " + TradingText.Ts(imp.CreatedAt) + ") and its " + MonUi.Int(imp.Accepted)
                          + " transaction(s) from the local database — positions, diagnostics and shadows are recomputed without them. Your file on disk is "
                          + "not touched; you can import it again afterwards. A removal record and an audit row are kept.";
            var okText = "Remove import " + (string.IsNullOrWhiteSpace(imp.FileName) ? importId : imp.FileName!) + " (" + MonUi.Int(imp.Accepted)
                         + (imp.Accepted == 1 ? " transaction)" : " transactions)");
            var dialog = new BrokerConfirmDialog(Owner, "Remove import", message, null, okText, danger: true,
                async reason =>
                {
                    var res = await PlatformClient.RemoveShadowImportAsync(accountId, importId, reason);
                    if (!res.Ok) return PlatformClient.DescribeFailure(res);
                    removed = (int)(ShJson.Num(ShJson.Get(res.Value, "removed"), "transactions") ?? 0);
                    return null;
                });
            if (dialog.ShowDialog() != true) return;
            SetStatus("Import " + importId + " removed (" + removed.ToString("N0", Inv) + " transactions).", FundPalette.Long);
            await RefreshAsync();
        }

        private async Task SetValuationAsync()
        {
            if (_accountId == null || _detail == null) return;
            var dialog = new ShadowValuationDialog(Owner, _accountId, _detail.Positions ?? new List<ShPosition>(), _detail.Valuation);
            if (dialog.ShowDialog() != true) return;
            SetStatus("Valuation saved.", FundPalette.Long);
            _diag = null;
            _diagError = null;
            _shadowResult = null;
            await LoadAccountAsync();
        }

        private async Task OpenSymbolMappingAsync()
        {
            var window = new ShadowSymbolMappingsWindow(Owner, _detail?.Positions ?? new List<ShPosition>());
            window.ShowDialog();
            if (!window.Changed) return;
            SetStatus("Symbol mapping changed — positions are re-priced from their mapped listing.", FundPalette.Long);
            _diag = null;
            _diagError = null;
            _shadowResult = null;
            await LoadAccountAsync();
        }

        private async Task ClassifyAsync()
        {
            if (_detail == null) return;
            var classes = _templates?.AssetClasses;
            if (classes == null || classes.Count == 0)
            {
                SetStatus("Classification not available: the backend did not report the asset classes (/v2/shadow/templates) — press F5 or restart the Hedge Fund backend.", FundPalette.Kill);
                return;
            }
            var dialog = new ShadowClassifyDialog(Owner, _detail.Positions ?? new List<ShPosition>(), classes);
            if (dialog.ShowDialog() != true) return;
            SetStatus("Classification saved — it is used by the diagnostics and the price lookups.", FundPalette.Long);
            _diag = null;
            _diagError = null;
            await LoadAccountAsync();
        }
    }
}
