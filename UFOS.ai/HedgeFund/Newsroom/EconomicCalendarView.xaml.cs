using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Navigation;
using System.Windows.Threading;

namespace UFOS.ai.HedgeFund.Newsroom
{
    /// <summary>
    /// Financial Newsroom → "Economic Calendar" (2026-09-25): the next 45 days of
    /// market-moving US data releases (release dates from FRED's release calendar)
    /// and FOMC rate decisions (hardcoded from federalreserve.gov in the backend),
    /// grouped by day, each with a "why it matters" note and the latest value of
    /// the underlying series. Reads GET /newsroom/calendar from the local HedgeFund
    /// backend (newsroom/calendar_collector.py polls FRED every 6 h); this view
    /// re-reads the backend every 5 minutes. Times are typical, not guaranteed;
    /// no consensus forecasts (not freely available). Source data only — no AI text.
    /// </summary>
    public partial class EconomicCalendarView : UserControl, INewsroomView
    {
        private const string ItemsUrl = NewsroomBackend.BaseUrl + "/newsroom/calendar";
        private static readonly TimeSpan AutoRefreshInterval = TimeSpan.FromMinutes(5);
        private static readonly TimeSpan WaitingRefreshInterval = TimeSpan.FromSeconds(3);
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        private readonly ObservableCollection<CalendarDayViewModel> _days = new();
        private readonly DispatcherTimer _autoRefreshTimer;
        private string _lastSignature = "";
        private bool _loading;
        private bool _waiting;
        private bool _shutDown;

        public EconomicCalendarView()
        {
            InitializeComponent();
            DayList.ItemsSource = _days;
            // Same pattern as MacroIndicatorsView: the timer lives as long as the view,
            // runs at Normal priority, skips ticks while the view isn't visible, refreshes
            // right away when it becomes visible again and is stopped by Shutdown().
            _autoRefreshTimer = new DispatcherTimer(DispatcherPriority.Normal) { Interval = AutoRefreshInterval };
            _autoRefreshTimer.Tick += async (s, e) =>
            {
                if (IsVisible) await LoadAsync(silent: true);
            };
            IsVisibleChanged += async (s, e) =>
            {
                if (e.NewValue is true && !_shutDown) await LoadAsync(silent: true);
            };
            _autoRefreshTimer.Start();
        }

        private async void View_Loaded(object sender, RoutedEventArgs e)
        {
            await LoadAsync(silent: _days.Count > 0);
        }

        /// <summary>Called by the newsroom window when it closes.</summary>
        public void Shutdown()
        {
            _shutDown = true;
            _autoRefreshTimer.Stop();
        }

        private async void RefreshButton_Click(object sender, RoutedEventArgs e)
        {
            await LoadAsync(silent: false);
        }

        private void Link_RequestNavigate(object sender, RequestNavigateEventArgs e)
        {
            NewsroomLinks.Open(e.Uri?.AbsoluteUri);
            e.Handled = true;
        }

        private async Task LoadAsync(bool silent)
        {
            if (_loading) return;
            _loading = true;
            if (!silent) ShowStatus("Loading the economic calendar…");

            try
            {
                using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
                using var doc = JsonDocument.Parse(await client.GetStringAsync(ItemsUrl));
                var root = doc.RootElement;

                var today = DateTime.Today;
                var events = new List<CalendarEventViewModel>();
                if (root.TryGetProperty("events", out var arr) && arr.ValueKind == JsonValueKind.Array)
                {
                    foreach (var el in arr.EnumerateArray())
                    {
                        var vm = CalendarEventViewModel.FromJson(el);
                        // The backend filters on its poll time (up to 6 h ago): drop days already past.
                        if (vm != null && vm.Date >= today) events.Add(vm);
                    }
                }

                JsonElement status = default;
                var hasStatus = root.TryGetProperty("status", out status) && status.ValueKind == JsonValueKind.Object;
                var collectorPending = hasStatus && status.TryGetProperty("ok", out var ok0) && ok0.ValueKind == JsonValueKind.Null;
                var collectorFailed = hasStatus && status.TryGetProperty("ok", out var ok1) && ok1.ValueKind == JsonValueKind.False;
                var statusMessage = hasStatus && status.TryGetProperty("message", out var m) && m.ValueKind == JsonValueKind.String
                    ? m.GetString() ?? "" : "";

                // Until the collector has finished its first poll, re-read every few seconds.
                SetWaiting(collectorPending);
                var updated = root.TryGetProperty("updated", out var up) && up.ValueKind == JsonValueKind.String &&
                              DateTimeOffset.TryParse(up.GetString(), Inv, DateTimeStyles.None, out var upd)
                    ? upd.ToLocalTime().ToString("yyyy-MM-dd HH:mm", Inv) : null;
                LastUpdatedText.Text = _waiting
                    ? $"Waiting for the first FRED poll — re-reading every {WaitingRefreshInterval.TotalSeconds:0}s · checked {DateTime.Now:HH:mm:ss}"
                    : $"Backend polled FRED {(updated != null ? "at " + updated : "—")} (every 6 h) · re-read every {AutoRefreshInterval.TotalMinutes:0} min · checked {DateTime.Now:HH:mm:ss}";

                // A failed collector can still deliver FOMC dates (they need no key): show both.
                SourceWarningText.Text = collectorFailed && !string.IsNullOrWhiteSpace(statusMessage)
                    ? "FRED release calendar: " + statusMessage : "";
                SourceWarningText.Visibility = SourceWarningText.Text.Length > 0 ? Visibility.Visible : Visibility.Collapsed;

                if (events.Count == 0)
                {
                    _days.Clear();
                    _lastSignature = "";
                    ShowStatus(EmptyMessage(collectorPending, collectorFailed, statusMessage));
                    return;
                }

                // Day headers ("Today", "in N days") depend on the date, so it is part of the signature.
                var signature = today.ToString("yyyy-MM-dd", Inv) + "|" + string.Join("|", events.Select(e => e.Signature));
                if (signature == _lastSignature)
                {
                    ShowList();
                    return;
                }
                _lastSignature = signature;

                var offset = ListScrollViewer.VerticalOffset;
                _days.Clear();
                foreach (var group in events.GroupBy(e => e.Date).OrderBy(g => g.Key))
                    _days.Add(CalendarDayViewModel.Create(group.Key, today, group.ToList()));
                ShowList();
                if (silent) ListScrollViewer.ScrollToVerticalOffset(offset);
            }
            catch (Exception ex)
            {
                SetWaiting(true); // backend not reachable yet (e.g. still starting) -> retry soon
                if (silent && _days.Count > 0)
                {
                    LastUpdatedText.Text = $"Auto-refresh failed at {DateTime.Now:HH:mm:ss} ({ex.Message}) — showing last data";
                }
                else
                {
                    ShowStatus("The HedgeFund backend isn't answering yet (status: " + NewsroomBackend.Status + "). " +
                               "It is started automatically with the Hedge Fund window — the first start installs " +
                               "Python packages and can take a few minutes. This view retries on its own.");
                }
            }
            finally
            {
                _loading = false;
            }
        }

        private void SetWaiting(bool waiting)
        {
            _waiting = waiting;
            var interval = waiting ? WaitingRefreshInterval : AutoRefreshInterval;
            if (_autoRefreshTimer.Interval != interval) _autoRefreshTimer.Interval = interval;
        }

        private static string EmptyMessage(bool pending, bool failed, string message)
        {
            if (failed && !string.IsNullOrWhiteSpace(message))
                return "Economic calendar collector: " + message + "\n\nIf the key is missing, set FRED_API_KEY in " +
                       "HedgeFund/backend/.env and restart the Hedge Fund window.";
            if (pending)
                return "Loading the release calendar from FRED — this takes a few seconds after the backend starts. " +
                       "This view refreshes automatically.";
            return "No curated US releases or FOMC decisions in the next 45 days.";
        }

        private void ShowStatus(string message)
        {
            StatusText.Text = message;
            StatusPanel.Visibility = Visibility.Visible;
            ListScrollViewer.Visibility = Visibility.Collapsed;
        }

        private void ShowList()
        {
            StatusPanel.Visibility = Visibility.Collapsed;
            ListScrollViewer.Visibility = Visibility.Visible;
        }

        private static Brush Frozen(byte r, byte g, byte b)
        {
            var brush = new SolidColorBrush(Color.FromRgb(r, g, b));
            brush.Freeze();
            return brush;
        }

        private static readonly Brush BrightBrush = Frozen(0xF2, 0xF2, 0xF2);
        private static readonly Brush NormalBrush = Frozen(0xBF, 0xBF, 0xBF);
        private static readonly Brush TodayBrush = Frozen(0x3E, 0xCF, 0x8E);

        /// <summary>One day card: header ("Today" / "Wed, Oct 1") + its entries.</summary>
        private sealed class CalendarDayViewModel
        {
            public string Header { get; init; } = "";
            public string SubHeader { get; init; } = "";
            public Brush HeaderBrush { get; init; } = BrightBrush;
            public List<CalendarEventViewModel> Events { get; init; } = new();

            public static CalendarDayViewModel Create(DateTime day, DateTime today, List<CalendarEventViewModel> events)
            {
                var n = (int)(day - today).TotalDays;
                var dateText = day.ToString("ddd, MMM d", Inv);
                return new CalendarDayViewModel
                {
                    Header = n == 0 ? "Today" : n == 1 ? "Tomorrow" : dateText,
                    SubHeader = n == 0 ? dateText : n == 1 ? dateText + " · in 1 day" : $"in {n} days",
                    HeaderBrush = n == 0 ? TodayBrush : BrightBrush,
                    Events = events,
                };
            }
        }

        /// <summary>One release or FOMC decision.</summary>
        private sealed class CalendarEventViewModel
        {
            public DateTime Date { get; init; }
            public string TimeDisplay { get; init; } = "";
            public string Name { get; init; } = "";
            public Brush NameBrush { get; init; } = NormalBrush;
            public FontWeight NameWeight { get; init; } = FontWeights.Normal;
            public bool ShowHighDot { get; init; }
            public bool IsFomc { get; init; }
            public bool IsSep { get; init; }
            public string Why { get; init; } = "";
            public string LastValue { get; init; } = "";
            public string LastPeriod { get; init; } = "";
            public bool HasLast => LastValue.Length > 0;
            public Uri? LinkUri { get; init; }
            public string LinkText { get; init; } = "";
            public bool HasLink => LinkUri != null;
            public string Signature { get; init; } = "";

            private static string Str(JsonElement e, string name) =>
                e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";

            public static CalendarEventViewModel? FromJson(JsonElement e)
            {
                if (e.ValueKind != JsonValueKind.Object) return null;
                if (!DateTime.TryParseExact(Str(e, "date"), "yyyy-MM-dd", Inv, DateTimeStyles.None, out var date)) return null;
                var name = Str(e, "name");
                if (name.Length == 0) return null;

                var isFomc = Str(e, "kind") == "fomc";
                var high = Str(e, "importance") == "high";
                var sep = e.TryGetProperty("sep", out var s) && s.ValueKind == JsonValueKind.True;

                // Backend "last_display" = "3.35% YoY (Aug 2026)" -> value + period on two lines.
                var last = Str(e, "last_display");
                var lastValue = last;
                var lastPeriod = "";
                var paren = last.IndexOf(" (", StringComparison.Ordinal);
                if (paren > 0 && last.EndsWith(")", StringComparison.Ordinal))
                {
                    lastValue = last[..paren];
                    lastPeriod = last[(paren + 2)..^1];
                }

                var link = Str(e, "release_link");
                var time = Str(e, "time_hint");
                return new CalendarEventViewModel
                {
                    Date = date,
                    TimeDisplay = time.Length > 0 ? time : "—",
                    Name = name,
                    NameBrush = high || isFomc ? BrightBrush : NormalBrush,
                    NameWeight = high || isFomc ? FontWeights.SemiBold : FontWeights.Normal,
                    ShowHighDot = high && !isFomc,
                    IsFomc = isFomc,
                    IsSep = isFomc && sep,
                    Why = Str(e, "why"),
                    LastValue = lastValue,
                    LastPeriod = lastPeriod,
                    LinkUri = Uri.TryCreate(link, UriKind.Absolute, out var uri) ? uri : null,
                    LinkText = isFomc ? "FOMC calendar on federalreserve.gov ↗" : "Release on FRED ↗",
                    Signature = $"{Str(e, "date")}:{Str(e, "kind")}:{name}:{time}:{last}:{sep}",
                };
            }
        }
    }
}
