using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Fund;
using UFOS.ai.Shared;

namespace UFOS.ai.HedgeFund.Platform
{
    // =====================================================================================
    // Shared building blocks of the stage-9 views "Monitoring" and "Shadow Account" (SPEC §19, WPF notes §19.13): chips,
    // tables, JSON readers for blocks kept raw, the AI commentary panel (labelled per CLAUDE.md / AiContent: ✦ trigger,
    // badge before, caveat after, violet container, cost line, number check, provenance) and the realised-vs-expected band
    // chart. Every text from the backend or an imported file is plain TextBlock.Text (never evaluated, no markup).
    // =====================================================================================

    internal static class MonUi
    {
        public static readonly CultureInfo Inv = CultureInfo.InvariantCulture;
        public static readonly Brush FocusSurface = FundPalette.Make(0x1A7C8CF8);
        public static readonly Brush RiskSurface = FundPalette.Make(0x1FD9A54B);
        public static readonly Brush LongSurface = FundPalette.Make(0x1A6FA98A);

        /// <summary>fix1023 B2: replaces the live "numbers now" line of a pause that came from a synthetic sandbox force-decay.</summary>
        public const string SyntheticPauseText = "Synthetic pause (sandbox force-decay) — the live rule numbers are not affected; the drawdown in the reason is synthetic.";

        public static bool IsSyntheticPause(MonPaused? p) => p?.Synthetic == true || p?.Numbers?.Synthetic == true;

        /// <summary>
        /// fix1023 B2: the numbers that triggered THIS pause (paused.numbers, from the event), labelled "SYNTHETIC" for a sandbox
        /// force-decay (plus the backend's numbers_note). Null when the payload has none — a real pause then shows only the live line
        /// "Numbers now …" (labelled "now"); a synthetic pause never shows live numbers.
        /// </summary>
        public static string? PauseNumbersText(MonPaused? p)
        {
            if (p == null) return null;
            var synthetic = IsSyntheticPause(p);
            var n = p.Numbers;
            if (n == null)
                return synthetic ? (string.IsNullOrWhiteSpace(p.NumbersNote) ? SyntheticPauseText : "Synthetic pause — " + p.NumbersNote!.Trim()) : null;
            var text = (synthetic ? "SYNTHETIC numbers at the pause: " : "Numbers at the pause: ") + RuleName(n.Rule ?? p.Rule)
                       + " · value " + Frac(n.Value, 2) + " vs threshold " + Frac(n.Threshold, 2) + TriggeredText(n.Triggered);
            if (synthetic && !string.IsNullOrWhiteSpace(p.NumbersNote)) text += " · " + p.NumbersNote!.Trim();
            return text;
        }

        private static string TriggeredText(JsonElement? t)
        {
            if (t is not JsonElement e) return "";
            return e.ValueKind switch
            {
                JsonValueKind.True => " · triggered",
                JsonValueKind.False => " · not triggered",
                JsonValueKind.String => " · triggered: " + e.GetString(),
                JsonValueKind.Array => " · triggered: " + string.Join(", ", e.EnumerateArray().Select(x => x.ValueKind == JsonValueKind.String ? x.GetString() : x.ToString())),
                _ => "",
            };
        }

        /// <summary>fix1023 B2: "last decay check" — the backend's newest check (a scheduled check or the pause event), never "none yet" when a pause exists.</summary>
        public static string LastCheckText(MonDecay? d)
        {
            if (d == null) return "—";
            var when = d.LastCheckAt != null ? TradingText.Ts(d.LastCheckAt) : null;
            if (d.LastCheckSource == "pause")
            {
                var at = when ?? (d.Paused?.At is string pa ? TradingText.Ts(pa) : "—");
                return "the pause at " + at + (d.LastCheckDay != null ? " (check day " + d.LastCheckDay + ")" : "") + "; no scheduled check since";
            }
            if (d.LastCheckDay != null || when != null)
                return (d.LastCheckDay ?? "") + (when != null ? (d.LastCheckDay != null ? " · " : "") + when : "");
            return d.Paused?.At is string pausedAt ? "no scheduled check yet; paused at " + TradingText.Ts(pausedAt) : "none yet";
        }

        public static Border Chip(string text, Brush brush)
        {
            var t = FundUi.Text(text, 9.5, brush, bold: true, mono: true, wrap: false);
            var b = new Border
            {
                BorderBrush = brush,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(4),
                Padding = new Thickness(5, 1, 5, 1),
                Margin = new Thickness(0, 0, 6, 2),
                VerticalAlignment = VerticalAlignment.Center,
                Child = t,
            };
            AutomationProperties.SetName(b, text);
            return b;
        }

        public static TextBlock Line(string? text, double size = 11, Brush? brush = null, bool mono = false, bool bold = false)
        {
            var t = FundUi.Text(text ?? "", size, brush ?? FundPalette.Dim, bold: bold, mono: mono);
            t.Margin = new Thickness(0, 1, 0, 2);
            return t;
        }

        public static TextBlock Head(string text)
        {
            var t = FundUi.Text(text.ToUpperInvariant(), 10.5, FundPalette.Faint, bold: true);
            t.Margin = new Thickness(0, 10, 0, 4);
            return t;
        }

        public static Button Small(string text, string automationName)
        {
            var b = FundUi.MakeButton(text);
            b.Padding = new Thickness(10, 2, 10, 2);
            b.MinHeight = 22;
            b.FontSize = 11;
            b.Margin = new Thickness(0, 0, 8, 0);
            AutomationProperties.SetName(b, automationName);
            return b;
        }

        public static Border Callout(string text, Brush brush, Brush surface) => TradingUi.Callout(text, brush, surface);

        public static TextBlock ErrorBlock(string text)
        {
            var t = FundUi.Text(text, 11.5, FundPalette.Kill);
            t.Margin = new Thickness(0, 4, 0, 8);
            AutomationProperties.SetLiveSetting(t, AutomationLiveSetting.Assertive);
            AutomationProperties.SetName(t, text);
            return t;
        }

        /// <summary>A label/value grid (two columns), each value plain text in mono.</summary>
        public static Grid Pairs(IEnumerable<(string Label, string Value)> rows, Brush? valueBrush = null)
        {
            var grid = new Grid { Margin = new Thickness(0, 2, 0, 4) };
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            var r = 0;
            foreach (var (label, value) in rows)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var l = FundUi.Text(label, 11, FundPalette.Faint);
                l.Margin = new Thickness(0, 1, 14, 1);
                Grid.SetRow(l, r);
                var v = FundUi.Text(value, 11, valueBrush ?? FundPalette.Text, mono: true);
                v.Margin = new Thickness(0, 1, 0, 1);
                AutomationProperties.SetName(v, label + ": " + value);
                Grid.SetRow(v, r);
                Grid.SetColumn(v, 1);
                grid.Children.Add(l);
                grid.Children.Add(v);
                r++;
            }
            return grid;
        }

        /// <summary>A simple read-only table: header row + text cells (plain text; wraps).</summary>
        public static Grid Table(IReadOnlyList<string> headers, IEnumerable<IReadOnlyList<string>> rows, string automationName, IReadOnlyList<double>? widths = null)
        {
            var grid = new Grid { Margin = new Thickness(0, 2, 0, 8) };
            for (var c = 0; c < headers.Count; c++)
                grid.ColumnDefinitions.Add(new ColumnDefinition
                {
                    Width = widths != null && c < widths.Count && widths[c] > 0 ? new GridLength(widths[c]) : new GridLength(1, GridUnitType.Star),
                });
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            for (var c = 0; c < headers.Count; c++)
            {
                var h = FundUi.Text(headers[c], 10, FundPalette.Faint, bold: true);
                h.Margin = new Thickness(0, 0, 8, 3);
                Grid.SetColumn(h, c);
                grid.Children.Add(h);
            }
            var r = 1;
            foreach (var row in rows)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                for (var c = 0; c < headers.Count; c++)
                {
                    var text = c < row.Count ? row[c] : "";
                    var cell = FundUi.Text(text, 10.5, c == 0 ? FundPalette.Text : FundPalette.Dim, mono: c > 0);
                    cell.Margin = new Thickness(0, 1, 8, 1);
                    AutomationProperties.SetName(cell, headers[c] + ": " + text);
                    Grid.SetRow(cell, r);
                    Grid.SetColumn(cell, c);
                    grid.Children.Add(cell);
                }
                r++;
            }
            if (r == 1)
            {
                grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
                var none = FundUi.Text("(none)", 10.5, FundPalette.Faint);
                Grid.SetRow(none, 1);
                Grid.SetColumnSpan(none, Math.Max(1, headers.Count));
                grid.Children.Add(none);
            }
            AutomationProperties.SetName(grid, automationName);
            return grid;
        }

        public static Brush SeverityBrush(string? severity) => severity switch
        {
            "critical" => FundPalette.Kill,
            "warning" => FundPalette.Risk,
            _ => FundPalette.Focus,
        };

        public static string SeverityLabel(string? severity) => severity switch
        {
            "critical" => "CRITICAL",
            "warning" => "WARNING",
            "info" => "INFO",
            null or "" => "—",
            _ => severity!.ToUpperInvariant(),
        };

        public static (string Text, Brush Brush) DecayChip(string? state) => state switch
        {
            "ok" => ("DECAY: OK", FundPalette.Long),
            "watch" => ("DECAY: WATCH", FundPalette.Risk),
            "paused" => ("PAUSED BY THE DECAY MONITOR", FundPalette.Kill),
            "insufficient" => ("DECAY: NOT ENOUGH SESSIONS YET", FundPalette.Faint),
            null or "" => ("DECAY: —", FundPalette.Faint),
            _ => ("DECAY: " + state!.ToUpperInvariant(), FundPalette.Dim),
        };

        public static string RuleName(string? rule) => rule switch
        {
            "return_band" => "Return below the expected band",
            "drawdown" => "Drawdown beyond the limit",
            "volatility" => "Volatility above the limit",
            null or "" => "—",
            _ => rule!,
        };

        /// <summary>A cumulative LOG return as a simple return in percent ("+2.12%").</summary>
        public static string LogAsPct(double? logReturn, int decimals = 2)
            => logReturn is double l ? FundFormat.Fraction(Math.Exp(l) - 1.0, decimals, signed: true) : FundFormat.Dash;

        /// <summary>A fraction (0.11) as percent ("11.0%").</summary>
        public static string Frac(double? v, int decimals = 1, bool signed = false) => FundFormat.Fraction(v, decimals, signed);

        public static string Num(double? v, int decimals = 2) => FundFormat.Num(v, decimals);

        public static string Int(int? v) => v is int i ? i.ToString("N0", Inv) : FundFormat.Dash;

        public static string Ts(string? iso) => FundFormat.LocalTime(iso, "yyyy-MM-dd HH:mm");

        public static string Plain(JsonElement? e) => e is JsonElement v ? FundFormat.Plain(v) : FundFormat.Dash;

        /// <summary>Money in a named currency ("1,234.50 EUR"); no symbol guessing.</summary>
        public static string Money(double? v, string? currency, int decimals = 2)
        {
            if (v is not double d || double.IsNaN(d) || double.IsInfinity(d)) return FundFormat.Dash;
            return FundFormat.Num(d, decimals) + (string.IsNullOrWhiteSpace(currency) ? "" : " " + currency);
        }
    }

    /// <summary>Readers for JSON blocks kept raw (diagnostics, shadows; any block may be {unavailable: reason}).</summary>
    internal static class ShJson
    {
        public static JsonElement? Get(JsonElement? e, string key)
        {
            if (e is not JsonElement v || v.ValueKind != JsonValueKind.Object) return null;
            return v.TryGetProperty(key, out var p) ? p : (JsonElement?)null;
        }

        public static string? Str(JsonElement? e, string key)
            => Get(e, key) is JsonElement p && p.ValueKind == JsonValueKind.String ? p.GetString() : null;

        public static double? Num(JsonElement? e, string key)
            => Get(e, key) is JsonElement p && p.ValueKind == JsonValueKind.Number && p.TryGetDouble(out var d) ? d : null;

        public static bool? Bool(JsonElement? e, string key)
        {
            if (Get(e, key) is not JsonElement p) return null;
            if (p.ValueKind == JsonValueKind.True) return true;
            if (p.ValueKind == JsonValueKind.False) return false;
            return null;
        }

        public static List<JsonElement> Arr(JsonElement? e, string key)
        {
            var list = new List<JsonElement>();
            if (Get(e, key) is JsonElement p && p.ValueKind == JsonValueKind.Array)
                foreach (var x in p.EnumerateArray()) list.Add(x);
            return list;
        }

        public static List<string> Strings(JsonElement? e, string key)
            => Arr(e, key).Where(x => x.ValueKind == JsonValueKind.String).Select(x => x.GetString() ?? "").ToList();

        /// <summary>The block's "unavailable" reason, or null when the block has data.</summary>
        public static string? Unavailable(JsonElement? block)
        {
            if (block is not JsonElement v) return "not reported by the backend";
            if (v.ValueKind == JsonValueKind.Null || v.ValueKind == JsonValueKind.Undefined) return "not reported by the backend";
            if (v.ValueKind != JsonValueKind.Object) return null;
            return Str(v, "unavailable");
        }
    }

    // =====================================================================================
    // AI commentary (report / Shadow Account) — budget key "reports", numbers only (§19.4 / §19.8).
    // =====================================================================================

    /// <summary>
    /// "✦ AI commentary…" for a report or a Shadow Account: options (Standard / Premium, English / German, Generate), the
    /// waiting state (AiContent.LoadingBadge — the synchronous POST may take up to 90 s; the UI stays responsive), the
    /// commentaries (newest first) and errors verbatim (402 budget: the backend's message + reason + budget key "reports").
    /// The panel says what is sent: the computed numbers only (labels generated by the backend; strategies as "strategy
    /// 1..n", positions as "position 1..n"; no names, ISINs, symbols, account numbers or file names).
    /// </summary>
    internal sealed class MonCommentaryPanel : StackPanel
    {
        private readonly Func<string, string, Task<PlatformResult<MonCommentaryEnvelope>>> _submit;
        private readonly string _groupId = Guid.NewGuid().ToString("N");
        private readonly Border _options;
        private readonly RadioButton _premium;
        private readonly RadioButton _german;
        private readonly Button _generate;
        private readonly TextBlock _status;
        private readonly StackPanel _errorHost = new();
        private readonly StackPanel _results = new();
        private readonly DispatcherTimer _wait;
        private DateTime _waitStarted;
        private bool _busy;

        public Button TriggerButton { get; }

        /// <summary>Raised after a commentary was stored (the host may reload its data).</summary>
        public event Action<MonCommentary>? Created;

        public MonCommentaryPanel(string what, string sentText, Func<string, string, Task<PlatformResult<MonCommentaryEnvelope>>> submit)
        {
            _submit = submit;
            AutomationProperties.SetName(this, "AI commentary on the " + what);
            TriggerButton = FundUi.MakeButton(AiContent.Glyph + " AI commentary…", FundPalette.AiSurface, FundPalette.Ai, FundPalette.Ai);
            TriggerButton.Padding = new Thickness(10, 2, 10, 2);
            TriggerButton.ToolTip = AiContent.TriggerTooltip;
            AutomationProperties.SetName(TriggerButton, "AI commentary on the " + what + " (optional, costs money)");
            AutomationProperties.SetHelpText(TriggerButton, AiContent.TriggerTooltip);
            TriggerButton.Click += (_, _) =>
            {
                _options!.Visibility = _options.Visibility == Visibility.Visible ? Visibility.Collapsed : Visibility.Visible;
                if (_options.Visibility == Visibility.Visible) _generate!.Focus();
            };

            var opt = new StackPanel();
            opt.Children.Add(FundUi.Text(AiContent.Glyph + " AI commentary on the " + what, 11.5, FundPalette.Ai, bold: true));
            opt.Children.Add(MonUi.Line("Optional. A language model (via OpenRouter) puts the computed numbers into words. " + sentText
                                        + " The backend checks every number in the answer against the numbers it sent; numbers it cannot find are marked red, never removed. "
                                        + "Budget: \"reports\" in the central AI budget window (checked before the call). Each commentary is stored and costs money.",
                                        10.5, FundPalette.Faint));
            var modelRow = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 6, 0, 2) };
            modelRow.Children.Add(Label("Model"));
            var standard = TradingUi.Radio("m" + _groupId, "Standard", "Standard model (cheaper)", true);
            _premium = TradingUi.Radio("m" + _groupId, "Premium", "Premium model (more expensive)", false);
            modelRow.Children.Add(standard);
            modelRow.Children.Add(_premium);
            opt.Children.Add(modelRow);
            var langRow = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 2, 0, 6) };
            langRow.Children.Add(Label("Language"));
            var english = TradingUi.Radio("l" + _groupId, "EN", "Answer in English", true);
            _german = TradingUi.Radio("l" + _groupId, "DE", "Answer in German", false);
            langRow.Children.Add(english);
            langRow.Children.Add(_german);
            opt.Children.Add(langRow);
            var row = new StackPanel { Orientation = Orientation.Horizontal };
            _generate = FundUi.MakeButton("Generate", FundPalette.AiSurface, FundPalette.Ai, FundPalette.Ai);
            _generate.ToolTip = AiContent.TriggerTooltip;
            AutomationProperties.SetName(_generate, "Generate the AI commentary");
            _generate.Click += async (_, _) => await GenerateAsync();
            row.Children.Add(_generate);
            var close = FundUi.MakeButton("Close");
            close.Margin = new Thickness(8, 0, 0, 0);
            AutomationProperties.SetName(close, "Close the AI commentary options");
            close.Click += (_, _) => { if (!_busy) _options!.Visibility = Visibility.Collapsed; };
            row.Children.Add(close);
            opt.Children.Add(row);
            _options = new Border
            {
                Background = FundPalette.AiSurface,
                BorderBrush = FundPalette.Ai,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(12, 10, 12, 10),
                Margin = new Thickness(0, 4, 0, 8),
                Visibility = Visibility.Collapsed,
                Child = opt,
            };
            Children.Add(_options);

            _status = FundUi.Text("", 10.5, FundPalette.Ai, bold: true, mono: true);
            _status.Visibility = Visibility.Collapsed;
            AutomationProperties.SetLiveSetting(_status, AutomationLiveSetting.Polite);
            Children.Add(_status);
            AutomationProperties.SetLiveSetting(_errorHost, AutomationLiveSetting.Assertive);
            Children.Add(_errorHost);
            Children.Add(_results);

            _wait = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
            _wait.Tick += (_, _) => ShowWaiting();
        }

        /// <summary>Shows the stored commentaries (newest first).</summary>
        public void SetCommentaries(IEnumerable<MonCommentary>? items)
        {
            _results.Children.Clear();
            foreach (var c in (items ?? Enumerable.Empty<MonCommentary>()).OrderByDescending(x => x.CreatedAt ?? "", StringComparer.Ordinal))
                _results.Children.Add(Build(c));
        }

        private async Task GenerateAsync()
        {
            if (_busy) return;
            var model = _premium.IsChecked == true ? PlatformClient.ExplainModelPremium : PlatformClient.ExplainModelStandard;
            var language = _german.IsChecked == true ? PlatformClient.ExplainLanguageGerman : PlatformClient.ExplainLanguageEnglish;
            _busy = true;
            _generate.IsEnabled = false;
            _errorHost.Children.Clear();
            _waitStarted = DateTime.UtcNow;
            ShowWaiting();
            _wait.Start();
            try
            {
                var res = await _submit(model, language);
                if (!res.Ok || res.Value?.Commentary == null)
                {
                    // budget reached / AI off / no key / bad_json / timeout / busy … — the backend's text, verbatim
                    var text = res.Ok ? "Unexpected response from the backend (commentary) — backend and app versions may not match." : PlatformClient.CommentaryFailureText(res);
                    var badge = FundUi.Text(AiContent.UnavailableBadge, 10, FundPalette.Kill, bold: true, mono: true);
                    _errorHost.Children.Add(badge);
                    _errorHost.Children.Add(MonUi.ErrorBlock(text));
                }
                else
                {
                    _options.Visibility = Visibility.Collapsed;
                    _results.Children.Insert(0, Build(res.Value.Commentary));
                    Created?.Invoke(res.Value.Commentary);
                }
            }
            finally
            {
                _wait.Stop();
                _status.Visibility = Visibility.Collapsed;
                _busy = false;
                _generate.IsEnabled = true;
            }
        }

        private void ShowWaiting()
        {
            var s = (int)(DateTime.UtcNow - _waitStarted).TotalSeconds;
            _status.Text = AiContent.LoadingBadge + " · " + s.ToString(MonUi.Inv) + " s (up to 90 s)";
            _status.Visibility = Visibility.Visible;
            AutomationProperties.SetName(_status, AiContent.LoadingAutomationName("commentary"));
        }

        /// <summary>The labelled AI block of one commentary (EU AI Act Art. 50 — CLAUDE.md "AI content labelling").</summary>
        public static Border Build(MonCommentary c)
        {
            FundAi.TryRead(c.Provenance, c.Model, c.CreatedAt, out var model, out var at);
            var modelText = string.IsNullOrWhiteSpace(model) ? "model not reported" : AiContent.ShortModelName(model);
            var output = c.Output ?? new ExplanationOutput();
            var unverified = c.Check?.Unverified ?? new List<UnverifiedNumber>();
            var unlocated = new List<UnverifiedNumber>();
            var stack = new StackPanel();
            var badge = FundUi.Text(AiContent.Badge(model, at) + " — commentary on the computed numbers; not verified — information, not a recommendation", 10.5, FundPalette.Ai, bold: true, mono: true);
            badge.Margin = new Thickness(0, 0, 0, 2);
            stack.Children.Add(badge);
            var label = ShJson.Str(c.Provenance, "label");
            var meta = (string.IsNullOrWhiteSpace(label) ? "" : label + " · ") + modelText + " · "
                       + (c.Language == "de" ? "German" : c.Language == "en" ? "English" : "language not reported")
                       + " · " + FundFormat.LocalTime(c.CreatedAt, "yyyy-MM-dd HH:mm") + " · cost " + FundFormat.Usd(c.CostUsd)
                       + (BtJson.Bool(c.Usage, "cost_estimated") == true ? " (estimated)" : "");
            stack.Children.Add(MonUi.Line(meta, 10.5, FundPalette.Faint, mono: true));

            var summary = ExplainPanel.Rich(output.Summary ?? "", "summary", unverified, unlocated, 12, FundPalette.TextBright, bold: false);
            summary.Margin = new Thickness(0, 6, 0, 6);
            stack.Children.Add(summary);
            var sections = output.Sections ?? new List<ExplanationSection>();
            for (var i = 0; i < sections.Count; i++)
            {
                var key = "sections." + i.ToString(MonUi.Inv);
                var t = ExplainPanel.Rich(sections[i].Title ?? "", key + ".title", unverified, unlocated, 11.5, FundPalette.Text, bold: true);
                t.Margin = new Thickness(0, 6, 0, 2);
                stack.Children.Add(t);
                var x = ExplainPanel.Rich(sections[i].Text ?? "", key + ".text", unverified, unlocated, 11.5, FundPalette.Dim, bold: false);
                x.LineHeight = 17;
                stack.Children.Add(x);
            }
            var hypotheses = output.Hypotheses ?? new List<string>();
            if (hypotheses.Count > 0)
            {
                stack.Children.Add(MonUi.Line("Hypotheses — possible causes outside the computed numbers; not checked", 10.5, FundPalette.Risk, bold: true));
                for (var i = 0; i < hypotheses.Count; i++)
                    stack.Children.Add(ExplainPanel.Rich("• " + hypotheses[i], "hypotheses." + i.ToString(MonUi.Inv), Shift(unverified, "hypotheses." + i.ToString(MonUi.Inv), 2), unlocated, 11, FundPalette.Dim, bold: false));
            }
            var limits = output.Limits ?? new List<string>();
            if (limits.Count > 0)
            {
                stack.Children.Add(MonUi.Line("Limits — what the numbers cannot answer", 10.5, FundPalette.Text, bold: true));
                for (var i = 0; i < limits.Count; i++)
                    stack.Children.Add(ExplainPanel.Rich("• " + limits[i], "limits." + i.ToString(MonUi.Inv), Shift(unverified, "limits." + i.ToString(MonUi.Inv), 2), unlocated, 11, FundPalette.Dim, bold: false));
            }

            // number check result (shown, never removed)
            var check = c.Check;
            var unknown = check?.UnknownIds ?? new List<string>();
            if (check == null)
                stack.Children.Add(MonUi.Line("Number check: the backend sent no check result.", 10.5, FundPalette.Risk));
            else if (unverified.Count == 0 && unknown.Count == 0)
                stack.Children.Add(MonUi.Line("Number check: every number in the text was found in the numbers sent" + (check.Ok == true ? " ✓" : " (check status not ok — see the backend)"),
                                              10.5, check.Ok == true ? FundPalette.Long : FundPalette.Risk));
            else
            {
                if (unverified.Count > 0)
                    stack.Children.Add(MonUi.Line("Number check: " + unverified.Count.ToString(MonUi.Inv) + (unverified.Count == 1 ? " number is" : " numbers are")
                                                  + " NOT in the numbers sent (marked red): " + string.Join(", ", unverified.Select(u => "\"" + (u.Text ?? "?") + "\"")), 10.5, FundPalette.Kill));
                if (unlocated.Count > 0)
                    stack.Children.Add(MonUi.Line("Could not be marked in the text: " + string.Join(", ", unlocated.Select(u => "\"" + (u.Text ?? "?") + "\"")), 10.5, FundPalette.Kill));
                if (unknown.Count > 0)
                    stack.Children.Add(MonUi.Line("Cited fact ids that do not exist: " + string.Join(", ", unknown), 10.5, FundPalette.Kill));
            }
            var caveat = FundUi.Text(AiContent.Caveat, 10, FundPalette.Faint);
            caveat.FontStyle = FontStyles.Italic;
            caveat.Margin = new Thickness(0, 6, 0, 0);
            stack.Children.Add(caveat);
            var border = new Border
            {
                Background = FundPalette.AiSurface,
                BorderBrush = FundPalette.Ai,
                BorderThickness = new Thickness(1),
                CornerRadius = new CornerRadius(8),
                Padding = new Thickness(12, 10, 12, 10),
                Margin = new Thickness(0, 4, 0, 8),
                Child = stack,
            };
            AutomationProperties.SetName(border, AiContent.AutomationName(model, at) + " Commentary on the computed numbers; not verified.");
            return border;
        }

        /// <summary>Moves the offsets of one field's unverified numbers by <paramref name="by"/> (the "• " prefix added for display).</summary>
        private static List<UnverifiedNumber> Shift(List<UnverifiedNumber> all, string field, int by)
            => all.Select(u => u.Section == field && u.Offset is int o
                    ? new UnverifiedNumber { Text = u.Text, Offset = o + by, Section = u.Section }
                    : u).ToList();

        private static TextBlock Label(string text)
        {
            var t = FundUi.Text(text, 10.5, FundPalette.Faint, wrap: false);
            t.Width = 70;
            t.VerticalAlignment = VerticalAlignment.Center;
            return t;
        }
    }

    // =====================================================================================
    // Realised vs expected band (§19.1): cumulative LOG return of the strategy vs the frozen expected mean k·μ, the
    // incubation band k·μ ± 1.96·σ·√k and the decay line of rule return_band. Drawn in log units, labelled as simple
    // returns. Keyboard: focusable; ←/→ move the cursor by one session, Home/End jump; the cursor's values are announced.
    // =====================================================================================

    internal sealed class MonitoringBandChart : FrameworkElement
    {
        private static readonly Brush BandFill = FundPalette.Make(0x267C8CF8);
        private static readonly Brush BandEdge = FundPalette.Make(0x807C8CF8);
        private static readonly Brush GridBrush = FundPalette.Make(0x14FFFFFF);
        private static readonly Typeface Mono = new(new FontFamily("Consolas"), FontStyles.Normal, FontWeights.Normal, FontStretches.Normal);
        private readonly List<MonPoint> _pts;
        private int _cursor = -1;

        /// <summary>Raised with a plain-text description of the point under the cursor.</summary>
        public event Action<string>? CursorChanged;

        public MonitoringBandChart(IEnumerable<MonPoint> points)
        {
            _pts = points.Where(p => p.CumLog.HasValue).OrderBy(p => p.N ?? 0).ToList();
            Height = 190;
            Focusable = true;
            FocusVisualStyle = null;
            Cursor = Cursors.Cross;
            ToolTip = "Realised cumulative return (bright line) vs the expected mean (dashed), the incubation band (blue area, ±1.96 σ·√k) "
                      + "and the decay line of the return rule (red dashed). ← / → move the cursor.";
            AutomationProperties.SetName(this, Summary());
            AutomationProperties.SetHelpText(this, "Use the left and right arrow keys to read the values of each session.");
            GotKeyboardFocus += (_, _) => { if (_cursor < 0 && _pts.Count > 0) SetCursor(_pts.Count - 1); InvalidateVisual(); };
            LostKeyboardFocus += (_, _) => InvalidateVisual();
            MouseLeftButtonDown += (_, _) => Focus();
        }

        private string Summary()
        {
            if (_pts.Count == 0) return "Band chart: no completed sessions yet.";
            var last = _pts[^1];
            return "Band chart over " + _pts.Count.ToString(MonUi.Inv) + " sessions. Last session " + (last.Day ?? "?") + ": realised "
                   + MonUi.LogAsPct(last.CumLog) + ", expected " + MonUi.LogAsPct(last.ExpMean) + ", band " + MonUi.LogAsPct(last.BandLo)
                   + " to " + MonUi.LogAsPct(last.BandHi) + (last.DecayLo.HasValue ? ", decay line " + MonUi.LogAsPct(last.DecayLo) : "") + ".";
        }

        public static string Describe(MonPoint p)
            => (p.Day ?? "?") + " · session " + (p.N?.ToString(MonUi.Inv) ?? "?") + " · realised " + MonUi.LogAsPct(p.CumLog)
               + " · expected " + MonUi.LogAsPct(p.ExpMean) + " · band " + MonUi.LogAsPct(p.BandLo) + " … " + MonUi.LogAsPct(p.BandHi)
               + (p.DecayLo.HasValue ? " · decay line " + MonUi.LogAsPct(p.DecayLo) : " · decay line not active yet")
               + " · day " + MonUi.LogAsPct(p.Ret) + " · drawdown " + MonUi.Frac(p.Drawdown, 2)
               + (p.RollVol.HasValue ? " · 20-session vol " + MonUi.Frac(p.RollVol, 1) : "");

        private void SetCursor(int i)
        {
            if (_pts.Count == 0) return;
            _cursor = Math.Clamp(i, 0, _pts.Count - 1);
            var text = Describe(_pts[_cursor]);
            AutomationProperties.SetItemStatus(this, text);
            CursorChanged?.Invoke(text);
            InvalidateVisual();
        }

        protected override void OnKeyDown(KeyEventArgs e)
        {
            base.OnKeyDown(e);
            if (_pts.Count == 0) return;
            switch (e.Key)
            {
                case Key.Left: SetCursor((_cursor < 0 ? _pts.Count : _cursor) - 1); e.Handled = true; break;
                case Key.Right: SetCursor(_cursor + 1); e.Handled = true; break;
                case Key.Home: SetCursor(0); e.Handled = true; break;
                case Key.End: SetCursor(_pts.Count - 1); e.Handled = true; break;
            }
        }

        protected override void OnMouseMove(MouseEventArgs e)
        {
            base.OnMouseMove(e);
            if (_pts.Count < 1) return;
            var (left, right, _, _) = Frame();
            var x = e.GetPosition(this).X;
            var t = right > left ? (x - left) / (right - left) : 0;
            SetCursor((int)Math.Round(t * (_pts.Count - 1)));
        }

        private (double Left, double Right, double Top, double Bottom) Frame()
            => (58, Math.Max(60, ActualWidth - 10), 8, Math.Max(20, ActualHeight - 22));

        protected override void OnRender(DrawingContext dc)
        {
            base.OnRender(dc);
            var ppd = VisualTreeHelper.GetDpi(this).PixelsPerDip;
            dc.DrawRectangle(FundPalette.WindowBg, null, new Rect(0, 0, ActualWidth, ActualHeight));
            if (_pts.Count == 0)
            {
                dc.DrawText(Text("No completed sessions yet — the band appears after the first session.", FundPalette.Faint, ppd), new Point(10, 10));
                return;
            }
            var (left, right, top, bottom) = Frame();
            var values = new List<double> { 0 };
            foreach (var p in _pts)
                foreach (var v in new[] { p.CumLog, p.ExpMean, p.BandLo, p.BandHi, p.DecayLo })
                    if (v is double d && !double.IsNaN(d) && !double.IsInfinity(d)) values.Add(d);
            var lo = values.Min();
            var hi = values.Max();
            if (hi - lo < 1e-6) { hi += 0.01; lo -= 0.01; }
            var pad = (hi - lo) * 0.06;
            lo -= pad;
            hi += pad;
            var n = _pts.Count;
            double X(int i) => n == 1 ? (left + right) / 2 : left + (right - left) * i / (n - 1);
            double Y(double v) => bottom - (bottom - top) * (v - lo) / (hi - lo);

            // grid + y labels (simple returns)
            for (var k = 0; k <= 4; k++)
            {
                var v = lo + (hi - lo) * k / 4.0;
                var y = Y(v);
                dc.DrawLine(new Pen(GridBrush, 1), new Point(left, y), new Point(right, y));
                var label = Text(MonUi.LogAsPct(v, 1), FundPalette.Faint, ppd);
                dc.DrawText(label, new Point(Math.Max(0, left - 6 - label.Width), y - label.Height / 2));
            }
            var zero = Y(0);
            dc.DrawLine(new Pen(FundPalette.Hairline, 1), new Point(left, zero), new Point(right, zero));

            // band area
            var band = _pts.Select((p, i) => (i, p)).Where(t => t.p.BandLo.HasValue && t.p.BandHi.HasValue).ToList();
            if (band.Count >= 2)
            {
                var geo = new StreamGeometry();
                using (var g = geo.Open())
                {
                    g.BeginFigure(new Point(X(band[0].i), Y(band[0].p.BandHi!.Value)), true, true);
                    foreach (var (i, p) in band.Skip(1)) g.LineTo(new Point(X(i), Y(p.BandHi!.Value)), true, false);
                    foreach (var (i, p) in Enumerable.Reverse(band)) g.LineTo(new Point(X(i), Y(p.BandLo!.Value)), true, false);
                }
                geo.Freeze();
                dc.DrawGeometry(BandFill, new Pen(BandEdge, 1), geo);
            }
            DrawSeries(dc, p => p.ExpMean, new Pen(FundPalette.Dim, 1) { DashStyle = DashStyles.Dash }, X, Y);
            DrawSeries(dc, p => p.DecayLo, new Pen(FundPalette.Kill, 1.2) { DashStyle = DashStyles.Dash }, X, Y);
            DrawSeries(dc, p => p.CumLog, new Pen(FundPalette.TextBright, 1.6), X, Y);

            // x labels
            var first = Text(_pts[0].Day ?? "", FundPalette.Faint, ppd);
            dc.DrawText(first, new Point(left, bottom + 4));
            var lastText = Text(_pts[^1].Day ?? "", FundPalette.Faint, ppd);
            dc.DrawText(lastText, new Point(Math.Max(left, right - lastText.Width), bottom + 4));

            // cursor
            if (_cursor >= 0 && _cursor < n && _pts[_cursor].CumLog is double cv)
            {
                var cx = X(_cursor);
                dc.DrawLine(new Pen(FundPalette.Make(0x59FFFFFF), 1), new Point(cx, top), new Point(cx, bottom));
                dc.DrawEllipse(FundPalette.TextBright, new Pen(FundPalette.WindowBg, 1), new Point(cx, Y(cv)), 3, 3);
            }
            if (IsKeyboardFocused)
                dc.DrawRectangle(null, new Pen(FundPalette.Focus, 1), new Rect(0.5, 0.5, Math.Max(0, ActualWidth - 1), Math.Max(0, ActualHeight - 1)));
        }

        private void DrawSeries(DrawingContext dc, Func<MonPoint, double?> pick, Pen pen, Func<int, double> x, Func<double, double> y)
        {
            Point? prev = null;
            for (var i = 0; i < _pts.Count; i++)
            {
                if (pick(_pts[i]) is not double v || double.IsNaN(v) || double.IsInfinity(v)) { prev = null; continue; }
                var pt = new Point(x(i), y(v));
                if (prev is Point p0) dc.DrawLine(pen, p0, pt);
                prev = pt;
            }
        }

        private static FormattedText Text(string text, Brush brush, double ppd)
            => new(text, CultureInfo.InvariantCulture, FlowDirection.LeftToRight, Mono, 9.5, brush, ppd);
    }
}
