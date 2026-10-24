using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Input;
using System.Windows.Interop;
using Microsoft.Web.WebView2.Core;
using UFOS.ai.Logging;
using UFOS.ai.Shared;

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

        private static string? _backendDir;

        /// <summary>The backend folder (…\Global Monitoring\backend): where it publishes its API session token.</summary>
        private static string? BackendDir
        {
            get
            {
                if (_backendDir != null) return _backendDir;
                var script = FindBackendScript(out _);
                _backendDir = script == null ? null : Path.GetDirectoryName(script);
                return _backendDir;
            }
        }
        private bool _weStartedBackend;
        private RotatingLogFile? _backendLogWriter; // size-capped: 5 MB, 3 old files (Shared/RotatingLogFile.cs, S10-A2 L6)
        // Set in Window_Closing: a backend start still waiting for the Python environment
        // must not launch the process after the window was closed.
        private volatile bool _windowClosing;

        // --- Globe page (WebView2) hardening, security audit S10-A2 M4 (ChartHost pattern, adapted) ---
        // globe.html is loaded with NavigateToString: location about:blank, opaque origin (sent as Origin "null" —
        // which is why backend/config.py's DEFAULT_ALLOWED_ORIGINS still lists "null"). Only THAT navigation is
        // allowed (_globeNavigationPending is set right before NavigateToString and consumed by its
        // NavigationStarting); every other top-level navigation and every frame navigation is cancelled, and the API
        // token / host messages are honoured only while that document is the one in the view (_globePageLive).
        private CoreWebView2Environment? _globeEnvironment;
        private bool _globeNavigationPending;
        private ulong _globeNavigationId;
        private bool _globePageLive;
        private readonly HashSet<string> _globeLoggedOnce = new(StringComparer.Ordinal);

        // The only remote resources the page may load (same values as globe.html's BACKEND_HTTP_URL /
        // BACKEND_WS_URL / NE_SOURCE_URL and its CSP connect-src — change them together).
        private const int GlobeBackendHttpPort = 8768;
        private const int GlobeBackendWsPort = 8767;
        private const string NaturalEarthUrl = "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/land-110m.json";

        // Natural Earth source, decided once per page load (ResolveNaturalEarthSource) and never mixed:
        // Vendored = vendor/land-110m.json embedded at build time and matching vendor/MANIFEST (sha256) — the CDN is not
        // allowed at all (CSP + resource allow-list); Cdn = no vendored copy in this build — the pinned CDN file only;
        // VendorRejected = a vendored copy is present but fails its check — hand-drawn outlines, the CDN is NOT a substitute.
        private enum NaturalEarthSource { Cdn, Vendored, VendorRejected }
        private const string NaturalEarthVersion = "2.0.2";
        private const string NaturalEarthResourcePrefix = "GlobalMonitoring.vendor."; // LogicalName in GlobalMonitoring.csproj
        private NaturalEarthSource _neSource = NaturalEarthSource.Cdn;
        private string? _neVendoredJson;
        private string? _neProblem;
        // globe.html's CSP connect-src placeholder: the pinned CDN URL in Cdn mode, removed otherwise. Left unreplaced it
        // is an unknown quoted keyword, which the CSP ignores (= no CDN access).
        private const string GlobeNaturalEarthConnectPlaceholder = "'GM_NE_CONNECT_SRC'";

        // globe.html's CSP allows exactly one inline script by its sha256; the hash is computed when the page is
        // loaded (PrepareGlobeHtml), so editing the script never needs a hand-updated hash. Left unreplaced, the
        // placeholder is an invalid hash and the CSP blocks the script (fails closed).
        private const string GlobeScriptHashPlaceholder = "'sha256-GM_INLINE_SCRIPT_HASH'";

        // Insider Trades and Macro Indicators are no longer part of this module:
        // moved to the Hedge Fund module's Financial Newsroom on 2026-09-24
        // (HedgeFund/Newsroom/, served by HedgeFund/backend).

        public MainWindow()
        {
            InitializeComponent();
            ApplyScreenSizeSafeguards();
            // user request 2026-10-23: the main content windows start maximized (within the work area: WM_GETMINMAXINFO hook);
            // WPF raises no StateChanged for the initial state, so the maximized look (square corners) is applied once here
            WindowMaximize.StartMaximized(this);
            Window_StateChanged(this, EventArgs.Empty);
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

            // Python environments are prepared once at app start (Shared/PythonEnvironment.cs);
            // wait until this backend's .venv is ready (standalone GlobalMonitoring.exe: prepare
            // it here). The script then runs with -SkipInstall and never touches pip.
            var envOk = await PythonEnvironment.WaitForModuleAsync(Path.GetDirectoryName(script)!, "Global Monitoring",
                PythonEnvironment.ProgressOf(text =>
                {
                    if (!_windowClosing) ShowBackendEnvStatus("Python environment: " + text);
                }));
            if (_windowClosing) return;
            if (!envOk)
            {
                AppLog.Error("Global Monitoring", "Python environment setup failed — the backend was not started.");
                ShowBackendEnvStatus("Python environment setup failed — see Backend Log");
                return;
            }
            ShowBackendEnvStatus(null);

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
                _backendLogWriter = new RotatingLogFile(logPath);
                _backendLogWriter.WriteLine($"--- {DateTime.Now:yyyy-MM-dd HH:mm:ss} starting backend via run_backend.ps1 ---");
            }
            catch (Exception ex)
            {
                Debug.WriteLine("[GMS Backend] could not open process.log: " + ex.Message);
                _backendLogWriter = null;
            }

            var psi = new ProcessStartInfo
            {
                // Absolute Windows PowerShell path + why Bypass stays for this .ps1: Shared/WindowsPowerShell.cs (S10-A2 L4).
                FileName = WindowsPowerShell.ExePath,
                Arguments = WindowsPowerShell.ScriptArguments(script, "-SkipInstall"),
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
                HandleEnvMarker(args.Data);
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
                ShowBackendEnvStatus(null);
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
        /// run_backend.ps1 prints "UFOS_ENV: &lt;state&gt;" lines while it prepares the Python
        /// environment (protocol in the script header): waiting / installing &lt;reason&gt; /
        /// upgrading / changed &lt;n&gt; / failed &lt;reason&gt; / ready. Show a hint in the title
        /// bar while packages are being installed, so a slow first start isn't a mystery.
        /// </summary>
        private void HandleEnvMarker(string line)
        {
            const string prefix = "UFOS_ENV:";
            if (!line.StartsWith(prefix, StringComparison.Ordinal)) return;
            var marker = line.Substring(prefix.Length).Trim();
            if (marker.StartsWith("waiting", StringComparison.OrdinalIgnoreCase))
                ShowBackendEnvStatus("Waiting for the Python package update to finish…");
            else if (marker.StartsWith("installing", StringComparison.OrdinalIgnoreCase) ||
                     marker.StartsWith("upgrading", StringComparison.OrdinalIgnoreCase))
                ShowBackendEnvStatus("Installing Python packages (first start or requirements changed)…");
            else if (marker.StartsWith("failed", StringComparison.OrdinalIgnoreCase))
            {
                const string failed = "Python package install failed — starting with the installed packages (see Backend Log)";
                ShowBackendEnvStatus(failed);
                _ = ClearBackendEnvStatusLaterAsync(failed);
            }
            else if (marker.StartsWith("ready", StringComparison.OrdinalIgnoreCase))
                ShowBackendEnvStatus(null);
        }

        private void ShowBackendEnvStatus(string? text)
        {
            try
            {
                Dispatcher.BeginInvoke(new Action(() =>
                {
                    BackendEnvStatusText.Text = text ?? string.Empty;
                    BackendEnvStatusText.Visibility = text == null ? Visibility.Collapsed : Visibility.Visible;
                }));
            }
            catch { }
        }

        private async Task ClearBackendEnvStatusLaterAsync(string text)
        {
            await Task.Delay(TimeSpan.FromSeconds(30));
            try
            {
                await Dispatcher.InvokeAsync(() =>
                {
                    if (BackendEnvStatusText.Text == text) ShowBackendEnvStatus(null);
                });
            }
            catch { }
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
                // /shutdown needs the running backend's session token (Shared/BackendAuth.cs).
                using var client = BackendAuth.CreateClient(BackendDir, TimeSpan.FromSeconds(2));
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
            // S10-A2 M4: no alert/confirm/prompt boxes, no host objects, no autofill / password saving, no swipe
            // navigation, and no browser accelerator keys (F5 / Ctrl+R would reload to a blank page, Ctrl+P, Ctrl+F …).
            GlobeView.CoreWebView2.Settings.AreDefaultScriptDialogsEnabled = false;
            GlobeView.CoreWebView2.Settings.AreHostObjectsAllowed = false;
            GlobeView.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = false;
            GlobeView.CoreWebView2.Settings.IsPasswordAutosaveEnabled = false;
            GlobeView.CoreWebView2.Settings.IsGeneralAutofillEnabled = false;
            GlobeView.CoreWebView2.Settings.IsSwipeNavigationEnabled = false;
            GlobeView.CoreWebView2.Settings.IsPinchZoomEnabled = false;

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
            // S10-A2 M4: navigation allow-list (only our NavigateToString page), no frames, no downloads, no
            // permissions, and no request to anything but the local backend and the pinned Natural Earth file.
            _globeEnvironment = environment;
            GlobeView.CoreWebView2.NavigationStarting += GlobeCore_NavigationStarting;
            GlobeView.CoreWebView2.ContentLoading += GlobeCore_ContentLoading;
            GlobeView.CoreWebView2.NavigationCompleted += GlobeCore_NavigationCompleted;
            GlobeView.CoreWebView2.FrameNavigationStarting += GlobeCore_FrameNavigationStarting;
            GlobeView.CoreWebView2.DownloadStarting += (s, args) =>
            {
                args.Cancel = true;
                args.Handled = true;
                LogGlobeOnce("download", "Global Monitoring: blocked a download started by the globe page.");
            };
            GlobeView.CoreWebView2.PermissionRequested += (s, args) =>
            {
                args.State = CoreWebView2PermissionState.Deny;
            };
            GlobeView.CoreWebView2.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All);
            GlobeView.CoreWebView2.WebResourceRequested += GlobeCore_WebResourceRequested;

            ResolveNaturalEarthSource();
            var html = PrepareGlobeHtml(LoadEmbeddedHtml("globe.html"), _neSource == NaturalEarthSource.Cdn);
            _globeNavigationPending = true;
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
            // S10-A2 M4: allow-list, not deny-list — only our own globe document may talk to the host (and so get
            // the API token below): its NavigateToString navigation must be the one currently shown.
            if (!_globePageLive || !IsGlobePageUri(e.Source))
            {
                AppLog.Warn("Global Monitoring", "Ignored a WebView2 message from an unexpected source: " + ClipUri(e.Source));
                return;
            }

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
                else if (type == "getApiToken")
                {
                    // globe.html needs the backend's current session token for its fetch()
                    // and WebSocket calls (backend/api_auth.py). Only our own embedded page
                    // gets it: the source allow-list at the top of this handler.
                    var token = BackendAuth.ReadToken(BackendDir) ?? string.Empty;
                    GlobeView.CoreWebView2?.PostWebMessageAsJson(
                        JsonSerializer.Serialize(new { type = "apiToken", token }));
                }
                else if (type == "getNaturalEarth")
                {
                    // The page asks which coastline source to use (S10-A2 M4): the vendored JSON itself, the pinned
                    // CDN file, or neither (rejected vendored copy) — with the reason, shown on the page.
                    var source = _neSource switch
                    {
                        NaturalEarthSource.Vendored => "vendored",
                        NaturalEarthSource.Cdn => "cdn",
                        _ => "rejected",
                    };
                    GlobeView.CoreWebView2?.PostWebMessageAsJson(JsonSerializer.Serialize(new
                    {
                        type = "naturalEarth",
                        source,
                        version = NaturalEarthVersion,
                        data = _neSource == NaturalEarthSource.Vendored ? _neVendoredJson : null,
                        reason = _neSource == NaturalEarthSource.VendorRejected ? _neProblem : null,
                    }));
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

        // ---- Globe page navigation / resource allow-list (S10-A2 M4) ----

        /// <summary>The URI forms a NavigateToString document is reported with (about:blank, or its data: URL).</summary>
        private static bool IsGlobePageUri(string? uri) =>
            uri != null && (string.Equals(uri, "about:blank", StringComparison.OrdinalIgnoreCase)
                            || uri.StartsWith("data:text/html", StringComparison.OrdinalIgnoreCase));

        private static string ClipUri(string? uri)
        {
            var s = uri ?? "(none)";
            return s.Length <= 120 ? s : s.Substring(0, 120) + "…";
        }

        private void LogGlobeOnce(string key, string message)
        {
            if (_globeLoggedOnce.Add(key)) AppLog.Warn("Global Monitoring", message);
        }

        private void GlobeCore_NavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs e)
        {
            if (_globeNavigationPending && IsGlobePageUri(e.Uri))
            {
                _globeNavigationPending = false;
                _globeNavigationId = e.NavigationId;
                _globePageLive = false; // becomes true when this navigation's content starts loading
                return;
            }
            e.Cancel = true;
            // WebView2 ignores a cancel for about:blank: if one still goes through, our page is gone — no more token.
            if (IsGlobePageUri(e.Uri)) _globePageLive = false;
            // A link the user clicked opens in the default browser (http(s) only, OpenInDefaultBrowser); anything
            // else (file:, data:, script-initiated navigations, other schemes) is only blocked.
            if (e.IsUserInitiated && Uri.TryCreate(e.Uri, UriKind.Absolute, out var u)
                && (u.Scheme == Uri.UriSchemeHttp || u.Scheme == Uri.UriSchemeHttps))
            {
                OpenInDefaultBrowser(e.Uri);
                return;
            }
            AppLog.Warn("Global Monitoring", "Blocked a navigation away from the globe page: " + ClipUri(e.Uri));
        }

        private void GlobeCore_ContentLoading(object? sender, CoreWebView2ContentLoadingEventArgs e)
        {
            if (e.NavigationId == _globeNavigationId && !e.IsErrorPage) _globePageLive = true;
        }

        private void GlobeCore_NavigationCompleted(object? sender, CoreWebView2NavigationCompletedEventArgs e)
        {
            if (e.NavigationId != _globeNavigationId || e.IsSuccess) return;
            _globePageLive = false;
            AppLog.Warn("Global Monitoring", "The globe page failed to load (WebView2: " + e.WebErrorStatus + ").");
        }

        private void GlobeCore_FrameNavigationStarting(object? sender, CoreWebView2NavigationStartingEventArgs e)
        {
            e.Cancel = true; // the globe page has no frames (its CSP says frame-src 'none' as well)
            LogGlobeOnce("frame", "Global Monitoring: blocked a frame navigation in the globe page (" + ClipUri(e.Uri) + ").");
        }

        private void GlobeCore_WebResourceRequested(object? sender, CoreWebView2WebResourceRequestedEventArgs e)
        {
            // Defence in depth next to the page's CSP (connect-src): the page document itself, the local backend and
            // the pinned Natural Earth file — everything else gets a 403.
            var uri = e.Request.Uri ?? string.Empty;
            if (IsGlobePageUri(uri) || IsAllowedGlobeResource(uri, _neSource == NaturalEarthSource.Cdn)) return;
            if (_globeEnvironment != null)
                e.Response = _globeEnvironment.CreateWebResourceResponse(null, 403, "Forbidden", "");
            LogGlobeOnce("res:" + (Uri.TryCreate(uri, UriKind.Absolute, out var u) ? u.Scheme + "://" + u.Host : "?"),
                         "Global Monitoring: blocked a globe page request to " + ClipUri(uri) + ".");
        }

        private static bool IsAllowedGlobeResource(string uri, bool naturalEarthFromCdn)
        {
            if (naturalEarthFromCdn && string.Equals(uri, NaturalEarthUrl, StringComparison.Ordinal)) return true;
            if (!Uri.TryCreate(uri, UriKind.Absolute, out var u) || u.Host != "127.0.0.1" || !string.IsNullOrEmpty(u.UserInfo))
                return false;
            return (u.Scheme == Uri.UriSchemeHttp && u.Port == GlobeBackendHttpPort)
                   || (u.Scheme == "ws" && u.Port == GlobeBackendWsPort);
        }

        /// <summary>
        /// Decides the Natural Earth source for this page load (see <see cref="NaturalEarthSource"/>) and logs which one is
        /// used. Vendored files are embedded at build time only when tools/fetch_world_atlas.py has written them.
        /// </summary>
        private void ResolveNaturalEarthSource()
        {
            _neVendoredJson = null;
            _neProblem = null;
            var assembly = Assembly.GetExecutingAssembly();
            var names = new HashSet<string>(assembly.GetManifestResourceNames(), StringComparer.Ordinal);
            var jsonName = NaturalEarthResourcePrefix + "land-110m.json";
            var manifestName = NaturalEarthResourcePrefix + "MANIFEST";
            var hasJson = names.Contains(jsonName);
            var hasManifest = names.Contains(manifestName);
            if (!hasJson && !hasManifest)
            {
                _neSource = NaturalEarthSource.Cdn;
                AppLog.Info("Global Monitoring", "Natural Earth coastlines: pinned CDN file " + NaturalEarthUrl +
                            " (no vendored copy in this build — run Global Monitoring/tools/fetch_world_atlas.py, then rebuild).");
                return;
            }

            string? problem = null;
            string? json = null;
            try
            {
                if (!hasJson) problem = "vendor/land-110m.json missing (MANIFEST present)";
                else if (!hasManifest) problem = "vendor/MANIFEST missing (land-110m.json present)";
                else if (!names.Any(n => n.StartsWith(NaturalEarthResourcePrefix + "LICENSE", StringComparison.Ordinal)))
                    problem = "vendor licence file missing";
                else
                {
                    var bytes = ReadResourceBytes(assembly, jsonName, 5 * 1024 * 1024);
                    var manifest = Encoding.UTF8.GetString(ReadResourceBytes(assembly, manifestName, 64 * 1024));
                    var line = manifest.Split('\n')
                        .Select(l => l.Trim())
                        .Where(l => l.Length > 0 && !l.StartsWith("#", StringComparison.Ordinal))
                        .Select(l => l.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries))
                        .FirstOrDefault(t => t.Length >= 5 && t[0] == "land-110m.json");
                    if (bytes == null) problem = "land-110m.json larger than 5 MB";
                    else if (line == null) problem = "MANIFEST has no land-110m.json line";
                    else if (line[1] != NaturalEarthVersion) problem = "MANIFEST lists version " + line[1] + ", expected " + NaturalEarthVersion;
                    else if (!string.Equals(Convert.ToHexString(SHA256.HashData(bytes)), line[3], StringComparison.OrdinalIgnoreCase))
                        problem = "land-110m.json does not match its sha256 in MANIFEST";
                    else json = Encoding.UTF8.GetString(bytes);
                }
            }
            catch (Exception ex)
            {
                problem = "could not read the vendored files (" + ex.GetType().Name + ")";
            }

            if (problem != null || json == null)
            {
                _neSource = NaturalEarthSource.VendorRejected;
                _neProblem = problem ?? "unknown problem";
                AppLog.Error("Global Monitoring", "Natural Earth coastlines: vendored copy rejected (" + _neProblem +
                             ") — the globe shows the hand-drawn outlines; the CDN is NOT used as a substitute. " +
                             "Re-run Global Monitoring/tools/fetch_world_atlas.py and rebuild.");
                return;
            }
            _neSource = NaturalEarthSource.Vendored;
            _neVendoredJson = json;
            AppLog.Info("Global Monitoring", "Natural Earth coastlines: vendored world-atlas@" + NaturalEarthVersion +
                        " (land-110m.json, sha256 checked against vendor/MANIFEST); the CDN is not contacted.");
        }

        /// <summary>The resource's bytes, or null when it is larger than <paramref name="limit"/>.</summary>
        private static byte[]? ReadResourceBytes(Assembly assembly, string name, int limit)
        {
            using var stream = assembly.GetManifestResourceStream(name)
                               ?? throw new InvalidOperationException("resource " + name + " not found");
            using var buffer = new MemoryStream();
            var chunk = new byte[81920];
            int read;
            while ((read = stream.Read(chunk, 0, chunk.Length)) > 0)
            {
                buffer.Write(chunk, 0, read);
                if (buffer.Length > limit) return null;
            }
            return buffer.ToArray();
        }

        /// <summary>
        /// Puts the sha256 of globe.html's single inline script into its CSP (placeholder <see cref="GlobeScriptHashPlaceholder"/>).
        /// Line endings are normalised to LF first, as the HTML parser does, so the hash matches the parsed script text.
        /// The Natural Earth connect-src placeholder becomes the pinned CDN URL only when <paramref name="naturalEarthFromCdn"/>.
        /// A page without the placeholder and without a script (the "resource not found" page) is returned as it is;
        /// anything else unexpected becomes a visible error page — never a page without its CSP.
        /// </summary>
        private static string PrepareGlobeHtml(string html, bool naturalEarthFromCdn)
        {
            html = html.Replace("\r\n", "\n").Replace('\r', '\n');
            var placeholderAt = html.IndexOf(GlobeScriptHashPlaceholder, StringComparison.Ordinal);
            var open = html.IndexOf("<script>", StringComparison.Ordinal);
            if (placeholderAt < 0 && html.IndexOf("<script", StringComparison.OrdinalIgnoreCase) < 0) return html;

            var close = open < 0 ? -1 : html.IndexOf("</script>", open, StringComparison.Ordinal);
            var connectAt = html.IndexOf(GlobeNaturalEarthConnectPlaceholder, StringComparison.Ordinal);
            var valid = placeholderAt >= 0
                        && html.IndexOf(GlobeScriptHashPlaceholder, placeholderAt + 1, StringComparison.Ordinal) < 0
                        && connectAt >= 0
                        && html.IndexOf(GlobeNaturalEarthConnectPlaceholder, connectAt + 1, StringComparison.Ordinal) < 0
                        && open >= 0 && close > open
                        && html.IndexOf("<script", open + 1, StringComparison.OrdinalIgnoreCase) < 0;
            if (!valid)
            {
                AppLog.Error("Global Monitoring", "globe.html: expected exactly one inline <script> block and one CSP hash placeholder — page not loaded.");
                return "<html><body style='background:#141414;color:#f2f2f2;font-family:Consolas;padding:20px;'>" +
                       "Globe UI could not be prepared: globe.html must contain exactly one inline &lt;script&gt; block and one each of " +
                       "the CSP script-hash and Natural Earth connect-src placeholders. Rebuild the app from an unmodified globe.html.</body></html>";
            }

            var script = html.Substring(open + "<script>".Length, close - open - "<script>".Length);
            var hash = Convert.ToBase64String(SHA256.HashData(Encoding.UTF8.GetBytes(script)));
            return html.Replace(GlobeScriptHashPlaceholder, "'sha256-" + hash + "'")
                       .Replace(GlobeNaturalEarthConnectPlaceholder, naturalEarthFromCdn ? NaturalEarthUrl : "");
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
            _windowClosing = true;
            TryStopBackendHelper();
        }

        // Title bar (borderless window, conventions "Borderless windows"): double-click toggles maximize;
        // dragging a MAXIMIZED window restores it only once the mouse actually moves
        // with the button held (a plain click does nothing), keeps the cursor at its
        // relative spot on the title bar, then drags. Same pattern as
        // HedgeFund/HedgeFundWindow.xaml.cs (TitleBar_MouseMove).
        private bool _restoreOnDrag;
        private System.Windows.Point _restoreDragStart;

        private void TitleBar_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
        {
            if (e.LeftButton != MouseButtonState.Pressed) return;
            if (e.ClickCount == 2)
            {
                _restoreOnDrag = false;
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
            var toDip = PresentationSource.FromVisual(this)?.CompositionTarget?.TransformFromDevice
                        ?? System.Windows.Media.Matrix.Identity;
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

        // Maximized: square corners. The WM_GETMINMAXINFO hook (ConstrainMaximizedBoundsToWorkArea) already fits
        // the maximized window to the work area, and a transparent window has no resize frame
        // hanging off the screen, so no extra padding is needed.
        private void Window_StateChanged(object? sender, EventArgs e)
        {
            var maximized = WindowState == WindowState.Maximized;
            RootBorder.CornerRadius = maximized ? new CornerRadius(0) : new CornerRadius(8);
            TitleBarBorder.CornerRadius = maximized ? new CornerRadius(0) : new CornerRadius(8, 8, 0, 0);
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
