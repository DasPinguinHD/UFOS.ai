using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Platform
{
    /// <summary>
    /// Studio → "Paper" (Strategy Platform stage 5b, SPEC §14.1/§14.2/§14.5; routes/JSON §14.7). Left: add a paper —
    /// PDF upload (file dialog; the 20 MB limit is checked here before the upload), pasted text (≤ 200,000 characters,
    /// optional title) or a link (arXiv / SSRN only; the rule is shown and checked here, the backend enforces it) — and
    /// the list of papers. Right: the selected paper (untrusted plain text), the AI extraction (labelled, cost line;
    /// a not-relevant text shows the backend's fixed note and "Reason given by the AI"), the review form (every field
    /// editable with the source snippet it came from — nothing runs until "Confirm & build strategy"; the reported
    /// Sharpe and the paper's period are REQUIRED user-confirmed numbers), and the replication card (paper vs our
    /// Sharpe, replicated / not replicated chip, paper period vs period used + clamp notes, gross/net and survivorship
    /// notes, "Open report"). Backend errors (SSRN login, url_not_allowed, pdf_invalid, budget …) are shown verbatim.
    /// The replication is polled every 3 s only while it waits or runs and the view is visible.
    /// </summary>
    internal sealed class StudioPaperView : UserControl
    {
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(3);
        public const string LinkRule = "Only arXiv and SSRN links are accepted: https://arxiv.org/abs/…, https://arxiv.org/pdf/… or "
                                       + "https://papers.ssrn.com/sol3/papers.cfm?abstract_id=…";
        private static readonly Regex AllowedLink = new(@"^https://(arxiv\.org/(abs|pdf)/\S+|papers\.ssrn\.com/sol3/papers\.cfm\?abstract_id=\d+)$",
                                                        RegexOptions.Compiled | RegexOptions.IgnoreCase);

        private readonly StackPanel _list;
        private readonly TextBlock _listStatus;
        private readonly StackPanel _addStatusHost;
        private readonly TextBox _link;
        private readonly TextBox _pasteText;
        private readonly TextBox _pasteTitle;
        private readonly Button _uploadButton;
        private readonly Button _linkButton;
        private readonly Button _pasteButton;
        private readonly StackPanel _detail;
        private readonly Border _notice;
        private readonly DispatcherTimer _poll;
        private readonly DispatcherTimer _waitTimer;
        private DateTime _waitStarted;
        private string _waitText = "";

        private List<PaperInfo> _papers = new();
        private string? _paperId;
        private PaperDetail? _current;
        private StackPanel? _replicationHost;
        private bool _busy;
        private bool _stopped;
        private bool _polling;
        private int _seq;
        private bool _loadedOnce;

        // review form
        private readonly Dictionary<string, TextBox> _fieldBoxes = new(StringComparer.Ordinal);
        private TextBox? _startBox;
        private TextBox? _endBox;
        private TextBox? _sharpeBox;
        private RadioButton? _basisNet;
        private RadioButton? _basisGross;
        private RadioButton? _basisUnknown;
        private RadioButton? _buildPremium;
        private RadioButton? _extractPremium;
        private TextBlock? _formError;

        /// <summary>"Open in Library" (argument: strategy id).</summary>
        public event Action<string>? OpenInLibraryRequested;

        public StudioPaperView()
        {
            AutomationProperties.SetName(this, "Studio — build a strategy from a paper");
            var root = new Grid();
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(300) });
            root.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });

            // ----- left: add + list -----
            var left = new StackPanel();
            left.Children.Add(Head("Add a paper"));
            _uploadButton = FundUi.MakeButton("Upload PDF…");
            _uploadButton.HorizontalAlignment = HorizontalAlignment.Stretch;
            _uploadButton.ToolTip = "Choose a PDF (at most 20 MB, 150 pages). Only its text is kept.";
            AutomationProperties.SetName(_uploadButton, "Upload a PDF paper, at most 20 megabytes");
            _uploadButton.Click += async (_, _) => await UploadAsync();
            left.Children.Add(_uploadButton);

            left.Children.Add(Small("Link", top: 10));
            _link = FundUi.Input("", mono: true);
            AutomationProperties.SetName(_link, "Paper link, arXiv or SSRN only");
            AutomationProperties.SetHelpText(_link, LinkRule);
            _link.KeyDown += async (_, e) => { if (e.Key == System.Windows.Input.Key.Enter) { e.Handled = true; await AddLinkAsync(); } };
            left.Children.Add(_link);
            var rule = FundUi.Text(LinkRule, 10, FundPalette.Faint);
            rule.Margin = new Thickness(0, 3, 0, 4);
            left.Children.Add(rule);
            _linkButton = FundUi.MakeButton("Add link");
            _linkButton.HorizontalAlignment = HorizontalAlignment.Left;
            AutomationProperties.SetName(_linkButton, "Add the paper from the link");
            _linkButton.Click += async (_, _) => await AddLinkAsync();
            left.Children.Add(_linkButton);

            left.Children.Add(Small("Paste text (up to 200,000 characters)", top: 10));
            _pasteTitle = FundUi.Input("", mono: false);
            _pasteTitle.MaxLength = PlatformClient.PaperMaxTitleChars;
            AutomationProperties.SetName(_pasteTitle, "Title of the pasted paper (optional)");
            left.Children.Add(Small("Title (optional)", top: 2));
            left.Children.Add(_pasteTitle);
            _pasteText = FundUi.Input("", mono: false);
            _pasteText.AcceptsReturn = true;
            _pasteText.TextWrapping = TextWrapping.Wrap;
            _pasteText.Height = 110;
            _pasteText.VerticalScrollBarVisibility = ScrollBarVisibility.Auto;
            _pasteText.MaxLength = PlatformClient.PaperMaxTextChars;
            _pasteText.Margin = new Thickness(0, 4, 0, 4);
            AutomationProperties.SetName(_pasteText, "Pasted paper text");
            left.Children.Add(_pasteText);
            _pasteButton = FundUi.MakeButton("Add text");
            _pasteButton.HorizontalAlignment = HorizontalAlignment.Left;
            AutomationProperties.SetName(_pasteButton, "Add the pasted text as a paper");
            _pasteButton.Click += async (_, _) => await AddTextAsync();
            left.Children.Add(_pasteButton);
            _addStatusHost = new StackPanel { Margin = new Thickness(0, 6, 0, 0) };
            AutomationProperties.SetLiveSetting(_addStatusHost, AutomationLiveSetting.Polite);
            left.Children.Add(_addStatusHost);

            var lh = Head("Papers");
            lh.Margin = new Thickness(0, 14, 0, 4);
            left.Children.Add(lh);
            _listStatus = FundUi.Text("", 10.5, FundPalette.Faint);
            left.Children.Add(_listStatus);
            _list = new StackPanel { Margin = new Thickness(0, 4, 0, 0) };
            AutomationProperties.SetName(_list, "Papers, newest first");
            left.Children.Add(_list);
            root.Children.Add(new Border
            {
                Background = FundPalette.Panel,
                BorderBrush = FundPalette.PanelBorder,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(10, 10, 10, 10),
                Child = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false, Content = left },
            });

            // ----- right: notice + detail -----
            var right = new StackPanel { Margin = new Thickness(12, 0, 10, 0) };
            _notice = StudioView.NoticeBlock();
            right.Children.Add(_notice);
            _detail = new StackPanel();
            AutomationProperties.SetName(_detail, "Selected paper");
            right.Children.Add(_detail);
            var scroll = new ScrollViewer { VerticalScrollBarVisibility = ScrollBarVisibility.Auto, Focusable = false, Content = right };
            Grid.SetColumn(scroll, 1);
            root.Children.Add(scroll);
            Content = root;
            Placeholder("Add a paper on the left, or select one.");

            _poll = new DispatcherTimer { Interval = PollInterval };
            _poll.Tick += async (_, _) => await PollReplicationAsync();
            _waitTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
            _waitTimer.Tick += (_, _) => ShowWaiting();
            IsVisibleChanged += (_, _) => UpdatePolling();
        }

        /// <summary>Called when the "Paper" tab is shown.</summary>
        public async void Activate()
        {
            if (_stopped) return;
            await StudioView.ApplyBackendNoticeAsync(_notice);
            await LoadListAsync();
            if (_paperId != null) await SelectAsync(_paperId);
            else if (!_loadedOnce && _papers.FirstOrDefault()?.Id is string first) await SelectAsync(first);
            _loadedOnce = true;
        }

        public void Shutdown()
        {
            _stopped = true;
            _poll.Stop();
            _waitTimer.Stop();
        }

        // =====================================================================================
        // Adding papers
        // =====================================================================================

        private async Task UploadAsync()
        {
            if (_busy) return;
            var dlg = new Microsoft.Win32.OpenFileDialog { Filter = "PDF files (*.pdf)|*.pdf", CheckFileExists = true, Multiselect = false, Title = "Upload a paper (PDF, at most 20 MB)" };
            var owner = Window.GetWindow(this);
            if ((owner != null ? dlg.ShowDialog(owner) : dlg.ShowDialog()) != true) return;
            long size;
            try { size = new FileInfo(dlg.FileName).Length; }
            catch (Exception ex) { AddStatus("The file could not be read: " + ex.Message, error: true); return; }
            if (size > PlatformClient.PaperMaxPdfBytes)
            {
                AddStatus("The PDF is " + (size / (1024.0 * 1024.0)).ToString("0.0", Inv) + " MB — the limit is 20 MB.", error: true);
                return;
            }
            byte[] bytes;
            try { bytes = await File.ReadAllBytesAsync(dlg.FileName); }
            catch (Exception ex) { AddStatus("The file could not be read: " + ex.Message, error: true); return; }
            await AddAsync(() => PlatformClient.AddPaperPdfAsync(bytes, Path.GetFileName(dlg.FileName)), "Uploading and extracting the text…");
        }

        private async Task AddLinkAsync()
        {
            if (_busy) return;
            var url = (_link.Text ?? "").Trim();
            if (!AllowedLink.IsMatch(url))
            {
                AddStatus(LinkRule, error: true);
                return;
            }
            await AddAsync(() => PlatformClient.AddPaperUrlAsync(url), "Fetching the paper…");
            if (_paperId != null) _link.Text = "";
        }

        private async Task AddTextAsync()
        {
            if (_busy) return;
            var text = _pasteText.Text ?? "";
            if (text.Trim().Length == 0)
            {
                AddStatus("Paste the paper's text first.", error: true);
                return;
            }
            await AddAsync(() => PlatformClient.AddPaperTextAsync(text, _pasteTitle.Text), "Adding the text…");
        }

        private async Task AddAsync(Func<Task<PlatformResult<PaperInfo>>> call, string waiting)
        {
            _busy = true;
            SetAddEnabled(false);
            AddStatus(waiting, error: false);
            try
            {
                var res = await call();
                if (_stopped) return;
                if (!res.Ok || res.Value?.Id == null)
                {
                    // url_not_allowed / pdf_invalid / too_large / ssrn_login / fetch_failed / fetch_rejected / extract_timeout — verbatim
                    AddStatus(res.Ok ? "Unexpected response from the backend (/v2/papers)." : res.Message, error: true);
                    return;
                }
                AddStatus(res.Value.Duplicate == true ? "This paper was added before — showing the existing one." : "Paper added.", error: false);
                _pasteText.Text = "";
                _pasteTitle.Text = "";
                _paperId = res.Value.Id;
            }
            finally
            {
                _busy = false;
                SetAddEnabled(true);
            }
            await LoadListAsync();
            if (_paperId != null) await SelectAsync(_paperId);
        }

        private void SetAddEnabled(bool on)
        {
            _uploadButton.IsEnabled = on;
            _linkButton.IsEnabled = on;
            _pasteButton.IsEnabled = on;
        }

        private void AddStatus(string text, bool error)
        {
            _addStatusHost.Children.Clear();
            _addStatusHost.Children.Add(error ? ErrorBlock(text) : FundUi.Text(text, 10.5, FundPalette.Dim));
        }

        // =====================================================================================
        // List + detail
        // =====================================================================================

        private async Task LoadListAsync()
        {
            _listStatus.Text = "Loading…";
            var res = await PlatformClient.ListPapersAsync(50);
            if (_stopped) return;
            if (!res.Ok || res.Value == null)
            {
                _listStatus.Text = res.Ok ? "Unexpected response from the backend (/v2/papers)." : res.Message;
                _listStatus.Foreground = FundPalette.Kill;
                return;
            }
            _papers = res.Value.Papers ?? new List<PaperInfo>();
            _listStatus.Text = _papers.Count.ToString(Inv) + (_papers.Count == 1 ? " paper" : " papers");
            _listStatus.Foreground = FundPalette.Faint;
            RenderList();
        }

        private void RenderList()
        {
            _list.Children.Clear();
            foreach (var p in _papers)
            {
                var id = p.Id ?? "";
                var selected = id == _paperId;
                var stack = new StackPanel { HorizontalAlignment = HorizontalAlignment.Left };
                stack.Children.Add(FundUi.Text(PaperTitle(p), 11, selected ? FundPalette.TextBright : FundPalette.Text, bold: selected));
                stack.Children.Add(FundUi.Text(SourceLabel(p.SourceKind) + " · " + StatusLabel(p.Status) + " · " + FundFormat.LocalTime(p.CreatedAt, "yyyy-MM-dd"), 10, FundPalette.Faint, mono: true));
                var b = FundUi.MakeButton("");
                b.Content = stack;
                b.HorizontalContentAlignment = HorizontalAlignment.Stretch;
                b.Padding = new Thickness(8, 5, 8, 5);
                b.Margin = new Thickness(0, 0, 0, 5);
                b.Background = selected ? FundPalette.Make(0x267C8CF8) : FundPalette.Make(0xFF1E1E1E);
                b.BorderBrush = selected ? FundPalette.Focus : FundPalette.PanelBorder;
                AutomationProperties.SetName(b, "Paper " + PaperTitle(p) + ", " + StatusLabel(p.Status) + (selected ? ", selected" : ""));
                b.Click += async (_, _) => { if (id.Length > 0) await SelectAsync(id); };
                _list.Children.Add(b);
            }
            if (_papers.Count == 0) _list.Children.Add(FundUi.Text("No papers yet.", 11, FundPalette.Faint));
        }

        private static string PaperTitle(PaperInfo p)
            => !string.IsNullOrWhiteSpace(p.Title) ? p.Title! : !string.IsNullOrWhiteSpace(p.Filename) ? p.Filename! : "Untitled paper (" + (p.Id ?? "?") + ")";

        private static string SourceLabel(string? kind) => kind switch
        {
            "pdf" => "PDF upload",
            "text" => "pasted text",
            "arxiv" => "arXiv",
            "ssrn" => "SSRN",
            null => "source not reported",
            _ => kind,
        };

        private static string StatusLabel(string? status) => status switch
        {
            "new" => "not extracted",
            "extracted" => "extracted — review pending",
            "not_relevant" => "not a strategy",
            "confirmed" => "confirmed",
            null => "status not reported",
            _ => status,
        };

        private async Task SelectAsync(string paperId)
        {
            var seq = ++_seq;
            _paperId = paperId;
            RenderList();
            if (_current?.Paper?.Id != paperId) Placeholder("Loading…");
            var res = await PlatformClient.GetPaperAsync(paperId);
            if (seq != _seq || _stopped) return;
            if (!res.Ok || res.Value == null)
            {
                _detail.Children.Clear();
                _detail.Children.Add(ErrorBlock(res.Ok ? "Unexpected response from the backend (paper)." : res.Message));
                return;
            }
            _current = res.Value;
            RenderDetail();
            UpdatePolling();
        }

        private void RenderDetail()
        {
            _detail.Children.Clear();
            _fieldBoxes.Clear();
            _replicationHost = null;
            var d = _current;
            var p = d?.Paper;
            if (d == null || p == null)
            {
                Placeholder("The backend sent no paper.");
                return;
            }
            _detail.Children.Add(FundUi.Text(PaperTitle(p), 15, FundPalette.TextBright, bold: true));
            var meta = new List<string> { SourceLabel(p.SourceKind) };
            if (!string.IsNullOrWhiteSpace(p.Authors)) meta.Add(p.Authors!);
            if (p.Year is int y) meta.Add(y.ToString(Inv));
            if (p.Pages is int pg) meta.Add(pg.ToString(Inv) + " pages");
            if (p.TextChars is int tc) meta.Add(tc.ToString("N0", Inv) + " characters of text");
            meta.Add(StatusLabel(p.Status));
            _detail.Children.Add(Line(string.Join(" · ", meta), 11, FundPalette.Dim));
            _detail.Children.Add(Line((p.Url ?? p.Filename ?? "") + (string.IsNullOrEmpty(p.Sha256) ? "" : " · sha256 " + Short(p.Sha256)) + " · added "
                                      + FundFormat.LocalTime(p.CreatedAt, "yyyy-MM-dd HH:mm") + " · " + (p.Id ?? ""), 10.5, FundPalette.Faint, mono: true));
            _detail.Children.Add(Line("The paper's text is untrusted data: it is passed to the AI only as quoted data and shown here as plain text.", 10, FundPalette.Faint));
            if (!string.IsNullOrEmpty(d.TextPreview))
            {
                var exp = new Expander { Header = FundUi.Text("Text preview (first 2,000 characters)", 10.5, FundPalette.Text, bold: true), Margin = new Thickness(0, 4, 0, 6) };
                AutomationProperties.SetName(exp, "Text preview of the paper");
                exp.Content = FundUi.Text(d.TextPreview, 10.5, FundPalette.Dim);
                _detail.Children.Add(exp);
            }

            var extraction = d.Extractions?.FirstOrDefault();
            var confirmed = d.Confirmation != null || p.Status == "confirmed";
            Section("1 · Extraction (AI)");
            if (extraction != null)
                _detail.Children.Add(extraction.Relevant == false ? ExtractionCostNote(extraction) : ExtractionHeader(extraction));
            if (!confirmed) _detail.Children.Add(ExtractControls(extraction != null));

            if (extraction != null && extraction.Relevant == false)
            {
                _detail.Children.Add(Neutral(string.IsNullOrWhiteSpace(extraction.Note) ? "The backend sent no note." : extraction.Note!));
                return;
            }

            if (extraction?.Fields != null && !confirmed)
            {
                Section("2 · Review — nothing runs until you confirm");
                _detail.Children.Add(ReviewForm(extraction));
            }
            if (d.Confirmation != null)
            {
                Section("2 · Confirmed values");
                _detail.Children.Add(ConfirmedBlock(d.Confirmation));
            }
            Section("3 · Replication");
            _replicationHost = new StackPanel();
            _detail.Children.Add(_replicationHost);
            RenderReplication(d.Replication, p.StrategyId ?? d.Confirmation?.StrategyId);
        }

        private FrameworkElement ExtractionHeader(PaperExtraction e)
        {
            FundAi.TryRead(e.Provenance, e.Model, e.CreatedAt, out var model, out var at);
            var stack = new StackPanel { Margin = new Thickness(0, 0, 0, 6) };
            stack.Children.Add(FundUi.Text(AiContent.Glyph + " AI-generated · " + (string.IsNullOrWhiteSpace(model) ? "model not reported" : model)
                                           + " · " + (at.HasValue ? at.Value.ToLocalTime().ToString("yyyy-MM-dd HH:mm", Inv) : FundFormat.LocalTime(e.CreatedAt, "yyyy-MM-dd HH:mm"))
                                           + " — extraction from the paper; not verified", 10.5, FundPalette.Ai, bold: true, mono: true));
            stack.Children.Add(Line(TierLabel(e.ModelTier) + " · cost " + FundFormat.Usd(e.CostUsd)
                                    + (e.TextUsedChars is int used ? " · " + used.ToString("N0", Inv) + " characters of the text used" : "")
                                    + (e.TextTruncated == true ? " (truncated — the rest of the paper was not read)" : ""), 10, FundPalette.Faint, mono: true));
            var caveat = FundUi.Text(AiContent.ResearchNotice, 10, FundPalette.Faint);   // Stage 10: research output notice (b)
            caveat.FontStyle = FontStyles.Italic;
            stack.Children.Add(caveat);
            AutomationProperties.SetName(stack, AiContent.AutomationName(model, at) + " Paper extraction.");
            return stack;
        }

        private FrameworkElement ExtractControls(bool again)
        {
            var row = new WrapPanel { Margin = new Thickness(0, 0, 0, 6) };
            var standard = Radio("PaperExtractModel", "Standard", "Extraction with the standard model", true);
            _extractPremium = Radio("PaperExtractModel", "Premium", "Extraction with the premium model", false);
            row.Children.Add(standard);
            row.Children.Add(_extractPremium);
            var b = FundUi.MakeButton(AiContent.Glyph + (again ? " Extract again (costs again)" : " Extract with AI"), FundPalette.AiSurface, FundPalette.Ai, FundPalette.Ai);
            b.ToolTip = AiContent.TriggerTooltip + " Uses the ideation budget. Nothing runs before you confirm the values.";
            AutomationProperties.SetName(b, again ? "Extract the strategy from the paper again with AI" : "Extract the strategy from the paper with AI");
            b.Click += async (_, _) => await ExtractAsync(b);
            row.Children.Add(b);
            return row;
        }

        private FrameworkElement ReviewForm(PaperExtraction e)
        {
            var f = e.Fields!;
            var stack = new StackPanel();
            AddField(stack, "hypothesis", "Hypothesis", f.Hypothesis);
            AddField(stack, "universe", "Universe", f.Universe);
            AddField(stack, "signal", "Signal construction", f.Signal);
            AddField(stack, "rebalancing", "Rebalancing", f.Rebalancing);
            AddField(stack, "holding_period", "Holding period", f.HoldingPeriod);

            stack.Children.Add(Small("Sample period of the paper (YYYY-MM-DD) — required", top: 8));
            var period = new StackPanel { Orientation = Orientation.Horizontal };
            _startBox = FundUi.Input(f.SamplePeriod?.Start ?? "");
            _startBox.Width = 120;
            AutomationProperties.SetName(_startBox, "Sample period start, YYYY-MM-DD");
            _endBox = FundUi.Input(f.SamplePeriod?.End ?? "");
            _endBox.Width = 120;
            _endBox.Margin = new Thickness(8, 0, 0, 0);
            AutomationProperties.SetName(_endBox, "Sample period end, YYYY-MM-DD");
            period.Children.Add(_startBox);
            period.Children.Add(_endBox);
            stack.Children.Add(period);
            stack.Children.Add(SourceLine(f.SamplePeriod?.Source));

            stack.Children.Add(Small("Reported Sharpe ratio — required (the replication target)", top: 8));
            _sharpeBox = FundUi.Input(f.Reported?.Sharpe is double sh ? sh.ToString("0.###", Inv) : "");
            _sharpeBox.Width = 120;
            _sharpeBox.HorizontalAlignment = HorizontalAlignment.Left;
            AutomationProperties.SetName(_sharpeBox, "Reported Sharpe ratio of the paper");
            stack.Children.Add(_sharpeBox);
            var basis = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 4, 0, 0) };
            var rb = f.Reported?.Basis;
            _basisNet = Radio("PaperBasis", "net of costs", "Reported result is net of costs", rb == "net");
            _basisGross = Radio("PaperBasis", "gross", "Reported result is gross (before costs)", rb == "gross");
            _basisUnknown = Radio("PaperBasis", "unknown", "It is unknown whether the result is net or gross", rb == "unknown");
            basis.Children.Add(_basisNet);
            basis.Children.Add(_basisGross);
            basis.Children.Add(_basisUnknown);
            stack.Children.Add(basis);
            var other = new List<string>();
            if (f.Reported?.Cagr is double cg) other.Add("CAGR " + cg.ToString("0.####", Inv));
            if (f.Reported?.MaxDd is double md) other.Add("max drawdown " + md.ToString("0.####", Inv));
            if (!string.IsNullOrWhiteSpace(f.Reported?.CostsAssumed)) other.Add("costs assumed: " + f.Reported!.CostsAssumed);
            if (other.Count > 0) stack.Children.Add(Line("Also reported (as extracted, not used): " + string.Join(" · ", other), 10.5, FundPalette.Dim));
            stack.Children.Add(SourceLine(f.Reported?.Source));
            if (f.Caveats is { Count: > 0 } cav)
            {
                stack.Children.Add(Small("Caveats named by the AI", top: 8));
                foreach (var c in cav) stack.Children.Add(Line("• " + c, 10.5, FundPalette.Dim));
            }

            var build = new WrapPanel { Margin = new Thickness(0, 10, 0, 0) };
            var standard = Radio("PaperBuildModel", "Standard", "Build the strategy with the standard model", true);
            _buildPremium = Radio("PaperBuildModel", "Premium", "Build the strategy with the premium model", false);
            build.Children.Add(standard);
            build.Children.Add(_buildPremium);
            var confirm = FundUi.MakeButton(AiContent.Glyph + " Confirm & build strategy", FundPalette.AiSurface, FundPalette.Ai, FundPalette.Ai);
            confirm.ToolTip = "Sends the confirmed values to the AI, which drafts the strategy in the DSL; a valid draft is backtested over the paper's period (counts as a trial).";
            AutomationProperties.SetName(confirm, "Confirm the values and build the strategy");
            confirm.Click += async (_, _) => await ConfirmAsync(e, confirm);
            build.Children.Add(confirm);
            stack.Children.Add(build);
            _formError = FundUi.Text("", 11, FundPalette.Kill);
            _formError.Visibility = Visibility.Collapsed;
            _formError.Margin = new Thickness(0, 6, 0, 0);
            stack.Children.Add(_formError);
            return FundUi.Panel(stack, new Thickness(12, 10, 12, 10), new Thickness(0, 2, 0, 8));
        }

        private void AddField(StackPanel stack, string key, string label, PaperField? field)
        {
            stack.Children.Add(Small(label, top: stack.Children.Count == 0 ? 0 : 8));
            var box = FundUi.Input(field?.Value ?? "", mono: false);
            box.AcceptsReturn = true;
            box.TextWrapping = TextWrapping.Wrap;
            box.MaxLength = PlatformClient.PaperMaxFieldChars;
            box.MinHeight = 28;
            AutomationProperties.SetName(box, label + " (editable)");
            AutomationProperties.SetHelpText(box, "Source: " + (field?.Source ?? "none given"));
            _fieldBoxes[key] = box;
            stack.Children.Add(box);
            stack.Children.Add(SourceLine(field?.Source));
        }

        private static TextBlock SourceLine(string? source)
        {
            var t = FundUi.Text("Source in the paper: " + (string.IsNullOrWhiteSpace(source) ? "none given by the AI — check this value yourself" : "“" + source + "”"),
                                10, string.IsNullOrWhiteSpace(source) ? FundPalette.Risk : FundPalette.Faint);
            t.Margin = new Thickness(0, 2, 0, 0);
            return t;
        }

        private static FrameworkElement ConfirmedBlock(PaperConfirmation c)
        {
            var stack = new StackPanel();
            stack.Children.Add(Line("Confirmed " + FundFormat.LocalTime(c.CreatedAt, "yyyy-MM-dd HH:mm") + " · strategy " + (c.StrategyId ?? "not created")
                                    + (c.Version is int v ? " v" + v.ToString(Inv) : ""), 10.5, FundPalette.Dim, mono: true));
            foreach (var p in BtJson.Props(c.Confirmed))
            {
                if (p.Name == "extraction_id" || p.Name == "model") continue;
                stack.Children.Add(Line(p.Name.Replace('_', ' ') + ": " + FundFormat.Plain(p.Value), 10.5, FundPalette.Text));
            }
            return FundUi.Panel(stack, new Thickness(12, 8, 12, 8), new Thickness(0, 2, 0, 8));
        }

        // =====================================================================================
        // Extraction + confirmation
        // =====================================================================================

        private async Task ExtractAsync(Button button)
        {
            if (_busy || _paperId == null) return;
            var paperId = _paperId;
            var model = _extractPremium?.IsChecked == true ? PlatformClient.ExplainModelPremium : PlatformClient.ExplainModelStandard;
            _busy = true;
            button.IsEnabled = false;
            StartWaiting(AiContent.Glyph + " Reading the paper…");
            try
            {
                var res = await PlatformClient.ExtractPaperAsync(paperId, model);
                if (_stopped) return;
                if (!res.Ok) { ShowDetailError(res.Message); return; }
            }
            finally
            {
                StopWaiting();
                _busy = false;
                button.IsEnabled = true;
            }
            await SelectAsync(paperId);
        }

        private async Task ConfirmAsync(PaperExtraction e, Button button)
        {
            if (_busy || _paperId == null || e.Id == null || _startBox == null || _endBox == null || _sharpeBox == null || _formError == null) return;
            var start = _startBox.Text.Trim();
            var end = _endBox.Text.Trim();
            var problems = new List<string>();
            if (!DateTime.TryParseExact(start, "yyyy-MM-dd", Inv, DateTimeStyles.None, out var s)) problems.Add("the sample start (YYYY-MM-DD)");
            if (!DateTime.TryParseExact(end, "yyyy-MM-dd", Inv, DateTimeStyles.None, out var en)) problems.Add("the sample end (YYYY-MM-DD)");
            if (problems.Count == 0 && en <= s) problems.Add("an end after the start");
            if (!double.TryParse(_sharpeBox.Text.Trim().Replace(',', '.'), NumberStyles.Float, Inv, out var sharpe) || double.IsNaN(sharpe) || double.IsInfinity(sharpe))
                problems.Add("the reported Sharpe ratio (a number)");
            if (problems.Count > 0)
            {
                _formError.Text = "Enter " + string.Join(", ", problems) + ".";
                _formError.Visibility = Visibility.Visible;
                return;
            }
            _formError.Visibility = Visibility.Collapsed;
            var model = _buildPremium?.IsChecked == true ? PlatformClient.ExplainModelPremium : PlatformClient.ExplainModelStandard;
            var body = new Dictionary<string, object?>
            {
                ["extraction_id"] = e.Id.Value,
                ["model"] = model,
                ["sample_start"] = start,
                ["sample_end"] = end,
                ["reported_sharpe"] = sharpe,
            };
            var basis = _basisNet?.IsChecked == true ? "net" : _basisGross?.IsChecked == true ? "gross" : _basisUnknown?.IsChecked == true ? "unknown" : null;
            if (basis != null) body["reported_basis"] = basis;
            foreach (var (key, box) in _fieldBoxes)
            {
                var v = (box.Text ?? "").Trim();
                if (v.Length > 0) body[key] = v;
            }
            if (!FundDialogs.Confirm(Window.GetWindow(this), "Confirm & build strategy",
                    "Build a strategy from these confirmed values? The AI drafts it in the DSL (ideation budget, " + TierLabel(model)
                    + " model); a valid draft becomes a strategy (origin paper) and is backtested over the paper's period "
                    + start + " … " + end + " — that replication backtest counts as a trial.", "Confirm & build"))
                return;
            var paperId = _paperId;
            FrameworkElement? after = null;
            _busy = true;
            button.IsEnabled = false;
            StartWaiting(AiContent.Glyph + " Building the strategy…");
            try
            {
                var res = await PlatformClient.ConfirmPaperAsync(paperId, body);
                if (_stopped) return;
                if (!res.Ok) { ShowDetailError(res.Message); return; }
                // review L3: a discarded draft shows the backend's fixed note (no AI text); draft_invalid / an invalid draft
                // additionally the validator's findings; topic_check_failed and the other reasons ONLY the note
                if (res.Value?.Draft is StudioDraft draft && (draft.DiscardedReason != null || draft.Valid != true))
                    after = DiscardedDraftBlock(draft, res.Value.Note);
            }
            finally
            {
                StopWaiting();
                _busy = false;
                button.IsEnabled = true;
            }
            await SelectAsync(paperId);
            if (after != null) _detail.Children.Insert(Math.Min(_detail.Children.Count, 1), after);
        }

        // =====================================================================================
        // Replication
        // =====================================================================================

        private void RenderReplication(PaperReplication? r, string? strategyId)
        {
            var host = _replicationHost;
            if (host == null) return;
            host.Children.Clear();
            if (r == null || r.State == "none")
            {
                host.Children.Add(Line("No replication yet — it runs after you confirm the values.", 11, FundPalette.Faint));
                return;
            }
            var stack = new StackPanel();
            var head = new StackPanel { Orientation = Orientation.Horizontal };
            head.Children.Add(FundUi.Text("Replication", 11.5, FundPalette.TextBright, bold: true, wrap: false));
            var (chipText, chipBrush) = r.Verdict switch
            {
                "replicated" => ("replicated", FundPalette.Long),
                "not replicated" => ("not replicated", FundPalette.Short),
                "failed" => ("failed", FundPalette.Kill),
                "pending" => ("pending", FundPalette.Focus),
                null => ("verdict not reported", FundPalette.Risk),
                _ => (r.Verdict!, FundPalette.Risk),
            };
            var chip = Chip(chipText, chipBrush);
            chip.Margin = new Thickness(8, 0, 0, 0);
            head.Children.Add(chip);
            stack.Children.Add(head);

            var tol = r.Tolerance is double t ? "±" + (t * 100).ToString("0", Inv) + " %" : "tolerance not reported";
            stack.Children.Add(Line("Paper Sharpe " + FundFormat.Num(r.ReportedSharpe) + " · our Sharpe " + FundFormat.Num(r.OurSharpe)
                                    + " — replicated = same sign and within " + tol + " of the paper's value", 11.5, FundPalette.Text, mono: true));
            stack.Children.Add(Line("Paper period " + Period(r.PaperPeriod) + " · period used " + (r.PeriodUsed != null ? Period(r.PeriodUsed) : "—")
                                    + (r.Clamped == true ? " (clamped to our data)" : ""), 10.5, r.Clamped == true ? FundPalette.Risk : FundPalette.Dim, mono: true));
            foreach (var n in r.ClampNotes ?? new List<string>()) stack.Children.Add(Line("• " + n, 10.5, FundPalette.Risk));
            foreach (var n in r.Notes ?? new List<string>()) stack.Children.Add(Line("• " + n, 10.5, FundPalette.Dim));

            switch (r.State)
            {
                case "waiting":
                    stack.Children.Add(Line("Waiting for the backtest slot — another job is running; it starts automatically.", 10.5, FundPalette.Risk));
                    break;
                case "start_failed":
                    stack.Children.Add(Line("The replication backtest could not start.", 10.5, FundPalette.Kill));
                    break;
                case "started":
                    if (r.RunStatus == null || BacktestStatus.IsActive(r.RunStatus))
                        stack.Children.Add(Line("Replication backtest " + (r.RunStatus ?? "started") + "… · run " + (r.RunId ?? "?"), 10.5, FundPalette.Focus, mono: true));
                    else
                        stack.Children.Add(Line("Replication backtest " + r.RunStatus + " · run " + (r.RunId ?? "?"), 10.5, r.RunStatus == "done" ? FundPalette.Long : FundPalette.Kill, mono: true));
                    break;
            }
            if (r.KeyMetrics is JsonElement km)
            {
                stack.Children.Add(Line("Return " + FundFormat.Fraction(BtJson.Num(km, "total_return"), 1, signed: true)
                                        + " · CAGR " + FundFormat.Fraction(BtJson.Num(km, "cagr"), 1, signed: true)
                                        + " · Sharpe " + FundFormat.Num(BtJson.Num(km, "sharpe"))
                                        + " · Max DD " + FundFormat.Fraction(BtJson.Num(km, "max_drawdown")), 10.5, FundPalette.Text, mono: true));
            }
            var buttons = new WrapPanel { Margin = new Thickness(0, 4, 0, 0) };
            if (r.RunId != null)
            {
                string runId = r.RunId;
                var open = FundUi.MakeButton("Open report");
                open.Margin = new Thickness(0, 0, 8, 0);
                AutomationProperties.SetName(open, "Open the report of the replication backtest");
                open.Click += (_, _) => BacktestReportWindow.Open(Window.GetWindow(this), strategyId, null, runId);
                buttons.Children.Add(open);
            }
            if (strategyId != null)
            {
                string sid = strategyId;
                var lib = FundUi.MakeButton("Open in Library");
                AutomationProperties.SetName(lib, "Open the paper's strategy in the Library");
                lib.Click += (_, _) => OpenInLibraryRequested?.Invoke(sid);
                buttons.Children.Add(lib);
            }
            if (buttons.Children.Count > 0) stack.Children.Add(buttons);
            var panel = FundUi.Panel(stack, new Thickness(12, 10, 12, 10), new Thickness(0, 2, 0, 8));
            AutomationProperties.SetName(panel, "Replication: " + chipText + ", paper Sharpe " + FundFormat.Num(r.ReportedSharpe) + ", our Sharpe " + FundFormat.Num(r.OurSharpe));
            host.Children.Add(panel);
        }

        private static bool ReplicationActive(PaperReplication? r)
            => r != null && (r.State == "waiting" || (r.State == "started" && (r.RunStatus == null || BacktestStatus.IsActive(r.RunStatus))));

        private void UpdatePolling()
        {
            if (!_stopped && IsVisible && ReplicationActive(_current?.Replication)) { if (!_poll.IsEnabled) _poll.Start(); }
            else _poll.Stop();
        }

        private async Task PollReplicationAsync()
        {
            if (_polling || _stopped || _busy || _paperId == null || !IsVisible) return;
            _polling = true;
            try
            {
                var paperId = _paperId;
                var res = await PlatformClient.GetPaperReplicationAsync(paperId);
                if (_stopped || paperId != _paperId || _current == null) return;
                if (!res.Ok || res.Value == null) return;   // the card keeps the last state; the next tick tries again
                var wasActive = ReplicationActive(_current.Replication);
                _current.Replication = res.Value;
                RenderReplication(res.Value, _current.Paper?.StrategyId ?? _current.Confirmation?.StrategyId);
                if (wasActive && !ReplicationActive(res.Value)) await LoadListAsync();
            }
            finally
            {
                _polling = false;
                UpdatePolling();
            }
        }

        // =====================================================================================
        // Small builders
        // =====================================================================================

        private void StartWaiting(string text)
        {
            _waitText = text;
            _waitStarted = DateTime.UtcNow;
            ShowWaiting();
            _waitTimer.Start();
        }

        private void StopWaiting()
        {
            _waitTimer.Stop();
            AddStatus("", error: false);
        }

        private void ShowWaiting()
        {
            var secs = (int)Math.Max(0, (DateTime.UtcNow - _waitStarted).TotalSeconds);
            _addStatusHost.Children.Clear();
            _addStatusHost.Children.Add(FundUi.Text(_waitText + " " + secs.ToString(Inv) + " s (the language model may take up to 90 s per call)", 10.5, FundPalette.Ai));
        }

        private void ShowDetailError(string message) => _detail.Children.Insert(Math.Min(_detail.Children.Count, 1), ErrorBlock(message));

        private void Placeholder(string text)
        {
            _detail.Children.Clear();
            _detail.Children.Add(FundUi.Text(text, 11.5, FundPalette.Faint));
        }

        private void Section(string title)
        {
            var t = FundUi.Text(title, 12, FundPalette.Text, bold: true);
            t.Margin = new Thickness(0, 14, 0, 6);
            _detail.Children.Add(t);
        }

        private static TextBlock Head(string text)
        {
            var t = FundUi.Text(text, 12, FundPalette.Text, bold: true);
            t.Margin = new Thickness(0, 0, 0, 6);
            return t;
        }

        private static TextBlock Small(string text, double top)
        {
            var t = FundUi.Text(text, 10.5, FundPalette.Faint);
            t.Margin = new Thickness(0, top, 0, 2);
            return t;
        }

        private static TextBlock Line(string text, double size, Brush brush, bool mono = false)
        {
            var t = FundUi.Text(text, size, brush, mono: mono);
            t.Margin = new Thickness(0, 1, 0, 2);
            return t;
        }

        private static Border Neutral(string text) => new()
        {
            Background = FundPalette.WindowBg,
            BorderBrush = FundPalette.Hairline,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(10, 5, 10, 6),
            Margin = new Thickness(0, 2, 0, 6),
            Child = FundUi.Text(text, 11, FundPalette.Text),
        };

        /// <summary>A not-relevant / topic-check-failed extraction: no AI text is shown, only that the call ran and its cost.</summary>
        private static FrameworkElement ExtractionCostNote(PaperExtraction e)
            => Line("Extraction " + FundFormat.LocalTime(e.CreatedAt, "yyyy-MM-dd HH:mm") + " · " + TierLabel(e.ModelTier) + " · cost " + FundFormat.Usd(e.CostUsd)
                    + (string.IsNullOrWhiteSpace(e.DiscardedReason) ? "" : " · " + e.DiscardedReason!.Replace('_', ' ')) + " — no AI text is shown", 10, FundPalette.Faint, mono: true);

        private static FrameworkElement DiscardedDraftBlock(StudioDraft d, string? note)
        {
            var stack = new StackPanel();
            stack.Children.Add(FundUi.Text("System note — no strategy, no backtest", 10, FundPalette.Faint, mono: true));
            stack.Children.Add(FundUi.Text(string.IsNullOrWhiteSpace(note)
                ? "The draft was not accepted" + (d.DiscardedReason != null ? " (" + d.DiscardedReason.Replace('_', ' ') + ")" : "") + "; the backend sent no note."
                : note!, 11, FundPalette.Text));
            var showValidator = d.DiscardedReason == null || d.DiscardedReason == "draft_invalid";
            var errs = d.Validation?.Errors ?? new List<StrategyIssue>();
            if (showValidator && errs.Count > 0)
            {
                stack.Children.Add(FundUi.Text("Validator findings on the proposed spec:", 10.5, FundPalette.Faint));
                foreach (var e in errs)
                    stack.Children.Add(FundUi.Text("error" + (string.IsNullOrWhiteSpace(e.Path) ? "" : " at " + e.Path) + (e.Position.Length > 0 ? " (" + e.Position + ")" : "")
                                                   + ": " + (e.Message ?? ""), 10.5, FundPalette.Kill, mono: true));
            }
            var b = Neutral("");
            b.Child = stack;
            AutomationProperties.SetName(b, "System note: " + (note ?? "the draft was not accepted"));
            return b;
        }

        private static Border AiBox(string heading, string text, PaperExtraction e)
        {
            FundAi.TryRead(e.Provenance, e.Model, e.CreatedAt, out var model, out var at);
            var stack = new StackPanel();
            stack.Children.Add(FundUi.Text(AiContent.Badge(model, at), 10, FundPalette.Ai, bold: true, mono: true));
            stack.Children.Add(FundUi.Text(heading, 11, FundPalette.Text, bold: true));
            stack.Children.Add(FundUi.Text(text, 11, FundPalette.Dim));
            var b = new Border
            {
                Background = FundPalette.AiSurface,
                BorderBrush = FundPalette.Ai,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(6),
                Padding = new Thickness(10, 6, 10, 8),
                Margin = new Thickness(0, 2, 0, 6),
                Child = stack,
            };
            AutomationProperties.SetName(b, AiContent.AutomationName(model, at) + " " + heading + ": " + text);
            return b;
        }

        private static string Period(PaperPeriodField? p) => p == null ? "—" : (p.Start ?? "?") + " … " + (p.End ?? "?");

        private static string TierLabel(string? tier) => tier switch
        {
            "standard" => "Standard",
            "premium" => "Premium",
            null or "" => "tier not reported",
            _ => tier!,
        };

        private static string Short(string? hash) => string.IsNullOrEmpty(hash) ? "—" : hash!.Length > 12 ? hash[..12] : hash;

        private static Border Chip(string text, Brush brush) => new()
        {
            BorderBrush = brush,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(999),
            Padding = new Thickness(6, 0, 6, 1),
            VerticalAlignment = VerticalAlignment.Center,
            Child = FundUi.Text(text, 9.5, brush, bold: true, wrap: false),
        };

        private static RadioButton Radio(string group, string content, string name, bool isChecked)
        {
            var rb = new RadioButton
            {
                GroupName = group,
                Content = content,
                IsChecked = isChecked,
                Foreground = FundPalette.Text,
                FontSize = 12,
                Margin = new Thickness(0, 0, 14, 0),
                VerticalAlignment = VerticalAlignment.Center,
            };
            AutomationProperties.SetName(rb, name);
            return rb;
        }

        private static Border ErrorBlock(string message) => new()
        {
            Background = FundPalette.KillSurface,
            BorderBrush = FundPalette.Kill,
            BorderThickness = new Thickness(1),
            CornerRadius = new CornerRadius(6),
            Padding = new Thickness(10, 6, 10, 6),
            Margin = new Thickness(0, 4, 0, 8),
            Child = FundUi.Text(message, 11.5, FundPalette.Make(0xFFF4A197)),
        };
    }
}
