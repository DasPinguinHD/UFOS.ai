using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using UFOS.ai.HedgeFund.Fund;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Symbol mapping (SPEC §20.10 / §20.11, user decisions 2026-10-18): ISIN → ONE listing the user picks. Two ways:
    //  - an EODHD listing from the backend's search candidates of that ISIN (venue + currency shown; any venue / currency), or
    //  - a US ticker checked against the Alpaca asset list.
    // Nothing is suggested, pre-selected or guessed from a name; a confirmation tick is required; every refusal is shown verbatim.
    // Mappings are GLOBAL (one per ISIN, all Shadow accounts), insert-only and audited by the backend. Original currency
    // everywhere: a listing in another currency than the cost shows its value only, without P&L (the backend decides).
    // =====================================================================================

    /// <summary>Map one ISIN to a listing (EODHD candidate) or a US ticker (Alpaca). True = a mapping was saved.</summary>
    internal sealed class ShadowSymbolMapDialog : FundPopupWindow
    {
        private static readonly Regex IsinShape = new("^[A-Z]{2}[A-Z0-9]{9}[0-9]$", RegexOptions.CultureInvariant);
        private static readonly Regex TickerShape = new("^[A-Z][A-Z0-9.]{0,9}$", RegexOptions.CultureInvariant);

        private readonly string? _costCurrency;
        private readonly string? _importName;   // the instrument name from the import (plain text) — named on the button
        private readonly TextBox _isin;
        private readonly RadioButton _modeListing;
        private readonly RadioButton _modeUs;
        private readonly StackPanel _listingPanel;
        private readonly StackPanel _usPanel;
        private readonly Button _search;
        private readonly TextBlock _budget;
        private readonly StackPanel _candidates;
        private readonly TextBox _ticker;
        private readonly TextBlock _alpacaName;   // fix1023 round 2: Alpaca's asset name from GET /v2/shadow/symbol-check
        private string? _checkedKey;              // "TICKER|ISIN" of the last check, null = not checked
        private ShSymbolCheck? _check;
        private readonly TextBlock _confirm;   // fix1023: a statement next to the button, no tick
        private readonly TextBox _reason;
        private readonly TextBlock _error;
        private readonly Button _ok;
        private readonly List<(RadioButton Button, ShListingCandidate Candidate)> _candidateButtons = new();
        private string? _searchedIsin;
        private bool _busy;
        private bool _closed;

        public ShSymbolMapping? Saved { get; private set; }

        /// <param name="isin">The ISIN from the import (read-only), or null to type one.</param>
        /// <param name="name">The instrument name from the import (plain text), shown for orientation only — never used to search.</param>
        /// <param name="costCurrency">The position's cost currency (for the "different currency" hint), or null.</param>
        public ShadowSymbolMapDialog(Window? owner, string? isin, string? name, string? costCurrency)
            : base(owner, "Map an instrument to a listing", 640, 640, resizable: true)
        {
            _costCurrency = string.IsNullOrWhiteSpace(costCurrency) ? null : costCurrency!.Trim().ToUpperInvariant();
            _importName = string.IsNullOrWhiteSpace(name) ? null : name!.Trim();
            var stack = new StackPanel { Margin = new Thickness(20, 16, 20, 16) };
            stack.Children.Add(FundUi.Text("Map an instrument to a listing", 14, FundPalette.TextBright, bold: true));
            stack.Children.Add(MonUi.Line("You pick the listing yourself — nothing is suggested, pre-selected or guessed from the name. The mapping applies to this ISIN "
                                          + "in every Shadow account and is audited. Values stay in the listing's own currency: a listing in another currency than your "
                                          + "cost shows its value only, without P&L (never converted).", 11, FundPalette.Dim));

            stack.Children.Add(TradingUi.Label("ISIN (from the import)"));
            _isin = FundUi.Input(isin ?? "");
            _isin.MaxLength = 12;
            _isin.IsReadOnly = !string.IsNullOrWhiteSpace(isin);
            AutomationProperties.SetName(_isin, "ISIN" + (_isin.IsReadOnly ? " (from the import, read only)" : ""));
            _isin.TextChanged += (_, _) => { ClearCandidates(); ResetCheck(); };
            stack.Children.Add(_isin);
            stack.Children.Add(MonUi.Line("Name in the import: " + (string.IsNullOrWhiteSpace(name) ? "—" : name) + (_costCurrency != null ? " · cost currency " + _costCurrency : ""),
                                          10.5, FundPalette.Faint));

            stack.Children.Add(TradingUi.Label("Price source"));
            var modes = new WrapPanel { Margin = new Thickness(0, 2, 0, 6) };
            _modeListing = TradingUi.Radio("symmap-mode", "Listing via EODHD (any venue, listing currency)", "Map to an EODHD listing found by ISIN", true);
            _modeUs = TradingUi.Radio("symmap-mode", "US ticker (Alpaca, USD)", "Map to a US ticker checked against the Alpaca asset list", false);
            _modeListing.Checked += (_, _) => ApplyMode();
            _modeUs.Checked += (_, _) => ApplyMode();
            modes.Children.Add(_modeListing);
            modes.Children.Add(_modeUs);
            stack.Children.Add(modes);

            // ----- EODHD listing -----
            _listingPanel = new StackPanel();
            var searchRow = new WrapPanel();
            _search = FundUi.MakeButton("Search the listings of this ISIN");
            _search.ToolTip = "Asks the backend for the EODHD search candidates of this ISIN — 1 EODHD call, 0 when the result is cached (30 days).";
            AutomationProperties.SetName(_search, "Search the EODHD listings of this ISIN (uses one EODHD call unless cached)");
            _search.Click += async (_, _) => await SearchAsync();
            searchRow.Children.Add(_search);
            _listingPanel.Children.Add(searchRow);
            _budget = MonUi.Line("", 10.5, FundPalette.Faint);
            _budget.Visibility = Visibility.Collapsed;
            AutomationProperties.SetLiveSetting(_budget, AutomationLiveSetting.Polite);
            _listingPanel.Children.Add(_budget);
            _candidates = new StackPanel { Margin = new Thickness(0, 6, 0, 0) };
            AutomationProperties.SetName(_candidates, "Listing candidates of this ISIN");
            _listingPanel.Children.Add(_candidates);
            stack.Children.Add(_listingPanel);

            // ----- US ticker -----
            _usPanel = new StackPanel { Visibility = Visibility.Collapsed };
            _usPanel.Children.Add(TradingUi.Label("US ticker (Alpaca spelling, e.g. SPGI or BRK.B)"));
            _ticker = FundUi.Input("");
            _ticker.MaxLength = 10;
            _ticker.CharacterCasing = CharacterCasing.Upper;
            _ticker.TextChanged += (_, _) => { ResetCheck(); UpdateOkText(); };
            AutomationProperties.SetName(_ticker, "US ticker symbol");
            _usPanel.Children.Add(_ticker);
            _alpacaName = MonUi.Line("Name at Alpaca: not checked yet — the first click on the button checks the ticker, the second saves.", 10.5, FundPalette.Faint);
            AutomationProperties.SetLiveSetting(_alpacaName, AutomationLiveSetting.Polite);
            _usPanel.Children.Add(_alpacaName);
            _usPanel.Children.Add(MonUi.Line("The backend checks it against the Alpaca asset list (active, tradable US stock or ETF, not OTC) and refuses non-US ISINs — "
                                             + "its reason is shown as sent.", 10.5, FundPalette.Faint));
            stack.Children.Add(_usPanel);

            _confirm = FundUi.Text("", 12, FundPalette.Text);
            _confirm.Margin = new Thickness(0, 10, 0, 4);
            stack.Children.Add(_confirm);
            stack.Children.Add(TradingUi.Label(TradingUi.NoteLabel));
            _reason = TradingUi.NoteBox("Optional note for the mapping");
            stack.Children.Add(_reason);
            _error = TradingUi.ErrorText();
            stack.Children.Add(_error);
            _ok = TradingUi.Primary("Save mapping");
            AutomationProperties.SetName(_ok, "Save the mapping");
            stack.Children.Add(TradingUi.Buttons(out var cancel, _ok));
            cancel.Click += (_, _) => { if (!_busy && !_closed) DialogResult = false; };
            _ok.Click += async (_, _) => await SubmitAsync();
            Closed += (_, _) => _closed = true;
            SetBody(new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = stack });
            ApplyMode();
            Loaded += (_, _) => { if (_isin.IsReadOnly) _search.Focus(); else _isin.Focus(); };
        }

        private bool ListingMode => _modeListing.IsChecked == true;

        private string IsinText => (_isin.Text ?? "").Trim().ToUpperInvariant();

        private void ApplyMode()
        {
            if (_listingPanel == null || _usPanel == null || _confirm == null) return;   // raised while building
            _listingPanel.Visibility = ListingMode ? Visibility.Visible : Visibility.Collapsed;
            _usPanel.Visibility = ListingMode ? Visibility.Collapsed : Visibility.Visible;
            _confirm.Text = ListingMode ? "Saving states that the chosen listing is a listing of this exact instrument."
                                        : "Saving states that this ticker is the US listing of this exact instrument.";
            AutomationProperties.SetName(_confirm, _confirm.Text);
            UpdateOkText();
            TradingUi.ShowError(_error, null);
        }

        /// <summary>
        /// fix1023 review minor 4: the button names what is mapped — "Map 'S&amp;P GLOBAL INC' (ISIN US78409V1044) to SPGI" (US ticker)
        /// or "… to &lt;listing&gt;" (EODHD) — so the statement next to it is concrete.
        /// </summary>
        private void UpdateOkText()
        {
            if (_ok == null || _ticker == null || TradingUi.IsBusy(_ok)) return;
            var what = (_importName != null ? "'" + (_importName.Length > 40 ? _importName.Substring(0, 39) + "…" : _importName) + "' " : "")
                       + "(ISIN " + (IsinText.Length > 0 ? IsinText : "?") + ")";
            string target;
            if (ListingMode)
            {
                var picked = _candidateButtons.FirstOrDefault(x => x.Button.IsChecked == true).Candidate;
                target = picked?.Listing ?? "the listing you pick";
            }
            else
            {
                var ticker = (_ticker.Text ?? "").Trim().ToUpperInvariant();
                if (ticker.Length > 0 && _checkedKey != CheckKey())
                {
                    var checkText = "Check " + ticker + " in the Alpaca asset list";
                    _ok.Content = checkText;
                    AutomationProperties.SetName(_ok, checkText);
                    return;
                }
                target = ticker.Length > 0 ? ticker + (string.IsNullOrWhiteSpace(_check?.AssetName) ? "" : " (Alpaca: " + _check!.AssetName!.Trim() + ")") : "a US ticker";
            }
            var text = "Map " + what + " to " + target;
            _ok.Content = text;
            AutomationProperties.SetName(_ok, text);
        }

        private string CheckKey() => (_ticker.Text ?? "").Trim().ToUpperInvariant() + "|" + IsinText;

        private void ResetCheck()
        {
            if (_alpacaName == null) return;   // raised while building
            _checkedKey = null;
            _check = null;
            _alpacaName.Text = "Name at Alpaca: not checked yet — the first click on the button checks the ticker, the second saves.";
            _alpacaName.Foreground = FundPalette.Faint;
        }

        /// <summary>GET /v2/shadow/symbol-check — read-only; shows Alpaca's asset name (or the refusal) next to the import's name.</summary>
        private async Task CheckTickerAsync(string ticker, string isin)
        {
            _busy = true;
            TradingUi.SetBusy(_ok, true, "Checking…");
            PlatformResult<ShSymbolCheck> res;
            try { res = await PlatformClient.CheckShadowSymbolAsync(ticker, isin); }
            finally { _busy = false; TradingUi.SetBusy(_ok, false); }
            if (_closed) return;
            if (!res.Ok || res.Value == null)
            {
                _alpacaName.Text = "Name at Alpaca: not available (" + (res.Ok ? "unexpected response from the backend" : PlatformClient.DescribeFailure(res)) + ")";
                _alpacaName.Foreground = FundPalette.Kill;
                UpdateOkText();
                return;
            }
            var c = res.Value;
            _check = c;
            _checkedKey = CheckKey();
            var name = string.IsNullOrWhiteSpace(c.AssetName) ? "no name reported" : c.AssetName!.Trim();
            var facts = string.Join(" · ", new[] { c.Exchange, c.AssetClass, c.Status, c.Tradable == false ? "not tradable" : null }.Where(x => !string.IsNullOrWhiteSpace(x)));
            var current = c.CurrentMapping?.Symbol is string cur ? " · this ISIN is mapped to " + cur + " now" : "";
            _alpacaName.Text = c.Found == false
                ? "Name at Alpaca: " + ticker + " is not in the Alpaca asset list" + (string.IsNullOrWhiteSpace(c.RefusedReason) ? "" : " — " + c.RefusedReason)
                : "Name at Alpaca: " + name + (facts.Length > 0 ? " (" + facts + ")" : "") + current
                  + (c.Usable == false ? " — NOT usable: " + (c.RefusedReason ?? "no reason given") : "");
            _alpacaName.Foreground = c.Usable == true ? FundPalette.Text : FundPalette.Kill;
            AutomationProperties.SetName(_alpacaName, _alpacaName.Text);
            UpdateOkText();
        }

        private void ClearCandidates()
        {
            if (_candidates == null) return;
            _candidates.Children.Clear();
            _candidateButtons.Clear();
            _searchedIsin = null;
            UpdateOkText();
        }

        private async Task SearchAsync()
        {
            if (_busy) return;
            var isin = IsinText;
            if (!IsinShape.IsMatch(isin)) { TradingUi.ShowError(_error, "Enter a 12-character ISIN (two letters, nine letters or digits, one check digit)."); return; }
            _busy = true;
            _search.IsEnabled = false;
            TradingUi.ShowError(_error, null);
            ClearCandidates();
            _candidates.Children.Add(MonUi.Line("Searching the listings of " + isin + "…", 11, FundPalette.Faint));
            PlatformResult<ShListings> res;
            try { res = await PlatformClient.GetShadowListingsAsync(isin); }
            finally { _busy = false; if (!_closed) _search.IsEnabled = true; }
            if (_closed) return;
            _candidates.Children.Clear();
            if (!res.Ok || res.Value == null)
            {
                _candidates.Children.Add(MonUi.ErrorBlock(res.Ok ? "Unexpected response from the backend (/v2/shadow/listings)." : PlatformClient.DescribeFailure(res)));
                return;
            }
            var l = res.Value;
            _budget.Text = ShMapText.Budget(l.Budget) + (l.Cached == true ? " · this search came from the cache (0 calls)" : "");
            _budget.Foreground = string.IsNullOrWhiteSpace(l.Budget?.Message) ? FundPalette.Faint : FundPalette.Risk;
            _budget.Visibility = Visibility.Visible;
            AutomationProperties.SetName(_budget, _budget.Text);
            var list = l.Candidates ?? new List<ShListingCandidate>();
            _searchedIsin = isin;
            if (list.Count == 0)
            {
                _candidates.Children.Add(MonUi.Line("No listing of " + isin + " in the EODHD search — enter a valuation price instead (Set valuation prices…).", 11, FundPalette.Risk));
                return;
            }
            _candidates.Children.Add(MonUi.Line("Pick ONE listing (none is pre-selected; \"primary\" is EODHD's flag, shown only):", 11, FundPalette.Text));
            foreach (var c in list.Where(c => !string.IsNullOrWhiteSpace(c.Listing)))
            {
                var other = _costCurrency != null && !string.IsNullOrWhiteSpace(c.Currency) && !string.Equals(c.Currency, _costCurrency, StringComparison.OrdinalIgnoreCase);
                var text = c.Listing + " · " + (c.Exchange ?? "venue —") + " · " + (c.Currency ?? "currency —") + " · " + (c.Name ?? "—")
                           + (string.IsNullOrWhiteSpace(c.Type) ? "" : " · " + c.Type) + (string.IsNullOrWhiteSpace(c.Country) ? "" : " · " + c.Country)
                           + (c.IsPrimary == true ? " · primary" : "")
                           + (c.PreviousClose is double pc ? " · previous close " + pc.ToString("0.####", CultureInfo.InvariantCulture) + " " + (c.Currency ?? "")
                                                            + (string.IsNullOrWhiteSpace(c.PreviousCloseDate) ? "" : " (" + c.PreviousCloseDate + ")") : "");
                var rb = TradingUi.Radio("symmap-listing", text, "Listing " + text, false);
                var content = new TextBlock { Text = text, TextWrapping = TextWrapping.Wrap, Foreground = FundPalette.Text, FontSize = 11.5 };
                rb.Content = content;
                rb.Margin = new Thickness(0, 2, 0, 2);
                rb.Checked += (_, _) => UpdateOkText();
                _candidates.Children.Add(rb);
                if (other)
                    _candidates.Children.Add(MonUi.Line("   different currencies — not converted (listing " + c.Currency + ", cost " + _costCurrency + ") — value only, no P&L", 10.5, FundPalette.Risk));
                _candidateButtons.Add((rb, c));
            }
        }

        private async Task SubmitAsync()
        {
            if (_busy) return;
            TradingUi.ShowError(_error, null);
            var isin = IsinText;
            if (!IsinShape.IsMatch(isin)) { TradingUi.ShowError(_error, "Enter a 12-character ISIN."); return; }
            var reason = (_reason.Text ?? "").Trim();
            if (!TradingUi.ValidNote(reason)) { TradingUi.ShowError(_error, "The note can be at most 500 characters."); return; }
            Task<PlatformResult<ShSymbolMappingEnvelope>> call;
            if (ListingMode)
            {
                if (_searchedIsin != isin) { TradingUi.ShowError(_error, "Search the listings of this ISIN first, then pick one."); return; }
                var picked = _candidateButtons.FirstOrDefault(x => x.Button.IsChecked == true).Candidate;
                if (picked?.Listing is not string listing) { TradingUi.ShowError(_error, "Pick one listing."); return; }
                call = PlatformClient.PutShadowListingMappingAsync(isin, listing, reason.Length > 0 ? reason : null);
            }
            else
            {
                var ticker = (_ticker.Text ?? "").Trim().ToUpperInvariant();
                if (!TickerShape.IsMatch(ticker)) { TradingUi.ShowError(_error, "Enter the ticker in Alpaca spelling (A–Z, digits, dot; up to 10 characters)."); return; }
                // fix1023 round 2: check first (Alpaca's asset name is shown next to the import's name), then save on the next click
                if (_checkedKey != CheckKey()) { await CheckTickerAsync(ticker, isin); return; }
                if (_check?.Usable == false)
                {
                    TradingUi.ShowError(_error, "Not usable: " + (string.IsNullOrWhiteSpace(_check.RefusedReason) ? "the backend gave no reason." : _check.RefusedReason));
                    return;
                }
                call = PlatformClient.PutShadowUsTickerMappingAsync(isin, ticker, reason.Length > 0 ? reason : null);
            }
            _busy = true;
            TradingUi.SetBusy(_ok, true, "Saving…");
            PlatformResult<ShSymbolMappingEnvelope> res;
            try { res = await call; }
            finally { _busy = false; TradingUi.SetBusy(_ok, false); }
            if (_closed) return;
            if (res.Ok && res.Value?.Mapping != null)
            {
                Saved = res.Value.Mapping;
                DialogResult = true;
                return;
            }
            // the backend's refusal (invalid_isin, non_us_isin, symbol_refused, listing_refused, eodhd_limit, key_missing, …) — verbatim
            TradingUi.ShowError(_error, res.Ok ? "Unexpected response from the backend (PUT /v2/shadow/symbol-mappings without mapping)." : PlatformClient.DescribeFailure(res));
            _ok.IsEnabled = true;
        }
    }

    /// <summary>
    /// §20.11 review fixes: "Refresh price (uses 1 of today's EODHD calls)" for one mapped EODHD listing — shows the daily counter
    /// before (GET /v2/shadow/price-sources, no EODHD call) and after (the refresh answer's budget); the result / reason verbatim.
    /// </summary>
    internal static class EodhdRefreshFlow
    {
        public const string ActionText = "Refresh price (uses 1 of today's EODHD calls)";

        /// <summary>True when a refresh request was sent (the caller reloads its data).</summary>
        public static async Task<bool> RunAsync(Window? owner, string listing)
        {
            var before = await PlatformClient.GetShadowPriceSourcesAsync();
            var eod = before.Ok ? before.Value?.Sources?.FirstOrDefault(x => x.Id == "eodhd") : null;
            var beforeText = before.Ok ? ShMapText.Budget(eod?.Budget) : "EODHD calls today: not available (" + PlatformClient.DescribeFailure(before) + ")";
            var cached = eod?.FailuresToday?.FirstOrDefault(f => string.Equals(f.Listing, listing, StringComparison.OrdinalIgnoreCase));
            var message = "Fetch the closes of " + listing + " from EODHD again now? This uses 1 of today's EODHD calls.\n\nBefore: " + beforeText
                          + (cached?.Reason != null ? "\n\nCached failure (until tomorrow or a refresh): " + cached.Reason : "");
            if (!FundDialogs.Confirm(owner, "Refresh EODHD price", message, "Refresh (1 call)")) return false;
            var res = await PlatformClient.RefreshShadowEodhdListingAsync(listing);
            string text;
            if (res.Ok && res.Value != null)
            {
                var r = res.Value;
                text = (r.Ok == true
                           ? "✓ " + (r.Listing ?? listing) + ": " + (r.Closes?.ToString(CultureInfo.InvariantCulture) ?? "?") + " closes"
                             + (r.Last is { } last ? " · last " + (last.Date ?? "?") + " " + (last.Close is double lc ? lc.ToString("0.####", CultureInfo.InvariantCulture) : "—") : " · no last close")
                           : "✕ " + (r.Listing ?? listing) + ": " + (r.Reason ?? "the refresh failed (no reason reported)"))
                       + (string.IsNullOrWhiteSpace(r.Cost) ? "" : "\nCost: " + r.Cost)
                       + "\n\nBefore: " + beforeText + "\nAfter: " + ShMapText.Budget(r.Budget);
            }
            else
                text = (res.Ok ? "Unexpected response from the backend (refresh)." : PlatformClient.DescribeFailure(res)) + "\n\nBefore: " + beforeText;
            FundDialogs.Info(owner, "Refresh EODHD price", text);
            return true;
        }
    }

    /// <summary>
    /// Shadow Account → "Symbol mapping…": the global mapping list (remove with confirmation, audited), the price sources with the
    /// EODHD daily budget, and the ISIN positions of the current account with "Map…". <see cref="Changed"/> = something was saved.
    /// </summary>
    internal sealed class ShadowSymbolMappingsWindow : FundPopupWindow
    {
        private static readonly Regex IsinLike = new("^[A-Z]{2}[A-Z0-9]{9}[0-9]$", RegexOptions.CultureInvariant);

        private readonly IReadOnlyList<ShPosition> _positions;
        private readonly StackPanel _body = new();
        private readonly TextBlock _status;
        private bool _busy;
        private bool _closed;
        private int _seq;

        public bool Changed { get; private set; }

        public ShadowSymbolMappingsWindow(Window? owner, IReadOnlyList<ShPosition> positions)
            : base(owner, "Symbol mapping", 760, 680, resizable: true)
        {
            _positions = positions;
            var root = new Grid { Margin = new Thickness(20, 14, 20, 14) };
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });
            root.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            var head = new StackPanel();
            head.Children.Add(FundUi.Text("Symbol mapping (all Shadow accounts)", 14, FundPalette.TextBright, bold: true));
            _status = MonUi.Line("Loading…", 10.5, FundPalette.Faint);
            AutomationProperties.SetLiveSetting(_status, AutomationLiveSetting.Polite);
            head.Children.Add(_status);
            root.Children.Add(head);
            var scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Content = _body, Margin = new Thickness(0, 8, 0, 0) };
            Grid.SetRow(scroll, 1);
            root.Children.Add(scroll);
            var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 10, 0, 0) };
            var other = FundUi.MakeButton("Map another ISIN…");
            AutomationProperties.SetName(other, "Map an ISIN that you type");
            other.Click += async (_, _) => await MapAsync(null, null, null);
            var refresh = FundUi.MakeButton("↻ Refresh");
            refresh.Margin = new Thickness(8, 0, 0, 0);
            AutomationProperties.SetName(refresh, "Reload the mappings and price sources");
            refresh.Click += async (_, _) => await LoadAsync();
            var close = FundUi.MakeButton("Close");
            close.Margin = new Thickness(8, 0, 0, 0);
            AutomationProperties.SetName(close, "Close the symbol mapping window");
            close.Click += (_, _) => { if (!_busy) Close(); };
            buttons.Children.Add(other);
            buttons.Children.Add(refresh);
            buttons.Children.Add(close);
            Grid.SetRow(buttons, 2);
            root.Children.Add(buttons);
            SetBody(root);
            Closed += (_, _) => _closed = true;
            Loaded += async (_, _) => { other.Focus(); await LoadAsync(); };
        }

        private async Task LoadAsync()
        {
            if (_closed) return;
            var seq = ++_seq;
            _status.Text = "Loading…";
            var mapsTask = PlatformClient.GetShadowSymbolMappingsAsync();
            var sourcesTask = PlatformClient.GetShadowPriceSourcesAsync();
            await Task.WhenAll(mapsTask, sourcesTask);
            if (_closed || seq != _seq) return;
            Render(mapsTask.Result, sourcesTask.Result);
            _status.Text = "Updated " + DateTime.Now.ToString("HH:mm:ss", CultureInfo.InvariantCulture) + " · plain text from your files and the backend — nothing is evaluated.";
        }

        private void Render(PlatformResult<ShSymbolMappings> maps, PlatformResult<ShPriceSources> sources)
        {
            _body.Children.Clear();
            // ----- price sources (one per data type, no fallback chain) -----
            _body.Children.Add(MonUi.Head("Price sources (one per data type — no fallback)"));
            if (!sources.Ok || sources.Value == null)
                _body.Children.Add(MonUi.ErrorBlock("Price sources not available: " + (sources.Ok ? "unexpected response from the backend (/v2/shadow/price-sources)." : PlatformClient.DescribeFailure(sources))));
            else
            {
                foreach (var src in sources.Value.Sources ?? new List<ShPriceSource>())
                {
                    _body.Children.Add(MonUi.Line("• " + (src.Label ?? src.Id ?? "source"), 11, FundPalette.Text));
                    if (src.KeyName != null)
                    {
                        var keyText = src.KeyPresent == true ? "key set" : src.KeyPresent == false ? "key not set — enter it in Hedge Fund → Keys & data" : "key status not available" + (src.KeyError != null ? ": " + src.KeyError : "");
                        _body.Children.Add(MonUi.Line("   " + keyText, 10.5, src.KeyPresent == true ? FundPalette.Faint : FundPalette.Risk));
                    }
                    if (src.Id == "eodhd")
                    {
                        _body.Children.Add(MonUi.Line("   " + ShMapText.Budget(src.Budget) + (src.CachedListingsToday is int cl ? " · " + cl.ToString(CultureInfo.InvariantCulture) + " listing(s) cached today" : ""),
                                                      10.5, string.IsNullOrWhiteSpace(src.Budget?.Message) ? FundPalette.Faint : FundPalette.Risk));
                        if (!string.IsNullOrWhiteSpace(src.Terms)) _body.Children.Add(MonUi.Line("   " + src.Terms, 10.5, FundPalette.Faint));
                        foreach (var f in src.FailuresToday ?? new List<ShEodhdFailure>())
                            _body.Children.Add(MonUi.Line("   failed today: " + (f.Reason ?? ((f.Listing ?? "?") + " — no reason reported")), 10.5, FundPalette.Risk));
                    }
                    if (src.Pairs is { Count: > 0 } pairs)
                        _body.Children.Add(MonUi.Line("   " + string.Join(" · ", pairs.Select(kv => kv.Key + " → " + kv.Value)), 10.5, FundPalette.Faint));
                }
                foreach (var n in sources.Value.NoSource ?? new List<string>()) _body.Children.Add(MonUi.Line("• no source: " + n, 10.5, FundPalette.Dim));
            }

            // ----- mappings -----
            var eodSource = sources.Ok ? sources.Value?.Sources?.FirstOrDefault(x => x.Id == "eodhd") : null;
            var failures = (eodSource?.FailuresToday ?? new List<ShEodhdFailure>()).Where(f => !string.IsNullOrWhiteSpace(f.Listing))
                .GroupBy(f => f.Listing!.ToUpperInvariant()).ToDictionary(g => g.Key, g => g.Last(), StringComparer.Ordinal);
            _body.Children.Add(MonUi.Head("Your mappings"));
            var mapped = new HashSet<string>(StringComparer.Ordinal);
            if (!maps.Ok || maps.Value == null)
                _body.Children.Add(MonUi.ErrorBlock("Mappings not available: " + (maps.Ok ? "unexpected response from the backend (/v2/shadow/symbol-mappings)." : PlatformClient.DescribeFailure(maps))));
            else
            {
                if (!string.IsNullOrWhiteSpace(maps.Value.Notice)) _body.Children.Add(MonUi.Line(maps.Value.Notice, 10.5, FundPalette.Faint));
                var list = maps.Value.Mappings ?? new List<ShSymbolMapping>();
                if (list.Count == 0) _body.Children.Add(MonUi.Line("No mappings yet.", 11, FundPalette.Faint));
                foreach (var m in list)
                {
                    if (m.Isin != null) mapped.Add(m.Isin);
                    var row = new Grid { Margin = new Thickness(0, 2, 0, 2) };
                    row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                    row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                    var text = ShMapText.Mapping(m);
                    row.Children.Add(MonUi.Line(text, 11, FundPalette.Text, mono: true));
                    var target = m;
                    var rm = MonUi.Small("Remove…", "Remove the mapping " + text);
                    rm.Margin = new Thickness(8, 0, 0, 0);
                    rm.IsEnabled = !string.IsNullOrWhiteSpace(m.Isin);
                    rm.Click += async (_, _) => await RemoveAsync(target);
                    var actions = new StackPanel { Orientation = Orientation.Horizontal };
                    if (m.PriceSource == "eodhd" && !string.IsNullOrWhiteSpace(m.Listing))
                    {
                        var listing = m.Listing!;
                        var rf = MonUi.Small(EodhdRefreshFlow.ActionText, "Refresh the EODHD price of " + listing + ", uses one of today's EODHD calls");
                        rf.Margin = new Thickness(8, 0, 0, 0);
                        rf.Click += async (_, _) => await RefreshAsync(listing);
                        actions.Children.Add(rf);
                    }
                    actions.Children.Add(rm);
                    Grid.SetColumn(actions, 1);
                    row.Children.Add(actions);
                    _body.Children.Add(row);
                    if (m.Listing != null && failures.TryGetValue(m.Listing.ToUpperInvariant(), out var fail))
                        _body.Children.Add(MonUi.Line("   " + (fail.Reason ?? "fetch failed (no reason reported)") + (fail.FetchedAt != null ? " · " + fail.FetchedAt : ""),
                                                      10.5, FundPalette.Risk));
                }
            }

            // ----- ISIN positions of this account -----
            var isinPositions = _positions.Where(p => IsinOf(p) != null).GroupBy(p => IsinOf(p)!).Select(g => g.First()).ToList();
            _body.Children.Add(MonUi.Head("ISIN positions of this account (" + isinPositions.Count.ToString(CultureInfo.InvariantCulture) + ")"));
            if (isinPositions.Count == 0) _body.Children.Add(MonUi.Line("No open position of this account has an ISIN.", 11, FundPalette.Faint));
            foreach (var p in isinPositions)
            {
                var isin = IsinOf(p)!;
                var row = new Grid { Margin = new Thickness(0, 2, 0, 2) };
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                row.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
                var text = isin + (string.IsNullOrWhiteSpace(p.Name) ? "" : " · " + p.Name) + " · " + (p.AssetClass ?? "unclassified") + " · cost " + (p.Currency ?? "?")
                           + (mapped.Contains(isin) ? " · mapped" : "");
                row.Children.Add(MonUi.Line(text, 11, mapped.Contains(isin) ? FundPalette.Dim : FundPalette.Text));
                var map = MonUi.Small(mapped.Contains(isin) ? "Map again…" : "Map…", "Map " + isin + " to a listing");
                map.Margin = new Thickness(8, 0, 0, 0);
                map.Click += async (_, _) => await MapAsync(isin, p.Name, p.Currency);
                Grid.SetColumn(map, 1);
                row.Children.Add(map);
                _body.Children.Add(row);
            }
        }

        private static string? IsinOf(ShPosition p)
        {
            var v = (p.Isin ?? p.Key ?? "").Trim().ToUpperInvariant();
            return IsinLike.IsMatch(v) ? v : null;
        }

        private async Task MapAsync(string? isin, string? name, string? currency)
        {
            if (_busy || _closed) return;
            var dialog = new ShadowSymbolMapDialog(this, isin, name, currency);
            if (dialog.ShowDialog() != true) return;
            Changed = true;
            await LoadAsync();
        }

        private async Task RefreshAsync(string listing)
        {
            if (_busy || _closed) return;
            _busy = true;
            bool sent;
            try { sent = await EodhdRefreshFlow.RunAsync(this, listing); }
            finally { _busy = false; }
            if (_closed || !sent) return;
            Changed = true;
            await LoadAsync();
        }

        private async Task RemoveAsync(ShSymbolMapping m)
        {
            if (_busy || _closed || string.IsNullOrWhiteSpace(m.Isin)) return;
            if (!FundDialogs.Confirm(this, "Remove mapping", "Remove the mapping " + ShMapText.Mapping(m) + "?\n\nThe instrument is then unpriced again until you map it "
                                                             + "or enter a valuation. The removal is audited.", "Remove mapping", danger: true)) return;
            _busy = true;
            PlatformResult<System.Text.Json.JsonElement> res;
            try { res = await PlatformClient.DeleteShadowSymbolMappingAsync(m.Isin!); }
            finally { _busy = false; }
            if (_closed) return;
            if (!res.Ok)
            {
                FundDialogs.Info(this, "Remove mapping", PlatformClient.DescribeFailure(res));
                return;
            }
            Changed = true;
            await LoadAsync();
        }
    }
}
