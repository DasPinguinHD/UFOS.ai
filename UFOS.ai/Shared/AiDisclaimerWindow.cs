using System;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Markup;

namespace UFOS.ai.Shared
{
    /// <summary>
    /// "Important notice on AI content and financial data" — shown modally on every
    /// app start before any window with AI or market data opens (EU AI Act Art. 50
    /// transparency + "not investment advice"). The app only continues after the user
    /// clicks "I agree and want to continue"; closing the notice any other way exits.
    ///
    /// Code-only (no .xaml file) and internal so it can be linked into every module
    /// project as-is — see AiContent.cs for why. The layout is parsed from an inline
    /// XAML string; colors mirror Styles/DesignSystem.xaml's tokens by value because a
    /// module started on its own doesn't have that dictionary loaded.
    ///
    /// Do not remove or soften this notice when polishing the UI (project conventions).
    /// If the text changes, keep the German original's meaning: AI via third-party APIs,
    /// no investment advice, risk of errors/hallucinations, verify independently.
    /// </summary>
    internal sealed class AiDisclaimerWindow : Window
    {
        // Bump whenever the notice text changes: every user then sees (and has to
        // agree to) the new text again, even if they had opted out of the notice.
        private const int NoticeVersion = 1;

        private const string LayoutXaml = """
            <Border xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
                    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
                    Background="#FF111111" BorderBrush="#FF2A2A2A" BorderThickness="1" CornerRadius="8" SnapsToDevicePixels="True">
                <Border.Resources>
                    <ControlTemplate x:Key="PillButtonTemplate" TargetType="Button">
                        <Border x:Name="bd" Background="{TemplateBinding Background}" CornerRadius="999">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center" />
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="bd" Property="Opacity" Value="0.88" />
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                    <ControlTemplate x:Key="PrimaryButtonTemplate" TargetType="Button">
                        <Border x:Name="bd" Background="{TemplateBinding Background}" CornerRadius="6" Padding="{TemplateBinding Padding}"
                                BorderBrush="{TemplateBinding BorderBrush}" BorderThickness="{TemplateBinding BorderThickness}">
                            <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center" />
                        </Border>
                        <ControlTemplate.Triggers>
                            <Trigger Property="IsMouseOver" Value="True">
                                <Setter TargetName="bd" Property="Opacity" Value="0.88" />
                            </Trigger>
                            <Trigger Property="IsKeyboardFocused" Value="True">
                                <Setter TargetName="bd" Property="BorderBrush" Value="#FFFFFFFF" />
                            </Trigger>
                            <Trigger Property="IsPressed" Value="True">
                                <Setter TargetName="bd" Property="RenderTransformOrigin" Value="0.5,0.5" />
                                <Setter TargetName="bd" Property="RenderTransform">
                                    <Setter.Value>
                                        <ScaleTransform ScaleX="0.97" ScaleY="0.97" />
                                    </Setter.Value>
                                </Setter>
                            </Trigger>
                        </ControlTemplate.Triggers>
                    </ControlTemplate>
                </Border.Resources>
                <Grid>
                    <Grid.RowDefinitions>
                        <RowDefinition Height="Auto" />
                        <RowDefinition Height="Auto" />
                    </Grid.RowDefinitions>

                    <!-- Titlebar (same chrome as MainWindow) -->
                    <Border x:Name="TitleBar" Grid.Row="0" Background="#FF000000" CornerRadius="8,8,0,0" Height="18">
                        <Grid>
                            <StackPanel Orientation="Horizontal" HorizontalAlignment="Left" VerticalAlignment="Center" Margin="10,0,0,0">
                                <Ellipse Width="5" Height="5" Fill="#FFB98CE8" VerticalAlignment="Center" />
                                <TextBlock Text="UFOS.ai · Notice" Margin="6,0,0,0" FontSize="10" FontFamily="Consolas" Foreground="#FF8F8F8F" VerticalAlignment="Center" />
                            </StackPanel>
                            <Button x:Name="CloseButton" HorizontalAlignment="Right" VerticalAlignment="Center" Margin="0,0,12,0"
                                    Template="{StaticResource PillButtonTemplate}" Width="14" Height="14" Background="#FFED6A5A"
                                    ToolTip="Close" Focusable="False">
                                <TextBlock Text="✕" Foreground="White" FontSize="9" FontWeight="Bold" HorizontalAlignment="Center" VerticalAlignment="Center" />
                            </Button>
                        </Grid>
                    </Border>

                    <StackPanel Grid.Row="1" Margin="28,22,28,24">
                        <TextBlock Text="✦ AI NOTICE" FontFamily="Consolas" FontSize="10" Foreground="#FFB98CE8" Margin="0,0,0,8" />
                        <TextBlock x:Name="HeadingText" Text="Important notice on AI content and financial data" FontSize="18" FontWeight="Bold"
                                   Foreground="#FFF2F2F2" TextWrapping="Wrap" Margin="0,0,0,12" />
                        <TextBlock FontSize="12.5" LineHeight="19" Foreground="#FFBFBFBF" TextWrapping="Wrap" Margin="0,0,0,14"
                                   Text="This app uses automated language models (artificial intelligence) via third-party interfaces to summarise and evaluate stock prices, market news and insider trades." />

                        <Grid Margin="0,0,0,10">
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="16" />
                                <ColumnDefinition Width="*" />
                            </Grid.ColumnDefinitions>
                            <TextBlock Grid.Column="0" Text="•" FontSize="12.5" Foreground="#FFBFBFBF" />
                            <TextBlock Grid.Column="1" FontSize="12.5" LineHeight="19" Foreground="#FFBFBFBF" TextWrapping="Wrap">
                                <Run Text="No investment advice:" FontWeight="SemiBold" Foreground="#FFF2F2F2" />
                                <Run Text="All analyses, assessments and data provided are for information purposes only. They do not constitute investment advice, financial analysis or a recommendation to buy or sell securities." />
                            </TextBlock>
                        </Grid>

                        <Grid Margin="0,0,0,16">
                            <Grid.ColumnDefinitions>
                                <ColumnDefinition Width="16" />
                                <ColumnDefinition Width="*" />
                            </Grid.ColumnDefinitions>
                            <TextBlock Grid.Column="0" Text="•" FontSize="12.5" Foreground="#FFBFBFBF" />
                            <TextBlock Grid.Column="1" FontSize="12.5" LineHeight="19" Foreground="#FFBFBFBF" TextWrapping="Wrap">
                                <Run Text="Risk of errors:" FontWeight="SemiBold" Foreground="#FFF2F2F2" />
                                <Run Text="AI systems can make mistakes or misinterpret data (&#x201C;hallucinations&#x201D;). Always verify important financial data independently before making investment decisions." />
                            </TextBlock>
                        </Grid>

                        <Border Background="#14B98CE8" BorderBrush="#FFB98CE8" BorderThickness="2,0,0,0" Padding="10,7" Margin="0,0,0,10">
                            <TextBlock FontSize="11" LineHeight="16" Foreground="#FFBFBFBF" TextWrapping="Wrap"
                                       Text="AI-generated content in this app is always marked with &#x201C;✦ AI-GENERATED&#x201D;, the model used and the time it was generated." />
                        </Border>
                        <TextBlock Text="UFOS.ai is a proof-of-concept portfolio project — not a published product." FontSize="10.5" FontStyle="Italic"
                                   Foreground="#FF8A8A8A" TextWrapping="Wrap" Margin="0,0,0,20" />

                        <Button x:Name="AgreeButton" HorizontalAlignment="Right" Template="{StaticResource PrimaryButtonTemplate}"
                                Background="#FF5AC8FA" BorderBrush="#FF5AC8FA" BorderThickness="1" Padding="18,9" IsDefault="True" Cursor="Hand">
                            <TextBlock Text="I agree and want to continue" FontSize="13" FontWeight="SemiBold" Foreground="#FF111111" />
                        </Button>
                    </StackPanel>
                </Grid>
            </Border>
            """;

        private readonly bool _exitsAppWhenDeclined;

        private AiDisclaimerWindow(bool exitsAppWhenDeclined)
        {
            _exitsAppWhenDeclined = exitsAppWhenDeclined;

            Title = "Important notice on AI content and financial data";
            Width = 580;
            SizeToContent = SizeToContent.Height;
            ResizeMode = ResizeMode.NoResize;
            WindowStyle = WindowStyle.None;
            AllowsTransparency = true;
            Background = System.Windows.Media.Brushes.Transparent;
            WindowStartupLocation = WindowStartupLocation.CenterScreen;
            ShowInTaskbar = exitsAppWhenDeclined;

            var root = (FrameworkElement)XamlReader.Parse(LayoutXaml);
            Content = root;

            if (root.FindName("TitleBar") is Border titleBar)
            {
                titleBar.MouseLeftButtonDown += (_, e) =>
                {
                    if (e.LeftButton == MouseButtonState.Pressed)
                    {
                        try { DragMove(); } catch { }
                    }
                };
            }

            if (root.FindName("CloseButton") is Button closeButton)
            {
                closeButton.ToolTip = exitsAppWhenDeclined ? "Close (exits UFOS.ai)" : "Close";
                closeButton.Click += (_, _) => Close();
            }

            if (root.FindName("AgreeButton") is Button agreeButton)
            {
                AutomationProperties.SetName(agreeButton, "I agree and want to continue");
                agreeButton.Click += (_, _) =>
                {
                    DialogResult = true;
                };
                Loaded += (_, _) => agreeButton.Focus();
            }
        }

        /// <summary>
        /// Call from App.OnStartup (before the StartupUri window is created). Returns
        /// true when the user agreed; otherwise shuts the application down and returns
        /// false. Safe to call while the app has no other window yet: the shutdown mode
        /// is switched to explicit while the dialog is open, so closing the dialog
        /// doesn't end the app before the main window exists.
        /// </summary>
        public static bool ConfirmAtStartup(Application app)
        {
            // Opt-out (MainWindow → Settings → "Show AI notice at startup") only counts
            // once the current notice version has been agreed to. The per-output AI
            // labels (EU AI Act Art. 50) are unaffected by this and always shown.
            if (!IsShownAtStartup)
            {
                return true;
            }

            var previousShutdownMode = app.ShutdownMode;
            app.ShutdownMode = ShutdownMode.OnExplicitShutdown;

            bool accepted;
            try
            {
                accepted = new AiDisclaimerWindow(exitsAppWhenDeclined: true).ShowDialog() == true;
            }
            catch
            {
                // If the notice can't even be shown, don't let the app continue silently.
                accepted = false;
            }

            if (!accepted)
            {
                app.StartupUri = null;
                app.Shutdown();
                return false;
            }

            RecordAcceptance();

            // The dialog became Application.MainWindow (first window created); clear it
            // so the StartupUri window becomes the real main window.
            app.MainWindow = null;
            app.ShutdownMode = previousShutdownMode;
            return true;
        }

        // ---------------- Opt-out state (per Windows user) ----------------
        // %LOCALAPPDATA%\UFOS.ai\ai-notice.json: which notice version was agreed to,
        // when, and whether the user wants it at startup. Any read/write problem falls
        // back to showing the notice (fail safe).

        private static string StatePath => Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "UFOS.ai", "ai-notice.json");

        /// <summary>False only if the user agreed to the current notice version and then opted out.</summary>
        public static bool IsShownAtStartup
        {
            get
            {
                var state = LoadState();
                return state is null
                    || state.Version != NoticeVersion
                    || state.AcceptedAt is null
                    || state.ShowAtStartup;
            }
        }

        /// <summary>
        /// Settings toggle. Opting out is only stored when the current notice version
        /// has been agreed to (always the case once MainWindow is open).
        /// </summary>
        public static void SetShowAtStartup(bool show)
        {
            var state = LoadState() ?? new AiNoticeState();
            if (!show && (state.AcceptedAt is null || state.Version != NoticeVersion))
            {
                return;
            }
            state.ShowAtStartup = show;
            SaveState(state);
        }

        private static void RecordAcceptance()
        {
            var state = LoadState() ?? new AiNoticeState();
            if (state.Version != NoticeVersion)
            {
                // New notice text: an older opt-out doesn't carry over.
                state.ShowAtStartup = true;
            }
            state.Version = NoticeVersion;
            state.AcceptedAt = DateTimeOffset.Now;
            SaveState(state);
        }

        private static AiNoticeState? LoadState()
        {
            try
            {
                if (!File.Exists(StatePath)) return null;
                return JsonSerializer.Deserialize<AiNoticeState>(File.ReadAllText(StatePath));
            }
            catch
            {
                return null;
            }
        }

        private static void SaveState(AiNoticeState state)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(StatePath)!);
                File.WriteAllText(StatePath, JsonSerializer.Serialize(state, new JsonSerializerOptions { WriteIndented = true }));
            }
            catch
            {
                // Not persisted -> the notice simply shows again next start.
            }
        }

        /// <summary>Re-open the notice for reading (e.g. from a Settings popup). Never exits the app.</summary>
        public static void ShowForReading(Window? owner)
        {
            var window = new AiDisclaimerWindow(exitsAppWhenDeclined: false);
            if (owner is not null)
            {
                window.Owner = owner;
                window.WindowStartupLocation = WindowStartupLocation.CenterOwner;
            }
            window.ShowDialog();
        }
    }

    /// <summary>Persisted state of the startup AI notice (see AiDisclaimerWindow).</summary>
    internal sealed class AiNoticeState
    {
        public int Version { get; set; }
        public DateTimeOffset? AcceptedAt { get; set; }
        public bool ShowAtStartup { get; set; } = true;
    }
}
