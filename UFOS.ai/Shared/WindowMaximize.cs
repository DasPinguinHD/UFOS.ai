using System;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;

namespace UFOS.ai.Shared
{
    /// <summary>
    /// Borderless windows (WindowStyle=None + AllowsTransparency) and "start maximized" (user request 2026-10-23: the main content
    /// windows open maximized; settings windows, log windows and small dialogs do not).
    /// <para>
    /// <see cref="ConstrainToWorkArea"/>: Windows sizes a maximized borderless window to the raw monitor rectangle (it covers the
    /// taskbar and its hidden resize frame hangs off the screen). The WM_GETMINMAXINFO hook limits the maximized bounds to the WORK
    /// AREA of the monitor the window is on (multi-monitor: the nearest monitor), so the taskbar stays visible and no padding is
    /// needed. Same hook as MainWindow / Global Monitoring / LiveStreamAgent, shared here (linked into every project via
    /// Shared/*.cs).
    /// </para>
    /// <para>
    /// <see cref="StartMaximized"/>: sets WindowState=Maximized BEFORE the first Show, so the window maximizes on the monitor of its
    /// startup location (owner / screen centre) and its restore bounds are the declared Width × Height at that location — a
    /// sensible size and position on "restore", never 0 × 0 or off-screen. The caller re-applies its maximized look (square corners)
    /// once, because WPF raises no StateChanged for the initial state.
    /// </para>
    /// </summary>
    internal static class WindowMaximize
    {
        public static void StartMaximized(Window window)
        {
            window.WindowState = WindowState.Maximized;
        }

        /// <summary>Hooks WM_GETMINMAXINFO (call in the constructor; works also when the handle already exists).</summary>
        public static void ConstrainToWorkArea(Window window)
        {
            var helper = new WindowInteropHelper(window);
            if (helper.Handle != IntPtr.Zero) AddHook(helper.Handle);
            else window.SourceInitialized += (_, _) => AddHook(new WindowInteropHelper(window).Handle);
        }

        private static void AddHook(IntPtr hwnd)
        {
            if (hwnd == IntPtr.Zero) return;
            HwndSource.FromHwnd(hwnd)?.AddHook(WindowProc);
        }

        private static IntPtr WindowProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
        {
            const int WM_GETMINMAXINFO = 0x0024;
            if (msg == WM_GETMINMAXINFO && ConstrainMaximizedBoundsToWorkArea(hwnd, lParam)) handled = true;
            return IntPtr.Zero;
        }

        private static bool ConstrainMaximizedBoundsToWorkArea(IntPtr hwnd, IntPtr lParam)
        {
            const int MONITOR_DEFAULTTONEAREST = 2;
            var monitor = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST);
            if (monitor == IntPtr.Zero) return false;
            var monitorInfo = new MONITORINFO { cbSize = Marshal.SizeOf<MONITORINFO>() };
            if (!GetMonitorInfo(monitor, ref monitorInfo)) return false;

            var mmi = Marshal.PtrToStructure<MINMAXINFO>(lParam);
            var workArea = monitorInfo.rcWork;
            var monitorArea = monitorInfo.rcMonitor;
            mmi.ptMaxPosition.X = Math.Abs(workArea.Left - monitorArea.Left);
            mmi.ptMaxPosition.Y = Math.Abs(workArea.Top - monitorArea.Top);
            mmi.ptMaxSize.X = Math.Abs(workArea.Right - workArea.Left);
            mmi.ptMaxSize.Y = Math.Abs(workArea.Bottom - workArea.Top);
            Marshal.StructureToPtr(mmi, lParam, true);
            return true;
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

        [DllImport("user32.dll")]
        private static extern IntPtr MonitorFromWindow(IntPtr handle, int flags);

        [DllImport("user32.dll")]
        private static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFO lpmi);
    }
}
