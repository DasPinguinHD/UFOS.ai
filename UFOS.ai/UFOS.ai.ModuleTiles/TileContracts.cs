using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;

namespace UFOS.ai.ModuleTiles
{
    // Module tile previews (TILES_TASK.md, user decisions 2026-10-24; contract scratchpad/mr/TILES_API.md incl. the lead's §10).
    // Every module tile in the MainWindow shows ONE of three previews, chosen per tile with its "⋯" button:
    //   Live (default) — 2–3 real key figures, refreshed every 30 s while the main window is visible;
    //   Last view      — a picture of the module window, stored ONLY blurred (TileBlur, in memory before any write);
    //   Illustration   — a static vector motif from the design system (no information).
    // A module is not done until it provides all three (CLAUDE.md / conventions). Each module implements
    // IModuleTilePreview in its own project; the main app registers the providers (Tiles/ModuleTileHost.cs).
    //
    // Deviation from TILES_API §2 (2026-10-24, W): WebView2 areas are found generically by TileCapture (every WebView2 /
    // WebView2CompositionControl in the window is captured with CoreWebView2.CapturePreviewAsync), so providers declare
    // only which windows are theirs (OwnsWindow) — no per-module WebView list to keep in sync.

    /// <summary>The preview a tile shows. Persisted per module in app-settings.json "tile_modes" (live | last_view | illustration).</summary>
    public enum TileMode { Live, LastView, Illustration }

    /// <summary>Colour role of a figure; the host maps it to a design-system brush (never a literal colour).</summary>
    public enum TileTone { Neutral, Positive, Negative, Caution, Ai }

    /// <summary>One key figure: "Equity" / "$100,412" (+ optional detail line, e.g. "budgets: see AI budgets").</summary>
    /// <param name="AiGenerated">The value is AI output (e.g. a verdict): the tile marks it "✦ AI" and its AutomationName says so.</param>
    public sealed record TileFigure(string Label, string Value, TileTone Tone = TileTone.Neutral, string? Detail = null, bool AiGenerated = false);

    /// <summary>A coloured count, e.g. Global Monitoring events per category. BrushKey = a design-system brush key.</summary>
    public sealed record TileDot(string Label, int Count, string BrushKey);

    /// <summary>The figures of one fresh read.</summary>
    public sealed class TileLiveData
    {
        public TileLiveData(DateTimeOffset asOf, IReadOnlyList<TileFigure> figures, IReadOnlyList<double>? spark = null, IReadOnlyList<TileDot>? dots = null)
        {
            AsOf = asOf;
            Figures = figures ?? throw new ArgumentNullException(nameof(figures));
            Spark = spark;
            Dots = dots;
        }

        /// <summary>When the figures were read (shown as "as of HH:MM").</summary>
        public DateTimeOffset AsOf { get; }
        /// <summary>1–3 figures are shown (more are ignored by the host, in order).</summary>
        public IReadOnlyList<TileFigure> Figures { get; }
        /// <summary>Optional mini sparkline (≤ 60 points, no axes).</summary>
        public IReadOnlyList<double>? Spark { get; }
        /// <summary>Optional coloured counts (≤ 6 shown).</summary>
        public IReadOnlyList<TileDot>? Dots { get; }
    }

    public enum TileLiveState
    {
        /// <summary>Read just now — shown and stored (unless <see cref="TileLiveResult.Sandbox"/>).</summary>
        Fresh,
        /// <summary>The module's backend is not running. The tile NEVER starts it: it shows the stored figures with
        /// "backend off — as of HH:MM", or "no data yet".</summary>
        BackendOff,
        /// <summary>Nothing to show yet (e.g. no verdict so far) — Reason says what.</summary>
        NoDataYet,
        /// <summary>The figures could not be read (Reason, user-facing) — the tile shows it, over the stored figures if any.</summary>
        Unavailable,
        /// <summary>The module is not built yet (Live and Last view are disabled in the menu with this reason).</summary>
        NotBuilt,
    }

    /// <summary>Result of <see cref="IModuleTilePreview.ReadLiveAsync"/>.</summary>
    /// <param name="Sandbox">The figures come from a sandbox / fixture profile: the tile shows a SANDBOX chip and NEVER stores them.</param>
    public sealed record TileLiveResult(TileLiveState State, TileLiveData? Data, string? Reason, bool Sandbox = false)
    {
        public static TileLiveResult Fresh(TileLiveData data, bool sandbox = false) => new(TileLiveState.Fresh, data, null, sandbox);
        public static TileLiveResult BackendOff(string? reason = null) => new(TileLiveState.BackendOff, null, reason);
        public static TileLiveResult NoData(string reason) => new(TileLiveState.NoDataYet, null, reason);
        public static TileLiveResult Unavailable(string reason, bool sandbox = false) => new(TileLiveState.Unavailable, null, reason, sandbox);
        public static TileLiveResult NotBuilt(string reason) => new(TileLiveState.NotBuilt, null, reason);
    }

    /// <summary>
    /// The preview provider of one module. Implemented in the module's own project; called by the main app's tile host on the UI
    /// thread (ReadLiveAsync may continue on the thread pool).
    /// </summary>
    public interface IModuleTilePreview
    {
        /// <summary>Stable id: hedgefund | global-monitoring | livestream-agent | transaction-log | system-logs |
        /// sentiment-optimization | strategy-discovery. Used for the stored files and the tile_modes setting.</summary>
        string ModuleId { get; }

        string DisplayName { get; }

        /// <summary>Null = Live is offered; else the reason shown on the DISABLED menu item ("module not built yet").</summary>
        string? LiveUnavailableReason { get; }

        /// <summary>Null = Last view is offered; else the reason shown on the DISABLED menu item.</summary>
        string? LastViewUnavailableReason { get; }

        /// <summary>
        /// Reads the figures. MUST NOT start a backend and MUST NOT cause an AI call or any paid / external call — local files
        /// and the running backend's read-only loopback endpoints (with its token) only. Never throws (except cancellation):
        /// failures are <see cref="TileLiveState.Unavailable"/> with a user-facing reason.
        /// </summary>
        Task<TileLiveResult> ReadLiveAsync(CancellationToken ct);

        /// <summary>The module's motif (vector, design-system brushes via <see cref="TileBrushes"/>). Called on the UI thread.</summary>
        Drawing CreateIllustration();

        /// <summary>True when <paramref name="window"/> belongs to this module (any of its windows, however it was opened).</summary>
        bool OwnsWindow(Window window);

        /// <summary>True while the window shows sandbox / fixture data — then no picture is taken.</summary>
        bool IsSandbox(Window window);
    }
}
