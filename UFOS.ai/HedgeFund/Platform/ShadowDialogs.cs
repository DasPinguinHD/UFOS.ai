using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Dialogs of the Shadow Account (SPEC §19.6–§19.8): new account, valuation prices, classification, generic column-mapping
    // profile and the import wizard. Chrome: FundPopupWindow (borderless, full-screen safe, Esc closes). Requests are sent from
    // the dialog; the backend's answer (incl. 400 field errors) is shown inline, verbatim. Every text from an imported file is
    // plain text. Your statements stay on this computer — the backend runs on loopback and stores them in its local database.
    // =====================================================================================

    internal sealed class ShadowAccountDialog : FundPopupWindow
    {
        private readonly TextBox _label;
        private readonly RadioButton _usd;
        private readonly RadioButton _other;
        private readonly TextBlock _error;
        private readonly Button _ok;
        private bool _busy;
        private bool _closed;

        public ShAccount? Created { get; private set; }

        public ShadowAccountDialog(Window? owner) : base(owner, "New Shadow account", 520, 420, resizable: false)
        {
            SizeToContent = SizeToContent.Height;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("New Shadow account", 14, FundPalette.TextBright, bold: true));
            stack.Children.Add(MonUi.Line("One Shadow account per real broker account of yours (own accounts only). " + ShadowAccountView.LocalNotice, 11, FundPalette.Dim));
            stack.Children.Add(TradingUi.Label("Label (1–60 characters, e.g. \"My TR depot\")"));
            _label = FundUi.Input("", mono: false);
            _label.MaxLength = 60;
            AutomationProperties.SetName(_label, "Label of the Shadow account");
            stack.Children.Add(_label);
            stack.Children.Add(TradingUi.Label("Base currency (the account's cash currency)"));
            var cur = new StackPanel { Orientation = Orientation.Horizontal };
            var eur = TradingUi.Radio("shcur", "EUR", "Base currency EUR", true);
            _usd = TradingUi.Radio("shcur", "USD", "Base currency USD", false);
            cur.Children.Add(eur);
            cur.Children.Add(_usd);
            stack.Children.Add(cur);
            stack.Children.Add(TradingUi.Label("Broker country (where the broker is based)"));
            var country = new StackPanel { Orientation = Orientation.Horizontal };
            var de = TradingUi.Radio("shcty", "DE (German broker)", "Broker country Germany", true);
            _other = TradingUi.Radio("shcty", "Other", "Broker country other than Germany", false);
            country.Children.Add(de);
            country.Children.Add(_other);
            stack.Children.Add(country);
            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);
            _ok = TradingUi.Primary("Create");
            _ok.IsEnabled = false;
            _ok.IsDefault = true;
            AutomationProperties.SetName(_ok, "Create the Shadow account");
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { if (!_busy && !_closed) DialogResult = false; };
            _ok.Click += async (_, _) => await SubmitAsync();
            _label.TextChanged += (_, _) => _ok.IsEnabled = !_busy && _label.Text.Trim().Length is >= 1 and <= 60;
            Closed += (_, _) => _closed = true;
            SetBody(stack);
            Loaded += (_, _) => _label.Focus();
        }

        private async Task SubmitAsync()
        {
            var label = _label.Text.Trim();
            if (_busy || label.Length < 1 || label.Length > 60) return;
            _busy = true;
            _ok.IsEnabled = false;
            var res = await PlatformClient.CreateShadowAccountAsync(label, _usd.IsChecked == true ? "USD" : "EUR", _other.IsChecked == true ? "other" : "DE");
            _busy = false;
            if (_closed) return; // Esc / close while the request ran — the window is gone (no DialogResult on a closed window)
            if (res.Ok && res.Value?.Account != null)
            {
                Created = res.Value.Account;
                DialogResult = true;
                return;
            }
            TradingUi.ShowError(_error, res.Ok ? "Unexpected response from the backend." : PlatformClient.DescribeFailure(res));
            _ok.IsEnabled = true;
        }
    }

    /// <summary>Your own valuation prices (as of a day) — used for instruments without market data (e.g. ISIN only); never a stand-in.</summary>
    internal sealed class ShadowValuationDialog : FundPopupWindow
    {
        private readonly string _accountId;
        private readonly TextBox _asOf;
        private readonly List<(string Key, string? Currency, TextBox Box)> _boxes = new();
        private readonly TextBlock _error;
        private readonly Button _ok;
        private bool _busy;
        private bool _closed;

        public ShadowValuationDialog(Window? owner, string accountId, IReadOnlyList<ShPosition> positions, ShValuation? current)
            : base(owner, "Valuation prices", 640, 620, resizable: true)
        {
            _accountId = accountId;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Your valuation prices", 14, FundPalette.TextBright, bold: true));
            stack.Children.Add(MonUi.Line("A price per unit in the instrument's currency, as of one day. Your price wins over market data on that day. Leave a field empty "
                                          + "to keep it unpriced (it is then counted as unpriced — never replaced by a stand-in).", 11, FundPalette.Dim));
            stack.Children.Add(TradingUi.Label("As of (YYYY-MM-DD)"));
            _asOf = FundUi.Input(current?.AsOf ?? DateTime.Now.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture));
            _asOf.MaxLength = 10;
            _asOf.Width = 130;
            _asOf.HorizontalAlignment = HorizontalAlignment.Left;
            AutomationProperties.SetName(_asOf, "Valuation date, YYYY-MM-DD");
            stack.Children.Add(_asOf);
            // existing prices by (key, currency); a price stored without a currency matches its key in any currency
            var prices = (current?.Prices ?? new List<ShPrice>()).Where(p => p.Key != null)
                .GroupBy(p => p.Key! + "|" + (p.Currency ?? "")).ToDictionary(g => g.Key, g => g.Last().Price);
            foreach (var p in positions.Where(p => !string.IsNullOrEmpty(p.Key)).Take(500))
            {
                var row = new Grid { Margin = new Thickness(0, 3, 0, 3) };
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(140) });
                var text = (p.Key ?? "") + (string.IsNullOrWhiteSpace(p.Name) ? "" : " · " + p.Name) + " · qty " + FundFormat.Qty(p.Qty) + " · " + (p.Currency ?? "?");
                row.Children.Add(FundUi.Text(text, 11, FundPalette.Text));
                var known = prices.TryGetValue(p.Key! + "|" + (p.Currency ?? ""), out var old) || prices.TryGetValue(p.Key! + "|", out old);
                var box = FundUi.Input(known && old is double o ? o.ToString("0.####", CultureInfo.InvariantCulture) : "");
                box.MaxLength = 20;
                AutomationProperties.SetName(box, "Price of " + text);
                Grid.SetColumn(box, 1);
                row.Children.Add(box);
                stack.Children.Add(row);
                _boxes.Add((p.Key!, p.Currency, box));
            }
            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);
            _ok = TradingUi.Primary("Save valuation");
            AutomationProperties.SetName(_ok, "Save the valuation prices");
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { if (!_busy && !_closed) DialogResult = false; };
            _ok.Click += async (_, _) => await SubmitAsync();
            Closed += (_, _) => _closed = true;
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
            Loaded += (_, _) => _asOf.Focus();
        }

        private async Task SubmitAsync()
        {
            if (_busy) return;
            var asOf = _asOf.Text.Trim();
            if (!DateTime.TryParseExact(asOf, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out _))
            {
                TradingUi.ShowError(_error, "The date must be written as YYYY-MM-DD.");
                return;
            }
            var list = new List<(string Key, double Price, string? Currency)>();
            foreach (var (key, currency, box) in _boxes)
            {
                var t = box.Text.Trim();
                if (t.Length == 0) continue;
                if (!double.TryParse(t, NumberStyles.Float, CultureInfo.InvariantCulture, out var v) || v <= 0 || double.IsInfinity(v))
                {
                    TradingUi.ShowError(_error, key + ": a price > 0 with a dot as decimal separator.");
                    return;
                }
                list.Add((key, v, currency));
            }
            if (list.Count == 0) { TradingUi.ShowError(_error, "Enter at least one price."); return; }
            _busy = true;
            _ok.IsEnabled = false;
            var res = await PlatformClient.PutShadowValuationAsync(_accountId, asOf, list);
            _busy = false;
            if (_closed) return;
            if (res.Ok) { DialogResult = true; return; }
            TradingUi.ShowError(_error, PlatformClient.DescribeFailure(res));
            _ok.IsEnabled = true;
        }
    }

    /// <summary>Classify an instrument (never guessed from its name) — used by the diagnostics and the price lookups.</summary>
    internal sealed class ShadowClassifyDialog : FundPopupWindow
    {
        private readonly ComboBox _key;
        private readonly ComboBox _class;
        private readonly TextBox _reason;
        private readonly TextBlock _error;
        private readonly Button _ok;
        private bool _busy;
        private bool _closed;

        public ShadowClassifyDialog(Window? owner, IReadOnlyList<ShPosition> positions, IReadOnlyList<string> classes)
            : base(owner, "Classify an instrument", 560, 420, resizable: false)
        {
            SizeToContent = SizeToContent.Height;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Classify an instrument", 14, FundPalette.TextBright, bold: true));
            stack.Children.Add(MonUi.Line("The statement's type column or your classification decides the asset class — it is never guessed from the name. "
                                          + "The diagnostics and the price lookups use it (market prices only for US-listed shares / ETFs in USD).", 11, FundPalette.Dim));
            stack.Children.Add(TradingUi.Label("Instrument"));
            _key = new ComboBox { FontSize = 12, MinHeight = 24 };
            AutomationProperties.SetName(_key, "Instrument to classify");
            foreach (var p in positions.Where(p => !string.IsNullOrEmpty(p.Key)))
            {
                var text = p.Key + (string.IsNullOrWhiteSpace(p.Name) ? "" : " · " + p.Name) + " · now: " + (p.AssetClass ?? "unclassified");
                _key.Items.Add(new ComboBoxItem { Content = text, Tag = p.Key });
            }
            if (_key.Items.Count > 0) _key.SelectedIndex = 0;
            stack.Children.Add(_key);
            stack.Children.Add(TradingUi.Label("Asset class"));
            _class = new ComboBox { FontSize = 12, MinHeight = 24 };
            AutomationProperties.SetName(_class, "Asset class");
            foreach (var c in classes) _class.Items.Add(new ComboBoxItem { Content = c, Tag = c });
            if (_class.Items.Count > 0) _class.SelectedIndex = 0;
            stack.Children.Add(_class);
            stack.Children.Add(TradingUi.Label("Reason (optional, stored with the classification)"));
            _reason = FundUi.Input("", mono: false);
            _reason.MaxLength = 200;
            AutomationProperties.SetName(_reason, "Reason for the classification (optional)");
            stack.Children.Add(_reason);
            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);
            _ok = TradingUi.Primary("Save");
            AutomationProperties.SetName(_ok, "Save the classification");
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { if (!_busy && !_closed) DialogResult = false; };
            _ok.Click += async (_, _) => await SubmitAsync();
            Closed += (_, _) => _closed = true;
            SetBody(stack);
            Loaded += (_, _) => _key.Focus();
        }

        private async Task SubmitAsync()
        {
            if (_busy || _key.SelectedItem is not ComboBoxItem { Tag: string key } || _class.SelectedItem is not ComboBoxItem { Tag: string cls }) return;
            _busy = true;
            _ok.IsEnabled = false;
            var res = await PlatformClient.PutShadowClassificationAsync(key, cls, _reason.Text);
            _busy = false;
            if (_closed) return;
            if (res.Ok) { DialogResult = true; return; }
            TradingUi.ShowError(_error, PlatformClient.DescribeFailure(res));
            _ok.IsEnabled = true;
        }
    }

    /// <summary>
    /// A generic column-mapping profile (§19.6): delimiter, decimal / thousands separators, date format, encoding, the header of
    /// each canonical field (date and type required) and the type map (raw type → kind, "ignore" skips a row). Saved as a new
    /// version per name (insert-only). The headers of the chosen file are offered as choices (plain text).
    /// </summary>
    internal sealed class ShadowProfileDialog : FundPopupWindow
    {
        private readonly TextBox _name;
        private readonly ComboBox _delimiter;
        private readonly ComboBox _decimal;
        private readonly ComboBox _thousands;
        private readonly TextBox _dateFormat;
        private readonly ComboBox _encoding;
        private readonly ComboBox _currency;
        private readonly ComboBox _sign;
        private readonly Dictionary<string, ComboBox> _columns = new(StringComparer.Ordinal);
        private readonly TextBox _typeMap;
        private readonly IReadOnlyList<string> _kinds;
        private readonly TextBlock _error;
        private readonly Button _ok;
        private bool _busy;
        private bool _closed;

        public ShProfile? Created { get; private set; }

        public ShadowProfileDialog(Window? owner, IReadOnlyList<string> header, IReadOnlyList<string> canonical, IReadOnlyList<string> kinds, string? delimiterGuess)
            : base(owner, "Column mapping profile", 720, 760, resizable: true)
        {
            _kinds = kinds;
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Generic importer — column mapping", 14, FundPalette.TextBright, bold: true));
            stack.Children.Add(MonUi.Line("Tell the importer which column of your file holds which field. Saved as a profile (a new version per name) so the next statement of the "
                                          + "same broker imports with one click. Nothing in the file is evaluated.", 11, FundPalette.Dim));
            stack.Children.Add(TradingUi.Label("Profile name (1–60 characters)"));
            _name = FundUi.Input("", mono: false);
            _name.MaxLength = 60;
            AutomationProperties.SetName(_name, "Profile name");
            stack.Children.Add(_name);

            var grid = new Grid { Margin = new Thickness(0, 6, 0, 0) };
            for (var c = 0; c < 4; c++) grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            _delimiter = Choice(grid, 0, 0, "Delimiter", new[] { ("auto", "auto-detect"), (";", "; semicolon"), (",", ", comma"), ("\t", "tab") }, delimiterGuess ?? "auto");
            _decimal = Choice(grid, 0, 1, "Decimal separator", new[] { (",", ", (German)"), (".", ". (English)") }, ",");
            _thousands = Choice(grid, 0, 2, "Thousands separator", new[] { ("", "none"), (".", "."), (",", ","), (" ", "space"), ("'", "'") }, ".");
            _encoding = Choice(grid, 0, 3, "Encoding", new[] { ("auto", "auto (UTF-8, else Windows-1252)"), ("utf-8", "UTF-8"), ("cp1252", "Windows-1252") }, "auto");
            _currency = Choice(grid, 1, 0, "Default currency", new[] { ("EUR", "EUR"), ("USD", "USD") }, "EUR");
            _sign = Choice(grid, 1, 1, "Amounts", new[] { ("signed", "signed (− = money out)"), ("positive", "always positive") }, "signed");
            var df = new StackPanel { Margin = new Thickness(0, 0, 8, 0) };
            df.Children.Add(TradingUi.Label("Date format (strptime)"));
            _dateFormat = FundUi.Input("%d.%m.%Y");
            _dateFormat.MaxLength = 30;
            AutomationProperties.SetName(_dateFormat, "Date format, for example %d.%m.%Y or %Y-%m-%d");
            df.Children.Add(_dateFormat);
            Grid.SetRow(df, 1);
            Grid.SetColumn(df, 2);
            Grid.SetColumnSpan(df, 2);
            grid.Children.Add(df);
            stack.Children.Add(grid);

            stack.Children.Add(TradingUi.Head("Columns (date and type are required)"));
            foreach (var field in canonical)
            {
                var row = new Grid { Margin = new Thickness(0, 2, 0, 2) };
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(130) });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                var required = field == "date" || field == "type";
                row.Children.Add(FundUi.Text(field + (required ? " *" : ""), 11.5, required ? FundPalette.TextBright : FundPalette.Text));
                var box = new ComboBox { IsEditable = true, FontSize = 12, MinHeight = 24 };
                box.Items.Add(new ComboBoxItem { Content = "", Tag = "" });
                foreach (var h in header.Where(h => !string.IsNullOrWhiteSpace(h)).Distinct()) box.Items.Add(new ComboBoxItem { Content = h, Tag = h });
                var guess = header.FirstOrDefault(h => string.Equals(h?.Trim(), field, StringComparison.OrdinalIgnoreCase));
                box.Text = guess ?? "";
                AutomationProperties.SetName(box, "Header of the column for " + field + (required ? " (required)" : " (optional)"));
                Grid.SetColumn(box, 1);
                row.Children.Add(box);
                stack.Children.Add(row);
                _columns[field] = box;
            }

            stack.Children.Add(TradingUi.Head("Type map — one line per raw type: raw type = kind"));
            stack.Children.Add(MonUi.Line("Kinds: " + string.Join(", ", kinds) + ". Example: Kauf = buy · Verkauf = sell · Dividende = dividend · Storno = ignore", 10.5, FundPalette.Faint));
            _typeMap = FundUi.Input("");
            _typeMap.AcceptsReturn = true;
            _typeMap.TextWrapping = TextWrapping.NoWrap;
            _typeMap.MinHeight = 110;
            _typeMap.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
            AutomationProperties.SetName(_typeMap, "Type map, one line per raw type: raw type equals kind");
            stack.Children.Add(_typeMap);
            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);
            _ok = TradingUi.Primary("Save profile");
            AutomationProperties.SetName(_ok, "Save the mapping profile");
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { if (!_busy && !_closed) DialogResult = false; };
            _ok.Click += async (_, _) => await SubmitAsync();
            Closed += (_, _) => _closed = true;
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
            Loaded += (_, _) => _name.Focus();
        }

        private static ComboBox Choice(Grid grid, int row, int col, string label, (string Value, string Text)[] options, string selected)
        {
            var s = new StackPanel { Margin = new Thickness(0, 0, 8, 6) };
            s.Children.Add(TradingUi.Label(label));
            var box = new ComboBox { FontSize = 12, MinHeight = 24 };
            foreach (var (value, text) in options)
            {
                var item = new ComboBoxItem { Content = text, Tag = value };
                box.Items.Add(item);
                if (value == selected) box.SelectedItem = item;
            }
            if (box.SelectedItem == null && box.Items.Count > 0) box.SelectedIndex = 0;
            AutomationProperties.SetName(box, label);
            s.Children.Add(box);
            Grid.SetRow(s, row);
            Grid.SetColumn(s, col);
            grid.Children.Add(s);
            return box;
        }

        private static string Value(ComboBox box) => box.SelectedItem is ComboBoxItem { Tag: string v } ? v : "";

        private async Task SubmitAsync()
        {
            if (_busy) return;
            var name = _name.Text.Trim();
            if (name.Length < 1 || name.Length > 60) { TradingUi.ShowError(_error, "Profile name: 1–60 characters."); return; }
            var columns = new Dictionary<string, object?>();
            foreach (var (field, box) in _columns)
            {
                var h = (box.Text ?? "").Trim();
                if (h.Length > 0) columns[field] = h;
            }
            if (!columns.ContainsKey("date") || !columns.ContainsKey("type")) { TradingUi.ShowError(_error, "Choose the columns for date and type."); return; }
            var typeMap = new Dictionary<string, object?>();
            var lineNo = 0;
            foreach (var raw in (_typeMap.Text ?? "").Replace("\r", "").Split('\n'))
            {
                lineNo++;
                var line = raw.Trim();
                if (line.Length == 0) continue;
                var eq = line.LastIndexOf('=');
                if (eq <= 0 || eq == line.Length - 1) { TradingUi.ShowError(_error, "Type map line " + lineNo.ToString(CultureInfo.InvariantCulture) + ": write raw type = kind."); return; }
                var rawType = line.Substring(0, eq).Trim();
                var kind = line.Substring(eq + 1).Trim().ToLowerInvariant();
                if (!_kinds.Contains(kind)) { TradingUi.ShowError(_error, "Type map line " + lineNo.ToString(CultureInfo.InvariantCulture) + ": kind must be one of " + string.Join(", ", _kinds) + "."); return; }
                typeMap[rawType] = kind;
            }
            if (typeMap.Count == 0) { TradingUi.ShowError(_error, "Add at least one line to the type map (e.g. Kauf = buy)."); return; }
            var mapping = new Dictionary<string, object?>
            {
                ["delimiter"] = Value(_delimiter),
                ["decimal"] = Value(_decimal),
                ["thousands"] = Value(_thousands),
                ["date_format"] = _dateFormat.Text.Trim(),
                ["encoding"] = Value(_encoding),
                ["columns"] = columns,
                ["type_map"] = typeMap,
                ["default_currency"] = Value(_currency),
                ["amount_sign"] = Value(_sign),
            };
            _busy = true;
            _ok.IsEnabled = false;
            var res = await PlatformClient.CreateShadowProfileAsync(name, mapping);
            _busy = false;
            if (_closed) return;
            if (res.Ok && res.Value?.Profile != null) { Created = res.Value.Profile; DialogResult = true; return; }
            TradingUi.ShowError(_error, res.Ok ? "Unexpected response from the backend." : PlatformClient.DescribeFailure(res));
            _ok.IsEnabled = true;
        }
    }

    /// <summary>
    /// Import wizard (§19.6): 1 File (size / row / column limits checked before anything is sent; NUL bytes refused) → 2 Format
    /// (auto-detect or a template — Trade Republic / Scalable Capital / IBKR Flex, each labelled "verify against a real export" — or
    /// the generic importer with a saved mapping profile) → 3 Preview (dry run: schema detection, counts, row errors, warnings, the
    /// first 20 normalised transactions) → 4 Import. Re-importing the same file adds nothing (duplicates). Plain text everywhere.
    /// </summary>
    internal sealed class ShadowImportWizard : FundPopupWindow
    {
        private readonly string _accountId;
        private readonly ShTemplates _templates;
        private readonly List<ShProfile> _profiles;
        private readonly TextBlock _stepText;
        private readonly StackPanel _body = new();
        private readonly TextBlock _error;
        private readonly Button _back;
        private readonly Button _next;
        private int _step = 1;
        private bool _busy;
        private bool _closed;

        private string? _path;
        private byte[]? _data;
        private List<string> _header = new();
        private string? _delimiterGuess;
        private string _template = PlatformClient.ShadowTemplateAuto;
        private string? _profileId;
        private ShImportResult? _preview;
        private ShImportRefusal? _refusal;

        public ShImportSummary? Imported { get; private set; }
        private ShCounts? _importCounts;
        public bool ProfilesChanged { get; private set; }

        public ShadowImportWizard(Window? owner, string accountId, string accountLabel, ShTemplates templates, List<ShProfile> profiles)
            : base(owner, "Import a broker statement", 820, 760, resizable: true)
        {
            _accountId = accountId;
            _templates = templates;
            _profiles = profiles.ToList();
            var root = new Grid { Margin = new Thickness(20, 14, 20, 14) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var head = new StackPanel();
            head.Children.Add(FundUi.Text("Import into " + accountLabel, 14, FundPalette.TextBright, bold: true));
            // live click-test 2026-10-18: the backend's notice already starts with the same sentence — shown once
            var notice = templates.Notice ?? "";
            head.Children.Add(MonUi.Line(notice.Contains("stay on this computer", StringComparison.OrdinalIgnoreCase)
                                             ? notice : (ShadowAccountView.LocalNotice + " " + notice).Trim(), 10.5, FundPalette.Long));
            _stepText = FundUi.Text("", 11, FundPalette.Focus, bold: true, mono: true);
            _stepText.Margin = new Thickness(0, 6, 0, 6);
            AutomationProperties.SetLiveSetting(_stepText, AutomationLiveSetting.Polite);
            head.Children.Add(_stepText);
            root.Children.Add(head);
            var scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = _body };
            Grid.SetRow(scroll, 1);
            root.Children.Add(scroll);
            var foot = new StackPanel();
            _error = TradingUi.ErrorText();
            foot.Children.Add(_error);
            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
            var close = FundUi.MakeButton("Close");
            AutomationProperties.SetName(close, "Close the import wizard");
            close.Click += (_, _) => { if (!_busy) Close(); };
            _back = FundUi.MakeButton("◂ Back");
            _back.Margin = new Thickness(8, 0, 0, 0);
            AutomationProperties.SetName(_back, "Back to the previous step");
            _back.Click += (_, _) => { if (!_busy && _step > 1 && _step < 5) { _step--; Render(); } };
            _next = TradingUi.Primary("Next ▸");
            _next.Margin = new Thickness(8, 0, 0, 0);
            _next.Click += async (_, _) => await NextAsync();
            buttons.Children.Add(close);
            buttons.Children.Add(_back);
            buttons.Children.Add(_next);
            foot.Children.Add(buttons);
            Grid.SetRow(foot, 2);
            root.Children.Add(foot);
            SetBody(root);
            Closed += (_, _) => _closed = true;
            Render();
        }

        /// <summary>The backend's import limits (GET /v2/shadow/templates) — never a stand-in: missing → <see cref="LimitsProblem"/>.</summary>
        private ShLimits? Limits => _templates.Limits;

        private string? LimitsProblem
            => Limits is { MaxBytes: long, MaxRows: int, MaxColumns: int, MaxFieldChars: int }
                ? null
                : "Import not available: the backend did not report the import limits (/v2/shadow/templates → limits) — restart the Hedge Fund backend.";

        private void Render()
        {
            if (_closed) return;
            _body.Children.Clear();
            TradingUi.ShowError(_error, null);
            _stepText.Text = string.Join("   ", new[] { "1 File", "2 Format", "3 Preview", "4 Import" }.Select((s, i) => (i + 1 == _step ? "▶ " : "") + s));
            AutomationProperties.SetName(_stepText, "Step " + _step.ToString(CultureInfo.InvariantCulture) + " of 4");
            _back.IsEnabled = !_busy && _step > 1 && _step < 5;
            switch (_step)
            {
                case 1: RenderFile(); break;
                case 2: RenderFormat(); break;
                case 3: RenderPreview(); break;
                default: RenderDone(); break;
            }
        }

        // ----- step 1: file -----

        private void RenderFile()
        {
            var problem = LimitsProblem;
            if (problem != null || Limits is not ShLimits l)
            {
                _body.Children.Add(MonUi.ErrorBlock(problem ?? "Import limits not available."));
                _next.IsEnabled = false;
                return;
            }
            _body.Children.Add(MonUi.Line("Choose the CSV export of YOUR OWN broker account. Limits: " + Mb(l.MaxBytes) + " · " + MonUi.Int(l.MaxRows) + " data rows · "
                                          + MonUi.Int(l.MaxColumns) + " columns · " + MonUi.Int(l.MaxFieldChars) + " characters per field. Encoding UTF-8 (BOM optional) or Windows-1252; "
                                          + "delimiter ; , or tab. Every cell is read as text — nothing is evaluated (formula-looking cells such as =… are stored as text and "
                                          + "neutralised on every export).", 11, FundPalette.Dim));
            var pick = FundUi.MakeButton("Choose file…");
            pick.HorizontalAlignment = HorizontalAlignment.Left;
            pick.Margin = new Thickness(0, 8, 0, 8);
            AutomationProperties.SetName(pick, "Choose the CSV file to import");
            pick.Click += (_, _) => PickFile();
            _body.Children.Add(pick);
            if (_data != null && _path != null)
            {
                _body.Children.Add(MonUi.Pairs(new[]
                {
                    ("File", Path.GetFileName(_path)),
                    ("Size", _data.Length.ToString("N0", CultureInfo.InvariantCulture) + " bytes"),
                    ("Lines (approx.)", CountLines(_data).ToString("N0", CultureInfo.InvariantCulture)),
                    ("Header (first line)", _header.Count == 0 ? "—" : string.Join(" | ", _header.Take(40)) + (_header.Count > 40 ? " | …" : "")),
                }));
                _body.Children.Add(MonUi.Line("The header is shown as plain text. The backend detects the format in the next steps.", 10.5, FundPalette.Faint));
            }
            _next.Content = "Next ▸";
            _next.IsEnabled = _data != null && !_busy;
            AutomationProperties.SetName(_next, "Next: choose the format");
        }

        private void PickFile()
        {
            var dialog = new Microsoft.Win32.OpenFileDialog
            {
                Title = "Choose a broker statement (CSV)",
                Filter = "CSV / text (*.csv;*.txt)|*.csv;*.txt|All files (*.*)|*.*",
                CheckFileExists = true,
                Multiselect = false,
            };
            if (dialog.ShowDialog(this) != true) return;
            if (LimitsProblem != null || Limits is not ShLimits l) { Refuse(LimitsProblem ?? "Import limits not available."); return; }
            try
            {
                var info = new FileInfo(dialog.FileName);
                var max = l.MaxBytes!.Value;
                if (info.Length > max)
                {
                    Refuse("The file is " + info.Length.ToString("N0", CultureInfo.InvariantCulture) + " bytes — larger than the limit of " + Mb(max) + ". Split it (e.g. by year).");
                    return;
                }
                var bytes = File.ReadAllBytes(dialog.FileName);
                // the file may have grown between the size check and the read (TOCTOU) — check the bytes actually read
                if (bytes.Length > max)
                {
                    Refuse("The file is " + bytes.Length.ToString("N0", CultureInfo.InvariantCulture) + " bytes (it changed while it was read) — larger than the limit of " + Mb(max) + ".");
                    return;
                }
                if (bytes.Length == 0) { Refuse("The file is empty."); return; }
                if (Array.IndexOf(bytes, (byte)0) >= 0) { Refuse("The file contains NUL bytes — it is not a text/CSV file (refused, nothing was sent)."); return; }
                var lines = CountLines(bytes);
                var maxRows = l.MaxRows!.Value;
                if (lines - 1 > maxRows)
                {
                    Refuse("The file has about " + lines.ToString("N0", CultureInfo.InvariantCulture) + " lines — more than " + MonUi.Int(maxRows) + " data rows. Split it.");
                    return;
                }
                var (header, delimiter) = ReadHeader(bytes);
                var maxCols = l.MaxColumns!.Value;
                if (header.Count > maxCols)
                {
                    Refuse("The header has " + header.Count.ToString(CultureInfo.InvariantCulture) + " columns — more than " + maxCols.ToString(CultureInfo.InvariantCulture) + ".");
                    return;
                }
                _path = dialog.FileName;
                _data = bytes;
                _header = header;
                _delimiterGuess = delimiter;
                _preview = null;
                _refusal = null;
                Render();
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is System.Security.SecurityException)
            {
                Refuse("The file could not be read: " + ex.Message);
            }
        }

        private void Refuse(string text)
        {
            _path = null;
            _data = null;
            _header = new List<string>();
            Render();
            TradingUi.ShowError(_error, text);
        }

        private static int CountLines(byte[] data)
        {
            var n = 0;
            foreach (var b in data) if (b == (byte)'\n') n++;
            return data.Length > 0 && data[^1] != (byte)'\n' ? n + 1 : n;
        }

        /// <summary>The first line split by the delimiter that yields the most columns (display + profile choices only; the backend detects for real).</summary>
        private static (List<string> Header, string? Delimiter) ReadHeader(byte[] data)
        {
            string text;
            try { text = new UTF8Encoding(false, true).GetString(data, 0, Math.Min(data.Length, 65536)); }
            catch (DecoderFallbackException) { text = Encoding.Latin1.GetString(data, 0, Math.Min(data.Length, 65536)); }
            text = text.TrimStart('﻿');
            var end = text.IndexOfAny(new[] { '\r', '\n' });
            var first = end >= 0 ? text.Substring(0, end) : text;
            string? best = null;
            var cols = new List<string> { first };
            foreach (var d in new[] { ";", ",", "\t" })
            {
                var parts = first.Split(d).Select(x => x.Trim().Trim('"')).ToList();
                if (parts.Count > cols.Count) { cols = parts; best = d; }
            }
            return (cols.Select(c => c.Length > 100 ? c.Substring(0, 100) : c).ToList(), best);
        }

        private static string Mb(long? bytes) => bytes is long b ? (b / 1048576.0).ToString("0.#", CultureInfo.InvariantCulture) + " MB" : "—";

        // ----- step 2: format -----

        private void RenderFormat()
        {
            _body.Children.Add(MonUi.Line("Choose how the file is read. \"Auto-detect\" compares the header with every template's required columns.", 11, FundPalette.Dim));
            var group = "tpl" + GetHashCode().ToString(CultureInfo.InvariantCulture);
            AddTemplateOption(group, PlatformClient.ShadowTemplateAuto, "Auto-detect (recommended)", "The best full match of the header wins; no full match → map the columns with the generic importer.", null);
            foreach (var t in (_templates.Templates ?? new List<ShTemplate>()).Where(t => t.Id != null && t.Id != PlatformClient.ShadowTemplateGeneric))
            {
                var label = TemplateLabel(t.Id, t.Label);
                // §19.16: only templates the backend still marks verify:true carry the mark and the verify note (Trade Republic no longer)
                var note = (t.Verify == true ? "VERIFY AGAINST A REAL EXPORT — " + (t.VerifyNote ?? "") : "")
                           + (string.IsNullOrWhiteSpace(t.Privacy) ? "" : (t.Verify == true ? "\n" : "") + t.Privacy)
                           + ((t.RequiredColumns?.Count ?? 0) > 0 ? "\nRequired columns: " + string.Join(", ", t.RequiredColumns!) : "")
                           + ((t.TypeMap?.Count ?? 0) > 0 ? "\nTypes: " + string.Join(", ", t.TypeMap!.Take(12).Select(kv => kv.Key + " → " + kv.Value)) + (t.TypeMap!.Count > 12 ? ", …" : "") : "");
                AddTemplateOption(group, t.Id!, label, note, t.Verify == true ? "VERIFY AGAINST A REAL EXPORT" : null);
            }
            AddTemplateOption(group, PlatformClient.ShadowTemplateGeneric, "Generic — my own column mapping (saved profile)",
                "Any CSV: map its columns to the canonical fields once and save it as a profile.", null);

            if (_template == PlatformClient.ShadowTemplateGeneric)
            {
                var box = new ComboBox { FontSize = 12, MinHeight = 24, Margin = new Thickness(24, 4, 0, 4) };
                AutomationProperties.SetName(box, "Mapping profile");
                foreach (var p in LatestProfiles())
                {
                    var item = new ComboBoxItem { Content = (p.Name ?? "?") + " · v" + (p.Version?.ToString(CultureInfo.InvariantCulture) ?? "?") + " · " + (p.Id ?? ""), Tag = p.Id };
                    box.Items.Add(item);
                    if (p.Id == _profileId) box.SelectedItem = item;
                }
                if (box.SelectedItem == null && box.Items.Count > 0) { box.SelectedIndex = 0; _profileId = (box.SelectedItem as ComboBoxItem)?.Tag as string; }
                box.SelectionChanged += (_, _) => { _profileId = (box.SelectedItem as ComboBoxItem)?.Tag as string; UpdateNext(); };
                _body.Children.Add(box);
                var create = MonUi.Small("New mapping profile…", "Create a new column mapping profile from this file's header");
                create.Margin = new Thickness(24, 2, 0, 4);
                create.HorizontalAlignment = HorizontalAlignment.Left;
                create.Click += (_, _) => CreateProfile();
                _body.Children.Add(create);
                if (box.Items.Count == 0) _body.Children.Add(MonUi.Line("No profile yet — create one.", 10.5, FundPalette.Risk));
            }
            _next.Content = "Preview (dry run) ▸";
            AutomationProperties.SetName(_next, "Preview: a dry run that imports nothing");
            UpdateNext();
        }

        private void UpdateNext()
        {
            if (_step == 2) _next.IsEnabled = !_busy && _data != null && (_template != PlatformClient.ShadowTemplateGeneric || !string.IsNullOrEmpty(_profileId));
        }

        private static string TemplateLabel(string? id, string? label) => id switch
        {
            "trade_republic" => "Trade Republic" + (string.IsNullOrWhiteSpace(label) ? "" : " — " + label),
            "scalable" => "Scalable Capital" + (string.IsNullOrWhiteSpace(label) ? "" : " — " + label),
            "ibkr_flex" => "IBKR Flex Query" + (string.IsNullOrWhiteSpace(label) ? "" : " — " + label),
            _ => string.IsNullOrWhiteSpace(label) ? (id ?? "?") : label!,
        };

        private void AddTemplateOption(string group, string id, string label, string note, string? chip)
        {
            var rb = TradingUi.Radio(group, label, label, _template == id);
            rb.Margin = new Thickness(0, 8, 0, 0);
            rb.Checked += (_, _) =>
            {
                if (_template == id) return;
                _template = id;
                _preview = null;
                _refusal = null;
                Render();
            };
            _body.Children.Add(rb);
            if (chip != null)
            {
                var c = MonUi.Chip(chip, FundPalette.Risk);
                c.Margin = new Thickness(24, 2, 0, 0);
                c.HorizontalAlignment = HorizontalAlignment.Left;
                _body.Children.Add(c);
            }
            var n = MonUi.Line(note, 10.5, chip != null ? FundPalette.Risk : FundPalette.Faint);
            n.Margin = new Thickness(24, 1, 0, 2);
            _body.Children.Add(n);
            AutomationProperties.SetHelpText(rb, note);
        }

        private IEnumerable<ShProfile> LatestProfiles()
            => _profiles.Where(p => p.Id != null).GroupBy(p => p.Name ?? p.Id!).Select(g => g.OrderByDescending(p => p.Version ?? 0).First()).OrderBy(p => p.Name, StringComparer.OrdinalIgnoreCase);

        private void CreateProfile()
        {
            var canonical = _templates.CanonicalFields;
            var kinds = _templates.Kinds;
            if (canonical == null || canonical.Count == 0 || kinds == null || kinds.Count == 0)
            {
                TradingUi.ShowError(_error, "Mapping profile not available: the backend did not report the canonical fields / kinds (/v2/shadow/templates) — restart the Hedge Fund backend.");
                return;
            }
            var dialog = new ShadowProfileDialog(this, _header, canonical, kinds, _delimiterGuess);
            if (dialog.ShowDialog() != true || dialog.Created == null) return;
            _profiles.Add(dialog.Created);
            _profileId = dialog.Created.Id;
            ProfilesChanged = true;
            Render();
        }

        // ----- step 3: preview -----

        private void RenderPreview()
        {
            if (_refusal != null)
            {
                _body.Children.Add(MonUi.ErrorBlock((_refusal.Code ?? "refused") + ": " + (_refusal.Error ?? "the backend refused the file")));
                if (_refusal.Detection != null) _body.Children.Add(DetectionBlock(_refusal.Detection));
                if (_refusal.Code == "unknown_format")
                {
                    var g = MonUi.Small("Use the generic importer (column mapping)…", "Go back and use the generic importer with a column mapping profile");
                    g.HorizontalAlignment = HorizontalAlignment.Left;
                    g.Click += (_, _) => { _template = PlatformClient.ShadowTemplateGeneric; _refusal = null; _step = 2; Render(); };
                    _body.Children.Add(g);
                }
                _next.Content = "Import";
                _next.IsEnabled = false;
                return;
            }
            var p = _preview;
            if (p == null) { _body.Children.Add(MonUi.Line("Running the dry run…", 11, FundPalette.Faint)); _next.IsEnabled = false; return; }
            _body.Children.Add(MonUi.Line("Dry run — nothing was imported yet. Check a few rows against your statement before importing.", 11, FundPalette.Text));
            if (p.Detection != null) _body.Children.Add(DetectionBlock(p.Detection));
            var c = p.Counts;
            _body.Children.Add(MonUi.Pairs(new[]
            {
                ("Data rows", MonUi.Int(c?.Rows)),
                ("Accepted", MonUi.Int(c?.Accepted)),
                ("Rejected (row errors)", MonUi.Int(c?.Rejected)),
                ("Duplicates (same content)", MonUi.Int(c?.Duplicates)),
                ("Already imported (broker transaction id known)", c?.AlreadyKnown is int ak ? ak.ToString("N0", CultureInfo.InvariantCulture) + " transaction(s) already imported — skipped" : "not reported"),
                ("Possible duplicates", c?.PossibleDuplicates is int pd ? pd.ToString("N0", CultureInfo.InvariantCulture) + " possible duplicates (content matches an earlier import) — skipped, see reasons" : "not reported"),
                ("Skipped (ignored types / status)", MonUi.Int(c?.Skipped)),
            }));
            foreach (var w in p.Warnings ?? new List<string>()) _body.Children.Add(MonUi.Line("⚠ " + w, 10.5, FundPalette.Risk));
            var errors = p.Errors ?? new List<ShRowError>();
            if (errors.Count > 0)
            {
                _body.Children.Add(TradingUi.Head("Row errors (" + errors.Count.ToString(CultureInfo.InvariantCulture) + (errors.Count >= 200 ? ", first 200 — the counts are complete" : "") + ")"));
                _body.Children.Add(MonUi.Table(new[] { "Row", "Field", "Message" },
                    errors.Select(e => (IReadOnlyList<string>)new[] { e.Row?.ToString(CultureInfo.InvariantCulture) ?? "—", e.Field ?? "—", e.Message ?? "" }), "Row errors", new double[] { 60, 120, 0 }));
            }
            var preview = p.Preview ?? new List<ShTxn>();
            _body.Children.Add(TradingUi.Head("Preview (first " + preview.Count.ToString(CultureInfo.InvariantCulture) + " normalised transactions)"));
            // live click-test 2026-10-18 ("large empty gap above the first rows"): the fixed widths (824 px) exceeded the wizard body, so the
            // star column "Instrument" got ~0 px and its header wrapped one letter per line. All widths are fixed now, in a horizontal scroller.
            _body.Children.Add(HScroll(MonUi.Table(new[] { "Row", "Date", "Kind", "Instrument", "Qty", "Price", "Ccy", "Fee", "Tax", "Net", "Original (FX)", "Raw type", "Note / id" },
                preview.Select(t => (IReadOnlyList<string>)new[]
                {
                    t.Row?.ToString(CultureInfo.InvariantCulture) ?? "—", t.Date ?? "—", ShText.Kind(t.Kind), (t.Key ?? "") + (string.IsNullOrWhiteSpace(t.Name) ? "" : " · " + t.Name),
                    FundFormat.Qty(t.Qty), MonUi.Num(t.Price, 4), t.Currency ?? "—", MonUi.Num(t.Fee), MonUi.Num(t.Tax), MonUi.Num(t.Net), ShText.Original(t),
                    t.RawType ?? "—", ShText.NoteAndId(t),
                }), "Preview of the normalised transactions", new double[] { 40, 80, 96, 230, 64, 72, 40, 56, 56, 72, 130, 110, 180 })));
            var accepted = c?.Accepted ?? 0;
            _next.Content = "Import " + accepted.ToString("N0", CultureInfo.InvariantCulture) + " transaction(s)";
            AutomationProperties.SetName(_next, "Import " + accepted.ToString(CultureInfo.InvariantCulture) + " transactions");
            _next.IsEnabled = !_busy && accepted > 0;
            if (accepted == 0) _body.Children.Add(MonUi.Line("Nothing to import (every row is a duplicate, already imported, skipped or rejected).", 10.5, FundPalette.Risk));
        }

        /// <summary>A wide table in a horizontal scroller; the mouse wheel goes on to the wizard's vertical scroll (never swallowed).</summary>
        private static ScrollViewer HScroll(FrameworkElement content)
        {
            var sv = new ScrollViewer
            {
                HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                VerticalScrollBarVisibility = ScrollBarVisibility.Disabled,
                Focusable = false,
                Content = content,
            };
            sv.PreviewMouseWheel += (sender, e) =>
            {
                if (e.Handled || sender is not FrameworkElement fe || fe.Parent is not UIElement parent) return;
                e.Handled = true;
                parent.RaiseEvent(new MouseWheelEventArgs(e.MouseDevice, e.Timestamp, e.Delta) { RoutedEvent = UIElement.MouseWheelEvent, Source = sender });
            };
            return sv;
        }

        private static UIElement DetectionBlock(ShDetection d)
        {
            var s = new StackPanel();
            s.Children.Add(TradingUi.Head("Schema detection"));
            s.Children.Add(MonUi.Pairs(new[]
            {
                ("Template", TemplateLabel(d.Template, null) + (d.Score is JsonElement sc ? " (score " + FundFormat.Plain(sc) + ")" : "")),
                ("Encoding / delimiter", (d.Encoding ?? "—") + " / " + (d.Delimiter == "\t" ? "tab" : d.Delimiter ?? "—")),
                ("Header", (d.Header?.Count ?? 0) > 0 ? string.Join(" | ", d.Header!.Take(40)) : "—"),
                ("Missing required columns", (d.Missing?.Count ?? 0) > 0 ? string.Join(", ", d.Missing!) : "none"),
            }, (d.Missing?.Count ?? 0) > 0 ? FundPalette.Risk : null));
            return s;
        }

        // ----- step 4: done -----

        private void RenderDone()
        {
            var i = Imported;
            _body.Children.Add(FundUi.Text(i != null ? "Imported ✓" : "Import finished", 13, FundPalette.Long, bold: true));
            if (i != null)
                _body.Children.Add(MonUi.Pairs(new[]
                {
                    ("Import", i.Id ?? "—"),
                    ("File", i.FileName ?? "—"),
                    ("sha256", i.Sha256 ?? "—"),
                    ("Template", (i.Template ?? "—") + (i.ProfileId != null ? " (" + i.ProfileId + ")" : "")),
                    ("Rows / accepted", MonUi.Int(i.Rows) + " / " + MonUi.Int(i.Accepted)),
                    ("Rejected / duplicates / skipped", MonUi.Int(i.Rejected) + " / " + MonUi.Int(i.Duplicates) + " / " + MonUi.Int(i.Skipped)),
                    ("Already imported", _importCounts?.AlreadyKnown is int ak ? ak.ToString("N0", CultureInfo.InvariantCulture) + " transaction(s) already imported — skipped" : "not reported"),
                    ("Possible duplicates", _importCounts?.PossibleDuplicates is int pd ? pd.ToString("N0", CultureInfo.InvariantCulture) + " possible duplicates (content matches an earlier import) — skipped, see reasons" : "not reported"),
                }));
            _body.Children.Add(MonUi.Line("Stored in the local database only. Re-importing the same file adds nothing (duplicates are detected).", 10.5, FundPalette.Faint));
            _next.Content = "Done";
            AutomationProperties.SetName(_next, "Done — close the wizard");
            _next.IsEnabled = true;
        }

        // ----- navigation -----

        private async Task NextAsync()
        {
            if (_busy) return;
            switch (_step)
            {
                case 1:
                    if (_data == null) return;
                    _step = 2;
                    Render();
                    break;
                case 2:
                    _step = 3;
                    _preview = null;
                    _refusal = null;
                    Render();
                    await SendAsync(dryRun: true);
                    break;
                case 3:
                    await SendAsync(dryRun: false);
                    break;
                default:
                    if (!_closed) DialogResult = Imported != null;
                    break;
            }
        }

        private async Task SendAsync(bool dryRun)
        {
            if (_data == null || _path == null) return;
            _busy = true;
            _next.IsEnabled = false;
            _back.IsEnabled = false;
            TradingUi.ShowError(_error, dryRun ? "Checking the file (dry run)…" : "Importing…");
            _error.Foreground = FundPalette.Faint;
            PlatformResult<ShImportResult> res;
            try
            {
                res = await PlatformClient.ImportShadowFileAsync(_accountId, _data, Path.GetFileName(_path), _template,
                                                                 _template == PlatformClient.ShadowTemplateGeneric ? _profileId : null, dryRun);
            }
            finally { _busy = false; }
            if (_closed) return;
            _error.Foreground = FundPalette.Kill;
            if (res.Ok && res.Value != null)
            {
                if (dryRun) { _preview = res.Value; Render(); return; }
                Imported = res.Value.Import;
                _importCounts = res.Value.Counts;
                _step = 5;
                Render();
                return;
            }
            var refusal = PlatformClient.ReadShadowRefusal(res);
            if (dryRun && refusal != null && (refusal.Code != null || refusal.Detection != null))
            {
                _refusal = refusal;
                if (string.IsNullOrWhiteSpace(_refusal.Error)) _refusal.Error = res.Message;
                Render();
                return;
            }
            Render();
            var text = res.Ok ? "Unexpected response from the backend (import)." : PlatformClient.DescribeFailure(res);
            if (PlatformClient.ErrorCode(res) == "duplicate_import")
                text += "\nNothing was imported. To import this file again, remove the earlier import first (Shadow Account → Overview → Remove …).";
            TradingUi.ShowError(_error, text);
        }
    }
}
