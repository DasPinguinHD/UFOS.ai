using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Threading;
using UFOS.ai.Logging;
using UFOS.ai.ModuleTiles;

namespace UFOS.ai.Tiles
{
    /// <summary>
    /// Module tile previews in the MainWindow (TILES_TASK.md, TILES_API.md incl. the lead's §10). One <see cref="TilePreviewView"/>
    /// per tile, the mode per tile from app-settings.json (<see cref="TileModeSettings"/>), Live refresh every 30 s while the main
    /// window is visible (paused while minimised / hidden), the Last-view capture service for every module window.
    ///
    /// Live never starts a backend and never triggers an AI / paid call (the providers' contract). When a backend is off the tile
    /// shows the last stored figures with "backend off — as of HH:MM", or "no data yet" — behaviour accepted by the user
    /// 2026-10-24 (tiles interview). A module that cannot offer a mode shows the Illustration with the reason (menu + tile).
    /// </summary>
    internal sealed class ModuleTileHost
    {
        private const string LogModule = "Tiles";
        private static readonly TimeSpan LiveInterval = TimeSpan.FromSeconds(30);
        private static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        private readonly Window _main;
        private readonly List<TileSlot> _slots = new();
        private readonly DispatcherTimer _timer;
        private readonly TileCaptureService _capture;
        private Dictionary<string, string> _problems = new();

        private sealed class TileSlot
        {
            public required Button Tile;
            public required IModuleTilePreview Provider;
            public required TilePreviewView View;
            public TileMode Mode = TileMode.Live;
            public bool Busy;
        }

        public ModuleTileHost(Window main, IReadOnlyList<(Button Tile, IModuleTilePreview Provider)> tiles)
        {
            _main = main;
            foreach (var (tile, provider) in tiles)
            {
                try
                {
                    var view = new TilePreviewView(provider.DisplayName);
                    Attach(tile, view);
                    var slot = new TileSlot { Tile = tile, Provider = provider, View = view };
                    view.MenuButton.Click += (_, e) =>
                    {
                        e.Handled = true;   // the tile's own click opens the module — the "⋯" never does
                        OpenMenu(slot);
                    };
                    _slots.Add(slot);
                }
                catch (Exception ex)
                {
                    AppLog.Error(LogModule, "The preview of the " + provider.DisplayName + " tile could not be set up — the tile shows no preview", ex);
                }
            }
            var providers = tiles.Select(t => t.Provider).ToList();
            _capture = new TileCaptureService(providers);
            _capture.PictureStored += id =>
            {
                foreach (var s in _slots.Where(s => s.Provider.ModuleId == id && s.Mode == TileMode.LastView)) _ = RefreshAsync(s);
            };
            _timer = new DispatcherTimer(DispatcherPriority.Background) { Interval = LiveInterval };
            _timer.Tick += async (_, _) => await RefreshAllAsync(liveOnly: true);
        }

        public void Start()
        {
            LoadModes();
            _capture.Start();
            _main.StateChanged += async (_, _) => await OnVisibilityChangedAsync();
            _main.IsVisibleChanged += async (_, _) => await OnVisibilityChangedAsync();
            _ = OnVisibilityChangedAsync();
        }

        private bool MainVisible => _main.IsVisible && _main.WindowState != WindowState.Minimized;

        private async Task OnVisibilityChangedAsync()
        {
            if (!MainVisible)
            {
                _timer.Stop();   // paused while minimised / hidden
                return;
            }
            if (!_timer.IsEnabled)
            {
                _timer.Start();
                await RefreshAllAsync(liveOnly: false);
            }
        }

        private void LoadModes()
        {
            var (modes, problems) = TileModeSettings.Load(_slots.Select(s => s.Provider.ModuleId).ToList());
            _problems = problems;
            foreach (var s in _slots) s.Mode = modes.TryGetValue(s.Provider.ModuleId, out var m) ? m : TileMode.Live;
            foreach (var p in problems) AppLog.Warn(LogModule, p.Value + " — the tile uses Live until it is fixed");
        }

        private string? ProblemOf(TileSlot s)
            => _problems.TryGetValue(s.Provider.ModuleId, out var p) ? p : _problems.TryGetValue("*", out var all) ? all : null;

        private async Task RefreshAllAsync(bool liveOnly)
        {
            if (!MainVisible) return;
            foreach (var s in _slots)
                if (!liveOnly || EffectiveMode(s).Mode == TileMode.Live) await RefreshAsync(s);
        }

        /// <summary>The mode actually shown + the reason when it differs from the chosen one (shown, never silent).</summary>
        private static (TileMode Mode, string? Note) EffectiveMode(TileSlot s)
        {
            var p = s.Provider;
            if (s.Mode == TileMode.Live && p.LiveUnavailableReason is string lr) return (TileMode.Illustration, "Live: " + lr);
            if (s.Mode == TileMode.LastView && p.LastViewUnavailableReason is string vr) return (TileMode.Illustration, "Last view: " + vr);
            return (s.Mode, null);
        }

        private async Task RefreshAsync(TileSlot s)
        {
            if (s.Busy) return;
            s.Busy = true;
            try
            {
                var (mode, note) = EffectiveMode(s);
                var problem = ProblemOf(s);
                switch (mode)
                {
                    case TileMode.Live:
                        await RenderLiveAsync(s, problem);
                        break;
                    case TileMode.LastView:
                        RenderLastView(s, problem);
                        break;
                    default:
                        RenderIllustration(s, note ?? problem);
                        break;
                }
            }
            catch (Exception ex)
            {
                AppLog.Warn(LogModule, "Tile preview " + s.Provider.ModuleId + " failed", ex);
                s.View.ShowMessage("error", "preview failed: " + ex.Message, TileTone.Negative);
            }
            finally
            {
                s.Busy = false;
                AutomationProperties.SetName(s.Tile, s.Provider.DisplayName + ". " + s.View.AutomationText);
            }
        }

        private async Task RenderLiveAsync(TileSlot s, string? problem)
        {
            var p = s.Provider;
            TileLiveResult r;
            try
            {
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                r = await p.ReadLiveAsync(cts.Token);
            }
            catch (OperationCanceledException)
            {
                r = TileLiveResult.Unavailable("the figures took longer than 10 s");
            }
            catch (Exception ex)
            {
                r = TileLiveResult.Unavailable(ex.GetType().Name + ": " + ex.Message);
            }

            string Prefix(string status) => problem == null ? status : problem + " · " + status;

            switch (r.State)
            {
                case TileLiveState.Fresh when r.Data != null:
                {
                    var status = "as of " + r.Data.AsOf.ToLocalTime().ToString("HH:mm", Inv);
                    if (r.Sandbox) status += " · sandbox figures are not stored";
                    else
                    {
                        try { TileLiveStore.Save(p.ModuleId, r.Data); }
                        catch (Exception ex)
                        {
                            AppLog.Warn(LogModule, "Tile figures of " + p.DisplayName + " could not be stored", ex);
                            status += " · not stored (" + ex.GetType().Name + ")";
                        }
                    }
                    s.View.ShowLive(r.Data, Prefix(status), problem == null ? TileTone.Neutral : TileTone.Caution, r.Sandbox);
                    return;
                }
                case TileLiveState.BackendOff:
                case TileLiveState.Unavailable:
                {
                    var off = r.State == TileLiveState.BackendOff;
                    TileLiveData? stored = null;
                    string? storeProblem = null;
                    try { stored = TileLiveStore.Load(p.ModuleId); }
                    catch (Exception ex) { storeProblem = "stored figures unreadable (" + ex.Message + ")"; }
                    var why = off ? "backend off" + (r.Reason == null ? "" : " (" + r.Reason + ")") : "figures could not be read (" + (r.Reason ?? "no reason given") + ")";
                    if (stored != null)
                    {
                        s.View.ShowLive(stored, Prefix(why + " — as of " + stored.AsOf.ToLocalTime().ToString("HH:mm", Inv)),
                                        off && problem == null ? TileTone.Neutral : TileTone.Caution, sandbox: false);
                        return;
                    }
                    s.View.ShowMessage("Live", Prefix(storeProblem ?? (off ? "no data yet — " + why : why)), off && storeProblem == null && problem == null ? TileTone.Neutral : TileTone.Caution);
                    return;
                }
                case TileLiveState.NotBuilt:
                    RenderIllustration(s, "Live: " + (r.Reason ?? NotBuiltTilePreview.Reason));
                    return;
                default:
                    s.View.ShowMessage("Live", Prefix(r.Reason ?? "no data yet"));
                    return;
            }
        }

        private void RenderLastView(TileSlot s, string? problem)
        {
            TileLastViewInfo? info;
            try { info = TileLastViewStore.Load(s.Provider.ModuleId); }
            catch (Exception ex)
            {
                s.View.ShowMessage("Last view", "the stored picture is unreadable (" + ex.Message + ") — delete it in the ⋯ menu", TileTone.Caution);
                return;
            }
            if (info == null)
            {
                s.View.ShowMessage("Last view", (problem == null ? "" : problem + " · ") + "Last view: no picture yet — it is taken while the module window is open and active");
                return;
            }
            s.View.ShowLastView(info, problem);
        }

        private static void RenderIllustration(TileSlot s, string? note) => s.View.ShowIllustration(s.Provider.CreateIllustration(), note);

        private void OpenMenu(TileSlot s)
        {
            bool hasPicture;
            try { hasPicture = TileLastViewStore.Load(s.Provider.ModuleId) != null; }
            catch { hasPicture = true; }   // a damaged picture can still be deleted
            s.View.OpenMenu(s.Mode, s.Provider.LiveUnavailableReason, s.Provider.LastViewUnavailableReason, hasPicture,
                choose: mode =>
                {
                    try
                    {
                        TileModeSettings.Save(s.Provider.ModuleId, mode);
                        s.Mode = mode;
                        _problems.Remove(s.Provider.ModuleId);
                    }
                    catch (Exception ex)
                    {
                        AppLog.Warn(LogModule, "Tile mode of " + s.Provider.DisplayName + " could not be saved", ex);
                        MessageBox.Show(_main, "The preview mode could not be saved: " + ex.Message, s.Provider.DisplayName, MessageBoxButton.OK, MessageBoxImage.Warning);
                        return;
                    }
                    _ = RefreshAsync(s);
                },
                deletePicture: () =>
                {
                    if (MessageBox.Show(_main, "Delete the stored picture of " + s.Provider.DisplayName + "?", "Delete last view",
                                        MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes) return;
                    try { TileLastViewStore.Delete(s.Provider.ModuleId); }
                    catch (Exception ex)
                    {
                        MessageBox.Show(_main, "The picture could not be deleted: " + ex.Message, s.Provider.DisplayName, MessageBoxButton.OK, MessageBoxImage.Warning);
                    }
                    _ = RefreshAsync(s);
                });
        }

        /// <summary>
        /// Puts the preview into the tile: the tile's Grid (rows 4 px accent / *) gets a third Auto row for the name block, the
        /// preview fills the * row, the "⋯" sits bottom right in the name row. Anything else is an error (logged, no preview).
        /// </summary>
        private static void Attach(Button tile, TilePreviewView view)
        {
            if (tile.Content is not Grid grid || grid.RowDefinitions.Count != 2)
                throw new InvalidOperationException("unexpected tile layout (expected a Grid with 2 rows)");
            var nameBlock = grid.Children.OfType<StackPanel>().FirstOrDefault(sp => Grid.GetRow(sp) == 1)
                            ?? throw new InvalidOperationException("unexpected tile layout (no name block in row 1)");
            grid.RowDefinitions.Add(new RowDefinition { Height = GridLength.Auto });
            Grid.SetRow(nameBlock, 2);
            nameBlock.Margin = new Thickness(0, 6, 26, 0);   // room for the "⋯"
            view.Root.Margin = new Thickness(0, 10, 0, 0);
            Grid.SetRow(view.Root, 1);
            grid.Children.Add(view.Root);
            view.MenuButton.Margin = new Thickness(0, 0, -6, -6);
            Grid.SetRow(view.MenuButton, 2);
            grid.Children.Add(view.MenuButton);
        }
    }
}
