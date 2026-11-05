using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Microsoft.Web.WebView2.Core;
using UFOS.ai.Logging;

namespace UFOS.ai.ModuleTiles
{
    /// <summary>
    /// Takes the "Last view" pictures (TILES_API §4 with the lead's decision §10.2 — NO close delay):
    ///  • every 60 s for each module window that is visible AND active (any window of the module, however it was opened —
    ///    the providers' <see cref="IModuleTilePreview.OwnsWindow"/> decides);
    ///  • at Closing one more picture, only when it is ready within 300 ms — taken synchronously (WPF content via
    ///    RenderTargetBitmap; WebView2 areas from the last periodic capture of that window, since their own capture is async).
    ///    Otherwise the last periodic picture stays. The close itself is never cancelled or delayed beyond that.
    /// What: the window content via RenderTargetBitmap at the window's DPI; every visible WebView2 / WebView2CompositionControl
    /// via CoreWebView2.CapturePreviewAsync, drawn at its place. Then <see cref="TileBlur"/> in memory and only the blurred PNG
    /// is written (<see cref="TileLastViewStore"/>). No picture while the provider reports sandbox data, or when the module
    /// offers no Last view. Failures are logged (System Logs) and never block anything; the old picture stays.
    /// </summary>
    public sealed class TileCaptureService
    {
        private const string LogModule = "Tiles";
        private static readonly TimeSpan Period = TimeSpan.FromSeconds(60);
        private static readonly TimeSpan ClosingBudget = TimeSpan.FromMilliseconds(300);
        private static bool _classHandlerRegistered;
        private static TileCaptureService? _instance;

        private readonly IReadOnlyList<IModuleTilePreview> _providers;
        private readonly DispatcherTimer _timer;
        private readonly ConditionalWeakTable<Window, WindowCaptureState> _windows = new();

        /// <summary>Raised on the UI thread after a picture of the module was stored.</summary>
        public event Action<string>? PictureStored;

        private sealed class WindowCaptureState
        {
            public bool Hooked;
            public bool Busy;
            public bool? LastSandbox;
            public DateTime LastCaptureUtc;
            /// <summary>WebView2 pictures of the last periodic capture (host element → decoded PNG), reused at Closing.</summary>
            public List<(WeakReference<FrameworkElement> Host, BitmapSource Picture)> WebViewPictures = new();
        }

        public TileCaptureService(IReadOnlyList<IModuleTilePreview> providers)
        {
            _providers = providers ?? throw new ArgumentNullException(nameof(providers));
            _timer = new DispatcherTimer(DispatcherPriority.Background) { Interval = Period };
            _timer.Tick += async (_, _) => await CaptureActiveWindowsAsync();
        }

        /// <summary>Starts the periodic capture and hooks Closing of every module window (also windows opened later).</summary>
        public void Start()
        {
            _instance = this;
            if (!_classHandlerRegistered)
            {
                // every Window raises Loaded once — the one place that sees windows opened from anywhere (tile, card, toast…)
                EventManager.RegisterClassHandler(typeof(Window), FrameworkElement.LoadedEvent, new RoutedEventHandler(OnAnyWindowLoaded));
                _classHandlerRegistered = true;
            }
            foreach (Window w in Application.Current.Windows) Hook(w);
            _timer.Start();
        }

        public void Stop() => _timer.Stop();

        private static void OnAnyWindowLoaded(object sender, RoutedEventArgs e)
        {
            if (sender is Window w && _instance is TileCaptureService s) s.Hook(w);
        }

        private IModuleTilePreview? ProviderOf(Window w)
        {
            foreach (var p in _providers)
            {
                try
                {
                    if (p.OwnsWindow(w)) return p;
                }
                catch (Exception ex)
                {
                    AppLog.Warn(LogModule, "Tile preview " + p.ModuleId + ": OwnsWindow failed", ex);
                }
            }
            return null;
        }

        private void Hook(Window w)
        {
            if (ProviderOf(w) == null) return;
            var st = _windows.GetValue(w, _ => new WindowCaptureState());
            if (st.Hooked) return;
            st.Hooked = true;
            w.Closing += Window_Closing;
        }

        private async Task CaptureActiveWindowsAsync()
        {
            foreach (var w in Application.Current.Windows.OfType<Window>().ToList())
            {
                if (!w.IsVisible || !w.IsActive || w.WindowState == System.Windows.WindowState.Minimized) continue;
                var p = ProviderOf(w);
                if (p == null || p.LastViewUnavailableReason != null) continue;
                Hook(w);
                var st = _windows.GetValue(w, _ => new WindowCaptureState());
                if (st.Busy || DateTime.UtcNow - st.LastCaptureUtc < Period - TimeSpan.FromSeconds(5)) continue;
                if (IsSandbox(p, w, st)) continue;
                st.Busy = true;
                try
                {
                    var webViews = await CaptureWebViewsAsync(w);
                    st.WebViewPictures = webViews.Select(x => (new WeakReference<FrameworkElement>(x.Host), x.Picture)).ToList();
                    var composite = Composite(w, webViews);
                    if (composite != null)
                    {
                        st.LastCaptureUtc = DateTime.UtcNow;
                        await BlurAndStoreAsync(p.ModuleId, composite);
                    }
                }
                catch (Exception ex)
                {
                    AppLog.Warn(LogModule, "Last view of " + p.DisplayName + " could not be taken — the previous picture stays", ex);
                }
                finally
                {
                    st.Busy = false;
                }
            }
        }

        private bool IsSandbox(IModuleTilePreview p, Window w, WindowCaptureState st)
        {
            bool sandbox;
            try { sandbox = p.IsSandbox(w); }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Tile preview " + p.ModuleId + ": IsSandbox failed — no picture", ex);
                return true;
            }
            if (sandbox && st.LastSandbox != true) AppLog.Info(LogModule, p.DisplayName + " shows sandbox data — no Last view picture is taken while it does.");
            st.LastSandbox = sandbox;
            return sandbox;
        }

        private void Window_Closing(object? sender, System.ComponentModel.CancelEventArgs e)
        {
            if (e.Cancel || sender is not Window w || !w.IsVisible || w.WindowState == System.Windows.WindowState.Minimized) return;
            var p = ProviderOf(w);
            if (p == null || p.LastViewUnavailableReason != null) return;
            var st = _windows.GetValue(w, _ => new WindowCaptureState());
            if (st.Busy || IsSandbox(p, w, st)) return;
            var sw = Stopwatch.StartNew();
            try
            {
                var webViews = new List<(FrameworkElement Host, BitmapSource Picture)>();
                foreach (var (hostRef, pic) in st.WebViewPictures)
                    if (hostRef.TryGetTarget(out var host) && host.IsVisible) webViews.Add((host, pic));
                // a WebView without a periodic picture yet would be a blank hole — keep the last periodic picture then
                if (FindWebViews(w).Count > webViews.Count) return;
                var composite = Composite(w, webViews);
                if (composite == null) return;
                if (sw.Elapsed > ClosingBudget)
                {
                    AppLog.Info(LogModule, "Closing picture of " + p.DisplayName + " took " + sw.ElapsedMilliseconds + " ms (> 300 ms) — the last periodic picture stays.");
                    return;
                }
                _ = BlurAndStoreAsync(p.ModuleId, composite);   // blur + write after the close (the frozen bitmap stays in memory only)
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Closing picture of " + p.DisplayName + " could not be taken — the previous picture stays", ex);
            }
        }

        private async Task BlurAndStoreAsync(string moduleId, BitmapSource composite)
        {
            var takenAt = DateTimeOffset.UtcNow;
            try
            {
                await Task.Run(() =>
                {
                    var png = TileBlur.BlurToPng(composite);
                    TileLastViewStore.Save(moduleId, png, takenAt);
                });
                PictureStored?.Invoke(moduleId);
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Last view of " + moduleId + " could not be stored — the previous picture stays", ex);
            }
        }

        // ------------------------------------------------------------------ rendering

        /// <summary>Window content + WebView pictures on one frozen bitmap (physical pixels), or null when the window has no size.</summary>
        private static BitmapSource? Composite(Window w, IReadOnlyList<(FrameworkElement Host, BitmapSource Picture)> webViews)
        {
            if (w.Content is not FrameworkElement root || root.ActualWidth < 20 || root.ActualHeight < 20) return null;
            var dpi = VisualTreeHelper.GetDpi(root);
            var pxW = (int)Math.Round(root.ActualWidth * dpi.DpiScaleX);
            var pxH = (int)Math.Round(root.ActualHeight * dpi.DpiScaleY);
            var content = new RenderTargetBitmap(pxW, pxH, dpi.PixelsPerInchX, dpi.PixelsPerInchY, PixelFormats.Pbgra32);
            content.Render(root);
            if (webViews.Count == 0)
            {
                content.Freeze();
                return content;
            }
            var dv = new DrawingVisual();
            using (var dc = dv.RenderOpen())
            {
                dc.DrawImage(content, new Rect(0, 0, root.ActualWidth, root.ActualHeight));
                foreach (var (host, pic) in webViews)
                {
                    if (!host.IsVisible || !root.IsAncestorOf(host)) continue;
                    var topLeft = host.TransformToAncestor(root).Transform(new Point(0, 0));
                    dc.DrawImage(pic, new Rect(topLeft, new Size(host.ActualWidth, host.ActualHeight)));
                }
            }
            var all = new RenderTargetBitmap(pxW, pxH, dpi.PixelsPerInchX, dpi.PixelsPerInchY, PixelFormats.Pbgra32);
            all.Render(dv);
            all.Freeze();
            return all;
        }

        /// <summary>The visible WebView2 controls of the window (HwndHost WebView2 and WebView2CompositionControl).</summary>
        private static List<(FrameworkElement Host, CoreWebView2 Core)> FindWebViews(Window w)
        {
            var list = new List<(FrameworkElement, CoreWebView2)>();
            void Walk(DependencyObject d)
            {
                var n = VisualTreeHelper.GetChildrenCount(d);
                for (var i = 0; i < n; i++)
                {
                    var c = VisualTreeHelper.GetChild(d, i);
                    switch (c)
                    {
                        case Microsoft.Web.WebView2.Wpf.WebView2 hwv when hwv.IsVisible && hwv.CoreWebView2 != null:
                            list.Add((hwv, hwv.CoreWebView2));
                            continue;
                        case Microsoft.Web.WebView2.Wpf.WebView2CompositionControl cwv when cwv.IsVisible && cwv.CoreWebView2 != null:
                            list.Add((cwv, cwv.CoreWebView2));
                            continue;
                    }
                    Walk(c);
                }
            }
            Walk(w);
            return list;
        }

        private static async Task<List<(FrameworkElement Host, BitmapSource Picture)>> CaptureWebViewsAsync(Window w)
        {
            var result = new List<(FrameworkElement, BitmapSource)>();
            foreach (var (host, core) in FindWebViews(w))
            {
                using var ms = new MemoryStream();
                await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, ms);
                ms.Position = 0;
                var img = new BitmapImage();
                img.BeginInit();
                img.CacheOption = BitmapCacheOption.OnLoad;   // decoded in memory; the stream is disposed right after
                img.StreamSource = ms;
                img.EndInit();
                img.Freeze();
                result.Add((host, img));
            }
            return result;
        }
    }
}
