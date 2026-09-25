using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.Logging;
using UFOS.ai.Services;

namespace UFOS.ai
{
    // Transaction Log: what every AI request in UFOS.ai cost, across all modules, read
    // from the shared ledger %LOCALAPPDATA%\UFOS.ai\ai-cost-log.jsonl (Services/AiCostLedger.cs;
    // the Python backends append their own lines). Amounts are OpenRouter's own
    // usage.cost (1 credit = 1 USD). Updates live via a FileSystemWatcher (debounced)
    // and on window activation.
    public partial class TransactionLogWindow : Window
    {
        private readonly DispatcherTimer _reloadDebounce;
        private FileSystemWatcher? _watcher;
        private int _reloadGeneration;
        private string? _loadedSignature;

        public TransactionLogWindow()
        {
            InitializeComponent();

            _reloadDebounce = new DispatcherTimer(DispatcherPriority.Background, Dispatcher)
            {
                Interval = TimeSpan.FromMilliseconds(500),
            };
            _reloadDebounce.Tick += (_, __) =>
            {
                _reloadDebounce.Stop();
                _ = ReloadAsync();
            };
        }

        private void Window_Loaded(object sender, RoutedEventArgs e)
        {
            StartWatching();
            _ = ReloadAsync(force: true);
        }

        private void Window_Activated(object? sender, EventArgs e)
        {
            if (IsLoaded)
            {
                _ = ReloadAsync();
            }
        }

        private void Window_Closed(object? sender, EventArgs e)
        {
            _reloadDebounce.Stop();
            if (_watcher is not null)
            {
                _watcher.EnableRaisingEvents = false;
                _watcher.Dispose();
                _watcher = null;
            }
        }

        // ===================== Live updates =====================

        private void StartWatching()
        {
            try
            {
                Directory.CreateDirectory(AiCostLedger.DirectoryPath);
                _watcher = new FileSystemWatcher(AiCostLedger.DirectoryPath, AiCostLedger.FileName)
                {
                    NotifyFilter = NotifyFilters.LastWrite | NotifyFilters.Size | NotifyFilters.FileName | NotifyFilters.CreationTime,
                    IncludeSubdirectories = false,
                };
                _watcher.Changed += Ledger_Changed;
                _watcher.Created += Ledger_Changed;
                _watcher.Deleted += Ledger_Changed;
                _watcher.Renamed += Ledger_Changed;
                _watcher.EnableRaisingEvents = true;
            }
            catch (Exception ex)
            {
                // Still usable without live updates (Refresh / activation reload).
                try { AppLog.Warn("TransactionLog", "Could not watch the AI cost ledger for changes", ex); } catch { }
            }
        }

        // Raised on a thread-pool thread, often several times per append -> restart the
        // 500 ms debounce on the dispatcher and reload once it elapses.
        private void Ledger_Changed(object sender, FileSystemEventArgs e)
        {
            try
            {
                Dispatcher.InvokeAsync(() =>
                {
                    _reloadDebounce.Stop();
                    _reloadDebounce.Start();
                });
            }
            catch
            {
                // Dispatcher already shut down.
            }
        }

        // ===================== Loading =====================

        // force = false (activation, file watcher) skips the rebuild when the file hasn't
        // changed since the last load, so re-focusing the window keeps the scroll position.
        private async Task ReloadAsync(bool force = false)
        {
            var signature = LedgerSignature();
            if (!force && signature == _loadedSignature)
            {
                return;
            }

            var generation = ++_reloadGeneration;
            var fileExists = File.Exists(AiCostLedger.LedgerPath);
            List<AiCostEntry> entries;
            try
            {
                entries = await Task.Run(() => AiCostLedger.ReadAll());
            }
            catch
            {
                entries = new List<AiCostEntry>();
            }

            // A newer reload started meanwhile -> let that one win.
            if (generation != _reloadGeneration)
            {
                return;
            }
            _loadedSignature = signature;

            var rows = entries
                .OrderByDescending(entry => entry.Timestamp ?? DateTimeOffset.MinValue)
                .Select(entry => new TransactionLogRow(entry))
                .ToList();

            var previousSelection = (EntriesList.SelectedItem as TransactionLogRow)?.Identity;
            EntriesList.ItemsSource = rows;
            if (previousSelection is not null)
            {
                EntriesList.SelectedItem = rows.FirstOrDefault(row => row.Identity == previousSelection);
            }

            UpdateSummary(entries);
            UpdateEmptyState(fileExists, rows.Count);

            foreach (var row in rows)
            {
                _ = row.LoadLogoAsync();
            }
        }

        private static string LedgerSignature()
        {
            try
            {
                var info = new FileInfo(AiCostLedger.LedgerPath);
                return info.Exists
                    ? info.Length.ToString(CultureInfo.InvariantCulture) + "|" + info.LastWriteTimeUtc.Ticks.ToString(CultureInfo.InvariantCulture)
                    : "missing";
            }
            catch
            {
                return Guid.NewGuid().ToString();
            }
        }

        private void UpdateSummary(List<AiCostEntry> entries)
        {
            var now = DateTimeOffset.Now;
            decimal allTime = 0, month = 0, today = 0;
            int withoutCost = 0;
            foreach (var entry in entries)
            {
                if (entry.CostUsd is not decimal cost)
                {
                    withoutCost++;
                    continue;
                }
                allTime += cost;
                if (entry.Timestamp is DateTimeOffset ts)
                {
                    var local = ts.ToLocalTime();
                    if (local.Year == now.Year && local.Month == now.Month)
                    {
                        month += cost;
                        if (local.Day == now.Day)
                        {
                            today += cost;
                        }
                    }
                }
            }

            TotalAllTimeText.Text = AiCostLedger.FormatUsd(allTime);
            TotalMonthText.Text = AiCostLedger.FormatUsd(month);
            TotalTodayText.Text = AiCostLedger.FormatUsd(today);
            RequestCountText.Text = entries.Count.ToString("N0", CultureInfo.InvariantCulture)
                + (withoutCost > 0 ? $" ({withoutCost.ToString("N0", CultureInfo.InvariantCulture)} without reported cost)" : string.Empty);
        }

        private void UpdateEmptyState(bool fileExists, int rowCount)
        {
            if (rowCount > 0)
            {
                EmptyStateText.Visibility = Visibility.Collapsed;
                EntriesList.Visibility = Visibility.Visible;
                return;
            }

            EmptyStateText.Text = fileExists
                ? "The ledger file exists but contains no readable entries yet."
                : "No AI requests recorded yet. Every AI request (e.g. ✦ Summarize with AI on an article) " +
                  "appears here with what OpenRouter charged for it.";
            EmptyStateText.Visibility = Visibility.Visible;
            EntriesList.Visibility = Visibility.Collapsed;
        }

        // ===================== Buttons =====================

        private void RefreshButton_Click(object sender, RoutedEventArgs e)
        {
            _ = ReloadAsync(force: true);
        }

        private void OpenFolderButton_Click(object sender, RoutedEventArgs e)
        {
            try
            {
                Directory.CreateDirectory(AiCostLedger.DirectoryPath);
                Process.Start(new ProcessStartInfo("explorer.exe", "\"" + AiCostLedger.DirectoryPath + "\"") { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                (Application.Current as IUiErrorReporter)?.ShowUiException(ex);
            }
        }

        // ===================== Window chrome (full screen + drag-restore, see CLAUDE.md) =====================

        // Dragging the title bar of a MAXIMIZED window: DragMove() does nothing there, so
        // the window is restored as soon as the mouse actually moves with the button held
        // (a plain click does nothing), placed so the cursor keeps its relative spot on the
        // title bar, and then dragged (same as HedgeFund/HedgeFundWindow.xaml.cs).
        private bool _restoreOnDrag;
        private Point _restoreDragStart;

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton != MouseButtonState.Pressed) return;
            if (e.ClickCount == 2)
            {
                WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
                return;
            }
            if (WindowState == WindowState.Maximized)
            {
                _restoreOnDrag = true;
                _restoreDragStart = e.GetPosition(this);
                ((UIElement)sender).CaptureMouse();
                return;
            }
            try { DragMove(); } catch { }
        }

        private void TitleBar_MouseMove(object sender, MouseEventArgs e)
        {
            if (!_restoreOnDrag || e.LeftButton != MouseButtonState.Pressed) return;
            var pos = e.GetPosition(this);
            if (Math.Abs(pos.X - _restoreDragStart.X) < SystemParameters.MinimumHorizontalDragDistance &&
                Math.Abs(pos.Y - _restoreDragStart.Y) < SystemParameters.MinimumVerticalDragDistance) return;

            _restoreOnDrag = false;
            ((UIElement)sender).ReleaseMouseCapture();

            var ratioX = ActualWidth > 0 ? pos.X / ActualWidth : 0.5;
            var restoreWidth = RestoreBounds.Width;
            // Cursor position in screen DIPs (PointToScreen returns device pixels).
            var toDip = PresentationSource.FromVisual(this)?.CompositionTarget?.TransformFromDevice ?? Matrix.Identity;
            var cursor = toDip.Transform(PointToScreen(pos));

            WindowState = WindowState.Normal;
            Left = cursor.X - restoreWidth * ratioX;
            Top = cursor.Y - 9; // middle of the 18 px title bar
            try { DragMove(); } catch { }
        }

        private void TitleBar_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
        {
            if (!_restoreOnDrag) return;
            _restoreOnDrag = false;
            ((UIElement)sender).ReleaseMouseCapture();
        }

        // A maximized borderless window (WindowStyle=None) is placed by Windows so that its
        // invisible resize frame hangs off the screen edges, cutting off the title bar and
        // the top corners. Pad the content by that frame while maximized and drop the
        // rounded corners there (same fix as HedgeFund/HedgeFundWindow.xaml.cs).
        private void Window_StateChanged(object? sender, EventArgs e)
        {
            if (WindowState == WindowState.Maximized)
            {
                var frame = SystemParameters.WindowResizeBorderThickness;
                const double paddedBorder = 4; // SM_CXPADDEDBORDER, not exposed by SystemParameters
                RootBorder.Margin = new Thickness(frame.Left + paddedBorder, frame.Top + paddedBorder,
                                                  frame.Right + paddedBorder, frame.Bottom + paddedBorder);
                RootBorder.CornerRadius = new CornerRadius(0);
                TitleBarBorder.CornerRadius = new CornerRadius(0);
            }
            else
            {
                RootBorder.Margin = new Thickness(0);
                RootBorder.CornerRadius = new CornerRadius(8);
                TitleBarBorder.CornerRadius = new CornerRadius(8, 8, 0, 0);
            }
        }

        private void MinimizeButton_Click(object sender, RoutedEventArgs e)
        {
            WindowState = WindowState.Minimized;
        }

        private void CloseButton_Click(object sender, RoutedEventArgs e)
        {
            Close();
        }
    }

    // One ledger line as shown in the Transaction Log list (public for WPF bindings).
    public sealed class TransactionLogRow : INotifyPropertyChanged
    {
        private readonly string? _logoDomain;
        private ImageSource? _logo;

        internal TransactionLogRow(AiCostEntry entry)
        {
            var vendor = VendorLogos.Resolve(entry.Model);
            _logoDomain = vendor.Domain;

            ModelName = VendorLogos.ShortModelName(entry.Model);
            VendorName = vendor.Name;
            Initials = vendor.Initials;

            var module = string.IsNullOrWhiteSpace(entry.Module) ? "Unknown module" : entry.Module;
            var feature = string.IsNullOrWhiteSpace(entry.FeatureLabel) ? "AI request" : entry.FeatureLabel;
            ModuleFeature = $"{module} · {feature}";

            LocalTimeText = entry.Timestamp is DateTimeOffset ts
                ? ts.ToLocalTime().ToString("d MMM yyyy, HH:mm", CultureInfo.InvariantCulture)
                : "—";

            TokensText = entry.PromptTokens is null && entry.CompletionTokens is null
                ? "tokens not reported"
                : $"{AiCostLedger.FormatTokens(entry.PromptTokens)} in / {AiCostLedger.FormatTokens(entry.CompletionTokens)} out tokens";

            CostText = AiCostLedger.FormatUsd(entry.CostUsd);

            var status = string.IsNullOrWhiteSpace(entry.Status) || entry.Status == "ok" ? string.Empty : $" · status: {entry.Status}";
            ToolTipText = $"{entry.Model}\n{entry.Provider} · {module} · {(string.IsNullOrWhiteSpace(entry.Feature) ? feature : entry.Feature)}{status}\n" +
                          (entry.CostUsd is null ? "Cost not reported by the provider" : $"Charged: {CostText}");
            AccessibleText = $"{ModelName} by {VendorName}, {ModuleFeature}, {LocalTimeText}, cost {CostText}";

            Identity = $"{entry.Timestamp:O}|{entry.Model}|{entry.Feature}|{entry.CostUsd}";

            if (_logoDomain is not null && VendorLogos.TryGetCached(_logoDomain, out var cached))
            {
                _logo = cached;
            }
        }

        public event PropertyChangedEventHandler? PropertyChanged;

        public string ModelName { get; }
        public string VendorName { get; }
        public string Initials { get; }
        public string ModuleFeature { get; }
        public string LocalTimeText { get; }
        public string TokensText { get; }
        public string CostText { get; }
        public string ToolTipText { get; }
        public string AccessibleText { get; }
        internal string Identity { get; }

        public ImageSource? Logo
        {
            get => _logo;
            private set
            {
                _logo = value;
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Logo)));
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(LogoVisibility)));
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(InitialsVisibility)));
            }
        }

        public Visibility LogoVisibility => _logo is null ? Visibility.Collapsed : Visibility.Visible;
        public Visibility InitialsVisibility => _logo is null ? Visibility.Visible : Visibility.Collapsed;

        // Called on the UI thread; the continuation comes back to it (no ConfigureAwait).
        internal async Task LoadLogoAsync()
        {
            if (_logo is not null || _logoDomain is null)
            {
                return;
            }
            var logo = await VendorLogos.GetLogoAsync(_logoDomain);
            if (logo is not null)
            {
                Logo = logo;
            }
        }
    }
}
