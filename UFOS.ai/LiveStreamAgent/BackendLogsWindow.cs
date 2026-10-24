using System;
using System.Linq;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Win32;
using UFOS.ai.Logging;

namespace UFOS.ai
{
    /// <summary>
    /// Baut das globale Log-Fenster mit exakt dem gleichen (dynamisch generierten) Design wie
    /// das ursprüngliche LivestreamAgent-Backend-Log-Fenster: dunkles, rahmenloses Chrome mit
    /// schwarzer Titelleiste und runden Save/Minimize/Close-Buttons. Zeigt die Einträge aus dem
    /// zentralen <see cref="AppLog"/> (alle Module) an, optional gefiltert nach Modul, und kann
    /// zusätzlich eine Legacy-Textquelle (z.B. rohen Backend-Prozess-Output) einblenden.
    /// Neue Module müssen sich nur um <see cref="AppLog"/>-Aufrufe kümmern und rufen zum Öffnen
    /// einfach <c>BackendLogsWindow.Show(this)</c> auf.
    /// </summary>
    public static class BackendLogsWindow
    {
        private const string AllModulesLabel = "Alle Module";

        public static Window? Show(Window? owner, Func<string>? legacyGetLogs = null, string title = "Backend Log")
        {
            Window? win = null;
            try
            {
                win = new Window
                {
                    Title = title,
                    Width = 700,
                    Height = 420,
                    Owner = owner,
                    WindowStartupLocation = owner != null ? WindowStartupLocation.CenterOwner : WindowStartupLocation.CenterScreen,
                    WindowStyle = WindowStyle.None,
                    ResizeMode = ResizeMode.NoResize,
                    AllowsTransparency = true,
                    Background = Brushes.Transparent
                };

                // Outer border to emulate LivestreamAgent chrome
                var outer = new Border
                {
                    Background = new SolidColorBrush(Color.FromRgb(17, 17, 17)),
                    CornerRadius = new CornerRadius(8),
                    Padding = new Thickness(0),
                    SnapsToDevicePixels = true
                };

                var root = new Grid { Margin = new Thickness(0) };
                root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(18) }); // title bar
                root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(28) }); // module filter bar
                root.RowDefinitions.Add(new RowDefinition { Height = new GridLength(1, GridUnitType.Star) });

                // Title bar (black) with round buttons on the right
                var titleBar = new Border { Background = Brushes.Black, CornerRadius = new CornerRadius(8, 8, 0, 0), Height = 18 };
                titleBar.MouseLeftButtonDown += (s, ev) => { try { if (ev.ButtonState == MouseButtonState.Pressed) win.DragMove(); } catch { } };

                var tbGrid = new Grid();
                tbGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = new GridLength(1, GridUnitType.Star) });
                tbGrid.ColumnDefinitions.Add(new ColumnDefinition { Width = GridLength.Auto });

                var titleText = new TextBlock { Text = title, Foreground = Brushes.White, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(8, 0, 0, 0), FontSize = 12, FontWeight = FontWeights.SemiBold };
                Grid.SetColumn(titleText, 0);
                tbGrid.Children.Add(titleText);

                var btnPanel = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 6, 0) };

                var roundStyleObj = (owner?.TryFindResource("RoundButtonStyle")) ?? Application.Current?.TryFindResource("RoundButtonStyle");
                Style? roundStyle = roundStyleObj as Style;

                // If the RoundButtonStyle isn't available (resource lookup failed), create a fallback style from code
                if (roundStyle == null)
                {
                    var template = new ControlTemplate(typeof(Button));
                    var borderFactory = new FrameworkElementFactory(typeof(Border));
                    borderFactory.SetValue(Border.CornerRadiusProperty, new CornerRadius(999));
                    borderFactory.SetBinding(Border.BackgroundProperty, new System.Windows.Data.Binding("Background") { RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent) });
                    borderFactory.SetBinding(Border.WidthProperty, new System.Windows.Data.Binding("Width") { RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent) });
                    borderFactory.SetBinding(Border.HeightProperty, new System.Windows.Data.Binding("Height") { RelativeSource = new RelativeSource(RelativeSourceMode.TemplatedParent) });
                    var contentPresenterFactory = new FrameworkElementFactory(typeof(ContentPresenter));
                    contentPresenterFactory.SetValue(ContentPresenter.HorizontalAlignmentProperty, HorizontalAlignment.Center);
                    contentPresenterFactory.SetValue(ContentPresenter.VerticalAlignmentProperty, VerticalAlignment.Center);
                    borderFactory.AppendChild(contentPresenterFactory);
                    template.VisualTree = borderFactory;

                    var style = new Style(typeof(Button));
                    style.Setters.Add(new Setter(Button.WidthProperty, 14.0));
                    style.Setters.Add(new Setter(Button.HeightProperty, 14.0));
                    style.Setters.Add(new Setter(Button.PaddingProperty, new Thickness(0)));
                    style.Setters.Add(new Setter(Button.BorderThicknessProperty, new Thickness(0)));
                    style.Setters.Add(new Setter(Button.BackgroundProperty, Brushes.Transparent));
                    style.Setters.Add(new Setter(Button.TemplateProperty, template));

                    roundStyle = style;
                }

                string BuildFullLogText(string? moduleFilter)
                {
                    var sb = new StringBuilder();
                    var entries = AppLog.GetBufferedEntries().AsEnumerable();
                    if (!string.IsNullOrEmpty(moduleFilter) && !string.Equals(moduleFilter, AllModulesLabel, StringComparison.OrdinalIgnoreCase))
                    {
                        entries = entries.Where(en => string.Equals(en.Module, moduleFilter, StringComparison.OrdinalIgnoreCase));
                    }
                    foreach (var entry in entries) sb.AppendLine(entry.ToString());

                    if (legacyGetLogs != null && (string.IsNullOrEmpty(moduleFilter) || string.Equals(moduleFilter, AllModulesLabel, StringComparison.OrdinalIgnoreCase)))
                    {
                        var legacyText = legacyGetLogs() ?? string.Empty;
                        if (!string.IsNullOrWhiteSpace(legacyText))
                        {
                            sb.AppendLine("---- Legacy Backend Buffer ----");
                            sb.Append(legacyText);
                        }
                    }
                    return sb.ToString();
                }

                // save button (small blue) placed before minimize/close
                var saveBtn = new Button { Width = 14, Height = 14, Margin = new Thickness(6, -2, 0, 0), VerticalAlignment = VerticalAlignment.Center };
                saveBtn.Style = roundStyle;
                try { saveBtn.Background = new SolidColorBrush(Color.FromRgb(0x00, 0x7A, 0xFF)); } catch { saveBtn.Background = Brushes.DodgerBlue; }
                saveBtn.ToolTip = "Save log";
                saveBtn.Content = new TextBlock { Text = "\uE74E", FontFamily = new FontFamily("Segoe MDL2 Assets"), Foreground = Brushes.White, FontSize = 8.5, FontWeight = FontWeights.Bold, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };

                var minimizeBtn = new Button { Width = 14, Height = 14, Margin = new Thickness(6, 0, 0, 0) };
                minimizeBtn.Style = roundStyle;
                try { minimizeBtn.Background = new SolidColorBrush(Color.FromRgb(0xED, 0xB4, 0x00)); } catch { minimizeBtn.Background = Brushes.Gold; }
                minimizeBtn.Click += (s, ev) => { try { win.WindowState = WindowState.Minimized; } catch { } };
                minimizeBtn.Content = new TextBlock { Text = "\u2013", Foreground = Brushes.White, FontSize = 9, FontWeight = FontWeights.Bold, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };

                var closeBtn = new Button { Width = 14, Height = 14, Margin = new Thickness(6, 0, 0, 0) };
                closeBtn.Style = roundStyle;
                try { closeBtn.Background = new SolidColorBrush(Color.FromRgb(0xED, 0x6A, 0x5A)); } catch { closeBtn.Background = Brushes.IndianRed; }
                closeBtn.Click += (s, ev) => { try { win.Close(); } catch { } };
                closeBtn.Content = new TextBlock { Text = "\u2715", Foreground = Brushes.White, FontSize = 9, FontWeight = FontWeights.Bold, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center };

                btnPanel.Children.Add(saveBtn);
                btnPanel.Children.Add(minimizeBtn);
                btnPanel.Children.Add(closeBtn);
                Grid.SetColumn(btnPanel, 1);
                tbGrid.Children.Add(btnPanel);

                titleBar.Child = tbGrid;
                Grid.SetRow(titleBar, 0);
                root.Children.Add(titleBar);

                // Module filter bar (dark, matches chrome) directly below the title bar
                var filterBar = new Border { Background = new SolidColorBrush(Color.FromRgb(17, 17, 17)), Padding = new Thickness(8, 4, 8, 4) };
                var filterPanel = new StackPanel { Orientation = Orientation.Horizontal };
                var filterLabel = new TextBlock { Text = "Modul:", Foreground = new SolidColorBrush(Color.FromRgb(154, 154, 154)), VerticalAlignment = VerticalAlignment.Center, Margin = new Thickness(0, 0, 6, 0), FontSize = 11 };
                var moduleCombo = new ComboBox { Width = 160, Background = new SolidColorBrush(Color.FromRgb(26, 26, 26)), Foreground = Brushes.White, BorderBrush = new SolidColorBrush(Color.FromRgb(42, 42, 42)) };
                filterPanel.Children.Add(filterLabel);
                filterPanel.Children.Add(moduleCombo);
                filterBar.Child = filterPanel;
                Grid.SetRow(filterBar, 1);
                root.Children.Add(filterBar);

                // Content area with padding
                var contentGrid = new Grid { Margin = new Thickness(8) };
                Grid.SetRow(contentGrid, 2);

                var tb = new TextBox
                {
                    Text = string.Empty,
                    IsReadOnly = true,
                    TextWrapping = TextWrapping.Wrap,
                    VerticalScrollBarVisibility = ScrollBarVisibility.Auto,
                    HorizontalScrollBarVisibility = ScrollBarVisibility.Auto,
                    Background = new SolidColorBrush(Color.FromRgb(17, 17, 17)),
                    Foreground = new SolidColorBrush(Color.FromRgb(240, 240, 240)),
                    FontFamily = new System.Windows.Media.FontFamily("Consolas"),
                    Margin = new Thickness(0)
                };

                // Apply the same compact scrollbar style used across the app, if available
                try
                {
                    var compact = (owner?.TryFindResource("CompactScrollBarStyle")) ?? Application.Current?.TryFindResource("CompactScrollBarStyle");
                    if (compact is Style compactStyle) tb.Resources.Add(typeof(ScrollBar), compactStyle);
                    var thumbStyleObj = (owner?.TryFindResource("CompactScrollThumbStyle")) ?? Application.Current?.TryFindResource("CompactScrollThumbStyle");
                    if (thumbStyleObj is Style thumbStyle) tb.Resources.Add(typeof(Thumb), thumbStyle);
                    tb.Loaded += (s, ev) => ApplyScrollStylesToVisualTree(tb, owner);
                }
                catch { }

                contentGrid.Children.Add(tb);
                root.Children.Add(contentGrid);

                outer.Child = root;
                win.Content = outer;

                // populate module filter with currently known modules
                void RefreshModuleFilter()
                {
                    var previous = moduleCombo.SelectedItem as string ?? AllModulesLabel;
                    var modules = AppLog.GetBufferedEntries().Select(en => en.Module).Distinct().OrderBy(m => m, StringComparer.OrdinalIgnoreCase).ToList();
                    modules.Insert(0, AllModulesLabel);
                    moduleCombo.ItemsSource = modules;
                    moduleCombo.SelectedItem = modules.Contains(previous) ? previous : AllModulesLabel;
                }

                void RefreshLogText()
                {
                    var selected = moduleCombo.SelectedItem as string ?? AllModulesLabel;
                    var text = BuildFullLogText(selected);
                    if (tb.Text != text)
                    {
                        tb.Text = text;
                        tb.CaretIndex = tb.Text.Length;
                        tb.ScrollToEnd();
                    }
                }

                RefreshModuleFilter();
                RefreshLogText();
                moduleCombo.SelectionChanged += (s, ev) => RefreshLogText();

                saveBtn.Click += (s, ev) =>
                {
                    try
                    {
                        var dlg = new SaveFileDialog()
                        {
                            Title = "Save Backend Log",
                            Filter = "Log files (*.log)|*.log|Text files (*.txt)|*.txt|All files (*.*)|*.*",
                            FileName = $"backend-log-{DateTime.Now:yyyy-MM-dd_HHmmss}.log",
                            DefaultExt = ".log",
                            AddExtension = true
                        };
                        var res = dlg.ShowDialog(win);
                        if (res == true)
                        {
                            System.IO.File.WriteAllText(dlg.FileName, tb.Text, Encoding.UTF8);
                        }
                    }
                    catch { }
                };

                // update periodically while open (refresh view + module list)
                var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(500) };
                timer.Tick += (s, ev) => { RefreshModuleFilter(); RefreshLogText(); };
                win.Closed += (s, ev) => timer.Stop();
                // Close on Escape key when this dynamic window is focused
                win.PreviewKeyDown += (s, ev) => { try { if (ev.Key == Key.Escape) win.Close(); } catch { } };
                timer.Start();

                win.Show();
                win.Activate();
                return win;
            }
            catch (Exception ex)
            {
                try { AppLog.Error("UI", "Failed to open log window: " + ex.Message, ex); } catch { }
                try { win?.Close(); } catch { }
                return null;
            }
        }

        // helper to apply styles to internal ScrollBar/Thumb elements once the visual tree is built
        private static void ApplyScrollStylesToVisualTree(DependencyObject root, Window? owner)
        {
            try
            {
                var sbStyle = (owner?.TryFindResource("CompactScrollBarStyle") ?? Application.Current?.TryFindResource("CompactScrollBarStyle")) as Style;
                var thStyle = (owner?.TryFindResource("CompactScrollThumbStyle") ?? Application.Current?.TryFindResource("CompactScrollThumbStyle")) as Style;
                if (sbStyle == null && thStyle == null) return;
                for (int i = 0; i < System.Windows.Media.VisualTreeHelper.GetChildrenCount(root); i++)
                {
                    var child = System.Windows.Media.VisualTreeHelper.GetChild(root, i);
                    if (child is ScrollBar sb && sbStyle != null) sb.Style = sbStyle;
                    if (child is Thumb th && thStyle != null) th.Style = thStyle;
                    ApplyScrollStylesToVisualTree(child, owner);
                }
            }
            catch { }
        }
    }
}
