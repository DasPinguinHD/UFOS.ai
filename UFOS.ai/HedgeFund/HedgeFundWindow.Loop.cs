using UFOS.ai.HedgeFund.Platform;

namespace UFOS.ai.HedgeFund
{
    /// <summary>
    /// Navigation rail entry "Loop" (Strategy Platform stage 6, discovery loop — SPEC §16, WPF notes §16.13): Platform/LoopView,
    /// created on first use like the Studio. "Open in Library" on a variant or finalist switches to the Library and selects that
    /// strategy (same path as the Studio's button). The view polls only while it is visible; closing the window stops it for good.
    /// </summary>
    public partial class HedgeFundWindow
    {
        private LoopView? _loopView;

        private void EnsureLoopView()
        {
            if (_loopView != null) return;
            _loopView = new LoopView();
            _loopView.OpenInLibraryRequested += StudioView_OpenInLibraryRequested;
            LoopHost.Child = _loopView;
        }
    }
}
