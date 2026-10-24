using UFOS.ai.HedgeFund.Platform;

namespace UFOS.ai.HedgeFund
{
    /// <summary>
    /// Navigation rail entry "Studio" (Strategy Platform stage 5a, SPEC §13.3): Platform/StudioView, created on first
    /// use like the Library. "Open in Library" on a draft card switches to the Library and selects that strategy.
    /// </summary>
    public partial class HedgeFundWindow
    {
        private StudioView? _studioView;
        private string? _pendingLibraryStrategyId;

        private void EnsureStudioView()
        {
            if (_studioView != null) return;
            _studioView = new StudioView();
            _studioView.OpenInLibraryRequested += StudioView_OpenInLibraryRequested;
            StudioHost.Child = _studioView;
        }

        private async void StudioView_OpenInLibraryRequested(string strategyId)
        {
            if (NavLibraryButton.IsChecked == true)
            {
                if (_libraryView != null) await _libraryView.ShowStrategyAsync(strategyId);
                return;
            }
            _pendingLibraryStrategyId = strategyId;
            NavLibraryButton.IsChecked = true;   // raises NavButton_Checked, which selects the pending strategy
        }
    }
}
