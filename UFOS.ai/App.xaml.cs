using System;
using System.Configuration;
using System.Data;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;
using UFOS.ai.Logging;
using UFOS.ai.Shared;

namespace UFOS.ai
{
    /// <summary>
    /// Interaction logic for App.xaml
    /// </summary>
    public partial class App : Application, IUiErrorReporter
    {
        // EU AI Act / "not investment advice" notice: shown on every start, before
        // MainWindow (StartupUri) is created. Declining exits the app. See
        // Shared/AiDisclaimerWindow.cs and the AI-labelling section in CLAUDE.md.
        protected override void OnStartup(StartupEventArgs e)
        {
            // Stage 10 (S10-U): the app must not run twice — a second start brings the running instance to the front and exits
            // (checked BEFORE the AI notice, so the second start shows nothing at all).
            if (!AcquireSingleInstance())
            {
                StartupUri = null;
                Shutdown(0);
                return;
            }
            base.OnStartup(e);
            AiDisclaimerWindow.ConfirmAtStartup(this);
            // user decision 2026-10-24 (option A): the guard could not be created — start normally, log it AND tell the user
            if (_guardUnavailable) Dispatcher.BeginInvoke(new Action(ShowGuardUnavailableNote), DispatcherPriority.ApplicationIdle);
        }

        protected override void OnExit(ExitEventArgs e)
        {
            ReleaseSingleInstance();
            base.OnExit(e);
        }

        // ------------------------------------------------------------------
        // Single instance (Stage 10, S10-U, 2026-10-24 — a 2nd instance was started on 2026-10-22).
        // One named mutex per user session ("Local\" + the user's SID). The first instance also opens a named auto-reset event
        // and waits on it in a background thread; a second start signals the event (after AllowSetForegroundWindow, so the
        // first instance may take the foreground) and exits with code 0. No window, no notice in the second instance.
        // ------------------------------------------------------------------

        private static Mutex? _instanceMutex;
        private static EventWaitHandle? _activateEvent;
        private static volatile bool _instanceClosing;
        private static bool _guardUnavailable;
        private const string GuardUnavailableText =
            "Single-instance protection unavailable this session — a second start would open a second window.";

        private static string InstanceKey
        {
            get
            {
                string sid;
                try { sid = WindowsIdentity.GetCurrent().User?.Value ?? "user"; }
                catch (Exception) { sid = "user"; }
                return @"Local\UFOS.ai.App." + sid;
            }
        }

        [DllImport("user32.dll")]
        private static extern bool AllowSetForegroundWindow(int dwProcessId);

        [DllImport("user32.dll")]
        private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr hWnd);

        private const int AsfwAny = -1;
        private const int SwRestore = 9;

        /// <summary>True = this is the only instance (keeps the mutex); false = another one runs and was asked to come to the front.</summary>
        private bool AcquireSingleInstance()
        {
            var key = InstanceKey;
            bool createdNew;
            try { _instanceMutex = new Mutex(true, key + ".Instance", out createdNew); }
            catch (Exception ex)
            {
                // cannot tell (e.g. access denied on the name) — never block the start because of the guard; log it
                try { AppLog.Warn("App", "Single-instance check unavailable — starting anyway (the user is told in the main window).", ex); } catch { }
                _guardUnavailable = true;
                return true;
            }
            if (!createdNew)
            {
                try { AllowSetForegroundWindow(AsfwAny); } catch { }
                try
                {
                    using var ev = EventWaitHandle.OpenExisting(key + ".Activate");
                    ev.Set();
                }
                catch (Exception) { /* the first instance is still starting — it shows its own window */ }
                try { AppLog.Info("App", "A second start of UFOS.ai was blocked; the running instance was asked to come to the front."); } catch { }
                try { _instanceMutex.Dispose(); } catch { }
                _instanceMutex = null;
                return false;
            }
            try
            {
                _activateEvent = new EventWaitHandle(false, EventResetMode.AutoReset, key + ".Activate");
                var thread = new Thread(() =>
                {
                    while (!_instanceClosing)
                    {
                        try { _activateEvent.WaitOne(); }
                        catch (Exception) { return; }   // disposed at exit
                        if (_instanceClosing) return;
                        try { Dispatcher.BeginInvoke(new Action(BringToFront)); } catch { return; }
                    }
                })
                { IsBackground = true, Name = "UFOS.ai single instance" };
                thread.Start();
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("App", "Single-instance activation listener could not start (a second start still exits).", ex); } catch { }
            }
            return true;
        }

        /// <summary>Restores (keeps a maximized state) and activates the main window — or the AI notice while it is still open.</summary>
        private void BringToFront()
        {
            try
            {
                var w = MainWindow != null && MainWindow.IsVisible ? MainWindow
                        : Windows.OfType<Window>().FirstOrDefault(x => x.IsVisible && x.IsActive) ?? Windows.OfType<Window>().FirstOrDefault(x => x.IsVisible);
                if (w == null) return;
                var hwnd = new WindowInteropHelper(w).Handle;
                if (w.WindowState == WindowState.Minimized && hwnd != IntPtr.Zero) ShowWindow(hwnd, SwRestore);
                w.Activate();
                if (hwnd != IntPtr.Zero) SetForegroundWindow(hwnd);
                var top = w.Topmost;
                w.Topmost = true;      // bring above other apps once, then back to the window's own setting
                w.Topmost = top;
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("App", "Bringing the running instance to the front failed.", ex); } catch { }
            }
        }

        /// <summary>
        /// Option A (user 2026-10-24): a small, non-blocking note in the main window's lower right corner when the guard is unavailable —
        /// closes itself after 30 s or with OK; never modal. Waits for the main window to load (after the AI notice).
        /// </summary>
        private void ShowGuardUnavailableNote()
        {
            try
            {
                var owner = MainWindow;
                if (owner == null) return;
                if (!owner.IsLoaded)
                {
                    owner.Loaded += (_, _) => Dispatcher.BeginInvoke(new Action(ShowGuardUnavailableNote), DispatcherPriority.ApplicationIdle);
                    return;
                }
                var text = new TextBlock
                {
                    Text = GuardUnavailableText,
                    TextWrapping = TextWrapping.Wrap,
                    Foreground = new SolidColorBrush(Color.FromRgb(0xE3, 0xC2, 0x7E)),
                    FontSize = 12,
                    MaxWidth = 380,
                };
                var ok = new Button { Content = "OK", Padding = new Thickness(12, 2, 12, 2), Margin = new Thickness(0, 8, 0, 0), HorizontalAlignment = HorizontalAlignment.Right };
                var panel = new StackPanel();
                panel.Children.Add(text);
                panel.Children.Add(ok);
                var note = new Window
                {
                    Owner = owner,
                    WindowStyle = WindowStyle.None,
                    ResizeMode = ResizeMode.NoResize,
                    ShowInTaskbar = false,
                    ShowActivated = false,
                    SizeToContent = SizeToContent.WidthAndHeight,
                    Background = new SolidColorBrush(Color.FromRgb(0x1E, 0x1A, 0x10)),
                    BorderBrush = new SolidColorBrush(Color.FromRgb(0xD9, 0xA5, 0x4B)),
                    BorderThickness = new Thickness(1),
                    Content = new Border { Padding = new Thickness(14, 10, 14, 10), Child = panel },
                    Title = "UFOS.ai — note",
                };
                System.Windows.Automation.AutomationProperties.SetName(note, GuardUnavailableText);
                ok.Click += (_, _) => note.Close();
                note.Loaded += (_, _) =>
                {
                    var area = SystemParameters.WorkArea;
                    var right = owner.WindowState == WindowState.Maximized ? area.Right : owner.Left + owner.ActualWidth;
                    var bottom = owner.WindowState == WindowState.Maximized ? area.Bottom : owner.Top + owner.ActualHeight;
                    note.Left = Math.Max(area.Left, right - note.ActualWidth - 24);
                    note.Top = Math.Max(area.Top, bottom - note.ActualHeight - 24);
                };
                var timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(30) };
                timer.Tick += (_, _) => { timer.Stop(); if (note.IsLoaded) note.Close(); };
                note.Closed += (_, _) => timer.Stop();
                note.Show();
                timer.Start();
            }
            catch (Exception ex)
            {
                try { AppLog.Warn("App", "The single-instance note could not be shown.", ex); } catch { }
            }
        }

        private static void ReleaseSingleInstance()
        {
            _instanceClosing = true;
            try { _activateEvent?.Set(); } catch { }
            try { _activateEvent?.Dispose(); } catch { }
            _activateEvent = null;
            try { _instanceMutex?.ReleaseMutex(); } catch { }
            try { _instanceMutex?.Dispose(); } catch { }
            _instanceMutex = null;
        }

        // Public helper used by windows to display/log UI errors. Routes through the
        // central AppLog so the error shows up in the global log window regardless of
        // which module raised it.
        public void ShowUiException(Exception ex)
        {
            try
            {
                try { AppLog.Error("UI", ex?.Message ?? "(unbekannter Fehler)", ex); } catch { }

                // show minimal user-facing notification
                try
                {
                    MessageBox.Show("An Error has occured. Details have been logged. Try restarting the App. Please report any bugs on the GitHub.", "Error", MessageBoxButton.OK, MessageBoxImage.Error);
                }
                catch { }
            }
            catch { }
        }
    }

}
