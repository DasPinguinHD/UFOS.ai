using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.HedgeFund.Platform;
using UFOS.ai.Logging;

namespace UFOS.ai.HedgeFund.MarketResearch
{
    /// <summary>
    /// MR7 — alerts (plan §4.4, SPEC MR7 + "MR7 backend notes"): 🔔 editor per symbol (price crosses up/down,
    /// ±x % today, new news; alert-line alerts come from the drawing tool), the Alerts section (list with on/off, edit,
    /// delete, last fired, live state / "not live" reasons, limits; alert log, 90 days, newest first, "load more"),
    /// the alert-line wiring (a saved alert line gets its line_cross alert; moving it updates the level server-side
    /// within 5 s — shown; deleting it switches the alert off — shown) and live `alert` messages (log row + badge;
    /// the toast itself is <see cref="ResearchAlertNotifier"/>, app-wide). The backend owns the drawing ↔ alert link:
    /// after an alert write the linked drawing is re-read and upserted, the WPF never PATCHes <c>alert_id</c>.
    /// Every backend message (validation 400 field texts, 409 limits/duplicates, not-live reasons) is shown verbatim.
    /// </summary>
    public partial class ResearchView
    {
        private const string AlertsRuntimeFallback = "Alerts only run while UFOS.ai is open — there is no background service.";
        private static readonly TimeSpan AlertsRefreshInterval = TimeSpan.FromSeconds(10);

        private List<AlertDto> _alerts = new();
        private AlertLimitsDto? _alertLimits;
        private AlertRuntimeDto? _alertRuntime;
        private string? _alertsLoadError;
        private bool _alertsLoading;
        private bool _alertsLoaded;
        private int _alertsGeneration;
        private string? _alertsMessage;
        private readonly List<AlertLogItem> _alertLog = new();
        private int _alertLogLimit = 100;
        private string? _alertLogError;
        private bool _alertsPageShown;
        private int _unseenAlerts;
        private string? _confirmDeleteAlertId;
        private DispatcherTimer? _alertsTimer;
        private DispatcherTimer? _alertsSoonTimer;

        // ---------- editor ----------
        private Popup? _alertEditor;
        private StackPanel? _alertEditorBody;
        private string _editorKind = "price_cross";   // price_cross · day_move · news · line_cross (edit only)
        private string _editorDirection = "up";
        private AlertDto? _editingAlert;
        private string? _editorSymbol;
        private string? _editorMessage;
        private bool _editorBusy;
        private TextBox? _editorLevelBox, _editorPctBox, _editorCooldownBox;
        private CheckBox? _editorEnabledBox;

        private void InitAlerts()
        {
            _alertsTimer = new DispatcherTimer(DispatcherPriority.Background) { Interval = AlertsRefreshInterval };
            _alertsTimer.Tick += (_, _) => { if (_alertsPageShown && IsShown) _ = LoadAlertsAsync(withLog: false); };
            _alertsSoonTimer = new DispatcherTimer(DispatcherPriority.Background) { Interval = TimeSpan.FromSeconds(6) };
            _alertsSoonTimer.Tick += (_, _) => { _alertsSoonTimer.Stop(); _ = LoadAlertsAsync(withLog: false); };
            _socket.AlertReceived += Socket_AlertReceived;
            // First show: the rules once (bell counts, linked lines); then only while the Alerts section is shown.
            Loaded += (_, _) => { if (!_alertsLoaded && !_alertsLoading) _ = LoadAlertsAsync(withLog: false); };
            UpdateBellButton();
            RenderAlertsPage();
        }

        private void ShutdownAlerts()
        {
            _alertsTimer?.Stop();
            _alertsSoonTimer?.Stop();
            _socket.AlertReceived -= Socket_AlertReceived;
            if (_alertEditor != null) _alertEditor.IsOpen = false;
        }

        private void OnSectionShown(bool alerts)
        {
            _alertsPageShown = alerts;
            if (alerts)
            {
                _unseenAlerts = 0;
                UpdateAlertsNav();
                _ = LoadAlertsAsync(withLog: true);
                _alertsTimer?.Start();
            }
            else
            {
                _alertsTimer?.Stop();
            }
        }

        private void UpdateBellButton()
        {
            var tab = _active;
            BellButton.IsEnabled = tab != null;
            var count = tab == null ? 0 : _alerts.Count(a => a.Symbol == tab.Symbol && a.Enabled == true);
            BellButton.Content = count > 0 ? "🔔 " + count.ToString(CultureInfo.InvariantCulture) : "🔔";
            var text = tab == null ? "Alerts for the open symbol" : "Alerts for " + tab.Symbol + (count > 0 ? " — " + count + " active" : " — none active");
            BellButton.ToolTip = text + " (price crosses, move today, news)";
            AutomationProperties.SetName(BellButton, text);
        }

        private void UpdateAlertsNav()
        {
            NavAlerts.Content = _unseenAlerts > 0 ? "ALERTS · " + _unseenAlerts.ToString(CultureInfo.InvariantCulture) + " NEW" : "ALERTS";
            AutomationProperties.SetName(NavAlerts, _unseenAlerts > 0 ? "Alerts, " + _unseenAlerts + " new" : "Alerts");
        }

        // =====================================================================================
        // Loading
        // =====================================================================================

        private async Task LoadAlertsAsync(bool withLog)
        {
            var generation = ++_alertsGeneration;
            _alertsLoading = true;
            if (_alerts.Count == 0) RenderAlertsPage();
            var r = await ResearchClient.GetAlertsAsync();
            if (_shutDown || generation != _alertsGeneration) return;
            _alertsLoading = false;
            if (r.Ok && r.Value?.Alerts != null)
            {
                _alerts = r.Value.Alerts;
                _alertLimits = r.Value.Limits;
                _alertRuntime = r.Value.Runtime;
                _alertsLoadError = null;
                _alertsLoaded = true;
                ResearchAlertNotifier.Report(_alerts);
            }
            else
            {
                _alertsLoadError = "Alerts not loaded: " + (r.Ok ? "empty response from the backend." : r.Message);
            }
            if (withLog) await LoadAlertLogAsync();
            UpdateBellButton();
            RenderAlertsPage();
            if (_alertEditor?.IsOpen == true && !_editorBusy) RenderAlertEditor();
        }

        private async Task LoadAlertLogAsync()
        {
            var r = await ResearchClient.GetAlertLogAsync(_alertLogLimit, null);
            if (_shutDown) return;
            if (r.Ok && r.Value?.Items != null)
            {
                _alertLog.Clear();
                _alertLog.AddRange(r.Value.Items);
                _alertLogError = null;
            }
            else _alertLogError = "Alert log not loaded: " + (r.Ok ? "empty response from the backend." : r.Message);
            RenderAlertsPage();
        }

        private void Socket_AlertReceived(object? sender, WsAlert alert)
        {
            if (_shutDown) return;
            // The live row (the same text the toast shows); the log route has the full row with `detail`.
            if (alert.LogId is long id && _alertLog.Any(x => x.Id == id)) return;
            _alertLog.Insert(0, new AlertLogItem
            {
                Id = alert.LogId, AlertId = alert.AlertId, Symbol = alert.Symbol, Ts = alert.Ts, Kind = alert.Kind,
                Message = alert.Message + (alert.Logged == false ? " (not written to the log — the Research database was unavailable)" : ""),
                Price = alert.Price,
            });
            if (_alertLog.Count > 500) _alertLog.RemoveAt(_alertLog.Count - 1);
            if (!_alertsPageShown) { _unseenAlerts++; UpdateAlertsNav(); }
            _alertsSoonTimer?.Stop();
            _alertsSoonTimer?.Start(); // statuses (cooldown) a few seconds later
            RenderAlertsPage();
        }

        // =====================================================================================
        // Alerts section
        // =====================================================================================

        private string AlertDescription(AlertDto a)
        {
            var p = a.Params;
            string Dir(string? d) => d == "up" ? "upward" : d == "down" ? "downward" : "either way";
            switch (a.Kind)
            {
                case "price_cross":
                    return (p?.Direction == "down" ? "crosses below " : "crosses above ") + (p?.Level is double lv ? FormatPrice(lv) : "?");
                case "line_cross":
                    return "crosses the alert line " + Dir(p?.Direction) + (a.Status?.Level is double ll ? " (now at " + FormatPrice(ll) + ")" : "");
                case "day_move":
                    return (p?.Direction == "up" ? "up " : p?.Direction == "down" ? "down " : "±") +
                           (p?.Pct is double pct ? pct.ToString("0.##", CultureInfo.InvariantCulture) : "?") + " % today" +
                           (a.Status?.Levels is { } levels
                               ? " (" + (levels.Up is double u ? "≥ " + FormatPrice(u) : "") + (levels.Up != null && levels.Down != null ? " / " : "") +
                                 (levels.Down is double dn ? "≤ " + FormatPrice(dn) : "") + ")"
                               : "");
                case "news":
                    return "new news";
                default:
                    return a.Kind ?? "alert";
            }
        }

        private static string StateWord(string? state) => state switch
        {
            "armed" => "ARMED",
            "waiting" => "WAITING",
            "cooldown" => "COOLDOWN",
            "not_live" => "NOT LIVE",
            "disabled" => "OFF",
            "not_running" => "NOT RUNNING",
            _ => (state ?? "UNKNOWN").ToUpperInvariant(),
        };

        private string StateBrushKey(string? state) => state switch
        {
            "armed" => "PositiveGreenBrush",
            "waiting" or "cooldown" => "CautionBrush",
            "not_live" or "not_running" => "AccentRedBrush",
            _ => "MutedForegroundBrush",
        };

        private void RenderAlertsPage()
        {
            AlertsPanel.Children.Clear();
            var max = _alertLimits?.MaxAlerts ?? 200;
            var active = _alerts.Count(a => a.Enabled == true);
            AlertsPanel.Children.Add(new TextBlock
            {
                Text = "ALERTS · " + _alerts.Count + "/" + max + " · " + active + " ACTIVE",
                Style = ViewStyle("CaptionStyle"),
            });
            // Always visible (plan §4.4 / Q3): alerts are evaluated only while the app runs.
            AlertsPanel.Children.Add(new TextBlock
            {
                Text = _alertRuntime?.Note ?? AlertsRuntimeFallback,
                FontSize = 11, Foreground = Token("SecondaryForegroundBrush"), TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 6, 0, 0),
            });
            if (_alertRuntime?.Running == false)
                AddAlertsLine("The alert task is not running — no alert can fire now (see the Research status at the bottom left).", "AccentRedBrush");
            if (_alertRuntime?.StoreError != null) AddAlertsLine(_alertRuntime.StoreError, "AccentRedBrush");
            if (_alerts.Count >= max) AddAlertsLine("At most " + max + " alerts — delete one first.", "CautionBrush");
            if (_alertsLoadError != null)
            {
                AddAlertsLine(_alertsLoadError, "AccentRedBrush");
                var retry = MakeTextButton("Retry", "Load the alerts again", (_, _) => _ = LoadAlertsAsync(withLog: true));
                retry.HorizontalAlignment = HorizontalAlignment.Left;
                retry.Margin = new Thickness(0, 6, 0, 0);
                AlertsPanel.Children.Add(retry);
            }
            if (_alertsMessage != null)
            {
                var msg = AddAlertsLine(_alertsMessage, "SecondaryForegroundBrush");
                AutomationProperties.SetLiveSetting(msg, AutomationLiveSetting.Polite);
            }

            var actions = new WrapPanel { Margin = new Thickness(0, 8, 0, 4) };
            if (_active != null)
            {
                var add = MakeTextButton("🔔 New alert for " + _active.Symbol + "…", "New alert for " + _active.Symbol, (_, _) => OpenAlertEditor(null, BellButton));
                actions.Children.Add(add);
            }
            else actions.Children.Add(new TextBlock { Text = "Open a symbol (Ctrl+K) to add an alert — or draw an alert line (⍾) on its chart.", FontSize = 11, Foreground = Token("MutedForegroundBrush"), TextWrapping = TextWrapping.Wrap });
            AlertsPanel.Children.Add(actions);

            if (_alertsLoading && _alerts.Count == 0) AddAlertsLine("Loading the alerts…", "MutedForegroundBrush");
            else if (_alerts.Count == 0 && _alertsLoadError == null) AddAlertsLine("No alerts yet.", "MutedForegroundBrush");
            foreach (var a in _alerts) AlertsPanel.Children.Add(BuildAlertRow(a));

            // ---- log ----
            AlertsPanel.Children.Add(new Border { Height = 1, Margin = new Thickness(0, 12, 0, 8), Background = Token("HairlineBrush") });
            AlertsPanel.Children.Add(new TextBlock { Text = "ALERT LOG · NEWEST FIRST · KEPT 90 DAYS", Style = ViewStyle("CaptionStyle") });
            if (_alertLogError != null) AddAlertsLine(_alertLogError, "AccentRedBrush");
            if (_alertLog.Count == 0 && _alertLogError == null) AddAlertsLine("No alert has fired yet.", "MutedForegroundBrush");
            var logPanel = new StackPanel { Margin = new Thickness(0, 6, 0, 0) };
            AutomationProperties.SetName(logPanel, "Alert log");
            foreach (var item in _alertLog) logPanel.Children.Add(BuildLogRow(item));
            AlertsPanel.Children.Add(logPanel);
            if (_alertLog.Count >= _alertLogLimit && _alertLogLimit < 500)
            {
                var more = MakeTextButton("Load more", "Load older alert log entries", (_, _) =>
                {
                    _alertLogLimit = Math.Min(500, _alertLogLimit + 100);
                    _ = LoadAlertLogAsync();
                });
                more.HorizontalAlignment = HorizontalAlignment.Left;
                more.Margin = new Thickness(0, 6, 0, 0);
                AlertsPanel.Children.Add(more);
            }
            else if (_alertLog.Count >= 500)
                AddAlertsLine("Showing the newest 500 entries (the backend keeps 90 days).", "MutedForegroundBrush");
        }

        private TextBlock AddAlertsLine(string text, string brush)
        {
            var t = new TextBlock { Text = text, FontSize = 11, Foreground = Token(brush), TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 6, 0, 0) };
            AutomationProperties.SetName(t, text);
            AlertsPanel.Children.Add(t);
            return t;
        }

        private UIElement BuildAlertRow(AlertDto a)
        {
            var id = a.Id ?? "";
            var state = a.Status?.State ?? (a.Enabled == true ? null : "disabled");
            var word = StateWord(state);
            var reason = a.Status?.Reason ?? a.DisabledReason;
            var description = (a.Symbol ?? "?") + " · " + AlertDescription(a);
            var fired = a.LastTriggeredAt != null ? "Last fired " + AlertTime(a.LastTriggeredAt) : "Never fired";
            var cooldown = a.CooldownSeconds is int cs ? " · quiet " + (cs % 60 == 0 ? cs / 60 + " min" : cs + " s") + " after firing" : "";
            var last = a.Status?.LastPrice?.Price is double lp ? " · last " + FormatPrice(lp) : "";

            var row = new Border
            {
                Padding = new Thickness(6, 5, 4, 5), Margin = new Thickness(0, 4, 0, 0), CornerRadius = new CornerRadius(6), BorderThickness = new Thickness(1),
                BorderBrush = Token("SubtleBorderBrush"), Background = _active?.Symbol == a.Symbol ? Tint("AccentBlueBrush", 0x12) : Brushes.Transparent,
            };
            var grid = new Grid();
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
            grid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });
            var text = new StackPanel();
            text.Children.Add(new TextBlock { Text = description, FontFamily = new FontFamily("Consolas"), FontSize = 11.5, Foreground = Token("PrimaryForegroundBrush"), TextTrimming = TextTrimming.CharacterEllipsis });
            var stateLine = new TextBlock { TextWrapping = TextWrapping.Wrap, FontSize = 10.5, Margin = new Thickness(0, 2, 0, 0) };
            stateLine.Inlines.Add(new System.Windows.Documents.Run(word) { FontFamily = new FontFamily("Consolas"), FontWeight = FontWeights.SemiBold, Foreground = Token(StateBrushKey(state)) });
            if (!string.IsNullOrWhiteSpace(reason)) stateLine.Inlines.Add(new System.Windows.Documents.Run(" — " + reason) { Foreground = Token("SecondaryForegroundBrush") });
            text.Children.Add(stateLine);
            text.Children.Add(new TextBlock { Text = fired + cooldown + last, FontSize = 9.5, Foreground = Token("QuietForegroundBrush"), Margin = new Thickness(0, 2, 0, 0) });
            grid.Children.Add(text);

            var buttons = new StackPanel { Orientation = Orientation.Horizontal, VerticalAlignment = VerticalAlignment.Top };
            if (_confirmDeleteAlertId == id)
            {
                buttons.Children.Add(new TextBlock { Text = "Delete?", FontSize = 10.5, Foreground = Token("CautionBrush"), VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 4, 0) });
                buttons.Children.Add(MakeTextButton("Delete", "Delete the alert " + description, (_, _) => { _confirmDeleteAlertId = null; _ = DeleteAlertAsync(a); }));
                var no = MakeTextButton("Keep", "Keep the alert", (_, _) => { _confirmDeleteAlertId = null; RenderAlertsPage(); });
                no.Margin = new Thickness(4, 0, 0, 0);
                buttons.Children.Add(no);
            }
            else
            {
                var on = a.Enabled == true;
                var toggle = RowButton(on ? "ON" : "OFF", (on ? "Switch off " : "Switch on ") + description, on ? "Switch this alert off" : "Switch this alert on", () => _ = PatchAlertAsync(a, new Dictionary<string, object?> { ["enabled"] = !on }));
                toggle.FontFamily = new FontFamily("Consolas");
                toggle.Foreground = Token(on ? "PositiveGreenBrush" : "MutedForegroundBrush");
                buttons.Children.Add(toggle);
                if (a.Symbol != null)
                    buttons.Children.Add(RowButton("⌖", "Open the chart of " + a.Symbol, "Open " + a.Symbol, () => OpenSymbol(a.Symbol!, null, null, null, null)));
                var edit = RowButton("✎", "Edit " + description, "Edit", () => { });
                edit.Click += (_, _) => OpenAlertEditor(a, edit);
                buttons.Children.Add(edit);
                buttons.Children.Add(RowButton("×", "Delete " + description, "Delete (the log entries stay)", () => { _confirmDeleteAlertId = id; RenderAlertsPage(); }));
            }
            Grid.SetColumn(buttons, 1);
            grid.Children.Add(buttons);
            row.Child = grid;
            AutomationProperties.SetName(row, description + ", " + word + (reason != null ? ": " + reason : "") + ". " + fired + ".");
            return row;
        }

        private UIElement BuildLogRow(AlertLogItem item)
        {
            var when = AlertTime(item.Ts);
            var text = new TextBlock { TextWrapping = TextWrapping.Wrap, FontSize = 11, Margin = new Thickness(0, 0, 0, 3) };
            text.Inlines.Add(new System.Windows.Documents.Run(when + "  ") { FontFamily = new FontFamily("Consolas"), Foreground = Token("MutedForegroundBrush") });
            // The message is backend text (news headlines are external): plain Run, never markup.
            text.Inlines.Add(new System.Windows.Documents.Run(item.Message ?? "") { Foreground = Token("PrimaryForegroundBrush") });
            AutomationProperties.SetName(text, when + ": " + (item.Message ?? ""));
            return text;
        }

        private static string AlertTime(string? iso)
        {
            if (string.IsNullOrWhiteSpace(iso) || !DateTimeOffset.TryParse(iso, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var dto)) return "—";
            var local = dto.ToLocalTime();
            return local.Date == DateTime.Today ? local.ToString("HH:mm:ss", CultureInfo.InvariantCulture) : local.ToString("yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture);
        }

        // =====================================================================================
        // Writes (POST / PATCH / DELETE) — backend messages verbatim
        // =====================================================================================

        private async Task<bool> PatchAlertAsync(AlertDto a, Dictionary<string, object?> changes)
        {
            if (a.Id == null) return false;
            var r = await ResearchClient.PatchAlertAsync(a.Id, changes);
            if (_shutDown) return false;
            if (r.Ok && r.Value != null)
            {
                ReplaceAlert(r.Value);
                _alertsMessage = null;
                if (r.Value.Kind == "line_cross") RefreshLinkedDrawing(r.Value);
            }
            else _alertsMessage = r.Ok ? "Not saved: empty response from the backend." : r.Message;
            UpdateBellButton();
            RenderAlertsPage();
            return r.Ok;
        }

        private async Task DeleteAlertAsync(AlertDto a)
        {
            if (a.Id == null) return;
            var r = await ResearchClient.DeleteAlertAsync(a.Id);
            if (_shutDown) return;
            if (r.Ok || r.HttpStatus == 404)
            {
                _alerts.RemoveAll(x => x.Id == a.Id);
                ResearchAlertNotifier.Report(_alerts);
                _alertsMessage = "Alert deleted" + (a.Kind == "line_cross" ? " — the alert line stays on the chart without an alert." : ".");
                if (a.Kind == "line_cross") RefreshLinkedDrawing(a);
            }
            else _alertsMessage = r.Message;
            UpdateBellButton();
            RenderAlertsPage();
        }

        private void ReplaceAlert(AlertDto updated)
        {
            var i = _alerts.FindIndex(x => x.Id == updated.Id);
            if (i >= 0) _alerts[i] = updated;
            else _alerts.Add(updated);
            ResearchAlertNotifier.Report(_alerts);
        }

        // =====================================================================================
        // Alert line (drawing tool ⍾) ↔ line_cross alert — the backend owns the link
        // =====================================================================================

        /// <summary>A new alert line was stored: create its line_cross alert (any direction, default cooldown).</summary>
        private async void OnAlertLineSaved(string symbol, DrawingDto line)
        {
            try
            {
                if (line.Id == null) return;
                var body = new Dictionary<string, object?>
                {
                    ["symbol"] = symbol,
                    ["kind"] = "line_cross",
                    ["params"] = new Dictionary<string, object?> { ["drawing_id"] = line.Id, ["direction"] = "any" },
                };
                var r = await ResearchClient.CreateAlertAsync(body);
                if (_shutDown) return;
                var level = line.Points?.FirstOrDefault()?.P;
                if (r.Ok && r.Value != null)
                {
                    ReplaceAlert(r.Value);
                    _drawingMessage = "Alert created for the line at " + (level is double l ? FormatPrice(l) : "?") +
                                      " (either direction). Edit or switch it off in ALERTS. Alerts only run while UFOS.ai is open.";
                    RefreshLinkedDrawing(r.Value);
                }
                else _drawingMessage = "The alert line was saved, but its alert was not created: " + (r.Ok ? "empty response from the backend." : r.Message);
                UpdateBellButton();
                RenderDrawingStatus();
                RenderAlertsPage();
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: creating the alert of an alert line failed.", ex);
            }
        }

        /// <summary>An alert line moved: the backend re-reads its price within 5 s — say so and show the new level.</summary>
        private void OnAlertLineMoved(string symbol, DrawingDto line)
        {
            var alert = _alerts.FirstOrDefault(a => a.Kind == "line_cross" && a.Params?.DrawingId == line.Id);
            if (alert == null) return;
            var level = line.Points?.FirstOrDefault()?.P;
            _drawingMessage = "Alert line moved to " + (level is double l ? FormatPrice(l) : "?") +
                              (alert.Enabled == true ? " — the alert uses the new level within 5 s." : " — its alert is switched off.");
            RenderDrawingStatus();
            _alertsSoonTimer?.Stop();
            _alertsSoonTimer?.Start(); // ~6 s later: status.level shows the applied level
        }

        /// <summary>An alert line was deleted: the backend switches its alert off.</summary>
        private void OnAlertLineDeleted(string symbol, string drawingId)
        {
            var alert = _alerts.FirstOrDefault(a => a.Kind == "line_cross" && a.Params?.DrawingId == drawingId);
            if (alert == null) return;
            _drawingMessage = "The alert line was deleted — its alert is switched off (it stays in ALERTS until you delete it).";
            RenderDrawingStatus();
            _alertsSoonTimer?.Stop();
            _alertsSoonTimer?.Start();
        }

        /// <summary>
        /// After an alert write the backend has set/cleared the line's <c>alert_id</c>: re-read the symbol's drawings and
        /// upsert THAT drawing on the page (only when its drawings are loaded and nothing is queued for it).
        /// </summary>
        private async void RefreshLinkedDrawing(AlertDto alert)
        {
            try
            {
                var symbol = alert.Symbol;
                var drawingId = alert.Params?.DrawingId ?? alert.DrawingId;
                if (symbol == null || drawingId == null || _chart.DrawingsSymbol != symbol) return;
                var r = await ResearchClient.GetDrawingsAsync(symbol);
                if (_shutDown || !r.Ok || r.Value?.Drawings == null || _chart.DrawingsSymbol != symbol) return;
                if (_drawingOps.Any(o => o.Id == drawingId)) return; // a queued change answers with the newest state anyway
                var d = r.Value.Drawings.FirstOrDefault(x => x.Id == drawingId);
                if (d == null) return;
                Confirm(d);
                _chart.UpsertDrawing(symbol, d);
                RenderObjects();
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Market Research: refreshing an alert line failed.", ex);
            }
        }

        // =====================================================================================
        // 🔔 editor
        // =====================================================================================

        private void BellButton_Click(object sender, RoutedEventArgs e)
        {
            if (_active == null) return;
            OpenAlertEditor(null, BellButton);
            if (_alerts.Count == 0 && !_alertsLoading) _ = LoadAlertsAsync(withLog: false);
        }

        private void OpenAlertEditor(AlertDto? alert, UIElement target)
        {
            var symbol = alert?.Symbol ?? _active?.Symbol;
            if (symbol == null) return;
            if (_alertEditor == null)
            {
                _alertEditorBody = new StackPanel { Width = 300 };
                _alertEditor = MakePopup(_alertEditorBody, "Alert editor");
            }
            _editingAlert = alert;
            _editorSymbol = symbol;
            _editorMessage = null;
            _editorKind = alert?.Kind ?? "price_cross";
            _editorDirection = alert?.Params?.Direction ?? (_editorKind == "price_cross" ? "up" : "any");
            _alertEditor.PlacementTarget = target;
            _alertEditor.Placement = PlacementMode.Bottom;
            RenderAlertEditor();
            _alertEditor.IsOpen = true;
            Dispatcher.BeginInvoke(DispatcherPriority.Input, new Action(() => (_editorLevelBox ?? _editorPctBox ?? _editorCooldownBox)?.Focus()));
        }

        private void RenderAlertEditor()
        {
            if (_alertEditorBody == null || _editorSymbol == null) return;
            var body = _alertEditorBody;
            body.Children.Clear();
            var editing = _editingAlert;
            var symbol = _editorSymbol;
            body.Children.Add(new TextBlock { Text = (editing == null ? "NEW ALERT · " : "EDIT ALERT · ") + symbol, Style = ViewStyle("CaptionStyle"), Margin = new Thickness(0, 0, 0, 6) });

            // Kind (fixed when editing: the backend refuses changing `kind`).
            if (editing == null)
            {
                var kinds = new WrapPanel { Margin = new Thickness(0, 0, 0, 6) };
                AutomationProperties.SetName(kinds, "Alert type");
                kinds.Children.Add(MakeSegmentButton("Price crosses", "Price crosses a level", _editorKind == "price_cross", () => { _editorKind = "price_cross"; _editorDirection = "up"; RenderAlertEditor(); }));
                kinds.Children.Add(MakeSegmentButton("Move today", "Moves by a percentage today", _editorKind == "day_move", () => { _editorKind = "day_move"; _editorDirection = "any"; RenderAlertEditor(); }));
                kinds.Children.Add(MakeSegmentButton("News", "New news for the symbol", _editorKind == "news", () => { _editorKind = "news"; RenderAlertEditor(); }));
                body.Children.Add(kinds);
                body.Children.Add(new TextBlock { Text = "Alert line: draw ⍾ on the chart — its alert is created with the line.", FontSize = 9.5, Foreground = Token("QuietForegroundBrush"), TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 6) });
            }
            else
            {
                body.Children.Add(new TextBlock { Text = AlertDescription(editing), FontFamily = new FontFamily("Consolas"), FontSize = 11, Foreground = Token("PrimaryForegroundBrush"), TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 6) });
            }

            _editorLevelBox = null;
            _editorPctBox = null;
            if (_editorKind == "price_cross")
            {
                body.Children.Add(DirectionRow(new[] { ("up", "Crosses above"), ("down", "Crosses below") }));
                var prefill = editing?.Params?.Level ?? (symbol == _active?.Symbol ? _lastTrade : null);
                _editorLevelBox = NumberBox(prefill is double pf ? pf.ToString("0.########", CultureInfo.InvariantCulture) : "", "Price level");
                _editorLevelBox.Width = 110;
                _editorLevelBox.MaxLength = 16;
                body.Children.Add(LabeledRow("LEVEL", Wrap(_editorLevelBox)));
            }
            else if (_editorKind == "day_move")
            {
                body.Children.Add(DirectionRow(new[] { ("up", "Up"), ("down", "Down"), ("any", "Either") }));
                _editorPctBox = NumberBox(editing?.Params?.Pct is double pc ? pc.ToString("0.##", CultureInfo.InvariantCulture) : "2", "Move in percent today");
                _editorPctBox.Width = 60;
                body.Children.Add(LabeledRow("%", Wrap(_editorPctBox)));
                body.Children.Add(new TextBlock { Text = "Last trade vs the previous close; fires at most once per day and side" + (AssetClassOf(symbol) == "crypto" ? " (UTC day)." : "."), FontSize = 9.5, Foreground = Token("QuietForegroundBrush"), TextWrapping = TextWrapping.Wrap });
            }
            else if (_editorKind == "line_cross")
            {
                body.Children.Add(DirectionRow(new[] { ("up", "Upward"), ("down", "Downward"), ("any", "Either") }));
                body.Children.Add(new TextBlock { Text = "The level is the line's price — move the line on the chart to change it (applies within 5 s).", FontSize = 9.5, Foreground = Token("QuietForegroundBrush"), TextWrapping = TextWrapping.Wrap });
            }
            else
            {
                body.Children.Add(new TextBlock { Text = "Fires once for every new news item of " + symbol + " (Alpaca · Benzinga).", FontSize = 11, Foreground = Token("SecondaryForegroundBrush"), TextWrapping = TextWrapping.Wrap });
            }

            var cooldownMin = (editing?.CooldownSeconds ?? _alertLimits?.CooldownDefault ?? 900) / 60.0;
            _editorCooldownBox = NumberBox(cooldownMin.ToString("0.##", CultureInfo.InvariantCulture), "Quiet time after firing, in minutes");
            _editorCooldownBox.Width = 60;
            body.Children.Add(LabeledRow("QUIET", Wrap(_editorCooldownBox), "min after firing (1 – 1440)"));
            _editorEnabledBox = new CheckBox { Content = "On", IsChecked = editing?.Enabled ?? true, Foreground = Token("PrimaryForegroundBrush"), Margin = new Thickness(0, 6, 0, 0) };
            AutomationProperties.SetName(_editorEnabledBox, "Alert switched on");
            if (editing?.DisabledReason != null) _editorEnabledBox.ToolTip = editing.DisabledReason;
            body.Children.Add(_editorEnabledBox);

            if (_editorMessage != null)
            {
                var msg = new TextBlock { Text = _editorMessage, FontSize = 10.5, Foreground = Token("CautionBrush"), TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 6, 0, 0) };
                AutomationProperties.SetLiveSetting(msg, AutomationLiveSetting.Assertive);
                AutomationProperties.SetName(msg, _editorMessage);
                body.Children.Add(msg);
            }
            body.Children.Add(new TextBlock { Text = _alertRuntime?.Note ?? AlertsRuntimeFallback, FontSize = 9.5, Foreground = Token("QuietForegroundBrush"), TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 8, 0, 0) });

            var buttons = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 8, 0, 0) };
            var save = MakeTextButton(editing == null ? "Create" : "Save", editing == null ? "Create the alert" : "Save the alert", (_, _) => _ = SaveAlertEditorAsync());
            save.IsEnabled = !_editorBusy;
            var cancel = MakeTextButton("Cancel", "Close without saving", (_, _) => { if (_alertEditor != null) _alertEditor.IsOpen = false; });
            cancel.Margin = new Thickness(6, 0, 0, 0);
            buttons.Children.Add(save);
            buttons.Children.Add(cancel);
            body.Children.Add(buttons);
            foreach (var box in new[] { _editorLevelBox, _editorPctBox, _editorCooldownBox })
                if (box != null) box.KeyDown += (_, e) => { if (e.Key == Key.Enter) { e.Handled = true; _ = SaveAlertEditorAsync(); } };
        }

        private UIElement DirectionRow((string Key, string Label)[] options)
        {
            var row = new WrapPanel { Margin = new Thickness(0, 0, 0, 6) };
            AutomationProperties.SetName(row, "Direction");
            foreach (var (key, label) in options)
            {
                var k = key;
                row.Children.Add(MakeSegmentButton(label, "Direction " + label, _editorDirection == key, () => { _editorDirection = k; RenderAlertEditor(); }));
            }
            return row;
        }

        private UIElement LabeledRow(string caption, UIElement input, string? suffix = null)
        {
            var row = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 2, 0, 2) };
            var label = SmallLabel(caption);
            label.Width = 48;
            row.Children.Add(label);
            row.Children.Add(input);
            if (suffix != null) row.Children.Add(new TextBlock { Text = suffix, FontSize = 10, Foreground = Token("MutedForegroundBrush"), VerticalAlignment = VerticalAlignment.Center });
            return row;
        }

        private static bool TryNumber(string? text, out double value)
            => double.TryParse((text ?? "").Trim().Replace(',', '.'), NumberStyles.Float, CultureInfo.InvariantCulture, out value) && double.IsFinite(value);

        private async Task SaveAlertEditorAsync()
        {
            if (_editorBusy || _editorSymbol == null) return;
            var editing = _editingAlert;
            var symbol = _editorSymbol;
            // Only parsing here — ranges and rules are the backend's (its messages are shown verbatim).
            Dictionary<string, object?> parameters;
            switch (_editorKind)
            {
                case "price_cross":
                    if (!TryNumber(_editorLevelBox?.Text, out var level)) { _editorMessage = "Enter the price level as a number (e.g. 190.5)."; RenderAlertEditor(); return; }
                    parameters = new Dictionary<string, object?> { ["level"] = level, ["direction"] = _editorDirection == "down" ? "down" : "up" };
                    break;
                case "day_move":
                    if (!TryNumber(_editorPctBox?.Text, out var pct)) { _editorMessage = "Enter the move in percent as a number (e.g. 2.5)."; RenderAlertEditor(); return; }
                    parameters = new Dictionary<string, object?> { ["pct"] = pct, ["direction"] = _editorDirection is "up" or "down" ? _editorDirection : "any" };
                    break;
                case "line_cross":
                    parameters = new Dictionary<string, object?> { ["drawing_id"] = editing?.Params?.DrawingId, ["direction"] = _editorDirection is "up" or "down" ? _editorDirection : "any" };
                    break;
                default:
                    parameters = new Dictionary<string, object?>();
                    break;
            }
            if (!TryNumber(_editorCooldownBox?.Text, out var minutes)) { _editorMessage = "Enter the quiet time in minutes as a number."; RenderAlertEditor(); return; }
            var cooldown = (int)Math.Round(minutes * 60);
            var enabled = _editorEnabledBox?.IsChecked == true;

            _editorBusy = true;
            _editorMessage = "Saving…";
            RenderAlertEditor();
            PlatformResult<AlertDto> r;
            try
            {
                if (editing?.Id == null)
                {
                    r = await ResearchClient.CreateAlertAsync(new Dictionary<string, object?>
                    {
                        ["symbol"] = symbol, ["kind"] = _editorKind, ["params"] = parameters, ["cooldown_seconds"] = cooldown, ["enabled"] = enabled,
                    });
                }
                else
                {
                    var changes = new Dictionary<string, object?>();
                    if (_editorKind != "news") changes["params"] = parameters;
                    if (editing.CooldownSeconds != cooldown) changes["cooldown_seconds"] = cooldown;
                    if ((editing.Enabled == true) != enabled) changes["enabled"] = enabled;
                    if (changes.Count == 0) { _editorBusy = false; if (_alertEditor != null) _alertEditor.IsOpen = false; return; }
                    r = await ResearchClient.PatchAlertAsync(editing.Id, changes);
                }
            }
            finally
            {
                _editorBusy = false;
            }
            if (_shutDown) return;
            if (r.Ok && r.Value != null)
            {
                ReplaceAlert(r.Value);
                _editorMessage = null;
                if (_alertEditor != null) _alertEditor.IsOpen = false;
                _alertsMessage = (editing == null ? "Alert created: " : "Alert saved: ") + (r.Value.Symbol ?? symbol) + " · " + AlertDescription(r.Value) + ".";
                if (r.Value.Kind == "line_cross") RefreshLinkedDrawing(r.Value);
                UpdateBellButton();
                RenderAlertsPage();
                return;
            }
            _editorMessage = r.Ok ? "Not saved: empty response from the backend." : r.Message; // 400 field texts / 409 verbatim
            RenderAlertEditor();
        }
    }
}
