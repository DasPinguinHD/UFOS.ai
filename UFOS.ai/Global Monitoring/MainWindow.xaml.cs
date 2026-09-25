using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Input;
using System.Windows.Interop;
using Microsoft.Web.WebView2.Core;
using UFOS.ai.Logging;

namespace UFOS.ai.GlobalMonitoring
{
    public partial class MainWindow : Window
    {
        // --- Backend process management ---
        // Analogous to LiveStreamAgent.xaml.cs's TryStartBackendHelper/TryStopBackendHelper:
        // start the Python backend (Global Monitoring/backend) automatically when this
        // window opens, and make sure it's gone when the window closes — not left running
        // as an orphaned process. globe.html's own WebSocket client already retries every
        // 5s, so unlike LiveStreamAgent we don't need to block window load on a health
        // check here: the page just connects whenever the backend becomes ready.
        private Process? _backendProcess;
        private bool _weStartedBackend;
        private StreamWriter? _backendLogWriter;

        // Insider Trades and Macro Indicators are no longer part of this module:
        // moved to the Hedge Fund module's Financial Newsroom on 2026-09-24
        // (HedgeFund/Newsroom/, served by HedgeFund/backend).

        public MainWindow()
        {
            InitializeComponent();
            ApplyScreenSizeSafeguards();
        }

        private async void Window_Loaded(object sender, RoutedEventArgs e)
        {
            AppLog.Info("Global Monitoring", "Window loaded — initializing globe view and backend.");

            try
            {
                await InitializeGlobeViewAsync();
            }
            catch (Exception ex)
            {
                AppLog.Error("Global Monitoring", "Failed to initialize globe view.", ex);
                MessageBox.Show(this, "Global Monitoring failed to initialize its view:\n" + ex.Message,
                    "Global Monitoring", MessageBoxButton.OK, MessageBoxImage.Warning);
            }

            try
            {
                await TryStartBackendHelperAsync();
            }
            catch (Exception ex)
            {
                Debug.WriteLine("[GMS Backend] failed to start: " + ex.Message);
                AppLog.Error("Global Monitoring", "Failed to start backend helper.", ex);
            }
        }

        private async Task TryStartBackendHelperAsync()
        {
            // Don't double-start: if a backend (ours from a previous run, or one the
            // user started manually for development) is already answering on the
            // health port, leave it alone — and don't kill it on Closing either,
            // since we didn't start it.
            var script = FindBackendScript(out var triedPaths);

            if (await IsBackendAlreadyRunningAsync())
            {
                // 2026-09-24: an already-running backend used to be accepted
                // blindly. Twice that meant old code kept serving the globe
                // (a backend started by hand; a backend orphaned when the
                // Visual Studio debugger was stopped). Now: compare the
                // running process's code timestamp with the files on disk
                // and restart it if the disk is newer.
                var keepRunning = await HandleAlreadyRunningBackendAsync(script);
                if (keepRunning) return;
            }

            if (script == null)
            {
                Debug.WriteLine("[GMS Backend] run_backend.ps1 not found. Tried: " + string.Join(" | ", triedPaths));
                AppLog.Error("Global Monitoring", "run_backend.ps1 not found — backend cannot be started automatically. " +
                    "Tried: " + string.Join(" | ", triedPaths));
                return;
            }
            AppLog.Info("Global Monitoring", "Starting backend via " + script);

            // The subprocess's stdout/stderr (venv setup, pip install, and
            // everything main.py itself logs) used to only reach
            // Debug.WriteLine below, which is invisible unless a debugger is
            // attached — running the built app normally meant this output
            // went nowhere anyone could read it back. Mirroring it into a
            // real file fixes that; it's on top of (not instead of) main.py's
            // own logging_setup.py, which separately covers the case where
            // Python's own logging module output is what you need.
            try
            {
                var backendDir = Path.GetDirectoryName(script)!;
                var logDir = Path.Combine(backendDir, "data", "logs");
                Directory.CreateDirectory(logDir);
                var logPath = Path.Combine(logDir, "process.log");
                _backendLogWriter = new StreamWriter(logPath, append: true) { AutoFlush = true };
                _backendLogWriter.WriteLine($"--- {DateTime.Now:yyyy-MM-dd HH:mm:ss} starting backend via run_backend.ps1 ---");
            }
            catch (Exception ex)
            {
                Debug.WriteLine("[GMS Backend] could not open process.log: " + ex.Message);
                _backendLogWriter = null;
            }

            var psi = new ProcessStartInfo
            {
                FileName = "powershell.exe",
                Arguments = $"-ExecutionPolicy Bypass -File \"{script}\"",
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                WorkingDirectory = Path.GetDirectoryName(script),
            };

            _backendProcess = new Process { StartInfo = psi, EnableRaisingEvents = true };
            _backendProcess.OutputDataReceived += (s, args) =>
            {
                if (string.IsNullOrEmpty(args.Data)) return;
                Debug.WriteLine("[GMS Backend] " + args.Data);
                try { _backendLogWriter?.WriteLine($"[{DateTime.Now:HH:mm:ss}] {args.Data}"); } catch { }
                try { AppLog.Info("Global Monitoring", args.Data); } catch { }
            };
            _backendProcess.ErrorDataReceived += (s, args) =>
            {
                if (string.IsNullOrEmpty(args.Data)) return;
                Debug.WriteLine("[GMS Backend][err] " + args.Data);
                try { _backendLogWriter?.WriteLine($"[{DateTime.Now:HH:mm:ss}][err] {args.Data}"); } catch { }
                try { AppLog.Warn("Global Monitoring", args.Data); } catch { }
            };
            _backendProcess.Exited += (s, args) =>
            {
                Debug.WriteLine("[GMS Backend] process exited.");
                try { _backendLogWriter?.WriteLine($"--- {DateTime.Now:yyyy-MM-dd HH:mm:ss} process exited ---"); } catch { }
                try { AppLog.Warn("Global Monitoring", "Backend process exited."); } catch { }
            };

            try
            {
                var started = _backendProcess.Start();
                _backendProcess.BeginOutputReadLine();
                _backendProcess.BeginErrorReadLine();
                _weStartedBackend = started;
                if (started) BackendProcessJob.Attach(_backendProcess);
                Debug.WriteLine($"[GMS Backend] started: Success={started} PID={(started ? _backendProcess.Id : -1)}");
                AppLog.Info("Global Monitoring", $"Backend process started via run_backend.ps1: Success={started} PID={(started ? _backendProcess.Id : -1)}");
            }
            catch (Exception ex)
            {
                Debug.WriteLine("[GMS Backend] failed to start process: " + ex.Message);
                AppLog.Error("Global Monitoring", "Failed to start backend process (run_backend.ps1).", ex);
                _backendProcess = null;
            }
        }

        /// <summary>
        /// Locates Global Monitoring/backend/run_backend.ps1 by walking up from
        /// the executable's folder.
        ///
        /// 2026-09-24 bug: this used to be the single fixed path
        /// BaseDirectory\..\..\..\backend\run_backend.ps1. That is only right
        /// when GlobalMonitoring.exe runs on its own from
        /// "Global Monitoring\bin\Debug\net10.0-windows". Opened from the root
        /// UFOS.ai app — the normal way, where every module window runs in the
        /// root app's process — BaseDirectory is the ROOT app's bin folder, so
        /// the path resolved to UFOS.ai\backend\run_backend.ps1, which doesn't
        /// exist. The backend was never started automatically; it only ever
        /// ran when started by hand (DEBUG_runbackend.bat / python main.py),
        /// and when that window was closed the frontend had nothing to talk to.
        /// </summary>
        private static string? FindBackendScript(out List<string> tried)
        {
            tried = new List<string>();
            var dir = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
            for (var depth = 0; dir != null && depth < 8; depth++, dir = dir.Parent)
            {
                // Hosted by the root app: <repo>\Global Monitoring\backend\run_backend.ps1
                var viaRoot = Path.Combine(dir.FullName, "Global Monitoring", "backend", "run_backend.ps1");
                tried.Add(viaRoot);
                if (File.Exists(viaRoot)) return viaRoot;

                // Standalone GlobalMonitoring.exe: <Global Monitoring>\backend\run_backend.ps1
                // (only when this folder really is the Global Monitoring project,
                // so LiveStreamAgent's own backend\run_backend.ps1 can never match)
                if (File.Exists(Path.Combine(dir.FullName, "GlobalMonitoring.csproj")))
                {
                    var standalone = Path.Combine(dir.FullName, "backend", "run_backend.ps1");
                    tried.Add(standalone);
                    if (File.Exists(standalone)) return standalone;
                }
            }
            return null;
        }

        /// <summary>
        /// Returns true if the already-running backend should be kept (its code
        /// is current, or it can't be checked/restarted), false if it was shut
        /// down and a fresh one should be started.
        /// </summary>
        private async Task<bool> HandleAlreadyRunningBackendAsync(string? script)
        {
            if (script == null)
            {
                AppLog.Info("Global Monitoring", "Backend already running on 127.0.0.1:8768 (backend folder not found, can't check its code version) — using it.");
                return true;
            }

            var diskMtime = NewestPyMtime(Path.GetDirectoryName(script)!);
            double? runningMtime = null;
            try
            {
                using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
                var json = await client.GetStringAsync("http://127.0.0.1:8768/version");
                using var doc = JsonDocument.Parse(json);
                runningMtime = doc.RootElement.GetProperty("code_mtime").GetDouble();
            }
            catch
            {
                // No /version endpoint: the running backend predates this check,
                // so its code is certainly older than what's on disk. It also
                // has no /shutdown endpoint, so it can't be restarted from here.
            }

            if (runningMtime == null)
            {
                const string msg = "An outdated Global Monitoring backend is still running on port 8768 (started before " +
                                   "the current code). It can't be restarted automatically. Please end the \"python.exe\" " +
                                   "process in Task Manager, then close and reopen Global Monitoring.";
                AppLog.Warn("Global Monitoring", msg);
                MessageBox.Show(this, msg, "Global Monitoring — outdated backend", MessageBoxButton.OK, MessageBoxImage.Warning);
                return true;
            }

            if (runningMtime.Value + 1 >= diskMtime)
            {
                AppLog.Info("Global Monitoring", "Backend already running on 127.0.0.1:8768 with current code — using it.");
                return true;
            }

            AppLog.Warn("Global Monitoring", "Running backend is older than the code on disk — asking it to shut down and starting a fresh one.");
            try
            {
                using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
                await client.PostAsync("http://127.0.0.1:8768/shutdown", new StringContent(""));
            }
            catch { /* it may drop the connection while exiting — that's fine */ }

            for (var i = 0; i < 20; i++)  // wait up to ~5s for the port to free up
            {
                await Task.Delay(250);
                if (!await IsBackendAlreadyRunningAsync()) return false;
            }
            AppLog.Error("Global Monitoring", "Old backend did not shut down within 5s — keeping it. End python.exe in Task Manager if data looks stale.");
            return true;
        }

        private static double NewestPyMtime(string backendDir)
        {
            double newest = 0;
            try
            {
                foreach (var file in Directory.EnumerateFiles(backendDir, "*.py", SearchOption.AllDirectories))
                {
                    if (file.Contains(Path.DirectorySeparatorChar + ".venv" + Path.DirectorySeparatorChar) ||
                        file.Contains(Path.DirectorySeparatorChar + "__pycache__" + Path.DirectorySeparatorChar))
                        continue;
                    var t = new DateTimeOffset(File.GetLastWriteTimeUtc(file)).ToUnixTimeMilliseconds() / 1000.0;
                    if (t > newest) newest = t;
                }
            }
            catch (Exception ex)
            {
                AppLog.Warn("Global Monitoring", "Could not scan backend files for their timestamps.", ex);
            }
            return newest;
        }

        private static async Task<bool> IsBackendAlreadyRunningAsync()
        {
            try
            {
                using var client = new HttpClient { Timeout = TimeSpan.FromMilliseconds(400) };
                var resp = await client.GetAsync("http://127.0.0.1:8768/health");
                return resp.IsSuccessStatusCode;
            }
            catch
            {
                return false;
            }
        }

        private void TryStopBackendHelper()
        {
            try
            {
                if (_backendProcess == null || !_weStartedBackend) return;
                if (!_backendProcess.HasExited)
                {
                    Debug.WriteLine($"[GMS Backend] stopping process tree, PID {_backendProcess.Id}");
                    // Kill(true) kills the whole process tree — the PowerShell wrapper's
                    // child `python main.py` process included, not just powershell.exe.
                    _backendProcess.Kill(true);
                }
                _backendProcess.Dispose();
            }
            catch (Exception ex)
            {
                Debug.WriteLine("[GMS Backend] failed to stop process: " + ex.Message);
            }
            finally
            {
                _backendProcess = null;
                _weStartedBackend = false;
                try
                {
                    _backendLogWriter?.WriteLine($"--- {DateTime.Now:yyyy-MM-dd HH:mm:ss} stopped by app ---");
                    _backendLogWriter?.Dispose();
                }
                catch { }
                _backendLogWriter = null;
            }
        }

        private async System.Threading.Tasks.Task InitializeGlobeViewAsync()
        {
            var userDataFolder = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "UFOS.ai", "GlobalMonitoring", "WebView2");
            Directory.CreateDirectory(userDataFolder);

            var environment = await CoreWebView2Environment.CreateAsync(null, userDataFolder);
            await GlobeView.EnsureCoreWebView2Async(environment);

            GlobeView.CoreWebView2.Settings.AreDevToolsEnabled = false;
            GlobeView.CoreWebView2.Settings.IsStatusBarEnabled = false;
            GlobeView.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;

            // Disable WebView2's own page zoom (Ctrl+wheel / pinch): that zoom
            // scales the whole page — including the sidebars — around the
            // cursor position. Only our own JS wheel handler inside the globe
            // area should react to scrolling, zooming the sphere itself around
            // its fixed center.
            GlobeView.CoreWebView2.Settings.IsZoomControlEnabled = false;
            GlobeView.ZoomFactor = 1.0;
            GlobeView.ZoomFactorChanged += (s, args) =>
            {
                if (Math.Abs(GlobeView.ZoomFactor - 1.0) > 0.001)
                {
                    GlobeView.ZoomFactor = 1.0;
                }
            };

            GlobeView.CoreWebView2.WebMessageReceived += CoreWebView2_WebMessageReceived;

            // Article links must open in the user's default browser, never
            // inside this WebView2: a target="_blank" link would otherwise pop
            // up a bare, chrome-less WebView2 window, and a plain link would
            // navigate the globe page itself away.
            GlobeView.CoreWebView2.NewWindowRequested += (s, args) =>
            {
                args.Handled = true;
                OpenInDefaultBrowser(args.Uri);
            };
            GlobeView.CoreWebView2.NavigationStarting += (s, args) =>
            {
                if (args.Uri.StartsWith("http://", StringComparison.OrdinalIgnoreCase) ||
                    args.Uri.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
                {
                    args.Cancel = true;
                    OpenInDefaultBrowser(args.Uri);
                }
            };

            var html = LoadEmbeddedHtml("globe.html");
            GlobeView.NavigateToString(html);

            LoadingPanel.Visibility = Visibility.Collapsed;
            GlobeView.Visibility = Visibility.Visible;
        }

        // globe.html posts window.chrome.webview.postMessage(JSON.stringify({type: "..."}))
        // for actions that need the native side (currently just "openUrl": open a
        // news link in the default browser). Anything we don't recognize is
        // ignored rather than treated as an error, so the page can grow more
        // message types later without this handler needing to reject them.
        private void CoreWebView2_WebMessageReceived(object? sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            string? json;
            try
            {
                json = e.TryGetWebMessageAsString();
            }
            catch (Exception ex)
            {
                Debug.WriteLine("[GMS] Could not read WebView2 message: " + ex.Message);
                AppLog.Warn("Global Monitoring", "Could not read WebView2 message.", ex);
                return;
            }

            if (string.IsNullOrEmpty(json)) return;

            try
            {
                using var doc = JsonDocument.Parse(json);
                var type = doc.RootElement.TryGetProperty("type", out var typeProp) ? typeProp.GetString() : null;
                if (type == "openUrl" && doc.RootElement.TryGetProperty("url", out var urlProp))
                {
                    // "Read full article" / local-news links (globe.html's openExternal()).
                    OpenInDefaultBrowser(urlProp.GetString());
                }
            }
            catch (Exception ex)
            {
                Debug.WriteLine("[GMS] Failed to parse WebView2 message: " + ex.Message);
                AppLog.Warn("Global Monitoring", "Failed to parse WebView2 message: " + json, ex);
            }
        }

        /// <summary>
        /// Opens an http(s) URL in the user's default browser. Anything else
        /// (file:, javascript:, custom protocols) is refused — the URL comes
        /// from external news feeds, so it must never be able to launch
        /// arbitrary local programs via ShellExecute.
        /// </summary>
        private static void OpenInDefaultBrowser(string? url)
        {
            if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) ||
                (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
            {
                AppLog.Warn("Global Monitoring", "Refused to open non-http(s) link: " + url);
                return;
            }
            try
            {
                Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true });
            }
            catch (Exception ex)
            {
                AppLog.Warn("Global Monitoring", "Could not open link in the default browser: " + uri.AbsoluteUri, ex);
            }
        }

        private static string LoadEmbeddedHtml(string resourceFileName)
        {
            var assembly = Assembly.GetExecutingAssembly();
            var resourceName = assembly.GetManifestResourceNames()
                .FirstOrDefault(n => n.EndsWith(resourceFileName, StringComparison.OrdinalIgnoreCase));

            if (resourceName == null)
            {
                return "<html><body style='background:#141414;color:#f2f2f2;font-family:Consolas;padding:20px;'>" +
                       "Globe UI resource not found (" + resourceFileName + ").</body></html>";
            }

            using var stream = assembly.GetManifestResourceStream(resourceName);
            using var reader = new StreamReader(stream!);
            return reader.ReadToEnd();
        }

        private void Window_Closing(object? sender, CancelEventArgs e)
        {
            TryStopBackendHelper();
        }

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton == MouseButtonState.Pressed)
            {
                if (e.ClickCount == 2)
                {
                    WindowState = WindowState == WindowState.Maximized ? WindowState.Normal : WindowState.Maximized;
                    return;
                }

                try { DragMove(); } catch { }
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

        // --- Fullscreen/resize clipping fix (mirrors UFOS.ai/MainWindow.xaml.cs) ---

        private void ApplyScreenSizeSafeguards()
        {
            var workArea = SystemParameters.WorkArea;
            MinWidth = Math.Min(MinWidth, workArea.Width);
            MinHeight = Math.Min(MinHeight, workArea.Height);
            if (Width > workArea.Width) Width = workArea.Width;
            if (Height > workArea.Height) Height = workArea.Height;
        }

        protected override void OnSourceInitialized(EventArgs e)
        {
            base.OnSourceInitialized(e);
            if (PresentationSource.FromVisual(this) is HwndSource hwndSource)
            {
                hwndSource.AddHook(WindowProc);
            }
        }

        private static IntPtr WindowProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
        {
            const int WM_GETMINMAXINFO = 0x0024;
            if (msg == WM_GETMINMAXINFO)
            {
                ConstrainMaximizedBoundsToWorkArea(hwnd, lParam);
                handled = true;
            }
            return IntPtr.Zero;
        }

        private static void ConstrainMaximizedBoundsToWorkArea(IntPtr hwnd, IntPtr lParam)
        {
            var mmi = Marshal.PtrToStructure<MINMAXINFO>(lParam);
            const int MONITOR_DEFAULTTONEAREST = 2;
            var monitor = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
            if (monitor != IntPtr.Zero)
            {
                var monitorInfo = new MONITORINFO { cbSize = Marshal.SizeOf<MONITORINFO>() };
                GetMonitorInfo(monitor, ref monitorInfo);
                var workArea = monitorInfo.rcWork;
                var monitorArea = monitorInfo.rcMonitor;
                mmi.ptMaxPosition.X = Math.Abs(workArea.Left - monitorArea.Left);
                mmi.ptMaxPosition.Y = Math.Abs(workArea.Top - monitorArea.Top);
                mmi.ptMaxSize.X = Math.Abs(workArea.Right - workArea.Left);
                mmi.ptMaxSize.Y = Math.Abs(workArea.Bottom - workArea.Top);
            }
            Marshal.StructureToPtr(mmi, lParam, true);
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct POINT
        {
            public int X;
            public int Y;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct MINMAXINFO
        {
            public POINT ptReserved;
            public POINT ptMaxSize;
            public POINT ptMaxPosition;
            public POINT ptMinTrackSize;
            public POINT ptMaxTrackSize;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct RECT
        {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct MONITORINFO
        {
            public int cbSize;
            public RECT rcMonitor;
            public RECT rcWork;
            public int dwFlags;
        }

        /// <summary>
        /// Ties the backend process tree to this app's lifetime via a Windows
        /// Job Object with KILL_ON_JOB_CLOSE. When this process ends for ANY
        /// reason — normal close, crash, or the Visual Studio debugger's Stop
        /// button (which kills the process without running Window_Closing) —
        /// Windows closes the job handle and terminates everything in the job:
        /// the powershell.exe wrapper and the python.exe it starts (child
        /// processes inherit job membership). 2026-09-24: stopping the
        /// debugger left python.exe running as an orphan with old code.
        /// </summary>
        private static class BackendProcessJob
        {
            private static IntPtr _job = IntPtr.Zero;

            public static void Attach(Process process)
            {
                try
                {
                    if (_job == IntPtr.Zero)
                    {
                        _job = CreateJobObject(IntPtr.Zero, null);
                        if (_job == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());

                        var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
                        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                        var length = Marshal.SizeOf<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>();
                        var ptr = Marshal.AllocHGlobal(length);
                        try
                        {
                            Marshal.StructureToPtr(info, ptr, false);
                            if (!SetInformationJobObject(_job, JobObjectExtendedLimitInformation, ptr, (uint)length))
                                throw new Win32Exception(Marshal.GetLastWin32Error());
                        }
                        finally { Marshal.FreeHGlobal(ptr); }
                    }
                    if (!AssignProcessToJobObject(_job, process.Handle))
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    AppLog.Info("Global Monitoring", "Backend process bound to the app's lifetime (Job Object) — it ends when the app ends.");
                }
                catch (Exception ex)
                {
                    // Not fatal: the explicit Kill in TryStopBackendHelper still covers a normal close.
                    AppLog.Warn("Global Monitoring", "Could not bind backend to the app's lifetime (Job Object); a debugger stop may leave it running.", ex);
                }
            }

            private const int JobObjectExtendedLimitInformation = 9;
            private const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;

            [StructLayout(LayoutKind.Sequential)]
            private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
            {
                public long PerProcessUserTimeLimit;
                public long PerJobUserTimeLimit;
                public uint LimitFlags;
                public UIntPtr MinimumWorkingSetSize;
                public UIntPtr MaximumWorkingSetSize;
                public uint ActiveProcessLimit;
                public UIntPtr Affinity;
                public uint PriorityClass;
                public uint SchedulingClass;
            }

            [StructLayout(LayoutKind.Sequential)]
            private struct IO_COUNTERS
            {
                public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
                public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
            }

            [StructLayout(LayoutKind.Sequential)]
            private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
            {
                public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
                public IO_COUNTERS IoInfo;
                public UIntPtr ProcessMemoryLimit;
                public UIntPtr JobMemoryLimit;
                public UIntPtr PeakProcessMemoryUsed;
                public UIntPtr PeakJobMemoryUsed;
            }

            [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
            private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string? lpName);

            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool SetInformationJobObject(IntPtr hJob, int infoType, IntPtr lpJobObjectInfo, uint cbJobObjectInfoLength);

            [DllImport("kernel32.dll", SetLastError = true)]
            private static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);
        }

        [DllImport("user32.dll")]
        private static extern IntPtr MonitorFromWindow(IntPtr hwnd, int dwFlags);

        [DllImport("user32.dll", CharSet = CharSet.Auto)]
        private static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFO lpmi);
    }
}
